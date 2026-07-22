# -*- coding: utf-8 -*-
"""
LLM orchestration utilities for Sonorus.
Handles LLM calls, logging, and response parsing.
"""

import re

from .settings import GEMINI_CHAT_DEFAULT_OR, load_settings
from .llm_logging import log_llm, LOGS_DIR

# Import llm module from parent directory
import llm

LLM_ERROR_FALLBACK = "I seem to be having trouble thinking..."


def resolve_chat_model(conv_settings, speaker_id=None):
    """Resolve the LLM model, checking per-NPC overrides first."""
    if speaker_id:
        override = conv_settings.get('npc_llm_model_overrides', {}).get(speaker_id, '')
        if override:
            return override
    return conv_settings.get('chat_model', GEMINI_CHAT_DEFAULT_OR)


def call_llm(prompt, user_input, speaker_id=None, kv_cache_prefix=None, kv_cache_context=None):
    """Call LLM via shared llm module (blocking, full response)"""
    settings = load_settings()
    conv_settings = settings.get('conversation', {})
    model = resolve_chat_model(conv_settings, speaker_id)
    max_tokens = conv_settings.get('max_tokens', 150)
    temperature = conv_settings.get('temperature', 1.0)

    print(f"[LLM] Model: {model}, Temp: {temperature}, MaxTokens: {max_tokens}")

    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": user_input}
    ]

    # llm.chat() handles logging internally
    result = llm.chat(
        messages,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        context="chat",
        kv_cache_prefix=kv_cache_prefix,
        kv_cache_context=kv_cache_context,
    )

    if result:
        print(f"[LLM] Success!")
        return result
    else:
        return LLM_ERROR_FALLBACK


def call_llm_stream(prompt, user_input, speaker_id=None, kv_cache_prefix=None, kv_cache_context=None):
    """
    Stream LLM response, yielding text chunks as they arrive.

    Returns a generator that yields (chunk, full_text_so_far) tuples.
    The full_text_so_far accumulates all chunks for post-processing.

    Usage:
        full_text = ""
        for chunk, accumulated in call_llm_stream(prompt, user_input):
            full_text = accumulated
            process_chunk(chunk)
        # full_text now has the complete response
    """
    settings = load_settings()
    conv_settings = settings.get('conversation', {})
    model = resolve_chat_model(conv_settings, speaker_id)
    max_tokens = conv_settings.get('max_tokens', 150)
    temperature = conv_settings.get('temperature', 1.0)

    print(f"[LLM] Model (streaming): {model}, Temp: {temperature}, MaxTokens: {max_tokens}")

    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": user_input}
    ]

    accumulated = []
    has_content = False

    for chunk in llm.chat_stream(
        messages,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        context="chat",
        kv_cache_prefix=kv_cache_prefix,
        kv_cache_context=kv_cache_context,
    ):
        if chunk:
            has_content = True
            accumulated.append(chunk)
            yield chunk, "".join(accumulated)

    if not has_content:
        yield LLM_ERROR_FALLBACK, LLM_ERROR_FALLBACK


def _normalize_quotes(text):
    """Normalize curly/smart quotes to straight ASCII quotes."""
    return (text
            .replace('\u201c', '"').replace('\u201d', '"')   # " " → "
            .replace('\u2018', "'").replace('\u2019', "'"))   # ' ' → '


_TIME_FRAGMENT = r'\d{1,2}:\d{2}\s*[APap]\.?[Mm]\.?'
_DATE_FRAGMENT = (
    r'\d{4}/\d{1,2}/\d{1,2}|'
    r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+'
    r'(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+'
    r'\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}|'
    r'(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+'
    r'\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}'
)

