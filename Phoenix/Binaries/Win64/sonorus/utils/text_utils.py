"""
Text processing utilities for Sonorus.
Handles text splitting, name sanitization, and NPC filtering.
"""

import re
from constants import (
    CONVERSATION_EARSHOT_DISTANCE,
    STEALTH_EARSHOT_DISTANCE,
    FOLLOWING_COMPANION_EARSHOT_DISTANCE,
    BROOM_EARSHOT_DISTANCE,
    VOICE_NAME_ALIASES,
)
from services.tts.voice_utils import get_game_language, get_voice_reference_names, has_voice_reference


def split_into_sentences(text):
    """
    Split text into sentences, respecting abbreviations.

    IMPORTANT: Call this AFTER expand_abbreviations() so "Dr." becomes "doctor"
    and won't cause false sentence breaks.
    """
    # After abbreviation expansion, most problematic cases are gone
    # Split on sentence-ending punctuation followed by space or end
    pattern = r'(?<=[.!?])\s+'
    sentences = re.split(pattern, text.strip())
    # Filter empty strings and strip whitespace
    return [s.strip() for s in sentences if s.strip()]


# Common abbreviations that end with a period but aren't sentence endings.
# Lowercase for case-insensitive matching.
_ABBREVIATIONS = {
    'mr', 'mrs', 'ms', 'dr', 'prof', 'sr', 'jr', 'st', 'ave', 'vs',
    'gen', 'col', 'sgt', 'cpl', 'lt', 'capt', 'maj', 'rev', 'hon',
    'esq', 'ltd', 'co', 'inc', 'corp', 'dept', 'univ', 'approx',
    'ft', 'mt', 'pt', 'vol', 'no', 'fig', 'etc', 'al',
}

# Pattern: period followed by whitespace, but only if NOT preceded by
# a known abbreviation. Uses negative lookbehind approximation by checking
# for uppercase letter before period (sentence enders are typically lowercase
# words or proper nouns ending with period, but abbreviations are short).
_SENTENCE_SPLIT_RE = re.compile(
    r'(?<=[.!?])'   # After sentence-ending punctuation
    r'(?<![A-Z]\.)'  # NOT after single capital + dot (e.g. "O." in "O.W.L.s")
    r'\s+'           # Followed by whitespace
    r'|'             # OR
    r'(?<=[。！？])'  # After CJK sentence-ending punctuation (no whitespace needed)
)


def split_into_sentences_safe(text):
    """
    Split text into sentences for TTS pipelining, without expanding abbreviations first.

    Handles common abbreviations (Mr., Mrs., Dr., Prof., St., etc.) and
    acronyms like O.W.L.s or N.E.W.T.s without false splits.

    More conservative than split_into_sentences — prefers under-splitting
    (sending more text per chunk) over splitting mid-abbreviation.
    """
    if not text or not text.strip():
        return []

    text = text.strip()

    # Step 1: Protect abbreviations by temporarily replacing their dots
    # Replace "Mr." → "Mr\x00" etc. so the split regex won't match
    PLACEHOLDER = '\x00'
    protected = text
    for abbr in _ABBREVIATIONS:
        # Case-insensitive: match "Mr." "mr." "MR." etc.
        pattern = r'\b' + re.escape(abbr) + r'\.'
        protected = re.sub(pattern, lambda m: m.group(0)[:-1] + PLACEHOLDER,
                          protected, flags=re.IGNORECASE)

    # Protect acronyms: sequences like "O.W.L." or "N.E.W.T."
    # Pattern: single letter + dot repeated 2+ times
    protected = re.sub(
        r'(?:[A-Za-z]\.){2,}',
        lambda m: m.group(0).replace('.', PLACEHOLDER),
        protected
    )

    # Step 2: Split on actual sentence boundaries
    parts = _SENTENCE_SPLIT_RE.split(protected)

    # Step 3: Restore dots
    sentences = []
    for part in parts:
        restored = part.replace(PLACEHOLDER, '.').strip()
        if restored:
            sentences.append(restored)

    return sentences


def remove_unpaired_double_quotes(text: str) -> str:
    """
    Remove an unpaired double quote from subtitle text.

    Keeps properly paired `"` characters intact and removes only one dangling
    quote when the total count is odd. Preference order:
    1) trailing quote
    2) leading quote
    3) last quote occurrence
    """
    if not text or '"' not in text:
        return text

    quote_indices = [i for i, ch in enumerate(text) if ch == '"']
    if len(quote_indices) % 2 == 0:
        return text

    stripped_right = text.rstrip()
    if stripped_right.endswith('"'):
        remove_idx = text.rfind('"')
    else:
        stripped_left = text.lstrip()
        if stripped_left.startswith('"'):
            remove_idx = len(text) - len(stripped_left)
        else:
            remove_idx = quote_indices[-1]

    return text[:remove_idx] + text[remove_idx + 1:]


def estimate_tokens(text: str) -> int:
    """
    Estimate token count for TTS chunking.

    Simple heuristic: ~1 token per word, with adjustments for punctuation.
    Not exact but good enough for chunking decisions.
    """
    if not text:
        return 0
    # Count words (split on whitespace)
    words = text.split()
    return len(words)


def chunk_text_for_tts(text: str, target_tokens: int = 50) -> list:
    """
    Split text into chunks targeting ~target_tokens per chunk.

    Strategy:
    1. Split into sentences first
    2. Combine consecutive sentences while under target
    3. Never split mid-sentence (sentences are atomic)

    Args:
        text: Preprocessed text (AFTER normalize_for_tts / expand_abbreviations)
        target_tokens: Target token count per chunk (default 50)

    Returns:
        List of text chunks, each roughly target_tokens or one sentence if longer
    """
    if not text:
        return []

    sentences = split_into_sentences(text)
    if not sentences:
        return [text] if text.strip() else []

    # If only one sentence, return as-is regardless of length
    if len(sentences) == 1:
        return sentences

    chunks = []
    current_chunk = []
    current_tokens = 0

    for sentence in sentences:
        sentence_tokens = estimate_tokens(sentence)

        # If adding this sentence stays under target, add it
        if current_tokens + sentence_tokens <= target_tokens:
            current_chunk.append(sentence)
            current_tokens += sentence_tokens
        else:
            # Current chunk is full enough, start new one
            if current_chunk:
                chunks.append(' '.join(current_chunk))
            current_chunk = [sentence]
            current_tokens = sentence_tokens

    # Don't forget the last chunk
    if current_chunk:
        chunks.append(' '.join(current_chunk))

    return chunks


def sanitize_name(name):
    """Strip LLM garbage from names (quotes, asterisks, markdown, etc.)"""
    if not name:
        return name
    # Strip quotes, asterisks, backticks, brackets, common markdown
    name = re.sub(r'^[\s\'"*`\[\]]+', '', name)  # Leading garbage
    name = re.sub(r'[\s\'"*`\[\]]+$', '', name)  # Trailing garbage
    return name.strip()


def parse_target_result(result):
    """Parse target selection result like 'Sebastian>player' into (speaker, target)"""
    if not result or result == "0":
        return None, None

    result = result.strip()
    # Strip leading "- " if LLM included it from the formatted list
    if result.startswith("- "):
        result = result[2:]

    if ">" not in result:
        return sanitize_name(result), "player"

    parts = result.split(">", 1)
    speaker = sanitize_name(parts[0].strip())
    target = sanitize_name(parts[1].strip()) if len(parts) > 1 else "player"

    # Strip leading "- " from each part
    if speaker.startswith("- "):
        speaker = speaker[2:]
    if target.startswith("- "):
        target = target[2:]

    return speaker, target


# Prefixes that are always insignificant (even if voice reference exists)
INSIGNIFICANT_PREFIXES = (
    "t3",  # Generic unnamed students (T3StudentGryffindorMale1, etc.)
    "midres",  # Mid-resolution generic NPCs
)


