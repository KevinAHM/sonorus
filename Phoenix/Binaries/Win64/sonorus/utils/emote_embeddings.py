"""Persistent semantic fallback for unsupported facial emote tags."""

import hashlib
import json
import math
import os
import re
import sqlite3
import struct
import threading
import time
from contextlib import closing
from typing import Callable, Dict, List, Optional, Tuple

from cognis.embeddings.gemini import OpenAICompatibleEmbedder
from cognis.utils import normalize_embedding_model_id

from constants import EMOTE_TAGS, EMOTE_TAG_ALIASES
from .settings import DATA_DIR, is_llm_provider_feature_disabled, load_settings


EMOTE_EMBEDDINGS_DB = os.path.join(DATA_DIR, "emote_embeddings.db")
EMBEDDING_DIMENSIONS = 768
EMBEDDING_BATCH_SIZE = 5
INDEX_SCHEMA_VERSION = 2
SUPPORTED_EMBEDDING_PROVIDERS = frozenset({"openai", "openrouter", "gemini"})

_FREEFORM_TAG_RE = re.compile(
    r'^\s*(?:"?\s*)?\[([A-Za-z][A-Za-z _-]{0,47})\]',
    re.IGNORECASE,
)
_AUDIO_TAGS = frozenset({
    "laugh", "laughs", "laughing", "laughter",
    "sigh", "sighs", "sighing",
    "breathe", "breathes", "breathing",
    "cough", "coughs", "coughing",
    "clear throat", "clears throat", "clearing throat",
    "yawn", "yawns", "yawning",
    "confirmation-en",
})
_VOICE_DELIVERY_TAGS = frozenset({
    "whisper", "whispers", "whispered", "whispering", "in a whisper",
    "hushed", "quiet", "quietly", "soft", "softly", "under breath",
    "murmur", "murmured", "murmuring", "mutter", "muttered", "muttering",
    "breathy", "breathless", "stammer", "stammered", "stammering",
    "stutter", "stuttered", "stuttering",
    "shout", "shouts", "shouted", "shouting", "yell", "yells", "yelled", "yelling",
    "scream", "screamed", "screaming", "bellow", "bellowed", "bellowing",
    "loud", "loudly", "booming", "roaring",
    "deadpan", "monotone", "flatly", "drawling", "sing-song",
    "pause", "pauses", "paused", "pausing",
    "confidentially", "unhurriedly", "raspily", "tremulously",
    "robotically", "tonelessly",
})
_NON_EMOTE_TAGS = _AUDIO_TAGS | _VOICE_DELIVERY_TAGS


def _normalize_tag(tag: str) -> str:
    normalized = str(tag or "").strip().lower().replace("_", " ")
    return re.sub(r"\s+", " ", normalized)


def extract_freeform_emote_tag(text: str) -> Optional[str]:
    """Extract one short English freeform tag while excluding non-emotion controls."""
    match = _FREEFORM_TAG_RE.search(text or "")
    if not match:
        return None
    tag = _normalize_tag(match.group(1))
    if not tag or tag in _NON_EMOTE_TAGS:
        return None
    return tag


def is_freeform_emotes_enabled(settings: Optional[Dict] = None) -> bool:
    """Return whether the AI may improvise emotion tags beyond the prompt examples."""
    if settings is None:
        settings = load_settings()
    return settings.get("conversation", {}).get("freeform_emote_tags", True) is not False


def is_emote_embedding_lookup_enabled(settings: Optional[Dict] = None) -> bool:
    """Return whether semantic fallback is available for unmatched freeform tags."""
    if settings is None:
        settings = load_settings()
    if not is_freeform_emotes_enabled(settings):
        return False
    if not settings.get("memory", {}).get("enabled", False):
        return False
    provider = str(settings.get("llm", {}).get("provider", "gemini") or "gemini").lower()
    if provider not in SUPPORTED_EMBEDDING_PROVIDERS:
        return False
    return not is_llm_provider_feature_disabled("memory", settings)