# Prefix-only cleanup for transcript/history scaffolding that models sometimes
# imitate from the prompt. Keep this conservative around bracket tags so valid
# emotes like [happy] and vocal tags like [laugh] survive.
_ROLE_HEADER_RE = re.compile(
    r'^\s*(?:---\s*)?(?:assistant|user)\s*(?:---|:)\s*',
    re.IGNORECASE,
)
_DATE_DIVIDER_RE = re.compile(
    rf'^\s*---\s*(?=[^\n]*?(?:{_TIME_FRAGMENT}|{_DATE_FRAGMENT}))[^\n]*?---\s*',
    re.IGNORECASE,
)
_BRACKETED_HISTORY_PREFIX_RE = re.compile(
    rf'^\s*\[(?=[^\]\n]*?(?:{_TIME_FRAGMENT}|{_DATE_FRAGMENT}))[^\]\n]{{1,160}}\]\s*',
    re.IGNORECASE,
)
_SPEAKER_PREFIX_RE = re.compile(
    r'^\s*'
    r'(?:'
    r'[A-Z][A-Za-z0-9_\'.-]*(?:\s+[A-Z][A-Za-z0-9_\'.-]*){1,5}'
    r'|[A-Z][A-Za-z0-9_\'.-]*\s*\([^)]*\)'
    r')'
    r'(?:\s*\([^)]*\))?\s*:\s*'
)
_TARGETED_SPEAKER_PREFIX_RE = re.compile(
    r'^\s*'
    r'[A-Z][A-Za-z0-9_\'.-]*(?:\s+[A-Z][A-Za-z0-9_\'.-]*){0,5}'
    r'\s*\([^)]*\)\s*:\s*'
)
_NPC_LABEL_RE = re.compile(
    r'^\s*\['
    r'[A-Z][A-Za-z0-9_\'.-]*(?:\s+[A-Z][A-Za-z0-9_\'.-]*){0,5}'
    r'(?:\s*\([^)]*\))?\s*'
    r'\]\s*'
)


def strip_response_metadata(text):
    """Strip imitated history-format metadata prefix from LLM output.

    Handles:
      [7:11 PM] Speaker (to Target): dialogue  ->  dialogue
      [7:11 PM] Speaker: dialogue               ->  dialogue
      [7:11 PM] dialogue                        ->  dialogue
      [Wednesday, January 14th, 1891 - 7:11 PM] Speaker: dialogue -> dialogue
      --- Wednesday, January 14th, 1891 - 7:11 PM --- dialogue    -> dialogue
      --- ASSISTANT --- [7:11 PM] Speaker: dialogue               -> dialogue
      [Speaker Name (to Target)] dialogue       ->  dialogue
      [Speaker Name] dialogue                   ->  dialogue
      dialogue (no metadata)                    ->  dialogue (unchanged)
    """
    if not text:
        return text

    result = text.strip()
    for _ in range(6):
        before = result
        removed_scaffold = False

        stripped_role = _ROLE_HEADER_RE.sub('', result, count=1)
        if stripped_role != result:
            result = stripped_role.lstrip()
            removed_scaffold = True

        stripped_divider = _DATE_DIVIDER_RE.sub('', result, count=1)
        if stripped_divider != result:
            result = stripped_divider.lstrip()
            removed_scaffold = True

        stripped_stamp = _BRACKETED_HISTORY_PREFIX_RE.sub('', result, count=1)
        if stripped_stamp != result:
            result = _SPEAKER_PREFIX_RE.sub('', stripped_stamp.lstrip(), count=1).lstrip()
            continue

        # Handle bare transcript labels after a role header, e.g.
        # "Gladwin Moon (to Adri Valter): ..."
        if removed_scaffold:
            result = _SPEAKER_PREFIX_RE.sub('', result, count=1).lstrip()
        else:
            result = _TARGETED_SPEAKER_PREFIX_RE.sub('', result, count=1).lstrip()

        # Handle bracketed speaker labels from merged assistant messages.
        result = _NPC_LABEL_RE.sub('', result, count=1).lstrip()

        if result == before:
            break

    return result.strip()