def is_significant_npc(voice_name: str) -> bool:
    """
    Check if an NPC is significant enough to track in dialogue history.

    Significance is determined by:
    1. NOT matching a blacklisted prefix (generic students, enemies, townspeople)
    2. Having a voice reference file

    Players can make any NPC significant by adding a voice reference file,
    EXCEPT for blacklisted prefixes which are always filtered out.

    Args:
        voice_name: Internal voice ID (e.g., "SebastianSallow", "T3StudentSlytherinMale6")

    Returns:
        True if NPC should be tracked, False otherwise
    """
    if not voice_name:
        return False

    # Player is always significant
    if voice_name.lower() == "player":
        return True

    # Blacklist check - these are never significant
    for prefix in INSIGNIFICANT_PREFIXES:
        if voice_name.lower().startswith(prefix):
            return False

    # Check if voice reference exists for current game language
    # (has_voice_reference also checks VOICE_NAME_ALIASES internally)
    language = get_game_language()
    return has_voice_reference(voice_name, language)


def voice_name_to_display_name(voice_name: str) -> str:
    """
    Convert a voice name to its likely display name by adding spaces before capitals.

    This is a heuristic - voice names like "SebastianSallow" become "Sebastian Sallow".
    NOT guaranteed to match actual display names (which are localized), but works for
    most named characters in English.

    Args:
        voice_name: Internal voice ID (e.g., "SebastianSallow")

    Returns:
        Display name variant (e.g., "Sebastian Sallow")
    """
    if not voice_name:
        return voice_name
    # Add space before each capital letter (except the first)
    result = voice_name[0]
    for char in voice_name[1:]:
        if char.isupper():
            result += ' '
        result += char
    return result


def get_significant_npc_names() -> tuple:
    """
    Get list of all significant NPC voice names AND their display name variants.

    Returns all voice names that have reference files (i.e., are significant),
    plus generated display name variants for Lua-side filtering.

    IMPORTANT: Display names are generated heuristically (adding spaces before capitals).
    This works for most named characters but is NOT guaranteed to match localized names.

    Returns:
        Tuple of (voice_names, display_names) where:
        - voice_names: List like ["SebastianSallow", "NatsaiOnai", ...]
        - display_names: List like ["Sebastian Sallow", "Natsai Onai", ...]
    """
    # Get voice references for current game language
    language = get_game_language()
    canonical_by_key = {}
    for voice_name in get_voice_reference_names(language):
        key = voice_name.casefold()
        current = canonical_by_key.get(key)
        if current is None or (current == current.lower() and voice_name != voice_name.lower()):
            canonical_by_key[key] = voice_name

    # Add aliased names (e.g., HOG_Sanctum_Guardian1 -> Guardian1)
    # so Lua knows these game IDs are significant too
    for alias, ref_name in VOICE_NAME_ALIASES.items():
        if ref_name.casefold() in canonical_by_key:
            canonical_by_key.setdefault(alias.casefold(), alias)

    # Preserve canonical casing for APIs such as GetScheduledEntityFromName.
    # Lua builds its separate case-insensitive filter set when this is synced.
    voice_names = sorted(canonical_by_key.values(), key=str.casefold)
    display_names = [voice_name_to_display_name(v) for v in voice_names]
    return voice_names, display_names


def filter_npcs_by_earshot(nearby_npcs, max_distance=None, player_in_stealth=False,
                           player_on_broom=False, companion_on_broom=False,
                           companion_id=None, companion_following=False):
    """
    Filter nearby NPCs to only those within earshot distance for conversations.

    Args:
        nearby_npcs: List of NPC dicts with 'name' and 'distance' fields
        max_distance: Max distance in UE units (default: CONVERSATION_EARSHOT_DISTANCE)
        player_in_stealth: If True, uses reduced stealth distance (Disillusionment active)
        player_on_broom: If True, player is currently flying on a broom
        companion_on_broom: If True, companion is currently flying on a broom
        companion_id: Companion's NPC ID (e.g., "SebastianSallow") for companion range extension
        companion_following: If True, the companion is actively following and gets walking grace range

    Returns:
        Filtered list of NPCs within earshot
    """
    if max_distance is None:
        if player_in_stealth:
            max_distance = STEALTH_EARSHOT_DISTANCE
        else:
            max_distance = CONVERSATION_EARSHOT_DISTANCE

    # Companion gets special range handling:
    # - much larger when flying together on brooms
    # - moderately larger while actively following on foot so trailing pathing doesn't abort dialogue
    broom_extended_range = player_on_broom and companion_on_broom and companion_id
    walking_extended_range = companion_following and companion_id

    filtered = []
    for npc in nearby_npcs:
        distance = npc.get('distance', float('inf'))
        npc_name = npc.get('name', '').lower().replace(' ', '')

        # Check if this is the companion (special handling for broom/walking follow range)
        if companion_id and (broom_extended_range or walking_extended_range):
            companion_id_normalized = companion_id.lower().replace(' ', '')
            if companion_id_normalized in npc_name or npc_name in companion_id_normalized:
                allowed_distance = (
                    BROOM_EARSHOT_DISTANCE
                    if broom_extended_range
                    else FOLLOWING_COMPANION_EARSHOT_DISTANCE
                )
                if distance <= allowed_distance:
                    filtered.append(npc)
                continue

        # Standard earshot check for all other NPCs
        if distance <= max_distance:
            filtered.append(npc)

    return filtered


def validate_speaker_in_nearby(npc_id, nearby_npcs, load_localization_func=None):
    """
    Validate that an NPC is actually in the nearby NPC list.

    Args:
        npc_id: Internal NPC ID (e.g., "SebastianSallow") - also works with display names
        nearby_npcs: List of NPC dicts with 'name' field (ID format)
        load_localization_func: Optional function to load localization data for fallback

    Returns:
        True if NPC is in nearby list, False otherwise
    """
    if not npc_id or not nearby_npcs:
        return False

    # Normalize ID for comparison (remove spaces in case display name was passed)
    npc_id_lower = npc_id.lower().replace(' ', '')

    for npc in nearby_npcs:
        nearby_id = npc.get('name', '')
        # Compare with spaces removed
        nearby_id_lower = nearby_id.lower().replace(' ', '')

        # Exact match
        if npc_id_lower == nearby_id_lower:
            return True

        # Partial match (handles "Sebastian" matching "SebastianSallow")
        if npc_id_lower in nearby_id_lower or nearby_id_lower in npc_id_lower:
            return True

        # Check display name via localization if function provided
        if load_localization_func:
            loc = load_localization_func()
            display_name = loc.get(nearby_id, '')
            if display_name:
                display_lower = display_name.lower().replace(' ', '')
                if npc_id_lower == display_lower or npc_id_lower in display_lower:
                    return True

    return False


# ============================================
# Director Mode Prefix (STT)
# ============================================

# Localized prefixes for triggering director mode via voice input.
# English ("direct") is always checked; the game language prefix is also checked.
DIRECTOR_PREFIXES = {
    "DE_DE": "regie",
    "ES_ES": "dirige",
    "FR_FR": "dirige",
    "IT_IT": "dirigi",
    "PT_BR": "dirija",
    "JA_JP": "\u6f14\u51fa",     # 演出
    "KO_KR": "\uc5f0\ucd9c",     # 연출
    "ZH_CN": "\u5bfc\u6f14",     # 导演
}