def build_emote_profile_documents() -> Dict[str, str]:
    """Build exactly one comma-separated canonical-plus-alias document per face."""
    documents = {}
    for emote in EMOTE_TAGS:
        aliases = [
            alias
            for alias, canonical in EMOTE_TAG_ALIASES.items()
            if canonical == emote
        ]
        documents[emote] = ", ".join([emote, *aliases])
    return documents


def _effective_embedding_model(settings: Dict) -> Tuple[str, str]:
    provider = str(settings.get("llm", {}).get("provider", "gemini") or "gemini").lower()
    configured = str(settings.get("memory", {}).get("embedding_model", "") or "").strip()
    if provider == "openrouter":
        model = configured or "openai/text-embedding-3-small"
        if "/" not in model:
            model = f"openai/{model}"
    elif provider == "gemini":
        model = configured or "gemini-embedding-2"
        if model.startswith("google/"):
            model = model.split("/", 1)[1]
    else:
        model = configured or "text-embedding-3-small"
        if model.startswith("openai/"):
            model = model.split("/", 1)[1]
    return provider, model


def _embedding_model_key(model: str) -> str:
    """Match Cognis vector compatibility by normalized model ID alone."""
    return normalize_embedding_model_id(model)


def _normalize_vector(vector: List[float]) -> List[float]:
    values = [float(value) for value in vector]
    norm = math.sqrt(sum(value * value for value in values))
    if norm <= 0:
        raise ValueError("Embedding vector has zero magnitude")
    return [value / norm for value in values]