def call_llm_stream_messages(messages, speaker_id=None, kv_cache_prefix=None, kv_cache_context=None):
    """Stream LLM response from a pre-built messages array.

    Same as call_llm_stream() but accepts messages directly instead of
    (prompt, user_input). Used by the chat path for multi-message caching.

    Yields (chunk, full_text_so_far) tuples.
    """
    settings = load_settings()
    conv_settings = settings.get('conversation', {})
    model = resolve_chat_model(conv_settings, speaker_id)
    max_tokens = conv_settings.get('max_tokens', 150)
    temperature = conv_settings.get('temperature', 1.0)

    print(f"[LLM] Model (streaming, messages): {model}, Temp: {temperature}, MaxTokens: {max_tokens}")

    accumulated = []
    has_content = False

    for chunk in llm.chat_stream(
        messages,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        context="chat",
        kv_cache_prefix=kv_cache_prefix,
        kv_cache_context=kv_cache_context,
    ):
        if chunk:
            has_content = True
            accumulated.append(chunk)
            yield chunk, "".join(accumulated)

    if not has_content:
        yield LLM_ERROR_FALLBACK, LLM_ERROR_FALLBACK


def call_llm_messages(messages, speaker_id=None, kv_cache_prefix=None, kv_cache_context=None):
    """Call LLM with a pre-built messages array (blocking, full response).

    Same as call_llm() but accepts messages directly instead of
    (prompt, user_input). Used by the non-streaming chat fallback path.
    """
    settings = load_settings()
    conv_settings = settings.get('conversation', {})
    model = resolve_chat_model(conv_settings, speaker_id)
    max_tokens = conv_settings.get('max_tokens', 150)
    temperature = conv_settings.get('temperature', 1.0)

    print(f"[LLM] Model (messages): {model}, Temp: {temperature}, MaxTokens: {max_tokens}")

    result = llm.chat(
        messages,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        context="chat",
        kv_cache_prefix=kv_cache_prefix,
        kv_cache_context=kv_cache_context,
    )

    if result:
        print(f"[LLM] Success!")
        return result
    else:
        return LLM_ERROR_FALLBACK


def stream_sentences_messages(messages, speaker_id=None, kv_cache_prefix=None, kv_cache_context=None):
    """Stream LLM response from a pre-built messages array and yield complete sentences.

    Same as stream_sentences() but accepts messages directly.

    Yields (sentence, full_text_so_far, is_final) tuples.
    """
    buffer = ""
    full_text = ""

    for chunk, accumulated in call_llm_stream_messages(
        messages,
        speaker_id=speaker_id,
        kv_cache_prefix=kv_cache_prefix,
        kv_cache_context=kv_cache_context,
    ):
        full_text = _normalize_quotes(accumulated)
        buffer += _normalize_quotes(chunk)

        while True:
            sentence, remainder = _try_split_sentence(buffer)
            if sentence is None:
                break
            buffer = remainder
            yield sentence, full_text, False

    if buffer.strip():
        yield buffer.strip(), full_text, True


def stream_sentences(prompt, user_input, speaker_id=None, kv_cache_prefix=None, kv_cache_context=None):
    """
    Stream LLM response and yield complete sentences as they form.

    Accumulates LLM tokens and yields each sentence as soon as a sentence
    boundary is detected. The final partial sentence (if any) is yielded
    when the stream ends.

    Handles abbreviations (Mr., Mrs., Dr., Prof., O.W.L.s etc.) without
    false sentence breaks.

    Yields:
        (sentence, full_text_so_far, is_final) tuples where:
        - sentence: A complete sentence string
        - full_text_so_far: All text accumulated so far
        - is_final: True for the last sentence
    """
    buffer = ""
    full_text = ""

    for chunk, accumulated in call_llm_stream(
        prompt,
        user_input,
        speaker_id=speaker_id,
        kv_cache_prefix=kv_cache_prefix,
        kv_cache_context=kv_cache_context,
    ):
        # Normalize curly/smart quotes to straight quotes so all downstream
        # parsing (sentence splitting, narration detection) only needs ASCII.
        full_text = _normalize_quotes(accumulated)
        buffer += _normalize_quotes(chunk)

        # Try to extract complete sentences from buffer
        while True:
            sentence, remainder = _try_split_sentence(buffer)
            if sentence is None:
                break
            buffer = remainder
            yield sentence, full_text, False

    # Yield remaining buffer as final sentence
    if buffer.strip():
        yield buffer.strip(), full_text, True


