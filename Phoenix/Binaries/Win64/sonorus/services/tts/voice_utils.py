"""
Shared voice utilities independent of any provider.
"""
import os
import json
import hashlib
import threading
from typing import Dict, Optional, Tuple
from utils.localization import get_lowercase_map

# Parent directories
SONORUS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VOICE_REFERENCES_DIR = os.path.join(SONORUS_DIR, "voice_references")

# Data directory for persistent files
try:
    from utils.settings import DATA_DIR
except ImportError:
    DATA_DIR = os.path.join(SONORUS_DIR, "data")

# ============================================
# Voice Reference Hash Tracking
# ============================================

# In-memory hash cache (per session) - avoids recomputing MD5 for same file
_reference_hash_cache: Dict[str, str] = {}
_hash_cache_lock = threading.Lock()

# Persistent hash file for legacy voice adoption
VOICE_HASH_FILE = os.path.join(DATA_DIR, "voice_reference_hashes.json")


def compute_reference_hash(file_path: str) -> Optional[str]:
    """
    Compute MD5 hash of reference file, return first 8 hex chars (lowercase).

    Args:
        file_path: Path to the reference audio file

    Returns:
        First 8 chars of MD5 hash (lowercase), or None on error
    """
    with _hash_cache_lock:
        if file_path in _reference_hash_cache:
            return _reference_hash_cache[file_path]

    try:
        md5 = hashlib.md5()
        with open(file_path, 'rb') as f:
            for chunk in iter(lambda: f.read(8192), b''):
                md5.update(chunk)
        hash_str = md5.hexdigest()[:8].lower()

        with _hash_cache_lock:
            _reference_hash_cache[file_path] = hash_str

        return hash_str
    except (FileNotFoundError, PermissionError, IOError) as e:
        print(f"[VoiceUtils] Failed to hash {file_path}: {e}")
        return None


def clear_reference_hash_cache():
    """Clear in-memory hash cache (call on server restart)."""
    global _reference_hash_cache
    with _hash_cache_lock:
        _reference_hash_cache.clear()
    print("[VoiceUtils] Reference hash cache cleared")


