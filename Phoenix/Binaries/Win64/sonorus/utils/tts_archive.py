"""
TTS archive helpers.

Stores recent synthesized lines as WAV files under sonorus/data/tts/.
Files are named from the dialogue-history row ID, speaker ID, and a
truncated file-safe text preview.
"""

import os
import re
import wave
import unicodedata
from typing import Dict, List, Optional

from .settings import DATA_DIR, get_setting


TTS_ARCHIVE_DIR = os.path.join(DATA_DIR, "tts")


def close_all():
    """No-op — TTS archive has no connections to close."""
    pass


def reinit(data_dir):
    """Update TTS archive directory to new player data dir."""
    global TTS_ARCHIVE_DIR
    TTS_ARCHIVE_DIR = os.path.join(data_dir, "tts")


MAX_ARCHIVED_TTS_FILES = 100
MAX_SPEAKER_LEN = 48
MAX_TEXT_LEN = 64


def _ensure_archive_dir() -> str:
    os.makedirs(TTS_ARCHIVE_DIR, exist_ok=True)
    return TTS_ARCHIVE_DIR


def archive_enabled() -> bool:
    return bool(get_setting("tts.archive_enabled", True))


def _ascii_safe(value: str, max_len: int, fallback: str) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.replace("'", "")
    text = re.sub(r"\s+", "_", text.strip())
    text = re.sub(r"[^A-Za-z0-9_-]", "", text)
    text = re.sub(r"_+", "_", text).strip("_")
    if not text:
        text = fallback
    return text[:max_len].rstrip("_") or fallback


def build_archive_filename(entry_id: int, speaker_id: str, text: str) -> str:
    speaker_safe = _ascii_safe(speaker_id, MAX_SPEAKER_LEN, "speaker")
    text_safe = _ascii_safe(text, MAX_TEXT_LEN, "line")
    return f"{int(entry_id)}_{speaker_safe}_{text_safe}.wav"


def find_history_entry_archive_path(entry_id: int, speaker_id: str = "", text: str = "") -> Optional[str]:
    """Find the archived WAV path for a dialogue-history row ID if it exists."""
    if not entry_id:
        return None

    exact_path = os.path.join(
        TTS_ARCHIVE_DIR,
        build_archive_filename(entry_id, speaker_id or "speaker", text or ""),
    )
    if os.path.exists(exact_path):
        return exact_path

    return None


def get_history_entry_archive_paths(entry: Optional[Dict]) -> List[str]:
    """Return archived WAV paths for a history entry, newest source row first."""
    if not isinstance(entry, dict):
        return []

    source_ids = entry.get("sourceEntryIds") or []
    speaker_id = entry.get("voiceName") or entry.get("speaker") or "speaker"
    text = entry.get("text") or ""
    results = []
    seen = set()

    for entry_id in sorted(source_ids, reverse=True):
        try:
            numeric_id = int(entry_id)
        except (TypeError, ValueError):
            continue

        path = find_history_entry_archive_path(numeric_id, speaker_id=speaker_id, text=text)
        if path and path not in seen:
            seen.add(path)
            results.append(path)

    return results


def _prune_old_archives() -> None:
    archive_dir = _ensure_archive_dir()
    try:
        wav_paths = [
            os.path.join(archive_dir, name)
            for name in os.listdir(archive_dir)
            if name.lower().endswith(".wav")
        ]
    except FileNotFoundError:
        return

    if len(wav_paths) <= MAX_ARCHIVED_TTS_FILES:
        return

    wav_paths.sort(key=lambda path: os.path.getmtime(path))
    for old_path in wav_paths[:-MAX_ARCHIVED_TTS_FILES]:
        try:
            os.remove(old_path)
        except OSError as e:
            print(f"[TTSArchive] Failed to remove old archive {old_path}: {e}")


def write_history_entry_archive(entry_id: int, speaker_id: str, text: str,
                                pcm_bytes: bytes, sample_rate: int,
                                channels: int = 1) -> Optional[str]:
    """Write a WAV archive file for a history entry."""
    if not archive_enabled() or not pcm_bytes:
        return None

    archive_dir = _ensure_archive_dir()
    filename = build_archive_filename(entry_id, speaker_id, text)
    path = os.path.join(archive_dir, filename)

    try:
        with wave.open(path, "wb") as wav_file:
            wav_file.setnchannels(max(1, int(channels or 1)))
            wav_file.setsampwidth(2)  # 16-bit PCM
            wav_file.setframerate(int(sample_rate))
            wav_file.writeframes(pcm_bytes)
        _prune_old_archives()
        print(f"[TTSArchive] Saved {filename}")
        return path
    except Exception as e:
        print(f"[TTSArchive] Failed to save {path}: {e}")
        return None


def stage_history_entry_archive(entry: Optional[Dict], pcm_bytes: bytes, sample_rate: int,
                                channels: int, speaker_id: str, text: str) -> None:
    """Stage synthesized PCM on the entry until it has a DB row ID."""
    if not archive_enabled() or entry is None or not pcm_bytes:
        return

    entry["_tts_archive"] = {
        "pcm_bytes": bytes(pcm_bytes),
        "sample_rate": int(sample_rate),
        "channels": max(1, int(channels or 1)),
        "speaker_id": speaker_id or entry.get("voiceName") or entry.get("speaker") or "speaker",
        "text": text or entry.get("text") or "",
    }


def flush_history_entry_archive(entry: Optional[Dict]) -> Optional[str]:
    """Write any staged archive once the entry has a DB row ID."""
    if not archive_enabled() or not isinstance(entry, dict):
        return None

    staged = entry.pop("_tts_archive", None)
    if not staged:
        return None

    source_ids = entry.get("sourceEntryIds") or []
    if not source_ids:
        # Put it back if the caller tried to flush too early.
        entry["_tts_archive"] = staged
        return None

    entry_id = source_ids[0]
    return write_history_entry_archive(
        entry_id=entry_id,
        speaker_id=staged.get("speaker_id") or entry.get("voiceName") or entry.get("speaker") or "speaker",
        text=staged.get("text") or entry.get("text") or "",
        pcm_bytes=staged.get("pcm_bytes") or b"",
        sample_rate=staged.get("sample_rate") or 24000,
        channels=staged.get("channels") or 1,
    )


def write_or_stage_history_entry_archive(entry: Optional[Dict], pcm_bytes: bytes,
                                         sample_rate: int, channels: int,
                                         speaker_id: str, text: str) -> Optional[str]:
    """Write immediately if the entry already has an ID, else stage for later."""
    if not archive_enabled() or not pcm_bytes:
        return None

    if isinstance(entry, dict) and entry.get("sourceEntryIds"):
        entry_id = entry["sourceEntryIds"][0]
        return write_history_entry_archive(
            entry_id=entry_id,
            speaker_id=speaker_id or entry.get("voiceName") or entry.get("speaker") or "speaker",
            text=text or entry.get("text") or "",
            pcm_bytes=pcm_bytes,
            sample_rate=sample_rate,
            channels=channels,
        )

    stage_history_entry_archive(entry, pcm_bytes, sample_rate, channels, speaker_id, text)
    return None


try:
    from . import player_context
    player_context.register("tts_archive", close_all, reinit)
except ImportError:
    pass