def extract_director_prefix(text: str, language: str = "EN_US") -> tuple[str | None, str]:
    """Check if text starts with a director mode prefix and strip it.

    Always checks English "direct " first, then the game-language prefix.

    Returns:
        (remaining_text, mode) where mode is "prompt" if prefix found, "chat" otherwise.
        remaining_text has the prefix stripped and is stripped of whitespace.
    """
    lower = text.lower()

    # Always check English
    if lower.startswith("direct "):
        remaining = text[7:].strip()
        if remaining:
            return remaining, "prompt"
        return None, "chat"

    # Check game-language prefix (if not English)
    if not language.startswith("EN"):
        base_lang = language.split("_")[0].upper()
        lang_normalization = {
            "ES": "ES_ES", "PT": "PT_BR", "DE": "DE_DE", "FR": "FR_FR",
            "IT": "IT_IT", "JA": "JA_JP", "KO": "KO_KR", "ZH": "ZH_CN"
        }
        normalized = lang_normalization.get(base_lang, language)
        prefix = DIRECTOR_PREFIXES.get(normalized)
        if prefix and lower.startswith(prefix + " "):
            remaining = text[len(prefix) + 1:].strip()
            if remaining:
                return remaining, "prompt"
            return None, "chat"

    return text, "chat"


# ============================================
# Companion Move Command Detection
# ============================================

# Keywords that suggest the player is commanding an NPC to move/relocate.
# Checked as whole words (word boundary match) in the player's input.
MOVE_KEYWORDS = {
    "EN": ["move", "go", "come", "away", "walk"],
    "DE": ["beweg", "geh", "komm", "weg", "lauf"],
    "ES": ["mueve", "ve", "ven", "fuera", "camina"],
    "FR": ["bouge", "va", "viens", "pousse", "marche"],
    "IT": ["muovi", "vai", "vieni", "via", "cammina"],
    "PT": ["move", "vai", "vem", "sai", "anda"],
    "JA": ["\u52d5\u3051", "\u884c\u3051", "\u6765\u3044", "\u3069\u3051", "\u6b69\u3051"],  # 動け, 行け, 来い, どけ, 歩け
    "KO": ["\uc6c0\uc9c1\uc5ec", "\uac00", "\uc640", "\ube44\ucf1c", "\uac78\uc5b4"],  # 움직여, 가, 와, 비켜, 걸어
    "ZH": ["\u8d70", "\u8fc7\u6765", "\u8ba9\u5f00", "\u79fb\u52a8", "\u8d70\u5f00"],  # 走, 过来, 让开, 移动, 走开
}


def has_move_keyword(text, language="EN_US"):
    """Check if text contains a move-related keyword for the given language.

    Returns True if any move keyword is found as a whole word in the text.
    Always checks English keywords, plus the game language if different.
    """
    if not text:
        return False

    lower = text.lower()
    base_lang = language.split("_")[0].upper() if language else "EN"

    # Always check English
    for kw in MOVE_KEYWORDS.get("EN", []):
        if re.search(r'\b' + re.escape(kw) + r'\b', lower):
            return True

    # Check game language if not English
    if base_lang != "EN":
        for kw in MOVE_KEYWORDS.get(base_lang, []):
            if kw in lower:  # CJK languages don't have word boundaries
                return True

    return False


# ============================================
# Audio Tag Localization for TTS
# ============================================

# Audio tag translations by language
# Includes both verb forms (sighs/sighing, laughs/laughing, etc.)
AUDIO_TAG_TRANSLATIONS = {
    "DE_DE": {
        "sigh": "seufzt", "sighs": "seufzt", "sighing": "seufzt",
        "laugh": "lacht", "laughs": "lacht", "laughing": "lacht",
        "whisper": "flüstert", "whispers": "flüstert", "whispering": "flüstert",
        "shout": "schreit", "shouts": "schreit", "shouting": "schreit",
        "clears throat": "räuspert sich", "clearing throat": "räuspert sich", "clear_throat": "räuspert sich",
        "pause": "Pause", "pauses": "Pause", "pausing": "Pause",
        "chuckle": "kichert", "chuckles": "kichert", "chuckling": "kichert",
        "gasp": "keucht", "gasps": "keucht", "gasping": "keucht",
        "cough": "hustet", "coughs": "hustet", "coughing": "hustet",
        "breathe": "atmet", "breathes": "atmet", "breathing": "atmet",
        "cry": "weint", "crying": "weint", "cries": "weint",
        "yawn": "gähnt", "yawns": "gähnt", "yawning": "gähnt",
        "groan": "stöhnt", "groans": "stöhnt", "groaning": "stöhnt",
    },
    "ES_ES": {
        "sigh": "suspira", "sighs": "suspira", "sighing": "suspira",
        "laugh": "ríe", "laughs": "ríe", "laughing": "ríe",
        "whisper": "susurra", "whispers": "susurra", "whispering": "susurra",
        "shout": "grita", "shouts": "grita", "shouting": "grita",
        "clears throat": "carraspea", "clearing throat": "carraspea", "clear_throat": "carraspea",
        "pause": "pausa", "pauses": "pausa", "pausing": "pausa",
        "chuckle": "ríe entre dientes", "chuckles": "ríe entre dientes", "chuckling": "ríe entre dientes",
        "gasp": "jadea", "gasps": "jadea", "gasping": "jadea",
        "cough": "tose", "coughs": "tose", "coughing": "tose",
        "breathe": "respira", "breathes": "respira", "breathing": "respira",
        "cry": "llora", "crying": "llora", "cries": "llora",
        "yawn": "bosteza", "yawns": "bosteza", "yawning": "bosteza",
        "groan": "gime", "groans": "gime", "groaning": "gime",
    },
    "FR_FR": {
        "sigh": "soupire", "sighs": "soupire", "sighing": "soupire",
        "laugh": "rit", "laughs": "rit", "laughing": "rit",
        "whisper": "chuchote", "whispers": "chuchote", "whispering": "chuchote",
        "shout": "crie", "shouts": "crie", "shouting": "crie",
        "clears throat": "se racle la gorge", "clearing throat": "se racle la gorge", "clear_throat": "se racle la gorge",
        "pause": "pause", "pauses": "pause", "pausing": "pause",
        "chuckle": "glousse", "chuckles": "glousse", "chuckling": "glousse",
        "gasp": "halète", "gasps": "halète", "gasping": "halète",
        "cough": "tousse", "coughs": "tousse", "coughing": "tousse",
        "breathe": "respire", "breathes": "respire", "breathing": "respire",
        "cry": "pleure", "crying": "pleure", "cries": "pleure",
        "yawn": "bâille", "yawns": "bâille", "yawning": "bâille",
        "groan": "gémit", "groans": "gémit", "groaning": "gémit",
    },
    "IT_IT": {
        "sigh": "sospira", "sighs": "sospira", "sighing": "sospira",
        "laugh": "ride", "laughs": "ride", "laughing": "ride",
        "whisper": "sussurra", "whispers": "sussurra", "whispering": "sussurra",
        "shout": "grida", "shouts": "grida", "shouting": "grida",
        "clears throat": "si schiarisce la gola", "clearing throat": "si schiarisce la gola", "clear_throat": "si schiarisce la gola",
        "pause": "pausa", "pauses": "pausa", "pausing": "pausa",
        "chuckle": "ridacchia", "chuckles": "ridacchia", "chuckling": "ridacchia",
        "gasp": "ansima", "gasps": "ansima", "gasping": "ansima",
        "cough": "tossisce", "coughs": "tossisce", "coughing": "tossisce",
        "breathe": "respira", "breathes": "respira", "breathing": "respira",
        "cry": "piange", "crying": "piange", "cries": "piange",
        "yawn": "sbadiglia", "yawns": "sbadiglia", "yawning": "sbadiglia",
        "groan": "geme", "groans": "geme", "groaning": "geme",
    },
    "PT_BR": {
        "sigh": "suspira", "sighs": "suspira", "sighing": "suspira",
        "laugh": "ri", "laughs": "ri", "laughing": "ri",
        "whisper": "sussurra", "whispers": "sussurra", "whispering": "sussurra",
        "shout": "grita", "shouts": "grita", "shouting": "grita",
        "clears throat": "limpa a garganta", "clearing throat": "limpa a garganta", "clear_throat": "limpa a garganta",
        "pause": "pausa", "pauses": "pausa", "pausing": "pausa",
        "chuckle": "ri baixinho", "chuckles": "ri baixinho", "chuckling": "ri baixinho",
        "gasp": "arqueja", "gasps": "arqueja", "gasping": "arqueja",
        "cough": "tosse", "coughs": "tosse", "coughing": "tosse",
        "breathe": "respira", "breathes": "respira", "breathing": "respira",
        "cry": "chora", "crying": "chora", "cries": "chora",
        "yawn": "boceja", "yawns": "boceja", "yawning": "boceja",
        "groan": "geme", "groans": "geme", "groaning": "geme",
    },
    "JA_JP": {
        "sigh": "ため息", "sighs": "ため息", "sighing": "ため息",
        "laugh": "笑う", "laughs": "笑う", "laughing": "笑う",
        "whisper": "囁く", "whispers": "囁く", "whispering": "囁く",
        "shout": "叫ぶ", "shouts": "叫ぶ", "shouting": "叫ぶ",
        "clears throat": "咳払い", "clearing throat": "咳払い", "clear_throat": "咳払い",
        "pause": "間", "pauses": "間", "pausing": "間",
        "chuckle": "くすくす笑う", "chuckles": "くすくす笑う", "chuckling": "くすくす笑う",
        "gasp": "息を呑む", "gasps": "息を呑む", "gasping": "息を呑む",
        "cough": "咳", "coughs": "咳", "coughing": "咳",
        "breathe": "息", "breathes": "息", "breathing": "息",
        "cry": "泣く", "crying": "泣く", "cries": "泣く",
        "yawn": "あくび", "yawns": "あくび", "yawning": "あくび",
        "groan": "うめく", "groans": "うめく", "groaning": "うめく",
    },
    "KO_KR": {
        "sigh": "한숨", "sighs": "한숨", "sighing": "한숨",
        "laugh": "웃음", "laughs": "웃음", "laughing": "웃음",
        "whisper": "속삭임", "whispers": "속삭임", "whispering": "속삭임",
        "shout": "외침", "shouts": "외침", "shouting": "외침",
        "clears throat": "헛기침", "clearing throat": "헛기침", "clear_throat": "헛기침",
        "pause": "멈춤", "pauses": "멈춤", "pausing": "멈춤",
        "chuckle": "킥킥", "chuckles": "킥킥", "chuckling": "킥킥",
        "gasp": "헉", "gasps": "헉", "gasping": "헉",
        "cough": "기침", "coughs": "기침", "coughing": "기침",
        "breathe": "숨", "breathes": "숨", "breathing": "숨",
        "cry": "울음", "crying": "울음", "cries": "울음",
        "yawn": "하품", "yawns": "하품", "yawning": "하품",
        "groan": "신음", "groans": "신음", "groaning": "신음",
    },
    "ZH_CN": {
        "sigh": "叹气", "sighs": "叹气", "sighing": "叹气",
        "laugh": "笑", "laughs": "笑", "laughing": "笑",
        "whisper": "低语", "whispers": "低语", "whispering": "低语",
        "shout": "喊", "shouts": "喊", "shouting": "喊",
        "clears throat": "清嗓", "clearing throat": "清嗓", "clear_throat": "清嗓",
        "pause": "停顿", "pauses": "停顿", "pausing": "停顿",
        "chuckle": "轻笑", "chuckles": "轻笑", "chuckling": "轻笑",
        "gasp": "喘气", "gasps": "喘气", "gasping": "喘气",
        "cough": "咳嗽", "coughs": "咳嗽", "coughing": "咳嗽",
        "breathe": "呼吸", "breathes": "呼吸", "breathing": "呼吸",
        "cry": "哭泣", "crying": "哭泣", "cries": "哭泣",
        "yawn": "打哈欠", "yawns": "打哈欠", "yawning": "打哈欠",
        "groan": "呻吟", "groans": "呻吟", "groaning": "呻吟",
    },
}