def load_voice_hashes() -> Dict[str, str]:
    """
    Load legacy voice hash mappings from persistent file.

    Returns:
        Dict mapping character_lang keys to hash values
    """
    try:
        if os.path.exists(VOICE_HASH_FILE):
            with open(VOICE_HASH_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        print(f"[VoiceUtils] Error loading voice hashes: {e}")
    return {}


def save_voice_hash(character_lang: str, hash_val: str):
    """
    Save hash for a legacy voice (atomic write for crash safety).

    Args:
        character_lang: Cache key (e.g., "SebastianSallow_EN_US")
        hash_val: 8-char hash to store
    """
    try:
        data = load_voice_hashes()
        data[character_lang] = hash_val

        # Atomic write: temp file + rename
        temp_path = VOICE_HASH_FILE + ".tmp"
        with open(temp_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        os.replace(temp_path, VOICE_HASH_FILE)
    except Exception as e:
        print(f"[VoiceUtils] Error saving voice hash: {e}")


def remove_voice_hash(character_lang: str):
    """
    Remove hash entry for a voice (called when voice is deleted/replaced).

    Args:
        character_lang: Cache key to remove
    """
    try:
        data = load_voice_hashes()
        if character_lang in data:
            del data[character_lang]
            temp_path = VOICE_HASH_FILE + ".tmp"
            with open(temp_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
            os.replace(temp_path, VOICE_HASH_FILE)
    except Exception as e:
        print(f"[VoiceUtils] Error removing voice hash: {e}")


def parse_hashed_voice_name(display_name: str) -> Tuple[str, Optional[str], Optional[str]]:
    """
    Parse voice name into components: base name, language, and hash.

    Voice naming formats:
    - "SebastianSallow" -> ("SebastianSallow", None, None)
    - "SebastianSallow_a1b2c3d4" -> ("SebastianSallow", None, "a1b2c3d4")
    - "SebastianSallow_DE_DE" -> ("SebastianSallow", "DE_DE", None)
    - "SebastianSallow_DE_DE_a1b2c3d4" -> ("SebastianSallow", "DE_DE", "a1b2c3d4")

    Args:
        display_name: Voice display name from provider

    Returns:
        Tuple of (base_name, lang_code_or_none, hash_or_none)
    """
    hash_val = None

    # Check for hash suffix first (last 9 chars = "_" + 8 hex)
    if len(display_name) > 9:
        potential_hash = display_name[-8:]
        if display_name[-9] == "_" and all(c in '0123456789abcdef' for c in potential_hash.lower()):
            hash_val = potential_hash.lower()
            display_name = display_name[:-9]  # Strip hash for further parsing

    # Now check for language suffix (_XX_XX pattern)
    detected_lang = None
    original_name = display_name

    if "_" in display_name:
        parts = display_name.rsplit("_", 2)
        if len(parts) == 3 and len(parts[1]) == 2 and len(parts[2]) == 2:
            # Likely "CharName_LA_NG" format
            original_name = parts[0]
            detected_lang = f"{parts[1].upper()}_{parts[2].upper()}"

    return original_name, detected_lang, hash_val


def build_hashed_voice_name(character_name: str, lang: Optional[str], ref_hash: str) -> str:
    """
    Build voice name with hash suffix.

    Args:
        character_name: Base character name
        lang: Language code (e.g., "DE_DE") or None for EN_US
        ref_hash: 8-char reference hash

    Returns:
        Voice name with hash suffix (e.g., "SebastianSallow_DE_DE_a1b2c3d4")
    """
    if lang and lang != "EN_US":
        return f"{character_name}_{lang}_{ref_hash}"
    else:
        return f"{character_name}_{ref_hash}"


def get_game_language() -> str:
    """
    Get the current game language from settings.

    Returns:
        Language code (e.g., "EN_US", "DE_DE", etc.)
    """
    try:
        from utils.settings import load_settings
        settings = load_settings()
        return settings.get('setup', {}).get('language', 'EN_US')
    except Exception:
        return 'EN_US'  # Fallback to English


# Cache of voice names that have references (populated lazily)
_voice_reference_cache: Optional[set] = None


def get_voice_reference_names(language: str = "EN_US") -> set:
    """
    Get set of all voice names that have reference files for a specific language.

    Args:
        language: Language code (e.g., "EN_US", "DE_DE") - determines search directory.
                  Undubbed languages automatically fall back to EN_US.

    Returns:
        Set of voice names (e.g., {"SebastianSallow", "NatsaiOnai", ...})
    """
    global _voice_reference_cache

    from constants import get_voice_language
    language = get_voice_language(language)

    # For now, we cache only for the query, since language can vary
    # TODO: Could make cache language-aware with dict of sets
    voice_names = set()

    # Determine search directory based on language
    if language == "EN_US":
        search_dir = VOICE_REFERENCES_DIR  # voice_references/
    else:
        lang_suffix = language.lower()
        search_dir = os.path.join(VOICE_REFERENCES_DIR, lang_suffix)  # voice_references/de_de/

    if not os.path.exists(search_dir):
        return voice_names

    # Pattern: {VoiceName}_reference.wav or {VoiceName}_reference_{duration}.wav
    for f in os.listdir(search_dir):
        if not f.endswith(".wav"):
            continue
        if "_reference_" in f:
            voice_name = f.split("_reference_")[0]
        elif f.endswith("_reference.wav"):
            voice_name = f[:-len("_reference.wav")]
        else:
            continue
        if voice_name:
            voice_names.add(voice_name)
            voice_names.add(voice_name.lower())

    # Update cache with EN_US results for backward compatibility
    if language == "EN_US":
        _voice_reference_cache = voice_names

    return voice_names


def invalidate_voice_reference_cache():
    """Clear the voice reference cache (call when voice_references folder changes)."""
    global _voice_reference_cache
    _voice_reference_cache = None


def has_voice_reference(voice_name: str, language: str = "EN_US") -> bool:
    """
    Check if a voice name has a reference file for a specific language.

    This is the primary filter for "significant" NPCs - if they have a voice
    reference, they're worth tracking in dialogue history and earshot.

    Args:
        voice_name: Internal voice ID (e.g., "SebastianSallow", "AdultMaleA")
        language: Language code (e.g., "EN_US", "DE_DE") - determines search directory.
                  Undubbed languages automatically fall back to EN_US.

    Returns:
        True if voice reference exists, False otherwise
    """
    if not voice_name:
        return False

    known_voices = get_voice_reference_names(language)

    # Exact match
    if voice_name.lower() in known_voices:
        return True

    # Try without spaces (e.g., "Nellie Oggspire" -> "NellieOggspire")
    name_no_spaces = voice_name.replace(" ", "")
    if name_no_spaces.lower() in known_voices:
        return True

    # Check aliased name (e.g., HOG_Sanctum_Guardian1 -> Guardian1)
    from constants import resolve_voice_name
    resolved = resolve_voice_name(voice_name)
    if resolved != voice_name and resolved.lower() in known_voices:
        return True

    return False

def find_voice_reference(character_name: str, duration: str = "15s", language: str = "EN_US") -> Optional[str]:
    """
    Find a voice reference file for a character in language-specific directory.

    Searches:
    1. Language-specific directory (e.g., voice_references/de_de/ for German)
    2. Preferred exact name: {name}_reference.wav
    3. Legacy exact name: {name}_reference_{duration}.wav
    4. Without spaces
    5. Case-insensitive search

    Args:
        character_name: Character name (e.g., "SebastianSallow" or "Sebastian Sallow")
        duration: Reference duration ("10s", "15s", or "60s")
        language: Language code (e.g., "EN_US", "DE_DE") - determines search directory.
                  Undubbed languages automatically fall back to EN_US.

    Returns:
        Path to reference file, or None if not found
    """
    from constants import get_voice_language
    language = get_voice_language(language)

    # Determine search directory based on language
    if language == "EN_US":
        search_dir = VOICE_REFERENCES_DIR  # voice_references/
    else:
        lang_suffix = language.lower()
        search_dir = os.path.join(VOICE_REFERENCES_DIR, lang_suffix)  # voice_references/de_de/

    if not os.path.exists(search_dir):
        return None

    # Try exact name first
    for filename in (
        f"{character_name}_reference.wav",
        f"{character_name}_reference_{duration}.wav",
    ):
        path = os.path.join(search_dir, filename)
        if os.path.exists(path):
            return path

    # Try without spaces (e.g., "Nellie Oggspire" -> "NellieOggspire")
    name_no_spaces = character_name.replace(" ", "")
    for filename in (
        f"{name_no_spaces}_reference.wav",
        f"{name_no_spaces}_reference_{duration}.wav",
    ):
        path_no_spaces = os.path.join(search_dir, filename)
        if os.path.exists(path_no_spaces):
            return path_no_spaces

    # Try uppercase variant using lowercase_map (e.g., "neridaroberts" -> "NeridaRoberts")
    # lowercase_map structure: {"NeridaRoberts": "neridaroberts"}
    lowercase_map = get_lowercase_map()
    name_no_spaces_lower = name_no_spaces.lower()

    for upper_name, lower_name in lowercase_map.items():
        if lower_name == name_no_spaces_lower:
            for filename in (
                f"{upper_name}_reference.wav",
                f"{upper_name}_reference_{duration}.wav",
            ):
                path = os.path.join(search_dir, filename)
                if os.path.exists(path):
                    print(f"[Voice] Lowercase map match: '{character_name}' -> '{upper_name}' -> {filename}")
                    return path

    # Try case-insensitive search with multiple name variants
    name_lower = character_name.lower()
    name_lower_no_spaces = name_lower.replace(" ", "")

    for f in os.listdir(search_dir):
        f_lower = f.lower()
        # Match with or without spaces in the character name
        if f_lower.endswith("_reference.wav") or f_lower.endswith(f"_reference_{duration}.wav"):
            if f_lower.startswith(name_lower) or f_lower.startswith(name_lower_no_spaces):
                return os.path.join(search_dir, f)

    # Try aliased name (e.g., HOG_Sanctum_Guardian1 -> Guardian1)
    from constants import resolve_voice_name
    resolved = resolve_voice_name(character_name)
    if resolved != character_name:
        return find_voice_reference(resolved, duration, language)

    return None
