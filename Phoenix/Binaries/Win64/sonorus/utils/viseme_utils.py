"""Helpers for narration-safe viseme timelines."""

from typing import Dict, List, Optional, Tuple


VISEME_CHANNEL_KEYS = (
    "jaw",
    "smile",
    "funnel",
    "press",
    "lip_up",
    "ee",
    "o_shape",
    "shh",
)


def get_boundary_time(
    boundary: Dict,
    idx: int,
    sample_rate: int = 48000,
    channels: int = 1,
) -> Optional[float]:
    """Return a reliable boundary start time, or None if not confirmed yet."""
    channels = max(1, int(channels or 1))
    bytes_per_second = max(1, int(sample_rate or 48000)) * 2 * channels

    start_bytes = boundary.get("start_bytes")
    if start_bytes is not None and start_bytes >= 0:
        return float(start_bytes) / bytes_per_second

    start_time = boundary.get("start_time", 0.0)
    if idx == 0 or boundary.get("start_time_confirmed"):
        return float(start_time)

    return None


def build_narration_ranges(
    sentence_boundaries: List[Dict],
    audio_duration: float,
    sample_rate: int = 48000,
    channels: int = 1,
) -> List[Tuple[float, float]]:
    """Build narration time ranges from sentence boundaries."""
    if not sentence_boundaries:
        return []

    ranges: List[Tuple[float, float]] = []
    for idx, boundary in enumerate(sentence_boundaries):
        if not boundary.get("is_narration"):
            continue

        start = get_boundary_time(boundary, idx, sample_rate=sample_rate, channels=channels)
        if start is None:
            continue

        if idx + 1 < len(sentence_boundaries):
            end = get_boundary_time(
                sentence_boundaries[idx + 1],
                idx + 1,
                sample_rate=sample_rate,
                channels=channels,
            )
            if end is None:
                end = audio_duration
        else:
            end = audio_duration

        if end > start:
            ranges.append((start, end))

    return ranges


def _zero_frame(frame: Dict) -> bool:
    changed = False
    for key in VISEME_CHANNEL_KEYS:
        current = float(frame.get(key, 0.0) or 0.0)
        if current != 0.0:
            changed = True
        frame[key] = 0.0
    return changed


def _make_neutral_frame(timestamp: float) -> Dict:
    frame = {"t": round(float(timestamp), 6)}
    for key in VISEME_CHANNEL_KEYS:
        frame[key] = 0.0
    return frame


def neutralize_narration_visemes(
    visemes: List[Dict],
    sentence_boundaries: List[Dict],
    audio_duration: Optional[float] = None,
    sample_rate: int = 48000,
    channels: int = 1,
    guard_epsilon: float = 0.02,
) -> Dict:
    """Zero viseme channels during narration ranges and add neutral guard frames."""
    if not visemes or not sentence_boundaries:
        return {"zeroed": 0, "guards": 0, "ranges": []}

    if not audio_duration or audio_duration <= 0:
        audio_duration = max(float(v.get("t", 0.0) or 0.0) for v in visemes)

    ranges = build_narration_ranges(
        sentence_boundaries,
        audio_duration,
        sample_rate=sample_rate,
        channels=channels,
    )
    if not ranges:
        return {"zeroed": 0, "guards": 0, "ranges": []}

    zeroed = 0
    for frame in visemes:
        timestamp = float(frame.get("t", 0.0) or 0.0)
        for start, end in ranges:
            if start <= timestamp < end:
                if _zero_frame(frame):
                    zeroed += 1
                break

    guards = 0
    existing_times = [float(frame.get("t", 0.0) or 0.0) for frame in visemes]
    for start, end in ranges:
        if not any(abs(ts - start) <= guard_epsilon for ts in existing_times):
            visemes.append(_make_neutral_frame(start))
            existing_times.append(start)
            guards += 1

        end_guard = max(start, end - min(guard_epsilon, max(0.001, (end - start) / 2.0)))
        if not any(abs(ts - end_guard) <= guard_epsilon for ts in existing_times):
            visemes.append(_make_neutral_frame(end_guard))
            existing_times.append(end_guard)
            guards += 1

    if guards:
        visemes.sort(key=lambda frame: float(frame.get("t", 0.0) or 0.0))

    return {"zeroed": zeroed, "guards": guards, "ranges": ranges}
