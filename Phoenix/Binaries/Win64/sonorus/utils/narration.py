"""Narration parsing utilities for inline *narration* in LLM output.

Format convention:
  "Hello there." *He glanced nervously at the door.* "What brings you here?"

Rules:
  - "..." → dialogue (character voice, with lipsync)
  - *multi-word standalone* → narration (narrator voice, no lipsync)
  - *word* inline within quotes → emphasis (same voice, italic subtitle)
"""

import re
from dataclasses import dataclass
from typing import List

# Matches *...* blocks (non-greedy, content can span multiple words)
_ASTERISK_BLOCK = re.compile(r'\*([^*]+)\*')


@dataclass
class Segment:
    text: str
    is_narration: bool


def parse_segments(text: str) -> List[Segment]:
    """Parse LLM output into dialogue and narration segments.

    Rules:
    - *multi-word blocks* = narration (narrator voice, no lipsync)
    - *single word* = emphasis (stays in dialogue, same voice)
    - Everything else = dialogue

    Note: We intentionally do NOT check if *blocks* are "inside quotes"
    because sentence splitting can produce text with orphaned/unbalanced
    quotes (e.g., a closing " from previous sentence), which causes
    naive quote-pairing to engulf narration blocks in false "quoted regions".
    The multi-word check alone is sufficient: our LLM prompt instructs
    *word* for emphasis and *multi-word* for narration.
    """
    if not text or not text.strip():
        return []

    text = text.strip()

    # Find all asterisk blocks that qualify as narration (multi-word)
    narration_spans = []
    for m in _ASTERISK_BLOCK.finditer(text):
        content = m.group(1).strip()
        if ' ' in content:  # Multi-word = narration
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
    # Remove leading/trailing quotes
    if text.startswith('"'):
        text = text[1:]
    if text.endswith('"'):
        text = text[:-1]
    return text.strip()


