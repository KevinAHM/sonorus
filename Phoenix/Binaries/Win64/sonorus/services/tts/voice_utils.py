"""
Shared voice utilities independent of any provider.
"""
import os
from typing import Optional

# Parent directories
SONORUS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VOICE_REFERENCES_DIR = os.path.join(SONORUS_DIR, "voice_references")


def find_voice_reference(character_name: str, duration: str = "15s") -> Optional[str]:
    """
    Find a voice reference file for a character.

    Searches:
    1. Exact name: {name}_reference_{duration}.wav
    2. Without spaces: {nameNoSpaces}_reference_{duration}.wav
    3. Case-insensitive search

    Args:
        character_name: Character name (e.g., "SebastianSallow" or "Sebastian Sallow")
        duration: Reference duration ("10s", "15s", or "60s")

    Returns:
        Path to reference file, or None if not found
    """
    # Try exact name first
    filename = f"{character_name}_reference_{duration}.wav"
    path = os.path.join(VOICE_REFERENCES_DIR, filename)
    if os.path.exists(path):
        return path

    # Try without spaces (e.g., "Nellie Oggspire" -> "NellieOggspire")
    name_no_spaces = character_name.replace(" ", "")
    filename_no_spaces = f"{name_no_spaces}_reference_{duration}.wav"
    path_no_spaces = os.path.join(VOICE_REFERENCES_DIR, filename_no_spaces)
    if os.path.exists(path_no_spaces):
        return path_no_spaces

    # Try case-insensitive search with multiple name variants
    if os.path.exists(VOICE_REFERENCES_DIR):
        name_lower = character_name.lower()
        name_lower_no_spaces = name_lower.replace(" ", "")

        for f in os.listdir(VOICE_REFERENCES_DIR):
            f_lower = f.lower()
            # Match with or without spaces in the character name
            if f"_{duration}.wav" in f_lower:
                if f_lower.startswith(name_lower) or f_lower.startswith(name_lower_no_spaces):
                    return os.path.join(VOICE_REFERENCES_DIR, f)

    return None