# Common abbreviations that end with "." but are NOT sentence endings
_STREAM_ABBREVIATIONS = {
    'mr', 'mrs', 'ms', 'dr', 'prof', 'sr', 'jr', 'st', 'ave', 'vs',
    'gen', 'col', 'sgt', 'lt', 'capt', 'maj', 'rev', 'hon', 'esq',
    'ltd', 'co', 'inc', 'corp', 'dept', 'ft', 'mt', 'no', 'etc', 'al',
}


def _try_split_sentence(buffer):
    """
    Try to extract one complete sentence from the front of buffer.

    Returns (sentence, remainder) if found, (None, None) if no complete
    sentence boundary is detected yet.

    Avoids splitting on abbreviations like "Mr." or acronyms like "O.W.L."
    """
    # Look for sentence-ending punctuation (optionally followed by a closing
    # quote) then whitespace, or CJK sentence-ending punctuation.
    # The optional ["'] handles dialogue endings like: Graphorn." *He...
    # Curly quotes are already normalized to straight by _normalize_quotes().
    sentence_end_re = re.compile(r"""[.!?]["']?\s+|[。！？]""")

    pos = 0
    while True:
        match = sentence_end_re.search(buffer, pos)
        if not match:
            return None, None

        punct_pos = match.start()
        punct_char = buffer[punct_pos]

        # Sentence includes punctuation + optional closing quote (non-whitespace
        # part of match). E.g. for '." ' → includes '."', for '. ' → includes '.'
        matched_text = match.group()
        sentence_end = match.start() + len(matched_text.rstrip())

        # CJK punctuation — always a sentence boundary
        if punct_char in ('。', '！', '？'):
            sentence = buffer[:sentence_end].strip()
            remainder = buffer[match.end():]
            if sentence:
                return sentence, remainder
            pos = match.end()
            continue

        # For '!' and '?', always treat as sentence boundary
        if punct_char in ('!', '?'):
            sentence = buffer[:sentence_end].strip()
            remainder = buffer[match.end():]
            if sentence:
                return sentence, remainder
            pos = match.end()
            continue

        # For '.', check if it's an abbreviation
        # Extract the word before the period
        before = buffer[:punct_pos]
        word_match = re.search(r'(\w+)$', before)
        if word_match:
            word = word_match.group(1).lower()
            if word in _STREAM_ABBREVIATIONS:
                # This is "Mr. Smith" etc — skip this match
                pos = match.end()
                continue

            # Check for acronym pattern: single letter before period,
            # and more single-letter-dot sequences before that
            # e.g., "O.W.L." or "N.E.W.T."
            if len(word) == 1:
                # Check if preceded by more X. patterns
                prefix = buffer[:word_match.start()]
                if re.search(r'(?:[A-Za-z]\.)+$', prefix):
                    pos = match.end()
                    continue

        # Looks like a real sentence boundary
        sentence = buffer[:sentence_end].strip()
        remainder = buffer[match.end():]
        if sentence:
            return sentence, remainder
        pos = match.end()


def parse_action(text):
    """Parse single action from LLM response (legacy, returns first action only).

    For multiple action support, use parse_actions() instead.
    """
    actions = parse_actions(text)
    return actions[0] if actions else "None"


def parse_actions(text):
    """Parse all actions from LLM response.

    Supports simple actions like [Action: JoinAsCompanion] and parameterized actions
    like [Action: AwardPoints Gryffindor 10].

    Returns list of action strings, empty list if none found.
    """
    canonical_action_names = {
        "joinascompanion": "JoinAsCompanion",
        "leavecompanion": "LeaveCompanion",
        "follow": "Follow",
        "stopfollowing": "StopFollowing",
        "endconversation": "EndConversation",
        "awardpoints": "AwardPoints",
        "deductpoints": "DeductPoints",
    }

    # Find all [Action: X] tags
    matches = re.findall(r'\[Action:\s*([A-Za-z]+(?:\s+[A-Za-z0-9]+)*)\]', text, re.IGNORECASE)
    if not matches:
        return []

    normalized = []
    for match in matches:
        parts = match.split()
        if not parts:
            continue
        canonical_verb = canonical_action_names.get(parts[0].lower(), parts[0])
        normalized.append(" ".join([canonical_verb] + parts[1:]))
    return normalized


