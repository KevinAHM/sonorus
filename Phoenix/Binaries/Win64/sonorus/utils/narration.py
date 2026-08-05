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
from typing import List, Optional, Tuple

# Matches *...* blocks (non-greedy, content can span multiple words)
_ASTERISK_BLOCK = re.compile(r"\*([^*]+)\*")
# Square-bracket blocks are metadata or user directions, never narration.
_SQUARE_BLOCK = re.compile(r"\[([^\[\]]+)\]")
_DIALOGUE_QUOTE_CHARS = frozenset({'"', "“", "”"})


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
    narration_spans.sort(key=lambda span: span[0])

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


def _append_segment(segments: List[Segment], text: str, is_narration: bool) -> None:
    """Append a non-empty segment, merging adjacent segments of the same kind."""
    clean = (text or "").strip()
    if not clean:
        return
    if segments and segments[-1].is_narration == is_narration:
        segments[-1].text = f"{segments[-1].text} {clean}".strip()
    else:
        segments.append(Segment(text=clean, is_narration=is_narration))


def _is_square_bracket_metadata(text: str) -> bool:
    """Return True when text contains only square-bracket blocks and whitespace."""
    stripped = (text or "").strip()
    if not stripped:
        return False

    matches = list(_SQUARE_BLOCK.finditer(stripped))
    if not matches or _SQUARE_BLOCK.sub("", stripped).strip():
        return False
    return True


def _has_dialogue_quote(text: str) -> bool:
    return any(char in _DIALOGUE_QUOTE_CHARS for char in (text or ""))


def _parse_quote_aware(
    text: str,
    *,
    inside_quote: bool = False,
    narration_min_words: int = 3,
) -> Tuple[List[Segment], bool]:
    """Parse quoted dialogue while repairing unasterisked prose as narration."""
    segments: List[Segment] = []
    buffer: List[str] = []

    def flush() -> None:
        if not buffer:
            return
        buffered_text = "".join(buffer)
        is_narration = not inside_quote
        # Square-bracket blocks are metadata or user directions. Keep them
        # attached to dialogue rather than sending them to the narrator.
        if is_narration and _is_square_bracket_metadata(buffered_text):
            is_narration = False
        _append_segment(segments, buffered_text, is_narration=is_narration)
        buffer.clear()

    i = 0
    while i < len(text):
        char = text[i]
        if char == '"':
            flush()
            inside_quote = not inside_quote
            i += 1
            continue
        if char == "“":
            flush()
            inside_quote = True
            i += 1
            continue
        if char == "”":
            flush()
            inside_quote = False
            i += 1
            continue

        if char == "*":
            close = text.find("*", i + 1)
            if close != -1:
                flush()
                content = text[i + 1:close].strip()
                if _is_narration_block(
                    content,
                    narration_min_words=narration_min_words,
                ):
                    _append_segment(segments, content, is_narration=True)
                else:
                    # Preserve short emphasis as dialogue rather than narrator prose.
                    _append_segment(segments, f"*{content}*", is_narration=False)
                i = close + 1
                continue

        buffer.append(char)
        i += 1

    flush()
    return segments, inside_quote


class StreamingNarrationParser:
    """Stateful quote-aware parser for sentence-at-a-time LLM output."""

    def __init__(self, narration_min_words: int = 3):
        self.narration_min_words = narration_min_words
        self.inside_quote = False
        self.saw_quote = False
        self.pending_leading: List[str] = []

    def parse(self, text: str) -> List[Segment]:
        clean = (text or "").strip()
        if not clean:
            return []

        # Before the model demonstrates quoted-dialogue format, hold plain
        # unmarked prose instead of immediately committing it to the NPC
        # voice. If a quote arrives later, the held prose can be repaired as
        # opening narration. Explicit *narration* is already unambiguous.
        if not self.saw_quote and not _has_dialogue_quote(clean):
            explicit_segments = parse_segments(
                clean,
                narration_min_words=self.narration_min_words,
            )
            if any(segment.is_narration for segment in explicit_segments):
                return explicit_segments
            self.pending_leading.append(clean)
            return []

        leading_segments: List[Segment] = []
        if not self.saw_quote and self.pending_leading:
            pending_text = " ".join(self.pending_leading).strip()
            self.pending_leading.clear()
            if _is_square_bracket_metadata(pending_text):
                clean = f"{pending_text} {clean}".strip()
            else:
                _append_segment(
                    leading_segments,
                    pending_text,
                    is_narration=True,
                )

        self.saw_quote = True
        segments, self.inside_quote = _parse_quote_aware(
            clean,
            inside_quote=self.inside_quote,
            narration_min_words=self.narration_min_words,
        )
        return leading_segments + segments

    def finish(self) -> List[Segment]:
        """Flush held opening prose when a response ends without any quotes."""
        if not self.pending_leading:
            return []

        pending_text = " ".join(self.pending_leading).strip()
        self.pending_leading.clear()
        if not pending_text:
            return []
        return [Segment(text=_strip_quotes(pending_text), is_narration=False)]


def normalize_narration_response(
    text: str,
    max_narration_blocks: Optional[int] = None,
) -> str:
    """Repair narration formatting, optionally limiting narration blocks."""
    if not text or not text.strip():
        return ""

    if _has_dialogue_quote(text):
        segments, _ = _parse_quote_aware(text)
    else:
        segments = parse_segments(text)

    parts: List[str] = []
    narration_blocks = 0
    for segment in segments:
        clean = (segment.text or "").strip()
        if not clean:
            continue
        if segment.is_narration:
            if (
                max_narration_blocks is not None
                and narration_blocks >= max(0, int(max_narration_blocks))
            ):
                continue
            narration_blocks += 1
            parts.append(f"*{clean}*")
        else:
            parts.append(f'"{clean}"')
    return " ".join(parts)


def strip_narration_segments(text: str, narration_min_words: int = 3) -> str:
    """Remove narration blocks while retaining dialogue and short emphasis."""
    return " ".join(
        segment.text
        for segment in parse_segments(text, narration_min_words=narration_min_words)
        if not segment.is_narration
    ).strip()


def _strip_quotes(text: str) -> str:
    """Strip outer double quotes from dialogue text, preserving inner emphasis."""
    text = text.strip()
    if text.startswith(('"', "“")):
        text = text[1:]
    if text.endswith(('"', "”")):
        text = text[:-1]
    return text.strip()
