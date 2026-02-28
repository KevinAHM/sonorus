# -*- coding: utf-8 -*-
"""
LLM orchestration utilities for Sonorus.
Handles LLM calls, logging, and response parsing.
"""

import re

from .settings import load_settings
from .llm_logging import log_llm, LOGS_DIR

# Import llm module from parent directory
import llm

LLM_ERROR_FALLBACK = "I seem to be having trouble thinking..."

def call_llm(prompt, user_input):
    """Call LLM via shared llm module (blocking, full response)"""
    settings = load_settings()
    conv_settings = settings.get('conversation', {})
    model = conv_settings.get('chat_model', 'google/gemini-3-flash-preview:nitro')
    max_tokens = conv_settings.get('max_tokens', 150)
    temperature = conv_settings.get('temperature', 1.0)

    print(f"[LLM] Model: {model}, Temp: {temperature}, MaxTokens: {max_tokens}")

    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": user_input}
    ]

    # llm.chat() handles logging internally
    result = llm.chat(messages, model=model, temperature=temperature, max_tokens=max_tokens, context="chat")

    if result:
        print(f"[LLM] Success!")
        return result
    else:
        return LLM_ERROR_FALLBACK


def call_llm_stream(prompt, user_input):
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
    model = conv_settings.get('chat_model', 'google/gemini-3-flash-preview:nitro')
    max_tokens = conv_settings.get('max_tokens', 150)
    temperature = conv_settings.get('temperature', 1.0)

    print(f"[LLM] Model (streaming): {model}, Temp: {temperature}, MaxTokens: {max_tokens}")

    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": user_input}
    ]

    accumulated = []
    has_content = False

    for chunk in llm.chat_stream(messages, model=model, temperature=temperature,
                                  max_tokens=max_tokens, context="chat"):
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


def stream_sentences(prompt, user_input):
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

    for chunk, accumulated in call_llm_stream(prompt, user_input):
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
    # Find all [Action: X] tags
    matches = re.findall(r'\[Action:\s*([A-Za-z]+(?:\s+[A-Za-z0-9]+)*)\]', text, re.IGNORECASE)
    return matches if matches else []


def strip_action_tag(text):
    """Remove all action tags from response text (including commitment actions)."""
    text = re.sub(r'\s*\[Action:\s*[A-Za-z]+(?:\s+[A-Za-z0-9]+)*\]', '', text)
    text = strip_commitment_action_tags(text)
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