def _encode_vector(vector: List[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def _decode_vector(blob: bytes, dimensions: int) -> List[float]:
    expected_size = dimensions * 4
    if len(blob) != expected_size:
        raise ValueError(f"Invalid vector blob size {len(blob)} (expected {expected_size})")
    return list(struct.unpack(f"<{dimensions}f", blob))


class EmoteEmbeddingIndex:
    """Small persistent vector index with durable freeform-tag resolutions."""

    def __init__(
        self,
        db_path: str = EMOTE_EMBEDDINGS_DB,
        embedder_factory: Optional[Callable[[str], object]] = None,
    ):
        self.db_path = db_path
        self._embedder_factory = embedder_factory
        self._lock = threading.RLock()
        self._thread_lock = threading.Lock()
        self._build_thread = None
        self._pending_settings = None
        self._fingerprint = None
        self._profiles: Dict[str, List[float]] = {}
        self._embedder = None
        self._embedder_fingerprint = None

    @staticmethod
    def _configuration(settings: Dict) -> Dict[str, object]:
        provider, model = _effective_embedding_model(settings)
        documents = build_emote_profile_documents()
        corpus_json = json.dumps(documents, sort_keys=True, separators=(",", ":"))
        corpus_fingerprint = hashlib.sha256(corpus_json.encode("utf-8")).hexdigest()
        identity = {
            "schema_version": INDEX_SCHEMA_VERSION,
            "model_key": _embedding_model_key(model),
            "dimensions": EMBEDDING_DIMENSIONS,
            "corpus_fingerprint": corpus_fingerprint,
        }
        identity_json = json.dumps(identity, sort_keys=True, separators=(",", ":"))
        identity["fingerprint"] = hashlib.sha256(identity_json.encode("utf-8")).hexdigest()
        identity["provider"] = provider
        identity["model"] = model
        identity["documents"] = documents
        return identity

    def _connect(self) -> sqlite3.Connection:
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS profiles (
                emote TEXT PRIMARY KEY,
                document TEXT NOT NULL,
                dimensions INTEGER NOT NULL,
                embedding BLOB NOT NULL
            );
            CREATE TABLE IF NOT EXISTS resolutions (
                tag TEXT PRIMARY KEY,
                emote TEXT NOT NULL,
                score REAL NOT NULL,
                fingerprint TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_emote_resolutions_fingerprint
                ON resolutions(fingerprint);
            """
        )
        return conn

    @staticmethod
    def _read_metadata(conn: sqlite3.Connection) -> Dict[str, str]:
        return {
            row["key"]: row["value"]
            for row in conn.execute("SELECT key, value FROM metadata")
        }

    @staticmethod
    def _load_profiles(conn: sqlite3.Connection) -> Dict[str, List[float]]:
        profiles = {}
        for row in conn.execute(
            "SELECT emote, dimensions, embedding FROM profiles ORDER BY emote"
        ):
            profiles[row["emote"]] = _normalize_vector(
                _decode_vector(row["embedding"], int(row["dimensions"]))
            )
        return profiles

    def _get_embedder(self, config: Dict[str, object]):
        # Recreate the embedder whenever vector compatibility changes. This also
        # prevents its text cache from crossing custom endpoint/model boundaries.
        embedder_fingerprint = str(config["fingerprint"])
        if self._embedder is not None and self._embedder_fingerprint == embedder_fingerprint:
            return self._embedder
        model = str(config["model"])
        if self._embedder_factory:
            self._embedder = self._embedder_factory(model)
        else:
            self._embedder = OpenAICompatibleEmbedder(
                model=model,
                full_dim=EMBEDDING_DIMENSIONS,
                small_dim=256,
            )
        self._embedder_fingerprint = embedder_fingerprint
        return self._embedder

    @staticmethod
    def _result_vector(result) -> List[float]:
        embeddings = getattr(result, "embeddings", {}) or {}
        vector = embeddings.get(EMBEDDING_DIMENSIONS)
        if vector is None and embeddings:
            vector = embeddings[max(embeddings)]
        if vector is None:
            raise ValueError("Embedding provider returned no vector")
        if len(vector) != EMBEDDING_DIMENSIONS:
            raise ValueError(
                f"Embedding provider returned {len(vector)} dimensions; "
                f"expected {EMBEDDING_DIMENSIONS}"
            )
        return _normalize_vector(vector)

    def ensure(self, settings: Optional[Dict] = None) -> bool:
        """Load a valid index or generate all 25 profile vectors in batches of five."""
        if settings is None:
            settings = load_settings()
        if not is_emote_embedding_lookup_enabled(settings):
            with self._lock:
                self._fingerprint = None
                self._profiles = {}
            return False

        config = self._configuration(settings)
        fingerprint = str(config["fingerprint"])

        with self._lock:
            if self._fingerprint == fingerprint and set(self._profiles) == set(EMOTE_TAGS):
                return True
            try:
                with closing(self._connect()) as conn:
                    metadata = self._read_metadata(conn)
                    if metadata.get("fingerprint") == fingerprint:
                        profiles = self._load_profiles(conn)
                        if set(profiles) == set(EMOTE_TAGS):
                            self._profiles = profiles
                            self._fingerprint = fingerprint
                            print(
                                f"[EmoteEmbedding] Loaded {len(profiles)} profiles "
                                f"for {config['provider']}/{config['model']}"
                            )
                            return True

                documents = config["documents"]
                items = list(documents.items())
                embedder = self._get_embedder(config)
                generated = {}
                print(
                    f"[EmoteEmbedding] Building {len(items)} profiles in batches of "
                    f"{EMBEDDING_BATCH_SIZE} for {config['provider']}/{config['model']}"
                )
                for start in range(0, len(items), EMBEDDING_BATCH_SIZE):
                    batch = items[start:start + EMBEDDING_BATCH_SIZE]
                    results = embedder.embed_documents_batch([document for _, document in batch])
                    if len(results) != len(batch):
                        raise ValueError(
                            f"Embedding batch returned {len(results)} results for {len(batch)} documents"
                        )
                    for (emote, _document), result in zip(batch, results):
                        generated[emote] = self._result_vector(result)
                    print(
                        f"[EmoteEmbedding] Generated profiles "
                        f"{start + 1}-{start + len(batch)} of {len(items)}"
                    )

                with closing(self._connect()) as conn:
                    with conn:
                        conn.execute("DELETE FROM profiles")
                        conn.execute("DELETE FROM resolutions")
                        conn.execute("DELETE FROM metadata")
                        conn.executemany(
                            "INSERT INTO profiles(emote, document, dimensions, embedding) "
                            "VALUES (?, ?, ?, ?)",
                            [
                                (
                                    emote,
                                    documents[emote],
                                    EMBEDDING_DIMENSIONS,
                                    _encode_vector(generated[emote]),
                                )
                                for emote in EMOTE_TAGS
                            ],
                        )
                        metadata = {
                            "fingerprint": fingerprint,
                            "provider": str(config["provider"]),
                            "model": str(config["model"]),
                            "dimensions": str(EMBEDDING_DIMENSIONS),
                            "corpus_fingerprint": str(config["corpus_fingerprint"]),
                            "schema_version": str(INDEX_SCHEMA_VERSION),
                        }
                        conn.executemany(
                            "INSERT INTO metadata(key, value) VALUES (?, ?)",
                            list(metadata.items()),
                        )
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

                self._profiles = generated
                self._fingerprint = fingerprint
                print(f"[EmoteEmbedding] Saved {len(generated)} profiles to {self.db_path}")
                return True
            except Exception as exc:
                self._fingerprint = None
                self._profiles = {}
                print(f"[EmoteEmbedding] Index unavailable: {exc}")
                return False

    def ensure_async(self, settings: Optional[Dict] = None) -> bool:
        """Schedule validation/generation without blocking startup or config saves."""
        if settings is None:
            settings = load_settings()
        if not is_emote_embedding_lookup_enabled(settings):
            return False
        with self._thread_lock:
            self._pending_settings = settings
            if self._build_thread and self._build_thread.is_alive():
                return True
            self._build_thread = threading.Thread(
                target=self._run_pending_builds,
                name="emote-embedding-index",
                daemon=True,
            )
            self._build_thread.start()
        return True

    def _run_pending_builds(self) -> None:
        """Process the latest requested configuration, including changes during a build."""
        while True:
            with self._thread_lock:
                settings = self._pending_settings
                self._pending_settings = None
            if settings is not None:
                self.ensure(settings)
            with self._thread_lock:
                if self._pending_settings is None:
                    self._build_thread = None
                    return

    def resolve(self, tag: str, settings: Optional[Dict] = None) -> Optional[Tuple[str, float, str]]:
        """Resolve and permanently cache an unsupported tag for the active index."""
        if settings is None:
            settings = load_settings()
        normalized_tag = _normalize_tag(tag)
        if not normalized_tag or not is_emote_embedding_lookup_enabled(settings):
            return None

        if not self.ensure(settings):
            return None

        with self._lock:
            with closing(self._connect()) as conn:
                row = conn.execute(
                    "SELECT emote, score FROM resolutions WHERE tag = ? AND fingerprint = ?",
                    (normalized_tag, self._fingerprint),
                ).fetchone()
                if row:
                    return row["emote"], float(row["score"]), "cached"

            try:
                config = self._configuration(settings)
                query_result = self._get_embedder(config).embed_query(normalized_tag)
                query_vector = self._result_vector(query_result)
                emote, score = max(
                    (
                        (candidate, sum(a * b for a, b in zip(query_vector, vector)))
                        for candidate, vector in self._profiles.items()
                    ),
                    key=lambda item: item[1],
                )

                with closing(self._connect()) as conn:
                    with conn:
                        conn.execute(
                            "INSERT OR REPLACE INTO resolutions"
                            "(tag, emote, score, fingerprint, created_at) VALUES (?, ?, ?, ?, ?)",
                            (normalized_tag, emote, score, self._fingerprint, time.time()),
                        )
                return emote, score, "embedding"
            except Exception as exc:
                print(f"[EmoteEmbedding] Failed to resolve '{normalized_tag}': {exc}")
                return None


_index = EmoteEmbeddingIndex()


def ensure_emote_index_async(settings: Optional[Dict] = None) -> bool:
    return _index.ensure_async(settings)


def resolve_freeform_emote_text(text: str) -> Optional[Tuple[str, float]]:
    """Resolve an unsupported leading tag to a canonical full-intensity face."""
    settings = load_settings()
    if not is_emote_embedding_lookup_enabled(settings):
        return None
    tag = extract_freeform_emote_tag(text)
    if not tag:
        return None
    resolved = _index.resolve(tag, settings)
    if not resolved:
        return None
    emote, score, source = resolved
    print(
        f"[EmoteEmbedding] requested={tag} matched={emote} "
        f"score={score:.3f} source={source}"
    )
    return emote, 1.0
