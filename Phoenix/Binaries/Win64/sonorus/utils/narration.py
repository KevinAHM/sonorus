"""Narration parsing utilities for inline *narration* in text output.

Format convention:
  "Hello there." *He glanced nervously at the door.* "What brings you here?"

Rules:
  - "..." -> dialogue (character voice, with lipsync)
  - *3+ word / punctuated standalone* -> narration (narrator voice, no lipsync)
  - *word* or short emphasis phrase -> emphasis (same voice)
"""

import re
from dataclasses import dataclass
from typing import List

# Matches *...* blocks (non-greedy, content can span multiple words)
_ASTERISK_BLOCK = re.compile(r"\*([^*]+)\*")


@dataclass
class Segment:
    text: str
    is_narration: bool


def _is_narration_block(content: str, narration_min_words: int = 3) -> bool:
    """Return True when an asterisk block looks like narration, not emphasis."""
    if not content:
        return False

    word_count = len(re.findall(r"\S+", content))
    narration_min_words = max(1, int(narration_min_words or 1))
    if word_count >= narration_min_words:
        return True

    return bool(re.search(r"[.!?,]", content))


def parse_segments(text: str, narration_min_words: int = 3) -> List[Segment]:
    """Parse text into dialogue and narration segments.

    Rules:
    - *narration_min_words+ word or punctuated blocks* = narration (narrator voice, no lipsync)
    - *single word or short phrase* = emphasis (stays in dialogue, same voice)
    - Everything else = dialogue

    Note: We intentionally do NOT check if *blocks* are "inside quotes"
    because sentence splitting can produce text with orphaned/unbalanced
    quotes (e.g., a closing " from previous sentence), which causes
    naive quote-pairing to engulf narration blocks in false "quoted regions".
    Instead we use a conservative heuristic that works for both LLM output
    and raw player text: short italic phrases stay emphasis, while longer or
    punctuated blocks are treated as narration. `narration_min_words` lets
    callers use a looser rule for player text without changing NPC behavior.
    """
    if not text or not text.strip():
        return []

    text = text.strip()

    # Find all asterisk blocks that qualify as narration.
    narration_spans = []
    for m in _ASTERISK_BLOCK.finditer(text):
        content = m.group(1).strip()
        if _is_narration_block(content, narration_min_words=narration_min_words):
            narration_spans.append((m.start(), m.end(), content))

    if not narration_spans:
        # No narration found - everything is dialogue
        clean = _strip_quotes(text)
        if clean:
            return [Segment(text=clean, is_narration=False)]
        return []

    # Build segments by splitting text at narration boundaries
    segments = []
    pos = 0

    for ns, ne, narr_text in narration_spans:
        # Everything before this narration block is dialogue
        if ns > pos:
            dialogue_chunk = text[pos:ns].strip()
            if dialogue_chunk:
                clean = _strip_quotes(dialogue_chunk)
                if clean:
                    segments.append(Segment(text=clean, is_narration=False))

        # The narration block itself
        if narr_text:
            segments.append(Segment(text=narr_text, is_narration=True))

        pos = ne

    # Remaining text after last narration block is dialogue
    if pos < len(text):
        dialogue_chunk = text[pos:].strip()
        if dialogue_chunk:
            clean = _strip_quotes(dialogue_chunk)
            if clean:
                segments.append(Segment(text=clean, is_narration=False))

    return segments


def _strip_quotes(text: str) -> str:
    """Strip outer double quotes from dialogue text, preserving inner emphasis."""
    text = text.strip()
    if text.startswith('"'):
        text = text[1:]
    if text.endswith('"'):
        text = text[:-1]
    return text.strip()