# Canonical tag forms per TTS provider
# Maps any variant to the form the provider expects
# Inworld: base/imperative form  [sigh], [laugh], [whisper]
# ElevenLabs: third-person present  [sighs], [laughs], [whispers]
_TAG_CANONICAL = {
    "inworld": {
        "sighs": "sigh", "sighing": "sigh",
        "laughs": "laugh", "laughing": "laugh",
        "whispers": "whisper", "whispering": "whisper",
        "shouts": "shout", "shouting": "shout",
        "chuckles": "chuckle", "chuckling": "chuckle",
        "gasps": "gasp", "gasping": "gasp",
        "coughs": "cough", "coughing": "cough",
        "breathes": "breathe", "breathing": "breathe",
        "cries": "cry", "crying": "cry",
        "yawns": "yawn", "yawning": "yawn",
        "groans": "groan", "groaning": "groan",
        "pauses": "pause", "pausing": "pause",
        "clearing throat": "clear_throat", "clears throat": "clear_throat",
    },
    "elevenlabs": {
        "sigh": "sighs", "sighing": "sighs",
        "laugh": "laughs", "laughing": "laughs",
        "whisper": "whispers", "whispering": "whispers",
        "shout": "shouts", "shouting": "shouts",
        "chuckle": "chuckles", "chuckling": "chuckles",
        "gasp": "gasps", "gasping": "gasps",
        "cough": "coughs", "coughing": "coughs",
        "breathe": "breathes", "breathing": "breathes",
        "cry": "cries", "crying": "cries",
        "yawn": "yawns", "yawning": "yawns",
        "groan": "groans", "groaning": "groans",
        "pause": "pauses", "pausing": "pauses",
        "clearing throat": "clears throat", "clear_throat": "clears throat",
    },
    "omnivoice": {
        # OmniVoice only supports [laughter] — all laugh variants map to it,
        # everything else gets stripped by omnivoice_text.preprocess_text
        "laugh": "laughter", "laughs": "laughter", "laughing": "laughter",
        "chuckle": "laughter", "chuckles": "laughter", "chuckling": "laughter",
    },
    "omnivoice_api": {
        "laugh": "laughter", "laughs": "laughter", "laughing": "laughter",
        "chuckle": "laughter", "chuckles": "laughter", "chuckling": "laughter",
    },
}


def normalize_audio_tags(text: str, tts_provider: str) -> str:
    """Normalize audio tag forms to what the TTS provider expects.

    Inworld expects base form: [sigh], [laugh], [whisper]
    ElevenLabs expects third-person: [sighs], [laughs], [whispers]

    Tags already in the correct form pass through unchanged.
    """
    if not text:
        return text

    canonical = _TAG_CANONICAL.get(tts_provider.lower())
    if not canonical:
        return text

    def replace_tag(match):
        tag_content = match.group(1).lower().strip()
        if tag_content in canonical:
            return f"[{canonical[tag_content]}]"
        return match.group(0)

    return re.sub(r'\[([^\]]+)\]', replace_tag, text)


def localize_audio_tags(text: str, language: str) -> str:
    """
    Replace English audio tags with language-specific equivalents.

    Inworld TTS requires localized tags to process them correctly (unlike ElevenLabs
    which handles normalization internally).

    Args:
        text: Text containing English tags like [sighs], [laughs]
        language: Language code (e.g., "DE_DE", "ES_MX")

    Returns:
        Text with localized audio tags, or original if English/unsupported
    """
    if not text or language.startswith("EN"):
        return text

    # Normalize language variants to standard form (ES_MX -> ES_ES, PT_PT -> PT_BR, etc.)
    base_lang = language.split("_")[0].upper()
    lang_normalization = {
        "ES": "ES_ES", "PT": "PT_BR", "DE": "DE_DE", "FR": "FR_FR",
        "IT": "IT_IT", "JA": "JA_JP", "KO": "KO_KR", "ZH": "ZH_CN"
    }
    normalized_lang = lang_normalization.get(base_lang, language)

    translations = AUDIO_TAG_TRANSLATIONS.get(normalized_lang, {})
    if not translations:
        return text

    def replace_tag(match):
        tag_content = match.group(1).lower().strip()
        if tag_content in translations:
            return f"[{translations[tag_content]}]"
        return match.group(0)  # Keep original if no translation

    return re.sub(r'\[([^\]]+)\]', replace_tag, text)


