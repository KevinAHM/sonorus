"""Provider-neutral preparation of local TTS voice references."""

from __future__ import annotations

import hashlib
import os
import re
import threading
from dataclasses import dataclass
from math import gcd
from pathlib import Path
from typing import Optional

from services.omnivoice_token_cache import load_omnivoice_token_cache

from .voice_utils import VOICE_REFERENCES_DIR

_AUDIO_EXTENSIONS = {".wav", ".mp3", ".flac", ".m4a", ".ogg", ".opus"}
_DURATION_SUFFIX = re.compile(r"_reference_(\d+)s$", re.IGNORECASE)
_LOCKS_GUARD = threading.Lock()
_TRANSCRIPT_LOCKS: dict[str, threading.Lock] = {}


class ReferencePreparationError(RuntimeError):
    pass


@dataclass(frozen=True)
class VoiceReferenceItem:
    character_name: str
    language: str
    path: Path


def _transcript_lock(path: Path) -> threading.Lock:
    key = os.path.normcase(str(path.resolve()))
    with _LOCKS_GUARD:
        return _TRANSCRIPT_LOCKS.setdefault(key, threading.Lock())


def is_preparable_reference(path: Path) -> bool:
    if not path.is_file() or path.suffix.lower() not in _AUDIO_EXTENSIONS:
        return False
    match = _DURATION_SUFFIX.search(path.stem)
    return not match or match.group(1) == "15"


def reference_character_name(path: Path) -> str:
    stem = path.stem
    match = _DURATION_SUFFIX.search(stem)
    if match:
        return stem[: match.start()]
    if stem.lower().endswith("_reference"):
        return stem[: -len("_reference")]
    return stem


def discover_voice_references(language: str) -> list[VoiceReferenceItem]:
    """Discover the active game's mapped voice-language references only."""
    from constants import get_voice_language

    mapped = get_voice_language(language or "EN_US")
    root = Path(VOICE_REFERENCES_DIR)
    directory = root if mapped == "EN_US" else root / mapped.lower()
    if not directory.is_dir():
        return []
    selected: dict[str, tuple[int, VoiceReferenceItem]] = {}
    for path in sorted(directory.iterdir(), key=lambda item: item.name.lower()):
        if not is_preparable_reference(path):
            continue
        character = reference_character_name(path)
        stem = path.stem.lower()
        priority = 0 if stem.endswith("_reference") else 1 if _DURATION_SUFFIX.search(stem) else 2
        key = character.casefold()
        candidate = VoiceReferenceItem(character, mapped, path)
        if key not in selected or priority < selected[key][0]:
            selected[key] = (priority, candidate)
    return [selected[key][1] for key in sorted(selected)]


def read_reference_transcript(audio_path: str | Path) -> Optional[str]:
    audio = Path(audio_path)
    path = audio.with_suffix(".txt")
    try:
        text = path.read_text(encoding="utf-8").strip() if path.is_file() else ""
        if text:
            return text
    except (OSError, UnicodeError) as exc:
        raise ReferencePreparationError(
            f"Could not read reference transcript {path.name}: {exc}"
        ) from exc

    # Local OmniVoice's pre-tokenized sidecar already carries the transcript
    # used to construct its voice prompt. Reuse it before invoking STT so the
    # provider-neutral setup can share completed local OmniVoice preparation.
    token_path = audio.with_suffix(".tokens.pt")
    if token_path.is_file():
        try:
            payload = load_omnivoice_token_cache(token_path)
            if isinstance(payload, dict):
                text = str(payload.get("ref_text") or "").strip()
                if text:
                    return text
        except Exception as exc:
            print(
                f"[Voice Setup] Could not reuse OmniVoice transcript "
                f"from {token_path.name}: {exc}"
            )
    return None


def reference_transcript_hash(text: Optional[str]) -> Optional[str]:
    normalized = str(text or "").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest() if normalized else None


def transcribe_reference(audio_path: str | Path) -> str:
    """Transcribe one reference through Sonorus's configured STT provider."""
    import numpy as np
    import soundfile as sf
    from scipy.signal import resample_poly
    from services import stt

    path = Path(audio_path)
    if stt.get_provider() is None:
        raise ReferencePreparationError(
            "No STT service is configured. Configure Speech-to-Text before "
            "preparing transcript-required voices."
        )
    try:
        data, sample_rate = sf.read(str(path), dtype="float32")
        if data.ndim > 1:
            data = data.mean(axis=1)
        if sample_rate != 16000:
            divisor = gcd(int(sample_rate), 16000)
            data = resample_poly(
                data, 16000 // divisor, int(sample_rate) // divisor
            ).astype(np.float32)
            sample_rate = 16000
        pcm = (data * 32767).clip(-32768, 32767).astype(np.int16).tobytes()
        result = stt.transcribe(pcm, sample_rate)
    except ReferencePreparationError:
        raise
    except Exception as exc:
        raise ReferencePreparationError(
            f"Could not transcribe {path.name}: {exc}"
        ) from exc
    if not isinstance(result, dict):
        raise ReferencePreparationError(
            f"STT failed for {path.name}: provider returned an invalid response"
        )
    text = str(result.get("text") or "").strip() if result.get("success") else ""
    if not text:
        raise ReferencePreparationError(
            f"STT failed for {path.name}: {result.get('error', 'empty transcript')}"
        )
    return text


def ensure_reference_transcript(audio_path: str | Path) -> str:
    """Read or generate a sidecar transcript; failed attempts remain retryable."""
    path = Path(audio_path)
    with _transcript_lock(path):
        existing = read_reference_transcript(path)
        if existing:
            return existing
        text = transcribe_reference(path)
        sidecar = path.with_suffix(".txt")
        temporary = sidecar.with_name(f"{sidecar.name}.tmp")
        try:
            temporary.write_text(text, encoding="utf-8")
            os.replace(temporary, sidecar)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise ReferencePreparationError(
                f"Could not save reference transcript {sidecar.name}: {exc}"
            ) from exc
        return text
