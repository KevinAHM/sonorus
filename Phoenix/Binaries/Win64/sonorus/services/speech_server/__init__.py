"""Shared Universal Speech Server integration."""

from .client import (
    GAME_LANGUAGE_CODES,
    MASKED_KEY,
    PROTOCOL_VERSION,
    SpeechServerClient,
    SpeechServerError,
    language_code,
    normalize_url,
    resolve_draft_key,
)

__all__ = [
    "GAME_LANGUAGE_CODES",
    "MASKED_KEY",
    "PROTOCOL_VERSION",
    "SpeechServerClient",
    "SpeechServerError",
    "language_code",
    "normalize_url",
    "resolve_draft_key",
]
