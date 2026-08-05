"""
llama.cpp slot KV cache helper.

This module only manages slot save/restore bookkeeping. It intentionally does
not decide which prompts are cacheable; callers provide a stable prompt prefix
for cacheable LLM calls.
"""

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests

from .settings import DATA_DIR


DEFAULT_CACHE_DIR = os.path.join(DATA_DIR, "llamacpp_kv_cache")
METADATA_FILENAME = "metadata.json"
METADATA_VERSION = 2
POOL_FILENAME_TEMPLATE = "sonorus_slot_{index:03d}.bin"


@dataclass
class SlotCacheResult:
    action: str
    enabled: bool
    success: bool
    hit: bool = False
    key: Optional[str] = None
    filename: Optional[str] = None
    model_id: Optional[str] = None
    error: Optional[str] = None
    response: Optional[Dict[str, Any]] = None
    evicted: List[str] = field(default_factory=list)


class LlamaCppSlotCache:
    """Save and restore llama.cpp slot 0 snapshots with LRU bookkeeping."""

    def __init__(
        self,
        api_url: str,
        api_key: str = "",
        enabled: bool = True,
        max_entries: int = 5,
        cache_dir: str = DEFAULT_CACHE_DIR,
        slot_id: int = 0,
        timeout: float = 10.0,
        session: Optional[requests.Session] = None,
    ):
        self.api_url = self._normalize_api_url(api_url)
        self.server_url = self._server_url_from_api_url(self.api_url)
        self.api_key = (api_key or "").strip()
        self.enabled = bool(enabled)
        self.max_entries = max(1, int(max_entries or 5))
        self.cache_dir = cache_dir
        self.slot_id = slot_id
        self.timeout = timeout
        self.session = session or requests.Session()
        self.metadata_path = os.path.join(self.cache_dir, METADATA_FILENAME)
        self._lock = threading.RLock()
        self._models_cache = None
        self._models_cache_time = 0.0

    @staticmethod
    def _normalize_api_url(api_url: str) -> str:
        api_url = (api_url or "http://127.0.0.1:8080/v1").strip().rstrip("/")
        if not api_url.lower().endswith("/v1"):
            api_url = f"{api_url}/v1"
        return api_url

    @staticmethod
    def _server_url_from_api_url(api_url: str) -> str:
        api_url = LlamaCppSlotCache._normalize_api_url(api_url)
        return api_url[:-3] if api_url.lower().endswith("/v1") else api_url

    def restore(self, prompt_prefix: Any, request_model: str = "", context: str = "chat") -> SlotCacheResult:
        """Restore slot 0 from the saved snapshot for this prefix/model, if present."""
        if not self.enabled:
            return SlotCacheResult(action="restore", enabled=False, success=True)

        try:
            cache_info = self.get_cache_info(prompt_prefix, request_model, context)
            with self._lock:
                metadata = self._load_metadata()
                entry = metadata.get("entries", {}).get(cache_info["key"])
            if not entry:
                return SlotCacheResult(
                    action="restore",
                    enabled=True,
                    success=True,
                    hit=False,
                    key=cache_info["key"],
                    model_id=cache_info["model_id"],
                )

            filename = entry["filename"]
            response = self._post_slot_action("restore", filename)
            with self._lock:
                metadata = self._load_metadata()
                current_entry = metadata.get("entries", {}).get(cache_info["key"])
                if current_entry and current_entry.get("filename") == filename:
                    current_entry["last_used"] = time.time()
                    self._save_metadata(metadata)
            return SlotCacheResult(
                action="restore",
                enabled=True,
                success=True,
                hit=True,
                key=cache_info["key"],
                filename=filename,
                model_id=cache_info["model_id"],
                response=response,
            )
        except Exception as exc:
            error = str(exc)
            print(f"[LlamaCppKV] Restore skipped: {error}")
            if self._looks_like_missing_file(error):
                self._remove_entry_for_error(prompt_prefix, request_model, context)
            return SlotCacheResult(action="restore", enabled=True, success=False, error=error)

    def save(self, prompt_prefix: Any, request_model: str = "", context: str = "chat") -> SlotCacheResult:
        """Save slot 0 to the snapshot for this prefix/model and refresh LRU state."""
        if not self.enabled:
            return SlotCacheResult(action="save", enabled=False, success=True)

        try:
            cache_info = self.get_cache_info(prompt_prefix, request_model, context)
            with self._lock:
                metadata = self._load_metadata()
                filename, evicted = self._allocate_filename(metadata, cache_info["key"])
                if evicted:
                    # Persist removal before the remote file is overwritten so a
                    # later local write failure cannot leave a stale mapping.
                    self._save_metadata(metadata)
                response = self._post_slot_action("save", filename)
                self._record_save(metadata, cache_info, filename, response)
                self._save_metadata(metadata)
            return SlotCacheResult(
                action="save",
                enabled=True,
                success=True,
                hit=True,
                key=cache_info["key"],
                filename=filename,
                model_id=cache_info["model_id"],
                response=response,
                evicted=evicted,
            )
        except Exception as exc:
            error = str(exc)
            print(f"[LlamaCppKV] Save skipped: {error}")
            return SlotCacheResult(action="save", enabled=True, success=False, error=error)

    def erase_slot(self) -> SlotCacheResult:
        """Erase the live llama.cpp slot cache. This does not delete saved files."""
        if not self.enabled:
            return SlotCacheResult(action="erase", enabled=False, success=True)
        try:
            url = f"{self.server_url}/slots/{self.slot_id}?action=erase"
            resp = self.session.post(url, headers=self._headers(), timeout=self.timeout)
            resp.raise_for_status()
            return SlotCacheResult(action="erase", enabled=True, success=True, response=self._json_or_empty(resp))
        except Exception as exc:
            error = str(exc)
            print(f"[LlamaCppKV] Erase skipped: {error}")
            return SlotCacheResult(action="erase", enabled=True, success=False, error=error)

    def get_cache_info(self, prompt_prefix: Any, request_model: str = "", context: str = "chat") -> Dict[str, str]:
        """Return deterministic cache key/filename metadata for a prefix/model pair."""
        prefix_text = self._canonical_prefix(prompt_prefix)
        model_id = self.get_model_identity(request_model)
        prompt_hash = hashlib.sha256(prefix_text.encode("utf-8")).hexdigest()
        key_material = f"{model_id}\n{prompt_hash}"
        key = hashlib.sha256(key_material.encode("utf-8")).hexdigest()
        return {
            "key": key,
            "model_id": model_id,
            "prompt_hash": prompt_hash,
            "context": context or "chat",
        }

    def get_model_identity(self, request_model: str = "") -> str:
        """Use loaded model id when unambiguous, otherwise fall back to the request model."""
        loaded_models = self.fetch_loaded_models()
        request_model = (request_model or "").strip()
        if len(loaded_models) == 1:
            return loaded_models[0]
        if request_model:
            return request_model
        if loaded_models:
            return loaded_models[0]
        return "llamacpp-default-model"

    def fetch_loaded_models(self, ttl: float = 10.0) -> List[str]:
        """Return model ids from /v1/models, falling back to /models if needed."""
        now = time.time()
        if self._models_cache is not None and now - self._models_cache_time < ttl:
            return list(self._models_cache)

        errors = []
        for url in (f"{self.api_url}/models", f"{self.server_url}/models"):
            try:
                resp = self.session.get(url, headers=self._headers(), timeout=self.timeout)
                resp.raise_for_status()
                models = self._extract_model_ids(resp.json())
                if models:
                    self._models_cache = models
                    self._models_cache_time = now
                    return list(models)
            except Exception as exc:
                errors.append(f"{url}: {exc}")

        print(f"[LlamaCppKV] Could not resolve loaded model id ({'; '.join(errors)})")
        self._models_cache = []
        self._models_cache_time = now
        return []

    def _post_slot_action(self, action: str, filename: str) -> Dict[str, Any]:
        url = f"{self.server_url}/slots/{self.slot_id}?action={action}"
        resp = self.session.post(
            url,
            headers=self._headers(),
            json={"filename": filename},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return self._json_or_empty(resp)

    def _allocate_filename(self, metadata: Dict[str, Any], key: str):
        """Choose a bounded pool filename, reusing the LRU entry when full."""
        entries = metadata.setdefault("entries", {})
        existing = entries.get(key)
        if existing:
            return existing["filename"], []

        pool_filenames = self._pool_filenames()
        used_filenames = {entry.get("filename") for entry in entries.values()}
        for filename in pool_filenames:
            if filename not in used_filenames:
                return filename, []

        evicted_key, evicted_entry = min(
            entries.items(),
            key=lambda item: (item[1].get("last_used", 0), item[1].get("created_at", 0)),
        )
        entries.pop(evicted_key)
        filename = evicted_entry["filename"]
        return filename, [filename]

    def _record_save(
        self,
        metadata: Dict[str, Any],
        cache_info: Dict[str, str],
        filename: str,
        response: Dict[str, Any],
    ):
        now = time.time()
        entries = metadata.setdefault("entries", {})
        entry = entries.get(cache_info["key"], {})
        created_at = entry.get("created_at", now)
        entries[cache_info["key"]] = {
            "filename": filename,
            "model_id": cache_info["model_id"],
            "prompt_hash": cache_info["prompt_hash"],
            "context": cache_info["context"],
            "created_at": created_at,
            "last_used": now,
            "last_saved": now,
            "save_count": int(entry.get("save_count", 0)) + 1,
            "n_saved": response.get("n_saved"),
            "n_written": response.get("n_written"),
        }

    def _remove_entry_for_error(self, prompt_prefix: Any, request_model: str, context: str):
        try:
            cache_info = self.get_cache_info(prompt_prefix, request_model, context)
            with self._lock:
                metadata = self._load_metadata()
                if metadata.get("entries", {}).pop(cache_info["key"], None):
                    self._save_metadata(metadata)
        except Exception:
            pass

    def _load_metadata(self) -> Dict[str, Any]:
        os.makedirs(self.cache_dir, exist_ok=True)
        try:
            with open(self.metadata_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("metadata root is not an object")
            if data.get("version") != METADATA_VERSION:
                return self._empty_metadata()
            data.setdefault("entries", {})
            self._prune_invalid_entries(data)
            return data
        except FileNotFoundError:
            return self._empty_metadata()
        except Exception as exc:
            backup_path = self.metadata_path + ".bad"
            try:
                os.replace(self.metadata_path, backup_path)
                print(f"[LlamaCppKV] Backed up invalid metadata to {backup_path}: {exc}")
            except Exception:
                print(f"[LlamaCppKV] Invalid metadata ignored: {exc}")
            return self._empty_metadata()

    def _save_metadata(self, metadata: Dict[str, Any]):
        os.makedirs(self.cache_dir, exist_ok=True)
        tmp_path = self.metadata_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2, sort_keys=True)
        os.replace(tmp_path, self.metadata_path)

    @staticmethod
    def _empty_metadata() -> Dict[str, Any]:
        return {"version": METADATA_VERSION, "entries": {}}

    def _pool_filenames(self) -> List[str]:
        return [POOL_FILENAME_TEMPLATE.format(index=index) for index in range(self.max_entries)]

    def _prune_invalid_entries(self, metadata: Dict[str, Any]):
        """Drop mappings that cannot belong to the configured filename pool."""
        valid_filenames = set(self._pool_filenames())
        entries = metadata.setdefault("entries", {})
        seen_filenames = set()
        for key, entry in list(entries.items()):
            filename = entry.get("filename") if isinstance(entry, dict) else None
            if filename not in valid_filenames or filename in seen_filenames:
                entries.pop(key, None)
                continue
            seen_filenames.add(filename)

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    @staticmethod
    def _canonical_prefix(prompt_prefix: Any) -> str:
        if isinstance(prompt_prefix, str):
            return prompt_prefix
        return json.dumps(prompt_prefix, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _extract_model_ids(payload: Any) -> List[str]:
        if isinstance(payload, dict):
            raw_models = payload.get("data") or payload.get("models") or []
        elif isinstance(payload, list):
            raw_models = payload
        else:
            raw_models = []

        ids = []
        for item in raw_models:
            if isinstance(item, str):
                model_id = item
            elif isinstance(item, dict):
                model_id = item.get("id") or item.get("model") or item.get("name") or item.get("path")
            else:
                model_id = None
            if model_id:
                ids.append(str(model_id))
        return ids

    @staticmethod
    def _json_or_empty(resp) -> Dict[str, Any]:
        try:
            data = resp.json()
            return data if isinstance(data, dict) else {"data": data}
        except Exception:
            return {}

    @staticmethod
    def _looks_like_missing_file(error: str) -> bool:
        lower = (error or "").lower()
        return (
            "404" in lower
            or "not found" in lower
            or "no such file" in lower
            or "could not open" in lower
        )


def create_from_settings(settings: Optional[Dict[str, Any]] = None) -> LlamaCppSlotCache:
    """Create a slot-cache helper from Sonorus settings."""
    if settings is None:
        from .settings import load_settings
        settings = load_settings()

    llm_settings = settings.get("llm", {})
    llama_settings = llm_settings.get("llamacpp", {})
    return LlamaCppSlotCache(
        api_url=llama_settings.get("api_url", "http://127.0.0.1:8080/v1"),
        api_key=llama_settings.get("api_key", ""),
        enabled=llama_settings.get("kv_cache_enabled", True),
        max_entries=llama_settings.get("kv_cache_max_entries", 10),
    )