def remove_brackets(text: str) -> str:
    """
    Remove bracketed annotations and clean whitespace.

    Removes patterns like [laughing], [sighs], [*], etc. and cleans up
    any double whitespaces left behind. Used by TTS to strip non-spoken
    annotations from text.

    Args:
        text: Raw text with potential bracketed annotations

    Returns:
        Cleaned text without brackets and normalized whitespace
    """
    # Remove bracketed text (anything within square brackets)
    text = re.sub(r'\[.*?\]', '', text)

    # Replace multiple whitespaces with single space
    text = re.sub(r'\s+', ' ', text)

    # Strip leading/trailing whitespace
    return text.strip()


def _parse_pronunciation_value(value: str) -> tuple:
    """Parse a pronunciation replacement value into (plain, ipa) forms.

    Supports formats:
        "Akeeyo"                       -> ("Akeeyo", None)
        "Akeeyo|/ˈæk.i.oʊ/"          -> ("Akeeyo", "/ˈæk.i.oʊ/")
        "/kriːt/"                      -> (None, "/kriːt/")
        "/ˈæk.i.oʊ/|Akeeyo"          -> ("Akeeyo", "/ˈæk.i.oʊ/")
        "you woond me|you /wuːnd/ me" -> ("you woond me", "you /wuːnd/ me")

    IPA form is returned with slashes intact (ready to use as-is).
    """
    plain = None
    ipa = None
    for part in value.split('|'):
        part = part.strip()
        if not part:
            continue
        # Standalone IPA: "/ˈæk.i.oʊ/" or phrase with embedded IPA: "you /wuːnd/ me"
        if re.search(r'/[^/]+/', part):
            ipa = part
        else:
            plain = part
    return plain, ipa



def apply_pronunciation_replacements(text: str, tts_provider: str = '') -> str:
    """Apply user-configured pronunciation replacements for TTS.

    Reads replacements from settings (audio.pronunciation_replacements).
    Each entry maps a word/phrase to its phonetic replacement.
    Matches are case-insensitive and respect word boundaries.

    For Inworld: prefers IPA, falls back to plain text.
    For other providers: uses plain text only, skips IPA-only entries.
    """
    from utils.settings import get_setting

    replacements = get_setting('audio.pronunciation_replacements', {})
    if not replacements or not isinstance(replacements, dict):
        return text

    use_ipa = tts_provider.lower() == 'inworld'

    for word, replacement in replacements.items():
        if not word or not replacement:
            continue
        plain, ipa = _parse_pronunciation_value(replacement)
        # Pick the right form for this provider
        if use_ipa:
            sub = ipa if ipa else plain  # Prefer IPA (already has /slashes/), fall back to plain
        else:
            sub = plain  # Plain text only
        if not sub:
            continue
        # Escape the word for regex, use word boundaries for whole-word matching
        pattern = r'\b' + re.escape(word) + r'\b'
        text = re.sub(pattern, sub, text, flags=re.IGNORECASE)

    return text