def strip_action_tag(text):
    """Remove all action tags from response text (including commitment actions)."""
    text = re.sub(r'\s*\[Action:\s*[A-Za-z]+(?:\s+[A-Za-z0-9]+)*\]', '', text)
    text = strip_commitment_action_tags(text)
    # Catch-all: strip any remaining [Action: ...] tags the LLM hallucinated with freeform text
    text = re.sub(r'\s*\[Action:[^\]]*\]', '', text)
    return text.strip()


# Commitment action regexes
_MEET_ACTION_RE = re.compile(
    r'\[Action:\s*[Mm]eet\s+"([^"]+)"\s+at\s+"([^"]+)"\s+on\s+"([^"]+)"\]',
    re.IGNORECASE,
)
# Fallback: unquoted format - datetime pattern anchors the end so lazy matches work
_MEET_ACTION_UNQUOTED_RE = re.compile(
    r'\[Action:\s*[Mm]eet\s+(.+?)\s+at\s+(.+?)\s+on\s+'
    r'(\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}\s*[AaPp][Mm])\s*\]',
    re.IGNORECASE,
)
_CANCEL_ACTION_RE = re.compile(
    r'\[Action:\s*CancelCommitment\s+(\S+)\]',
    re.IGNORECASE,
)


def extract_json(text):
    """Robustly extract a JSON array or object from LLM output.

    Uses json_repair to handle malformed JSON (unescaped quotes, trailing commas,
    single quotes, etc.).

    Handles: ```json fences, "json:" prefixes, leading prose, trailing text.
    Strategy: find the first '[' or '{' and the last matching ']' or '}',
    then repair-parse that substring.  Returns the parsed Python object or None.
    """
    if not text:
        return None

    # Strip common wrappers: ```json ... ```, "json:", leading prose
    s = text.strip()
    # Remove markdown code fences
    s = re.sub(r'^```(?:json)?\s*', '', s, flags=re.IGNORECASE)
    s = re.sub(r'\s*```\s*$', '', s)
    s = s.strip()
    # Remove leading "json:" or "JSON:" label
    s = re.sub(r'^json\s*:\s*', '', s, flags=re.IGNORECASE).strip()

    # Find first [ or { and last matching ] or }
    bracket_map = {'[': ']', '{': '}'}
    first_idx = -1
    open_char = None
    for i, c in enumerate(s):
        if c in bracket_map:
            first_idx = i
            open_char = c
            break

    if first_idx < 0:
        return None

    close_char = bracket_map[open_char]
    last_idx = s.rfind(close_char)
    if last_idx <= first_idx:
        return None

    import json_repair
    try:
        return json_repair.loads(s[first_idx:last_idx + 1])
    except Exception:
        return None


def parse_commitment_actions(text):
    """Parse commitment actions from LLM response.

    Returns list of dicts:
      - {"type": "meet", "target": str, "location": str, "datetime": str}
      - {"type": "cancel", "commitment_id": str}
    """
    actions = []

    # Try quoted format first, fall back to unquoted if no matches
    meet_matches = list(_MEET_ACTION_RE.finditer(text))
    if not meet_matches:
        meet_matches = list(_MEET_ACTION_UNQUOTED_RE.finditer(text))

    for match in meet_matches:
        actions.append({
            "type": "meet",
            "target": match.group(1).strip(),
            "location": match.group(2).strip(),
            "datetime": match.group(3).strip(),
        })

    for match in _CANCEL_ACTION_RE.finditer(text):
        actions.append({
            "type": "cancel",
            "commitment_id": match.group(1),
        })

    return actions


def strip_commitment_action_tags(text):
    """Remove commitment action tags from response text."""
    text = _MEET_ACTION_RE.sub('', text)
    text = _MEET_ACTION_UNQUOTED_RE.sub('', text)
    text = _CANCEL_ACTION_RE.sub('', text)
    return text
