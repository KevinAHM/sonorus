"""
Event logging system for Sonorus - centralized event tracking and dashboard display.
Thread-safe logging of LLM calls, TTS operations, voice cloning, and vision captures.
"""
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Optional, Dict, Any, List

# Module state
from utils.settings import DATA_DIR
EVENTS_FILE = Path(DATA_DIR) / "system_events.json"
MAX_EVENTS = 100
_events_lock = threading.Lock()


def _generate_event_id() -> str:
    """Generate unique event ID: evt_{timestamp_ms}_{uuid_short}"""
    timestamp_ms = int(time.time() * 1000)
    uuid_short = str(uuid.uuid4())[:8]
    return f"evt_{timestamp_ms}_{uuid_short}"


def _load_events() -> List[Dict[str, Any]]:
    """Load events from JSON file. Returns empty list if file doesn't exist or is empty."""
    try:
        if EVENTS_FILE.exists():
            content = EVENTS_FILE.read_text(encoding='utf-8').strip()
            if content:
                return json.loads(content)
    except Exception as e:
        print(f"[EventLogger] Error loading events: {e}")
    return []


def _save_events(events: List[Dict[str, Any]]) -> None:
    """Save events to JSON file with auto-trim to MAX_EVENTS."""
    try:
        # Keep only most recent MAX_EVENTS
        events = events[-MAX_EVENTS:]

        with open(EVENTS_FILE, 'w', encoding='utf-8') as f:
            json.dump(events, f, indent=2)
    except Exception as e:
        print(f"[EventLogger] Error saving events: {e}")


def log_event(event_type: str, status: str = "success", data: Optional[Dict[str, Any]] = None, error: Optional[str] = None) -> str:
    """
    Log a system event (LLM, TTS, voice clone, or vision).

    Args:
        event_type: "llm" | "tts" | "voice_clone" | "vision"
        status: "success" | "error" | "warning"
        data: Type-specific event data
        error: Error message if status="error"

    Returns:
        Event ID
    """
    event_id = _generate_event_id()
    event = {
        "id": event_id,
        "timestamp": time.time(),
        "type": event_type,
        "status": status,
        "data": data or {},
        "error": error
    }

    with _events_lock:
        events = _load_events()
        events.append(event)
        _save_events(events)

    return event_id


def get_recent_events(limit: int = 100) -> List[Dict[str, Any]]:
    """Get most recent events (reverse chronological order)."""
    with _events_lock:
        events = _load_events()

    # Return most recent first
    return list(reversed(events[-limit:]))


def clear_events() -> None:
    """Clear all events."""
    with _events_lock:
        _save_events([])


# ============================================
# Event logging helpers for specific types
# ============================================

def log_llm_event(
    model: str,
    context: str,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    total_tokens: Optional[int] = None,
    duration_ms: Optional[float] = None,
    status: str = "success",
    error: Optional[str] = None
) -> str:
    """
    Log an LLM call event.

    Args:
        model: Model name (e.g., "google/gemini-3-flash-preview")
        context: Context of the call ("chat", "target_selection", "interjection", "vision", "sentiment")
        input_tokens: Number of input tokens used
        output_tokens: Number of output tokens generated
        total_tokens: Total tokens used
        duration_ms: Request latency in milliseconds
        status: "success" | "error"
        error: Error message if failed

    Returns:
        Event ID
    """
    return log_event(
        event_type="llm",
        status=status,
        data={
            "model": model,
            "context": context,
            "tokens": {
                "input": input_tokens,
                "output": output_tokens,
                "total": total_tokens
            },
            "duration_ms": duration_ms
        },
        error=error
    )


def log_tts_event(
    voice_id: str,
    text_excerpt: str,
    audio_bytes: int,
    text_length: Optional[int] = None,
    duration_ms: Optional[float] = None,
    status: str = "success",
    error: Optional[str] = None
) -> str:
    """
    Log a TTS synthesis event.

    Args:
        voice_id: Voice ID or character name
        text_excerpt: First 50-100 chars of text being synthesized
        audio_bytes: Number of audio bytes generated
        text_length: Total character count of text being synthesized
        duration_ms: Request latency in milliseconds
        status: "success" | "error"
        error: Error message if failed

    Returns:
        Event ID
    """
    return log_event(
        event_type="tts",
        status=status,
        data={
            "voice_id": voice_id,
            "text_excerpt": text_excerpt[:100],  # Truncate to 100 chars
            "text_length": text_length,
            "audio_bytes": audio_bytes,
            "duration_ms": duration_ms
        },
        error=error
    )


def log_voice_clone_event(
    character_name: str,
    language: str,
    reference_filename: str,
    voice_id: Optional[str] = None,
    status: str = "success",
    error: Optional[str] = None
) -> str:
    """
    Log a voice cloning event (PRIORITY).

    Args:
        character_name: Character whose voice is being cloned
        language: Language code (e.g., "EN_US")
        reference_filename: Name of the reference audio file used
        voice_id: ID of the created voice (if successful)
        status: "success" | "error"
        error: Error message if failed

    Returns:
        Event ID
    """
    return log_event(
        event_type="voice_clone",
        status=status,
        data={
            "character_name": character_name,
            "language": language,
            "reference_filename": reference_filename,
            "voice_id": voice_id
        },
        error=error
    )


def log_vision_event(
    trigger_reason: str,
    location_name: str,
    scene_description_excerpt: str,
    model: str,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    total_tokens: Optional[int] = None,
    status: str = "success",
    error: Optional[str] = None
) -> str:
    """
    Log a vision capture event (PRIORITY).

    Args:
        trigger_reason: Why capture happened ("distance" | "time_interval")
        location_name: Name of the location captured
        scene_description_excerpt: First 100 chars of vision description
        model: Vision model used
        input_tokens: Vision model input tokens
        output_tokens: Vision model output tokens
        total_tokens: Total tokens used
        status: "success" | "error"
        error: Error message if failed

    Returns:
        Event ID
    """
    return log_event(
        event_type="vision",
        status=status,
        data={
            "trigger_reason": trigger_reason,
            "location_name": location_name,
            "description_excerpt": scene_description_excerpt[:100],  # Truncate to 100 chars
            "model": model,
            "tokens": {
                "input": input_tokens,
                "output": output_tokens,
                "total": total_tokens
            }
        },
        error=error
    )


if __name__ == "__main__":
    # Quick test
    print("Testing event_logger...")

    # Test LLM event
    eid1 = log_llm_event(
        model="google/gemini-3-flash-preview",
        context="chat",
        input_tokens=150,
        output_tokens=42,
        total_tokens=192
    )
    print(f"Logged LLM event: {eid1}")

    # Test TTS event
    eid2 = log_tts_event(
        voice_id="sebastian-voice-123",
        text_excerpt="The Dark Arts are indeed intriguing to most wizards...",
        audio_bytes=8192,
        duration_ms=2500
    )
    print(f"Logged TTS event: {eid2}")

    # Test voice clone event
    eid3 = log_voice_clone_event(
        character_name="Sebastian Sallow",
        language="EN_US",
        reference_filename="sebastian_neutral_001.wav",
        voice_id="voice-clone-789"
    )
    print(f"Logged voice clone event: {eid3}")

    # Get recent events
    recent = get_recent_events(limit=10)
    print(f"\nRecent events: {len(recent)}")
    for evt in recent:
        print(f"  {evt['id']}: {evt['type']} ({evt['status']})")