def strip_parentheses(text: str) -> str:
    """Remove parenthetical content like (whispering) from text."""
    text = re.sub(r'\(.*?\)', '', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def preprocess_text(text: str) -> str:
    """Normalize text, strip quotes, and collapse whitespace"""
    replacements = [
        ("…", "..."),
        ("—", ", "),
        ("–", ", "),
        (":", ","),
        (";", ","),
        ("\n", " "),
        ('"', ""),
        (""", ""),
        (""", ""),
        ("'", "'"),
        ("'", "'"),
        ("‚", "'"),
        ("‛", "'"),
        ("ʼ", "'"),
        ("ʹ", "'"),
        ("ʻ", "'"),
        ("ʾ", "'"),
        ("ʿ", "'"),
        ("′", "'"),
        ("‵", "'"),
        ("＇", "'"),
        ("ꞌ", "'"),
        ("*", ""),
    ]
    for src, dst in replacements:
        text = text.replace(src, dst)

    return text


# ============================================
# TTS Text Normalization
# ============================================

# Number word lists
ONES = ['', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine',
        'ten', 'eleven', 'twelve', 'thirteen', 'fourteen', 'fifteen', 'sixteen',
        'seventeen', 'eighteen', 'nineteen']
TENS = ['', '', 'twenty', 'thirty', 'forty', 'fifty', 'sixty', 'seventy', 'eighty', 'ninety']
ORDINAL_ONES = ['', 'first', 'second', 'third', 'fourth', 'fifth', 'sixth', 'seventh',
                'eighth', 'ninth', 'tenth', 'eleventh', 'twelfth', 'thirteenth',
                'fourteenth', 'fifteenth', 'sixteenth', 'seventeenth', 'eighteenth', 'nineteenth']
ORDINAL_TENS = ['', '', 'twentieth', 'thirtieth', 'fortieth', 'fiftieth',
                'sixtieth', 'seventieth', 'eightieth', 'ninetieth']


def number_to_words(num: int, andword: str = '', zero: str = 'zero', group: int = 0) -> str:
    """Convert integer to English words."""
    if num == 0:
        return zero

    def convert(n: int) -> str:
        if n < 20:
            return ONES[n]
        if n < 100:
            return TENS[n // 10] + (' ' + ONES[n % 10] if n % 10 else '')
        if n < 1000:
            remainder = n % 100
            result = ONES[n // 100] + ' hundred'
            if remainder:
                result += (' ' + andword + ' ' if andword else ' ') + convert(remainder)
            return result
        if n < 1_000_000:
            thousands = n // 1000
            remainder = n % 1000
            result = convert(thousands) + ' thousand'
            if remainder:
                result += ' ' + convert(remainder)
            return result
        if n < 1_000_000_000:
            millions = n // 1_000_000
            remainder = n % 1_000_000
            result = convert(millions) + ' million'
            if remainder:
                result += ' ' + convert(remainder)
            return result
        billions = n // 1_000_000_000
        remainder = n % 1_000_000_000
        result = convert(billions) + ' billion'
        if remainder:
            result += ' ' + convert(remainder)
        return result

    # Group mode 2: year-like pronunciation (e.g., 1984 -> "nineteen eighty four")
    if group == 2 and 1000 < num < 10000:
        high = num // 100
        low = num % 100
        if low == 0:
            return convert(high) + ' hundred'
        elif low < 10:
            return convert(high) + ' ' + ('oh' if zero == 'oh' else zero) + ' ' + ONES[low]
        else:
            return convert(high) + ' ' + convert(low)

    return convert(num)


def ordinal_to_words(num: int) -> str:
    """Convert integer to ordinal words (1 -> 'first', 2 -> 'second', etc.)."""
    if num < 20:
        return ORDINAL_ONES[num] if num < len(ORDINAL_ONES) and ORDINAL_ONES[num] else number_to_words(num) + 'th'
    if num < 100:
        tens_digit = num // 10
        ones_digit = num % 10
        if ones_digit == 0:
            return ORDINAL_TENS[tens_digit]
        return TENS[tens_digit] + ' ' + ORDINAL_ONES[ones_digit]

    cardinal = number_to_words(num)
    if cardinal.endswith('y'):
        return cardinal[:-1] + 'ieth'
    if cardinal.endswith('one'):
        return cardinal[:-3] + 'first'
    if cardinal.endswith('two'):
        return cardinal[:-3] + 'second'
    if cardinal.endswith('three'):
        return cardinal[:-5] + 'third'
    if cardinal.endswith('ve'):
        return cardinal[:-2] + 'fth'
    if cardinal.endswith('e'):
        return cardinal[:-1] + 'th'
    if cardinal.endswith('t'):
        return cardinal + 'h'
    return cardinal + 'th'


# Unicode to ASCII mapping
UNICODE_MAP = {
    'à': 'a', 'á': 'a', 'â': 'a', 'ã': 'a', 'ä': 'a', 'å': 'a', 'æ': 'ae',
    'ç': 'c', 'è': 'e', 'é': 'e', 'ê': 'e', 'ë': 'e', 'ì': 'i', 'í': 'i',
    'î': 'i', 'ï': 'i', 'ñ': 'n', 'ò': 'o', 'ó': 'o', 'ô': 'o', 'õ': 'o',
    'ö': 'o', 'ø': 'o', 'ù': 'u', 'ú': 'u', 'û': 'u', 'ü': 'u', 'ý': 'y',
    'ÿ': 'y', 'ß': 'ss', 'œ': 'oe', 'ð': 'd', 'þ': 'th',
    'À': 'A', 'Á': 'A', 'Â': 'A', 'Ã': 'A', 'Ä': 'A', 'Å': 'A', 'Æ': 'AE',
    'Ç': 'C', 'È': 'E', 'É': 'E', 'Ê': 'E', 'Ë': 'E', 'Ì': 'I', 'Í': 'I',
    'Î': 'I', 'Ï': 'I', 'Ñ': 'N', 'Ò': 'O', 'Ó': 'O', 'Ô': 'O', 'Õ': 'O',
    'Ö': 'O', 'Ø': 'O', 'Ù': 'U', 'Ú': 'U', 'Û': 'U', 'Ü': 'U', 'Ý': 'Y',
    '\u201C': '"', '\u201D': '"', '\u2018': "'", '\u2019': "'",
    '\u2026': '...', '\u2013': '-', '\u2014': '-'
}


def convert_to_ascii(text: str) -> str:
    """Convert unicode characters to ASCII equivalents."""
    import unicodedata
    result = ''.join(UNICODE_MAP.get(c, c) for c in text)
    # NFD normalization + strip combining marks
    result = unicodedata.normalize('NFD', result)
    result = ''.join(c for c in result if unicodedata.category(c) != 'Mn')
    return result


# Abbreviation expansions (case-insensitive)
ABBREVIATIONS = [
    (r'\bmrs\.', 'misuss'),
    (r'\bms\.', 'miss'),
    (r'\bmr\.', 'mister'),
    (r'\bdr\.', 'doctor'),
    (r'\bst\.', 'saint'),
    (r'\bco\.', 'company'),
    (r'\bjr\.', 'junior'),
    (r'\bmaj\.', 'major'),
    (r'\bgen\.', 'general'),
    (r'\bdrs\.', 'doctors'),
    (r'\brev\.', 'reverend'),
    (r'\blt\.', 'lieutenant'),
    (r'\bhon\.', 'honorable'),
    (r'\bsgt\.', 'sergeant'),
    (r'\bcapt\.', 'captain'),
    (r'\besq\.', 'esquire'),
    (r'\bltd\.', 'limited'),
    (r'\bcol\.', 'colonel'),
    (r'\bft\.', 'fort'),
]

# Case-sensitive abbreviations
CASED_ABBREVIATIONS = [
    (r'\bTTS\b', 'text to speech'),
    (r'\bHz\b', 'hertz'),
    (r'\bkHz\b', 'kilohertz'),
    (r'\bKBs\b', 'kilobytes'),
    (r'\bKB\b', 'kilobyte'),
    (r'\bMBs\b', 'megabytes'),
    (r'\bMB\b', 'megabyte'),
    (r'\bGBs\b', 'gigabytes'),
    (r'\bGB\b', 'gigabyte'),
    (r'\bTBs\b', 'terabytes'),
    (r'\bTB\b', 'terabyte'),
    (r'\bAPIs\b', "a p i's"),
    (r'\bAPI\b', 'a p i'),
    (r'\bCLIs\b', "c l i's"),
    (r'\bCLI\b', 'c l i'),
    (r'\bCPUs\b', "c p u's"),
    (r'\bCPU\b', 'c p u'),
    (r'\bGPUs\b', "g p u's"),
    (r'\bGPU\b', 'g p u'),
    (r'\bAve\b', 'avenue'),
    (r'\betc\b', 'etcetera'),
]


def expand_abbreviations(text: str) -> str:
    """Expand common abbreviations to full words."""
    # Case-insensitive abbreviations
    for pattern, replacement in ABBREVIATIONS:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    # Case-sensitive abbreviations
    for pattern, replacement in CASED_ABBREVIATIONS:
        text = re.sub(pattern, replacement, text)
    return text


# Number normalization regex patterns
NUM_PREFIX_RE = re.compile(r'#(\d)')
NUM_SUFFIX_RE = re.compile(r'(\d)([KMBT])', re.IGNORECASE)
NUM_LETTER_SPLIT_RE = re.compile(r'(\d)([a-z])|([a-z])(\d)', re.IGNORECASE)
COMMA_NUMBER_RE = re.compile(r'(\d[\d,]+\d)')
DATE_RE = re.compile(r'(^|[^/])(\d\d?[/-]\d\d?[/-]\d\d(?:\d\d)?)($|[^/])')
PHONE_NUMBER_RE = re.compile(r'\(?\d{3}\)?[-.\s]\d{3}[-.\s]?\d{4}')
TIME_RE = re.compile(r'(\d\d?):(\d\d)(?::(\d\d))?')
POUNDS_RE = re.compile(r'£([\d,]*\d+)')
DOLLARS_RE = re.compile(r'\$([\d.,]*\d+)')
DECIMAL_NUMBER_RE = re.compile(r'(\d+(?:\.\d+)+)')
MULTIPLY_RE = re.compile(r'(\d)\s?\*\s?(\d)')
DIVIDE_RE = re.compile(r'(\d)\s?/\s?(\d)')
ADD_RE = re.compile(r'(\d)\s?\+\s?(\d)')
SUBTRACT_RE = re.compile(r'(\d)?\s?-\s?(\d)')
FRACTION_RE = re.compile(r'(\d+)/(\d+)')
ORDINAL_RE = re.compile(r'(\d+)(st|nd|rd|th)', re.IGNORECASE)
NUMBER_RE = re.compile(r'\d+')


def normalize_numbers(text: str) -> str:
    """Convert numbers to spoken words."""
    # Number prefix (#1 -> "number 1")
    text = NUM_PREFIX_RE.sub(lambda m: f"number {m.group(1)}", text)

    # Number suffix (5K -> "5 thousand")
    def suffix_replace(m):
        suffix_map = {'k': 'thousand', 'm': 'million', 'b': 'billion', 't': 'trillion'}
        return f"{m.group(1)} {suffix_map[m.group(2).lower()]}"
    text = NUM_SUFFIX_RE.sub(suffix_replace, text)

    # Split numbers from letters (5x -> "5 x", x5 -> "x 5")
    for _ in range(2):
        def letter_split(m):
            if m.group(1) and m.group(2):
                return f"{m.group(1)} {m.group(2)}"
            if m.group(3) and m.group(4):
                return f"{m.group(3)} {m.group(4)}"
            return m.group(0)
        text = NUM_LETTER_SPLIT_RE.sub(letter_split, text)

    # Remove commas from numbers (1,000 -> 1000)
    text = COMMA_NUMBER_RE.sub(lambda m: m.group(0).replace(',', ''), text)

    # Dates (12/25/2024 -> "12 dash 25 dash 2024")
    def date_replace(m):
        parts = re.split(r'[./-]', m.group(2))
        return m.group(1) + ' dash '.join(parts) + m.group(3)
    text = DATE_RE.sub(date_replace, text)

    # Phone numbers
    def phone_replace(m):
        digits = re.sub(r'\D', '', m.group(0))
        if len(digits) == 10:
            return f"{' '.join(digits[:3])}, {' '.join(digits[3:6])}, {' '.join(digits[6:])}"
        return m.group(0)
    text = PHONE_NUMBER_RE.sub(phone_replace, text)

    # Time (3:45 -> "3 45", 12:00 -> "12 o'clock")
    def time_replace(m):
        hours, minutes = m.group(1), m.group(2)
        seconds = m.group(3)
        h, min_val = int(hours), int(minutes)

        if not seconds:
            if min_val == 0:
                if h == 0:
                    return '0'
                if h <= 12:
                    return f"{hours} o'clock"
                return f"{hours} minutes"
            if minutes.startswith('0'):
                return f"{hours} oh {minutes[1]}"
            return f"{hours} {minutes}"

        s = int(seconds)
        if h != 0:
            if min_val == 0:
                res = f"{hours} oh oh"
            elif minutes.startswith('0'):
                res = f"{hours} oh {minutes[1]}"
            else:
                res = f"{hours} {minutes}"
        elif min_val != 0:
            if s == 0:
                res = f"{minutes} oh oh"
            elif seconds.startswith('0'):
                res = f"{minutes} oh {seconds[1]}"
            else:
                res = f"{minutes} {seconds}"
        else:
            res = seconds
        if s != 0:
            if seconds.startswith('0'):
                res += f" oh {seconds[1]}"
            else:
                res += f" {seconds}"
        return res
    text = TIME_RE.sub(time_replace, text)

    # Currency - pounds
    text = POUNDS_RE.sub(lambda m: f"{m.group(1).replace(',', '')} pounds", text)

    # Currency - dollars
    def dollar_replace(m):
        amount = m.group(1).replace(',', '')
        parts = amount.split('.')
        dollars = int(parts[0]) if parts[0] else 0
        cents = int(parts[1]) if len(parts) > 1 and parts[1] else 0
        if dollars and cents:
            d_word = 'dollar' if dollars == 1 else 'dollars'
            c_word = 'cent' if cents == 1 else 'cents'
            return f"{dollars} {d_word}, {cents} {c_word}"
        if dollars:
            return f"{dollars} {'dollar' if dollars == 1 else 'dollars'}"
        if cents:
            return f"{cents} {'cent' if cents == 1 else 'cents'}"
        return 'zero dollars'
    text = DOLLARS_RE.sub(dollar_replace, text)

    # Decimal numbers (version numbers like 3.14.159 -> "3 point 1 4 point 1 5 9")
    def decimal_replace(m):
        parts = m.group(0).split('.')
        return ' point '.join(' '.join(p) for p in parts)
    text = DECIMAL_NUMBER_RE.sub(decimal_replace, text)

    # Math operators
    text = MULTIPLY_RE.sub(r'\1 times \2', text)
    text = DIVIDE_RE.sub(r'\1 over \2', text)
    text = ADD_RE.sub(r'\1 plus \2', text)
    text = SUBTRACT_RE.sub(lambda m: (m.group(1) or '') + ' minus ' + m.group(2), text)
    text = FRACTION_RE.sub(r'\1 over \2', text)

    # Ordinals (1st -> "first", 23rd -> "twenty third")
    text = ORDINAL_RE.sub(lambda m: ordinal_to_words(int(m.group(1))), text)

    # Cardinal numbers
    def number_replace(m):
        num = int(m.group(0))
        # Year-like numbers (1000-2999)
        if 1000 < num < 3000:
            if num == 2000:
                return 'two thousand'
            if 2000 < num < 2010:
                return 'two thousand ' + number_to_words(num % 100)
            if num % 100 == 0:
                return number_to_words(num // 100) + ' hundred'
            return number_to_words(num, zero='oh', group=2)
        return number_to_words(num)
    text = NUMBER_RE.sub(number_replace, text)

    return text


# Special character replacements
SPECIAL_CHARACTERS = [
    (r'@', ' at '),
    (r'&', ' and '),
    (r'%', ' percent '),
    (r':', '.'),
    (r';', ','),
    (r'\+', ' plus '),
    (r'\\', ' backslash '),
    (r'~', ' about '),
    (r'(^| )<3', r'\1heart '),
    (r'<=', ' less than or equal to '),
    (r'>=', ' greater than or equal to '),
    (r'<', ' less than '),
    (r'>', ' greater than '),
    (r'=', ' equals '),
    (r'/', ' slash '),
    (r'_', ' '),
]

LINK_HEADER_RE = re.compile(r'https?://', re.IGNORECASE)
DASH_RE = re.compile(r'(.) - (.)')
DOT_RE = re.compile(r'([A-Z])\.([A-Z])', re.IGNORECASE)
PARENTHESES_RE = re.compile(r'[\(\[\{][^\)\]\}]*[\)\]\}](.)?')


def normalize_special(text: str) -> str:
    """Normalize special formatting like URLs, dashes, and parentheses."""
    # URL headers
    text = LINK_HEADER_RE.sub('h t t p s colon slash slash ', text)

    # Spaced dashes (becomes comma)
    text = DASH_RE.sub(r'\1, \2', text)

    # Acronyms with dots (U.S.A -> U dot S dot A)
    text = DOT_RE.sub(r'\1 dot \2', text)

    # Parenthetical content (convert brackets to commas)
    def paren_replace(m):
        result = re.sub(r'[\(\[\{]', ', ', m.group(0))
        result = re.sub(r'[\)\]\}]', ', ', result)
        after = m.group(1)
        if after and after in '$.!?,':
            result = result[:-2] + after
        return result
    text = PARENTHESES_RE.sub(paren_replace, text)

    return text


def expand_special_characters(text: str) -> str:
    """Expand special characters to spoken equivalents."""
    for pattern, replacement in SPECIAL_CHARACTERS:
        text = re.sub(pattern, replacement, text)
    return text


def collapse_whitespace(text: str) -> str:
    """Collapse multiple whitespaces and fix spacing around punctuation."""
    text = re.sub(r'\s+', ' ', text)
    text = re.sub(r' ([.\?!,])', r'\1', text)
    return text


def dedup_punctuation(text: str) -> str:
    """Deduplicate repeated punctuation marks."""
    # Preserve ellipsis
    text = re.sub(r'\.\.\.+', '[ELLIPSIS]', text)
    # Dedup commas
    text = re.sub(r',+', ',', text)
    # Period absorbs surrounding punctuation
    text = re.sub(r'[.,]*\.[.,]*', '.', text)
    # Exclamation absorbs periods/commas
    text = re.sub(r'[.,!]*![.,!]*', '!', text)
    # Question mark absorbs all others
    text = re.sub(r'[.,!?]*\?[.,!?]*', '?', text)
    # Restore ellipsis
    text = re.sub(r'\[ELLIPSIS\]', '...', text)
    return text


def normalize_for_tts(text: str) -> str:
    """
    Full text normalization pipeline for TTS.

    Converts text to a form suitable for text-to-speech synthesis by:
    - Converting unicode to ASCII
    - Expanding abbreviations
    - Converting numbers to words
    - Expanding special characters
    - Normalizing whitespace and punctuation

    Args:
        text: Raw text to normalize

    Returns:
        Normalized text ready for TTS
    """
    if not text:
        return text

    text = convert_to_ascii(text)
    text = expand_abbreviations(text)
    text = normalize_special(text)
    text = normalize_numbers(text)
    text = expand_special_characters(text)
    text = collapse_whitespace(text)
    text = dedup_punctuation(text)
    text = text.strip()

    return text


# ============================================
# Voice Spell Detection
# ============================================

# Type 1: Exact-only — real English words, only match as whole utterance for spell casting
SPELL_EXACT = {
    "transformation": "Transformation",
    "conjuration": "Conjuration",
    "vanishment": "Vanishment",
    "finite": "Finite",
    "disillusionment": "Disillusionment",
    "apparition": "Apparition",
}

# Type 2: Anywhere — HP-specific words, safe to detect/correct anywhere in text
SPELL_ANYWHERE = {
    # Control spells (Yellow)
    "arresto momentum": "ArrestoMomentum",
    "glacius": "Glacius",
    "levioso": "Levioso",

    # Force spells (Purple)
    "accio": "Accio",
    "depulso": "Depulso",
    "descendo": "Descendo",
    "flipendo": "Flipendo",

    # Damage spells (Red)
    "confringo": "Confringo",
    "diffindo": "Diffindo",
    "expelliarmus": "Expelliarmus",
    "incendio": "Incendio",
    "expulso": "Expulso",

    # Utility spells
    "lumos": "Lumos",
    "reparo": "Reparo",
    "wingardium leviosa": "WingardiumLeviosa",
    "evanesco": "Vanishment",
    "alohomora": "Alohomora",
    "aguamenti": "Aguamenti",
    "engorgio": "Engorgio",
    "reducio": "Reducio",
    "silencio": "Silencio",

    # Unforgivable Curses
    "avada kedavra": "AvadaKedavra",
    "crucio": "Crucio",
    "imperio": "Imperius",
    "imperius": "Imperius",

    # Essential spells
    "revelio": "Revelio",
    "protego": "Protego",
    "stupefy": "Stupefy",
    "petrificus totalus": "PetrificusTotalus",
    "petrificus": "PetrificusTotalus",

    # Dark Arts / Advanced
    "incarcerous": "Incarcerous",
    "fiend fyre": "FiendFyre",
    "reducto": "Reducto",
    "expecto patronum": "ExpectoPatronum",
    "bat bogey": "BatBogey",
    "tarantallegra": "Tarantallegra",
    "trip jinx": "TripJinx",

    # DLC spells
    "animagus form": "AnimagusForm",
    "animagus": "AnimagusForm",
    "apparate": "Apparition",

    # Other spells
    "confundo": "Confundo",
    "oppugno": "Oppugno",
    "obliviate": "Obliviate",
    "episkey": "Episkey",

    # Common mispronunciations/alternatives (for casting)
    "stupify": "Stupefy",
    "stupiphy": "Stupefy",
    "expeliarmus": "Expelliarmus",
    "avada cadavra": "AvadaKedavra",
    "wingardium": "WingardiumLeviosa",
    "leviosa": "Levioso",
    "aresto momentum": "ArrestoMomentum",
    "arresto": "ArrestoMomentum",
    "nox": "Lumos",  # Nox cancels Lumos (toggle)
    "bombarda": "Confringo",  # Bombarda is a talent upgrade for Confringo
}

# Combined index for whole-utterance spell detection (casting)
SPELL_INDEX = {**SPELL_EXACT, **SPELL_ANYWHERE}

# ============================================
# Spell Text Corrections (for conversational text)
# ============================================
# Fixes spell names in STT-transcribed text before sending to LLM.
# Only HP-specific words safe to replace anywhere in a sentence.
# key: lowercase text from STT output -> value: correct display spelling
SPELL_TEXT_CORRECTIONS = {
    # --- Correct spellings (proper capitalization) ---
    "arresto momentum": "Arresto Momentum",
    "glacius": "Glacius",
    "levioso": "Levioso",
    "accio": "Accio",
    "depulso": "Depulso",
    "descendo": "Descendo",
    "flipendo": "Flipendo",
    "confringo": "Confringo",
    "diffindo": "Diffindo",
    "expelliarmus": "Expelliarmus",
    "incendio": "Incendio",
    "expulso": "Expulso",
    "lumos": "Lumos",
    "nox": "Nox",
    "reparo": "Reparo",
    "wingardium leviosa": "Wingardium Leviosa",
    "wingardium": "Wingardium",
    "alohomora": "Alohomora",
    "aguamenti": "Aguamenti",
    "evanesco": "Evanesco",
    "engorgio": "Engorgio",
    "reducio": "Reducio",
    "silencio": "Silencio",
    "avada kedavra": "Avada Kedavra",
    "crucio": "Crucio",
    "imperio": "Imperio",
    "imperius": "Imperius",
    "revelio": "Revelio",
    "protego": "Protego",
    "stupefy": "Stupefy",
    "petrificus totalus": "Petrificus Totalus",
    "petrificus": "Petrificus",
    "incarcerous": "Incarcerous",
    "fiend fyre": "Fiend Fyre",
    "reducto": "Reducto",
    "expecto patronum": "Expecto Patronum",
    "bat bogey": "Bat Bogey",
    "tarantallegra": "Tarantallegra",
    "trip jinx": "Trip Jinx",
    "animagus": "Animagus",
    "apparate": "Apparate",
    "confundo": "Confundo",
    "oppugno": "Oppugno",
    "obliviate": "Obliviate",
    "episkey": "Episkey",
    "bombarda": "Bombarda",
    "leviosa": "Leviosa",
    "arresto": "Arresto",

    # --- Common STT mistranscriptions ---
    # Lumos
    "lumis": "Lumos",
    "lumas": "Lumos",
    "lumus": "Lumos",
    "lou mouse": "Lumos",
    # Stupefy
    "stupify": "Stupefy",
    "stupiphy": "Stupefy",
    # Expelliarmus
    "expeliarmus": "Expelliarmus",
    # Avada Kedavra
    "avada cadavra": "Avada Kedavra",
    # Arresto Momentum
    "aresto momentum": "Arresto Momentum",
}

# Pre-compiled correction patterns (lazy init, sorted longest-first)
_SPELL_CORRECTION_PATTERNS = None


def _compile_spell_corrections():
    """Compile regex patterns for spell text corrections (one-time)."""
    global _SPELL_CORRECTION_PATTERNS
    if _SPELL_CORRECTION_PATTERNS is not None:
        return
    # Sort by key length descending so multi-word matches take priority
    sorted_entries = sorted(SPELL_TEXT_CORRECTIONS.items(), key=lambda x: len(x[0]), reverse=True)
    _SPELL_CORRECTION_PATTERNS = [
        (re.compile(r'\b' + re.escape(key) + r'\b', re.IGNORECASE), value)
        for key, value in sorted_entries
    ]


def correct_spell_names_in_text(text):
    """
    Fix spell names in conversational text (STT mistranscriptions and capitalization).
    Only replaces HP-specific words safe to correct anywhere in a sentence.
    """
    if not text:
        return text
    _compile_spell_corrections()
    for pattern, replacement in _SPELL_CORRECTION_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def normalize_spell_text(text):
    """Normalize text for spell matching (lowercase, strip punctuation, trim whitespace)."""
    if not text:
        return ""
    # Lowercase, strip punctuation, normalize whitespace
    text = text.lower()
    text = re.sub(r'[^\w\s]', '', text)  # Remove punctuation
    text = ' '.join(text.split())  # Normalize whitespace
    return text


def detect_spell_in_text(text):
    """
    Detect if text IS a spell command (exact match only).
    The normalized text must match a spell name exactly - no substring matching.

    Returns:
        tuple: (internal_spell_name, matched_text) if found, (None, None) otherwise
    """
    if not text:
        return None, None

    normalized = normalize_spell_text(text)

    if normalized in SPELL_INDEX:
        return SPELL_INDEX[normalized], normalized

    return None, None
