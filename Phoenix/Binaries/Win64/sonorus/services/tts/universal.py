"""Universal protocol-v2 TTS provider for the CrispASR speech server."""

import base64
import json
import math
import os
import queue
import re
import sys
import threading
import time
from typing import Callable, Dict, Optional

import websocket

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

from .base import BaseTTSProvider, VoiceCache
from .omnivoice import _OmniVoiceEQ
from .universal_client import (
    UniversalAPIError,
    UniversalSpeechClient,
    model_profile,
    select_tts_model,
)
from .voice_utils import compute_reference_hash, parse_hashed_voice_name
from .reference_preparation import (
    ensure_reference_transcript,
    read_reference_transcript,
    reference_transcript_hash,
)
from utils.settings import load_settings

_synth_lock = threading.Lock()
_BRACKET_TAG_RE = re.compile(r"\[[^\]]*\]")
_PERFORMANCE_LOCK = threading.Lock()
_PERFORMANCE = {}
_LLM_PERFORMANCE = {}


def _performance_key(config: Dict):
    return (
        config.get("api_url"),
        config.get("registry_revision"),
        config.get("model"),
        json.dumps(config.get("options") or {}, sort_keys=True),
        bool(config.get("upscale")),
        "auto" if config.get("adaptive_batching") else "none",
    )


def _speech_measure(text: str) -> tuple[int, int]:
    cleaned = _BRACKET_TAG_RE.sub(" ", text or "")
    return sum(1 for char in cleaned if not char.isspace()), len(cleaned.split())


def _filter_model_tags(text: str, tags: list[dict]) -> str:
    """Canonicalize supported tags and remove all other bracket controls."""
    accepted = {
        value.lower(): tag["token"]
        for tag in tags
        for value in (tag["token"], *tag.get("aliases", []))
    }

    def replace(match):
        return f" {accepted.get(match.group(0).lower(), '')} "

    return " ".join(_BRACKET_TAG_RE.sub(replace, text or "").split())


def _filter_segment_tags(segments, tags: list[dict]):
    for item in segments:
        if isinstance(item, tuple):
            yield (_filter_model_tags(item[0], tags), *item[1:])
        else:
            yield _filter_model_tags(item, tags)


def _estimate_text_seconds(text: str, policy: Dict, calibration=None) -> float:
    chars, words = _speech_measure(text)
    if calibration:
        char_rate, word_rate = calibration
    else:
        char_rate = float(policy.get("fallbackCharactersPerSecond", 14.0))
        word_rate = float(policy.get("fallbackWordsPerSecond", 2.7))
    estimates = []
    if char_rate > 0:
        estimates.append(chars / char_rate)
    if word_rate > 0:
        estimates.append(words / word_rate)
    return max(estimates or [0.0]) * float(policy.get("safetyFactor", 1.15))


class _BatchState:
    def __init__(self, config: Dict, get_headroom=None):
        self.config = config
        self.get_headroom = get_headroom
        self.condition = threading.Condition()
        self.completed = {}
        self.sent_texts = {}
        self.sent_unit_counts = {}
        self.calibration = None
        self.alignment_failed = False
        self.visible_started = None
        self.visible_completed = None
        self.visible_characters = 0
        self.visible_rate = None
        self.cancelled = False

    def cancel(self):
        with self.condition:
            self.cancelled = True
            self.condition.notify_all()

    def record_visible_unit(self, text: str):
        now = time.monotonic()
        with self.condition:
            if self.visible_started is None:
                self.visible_started = now
            self.visible_characters += _speech_measure(text)[0]
            elapsed = now - self.visible_started
            if elapsed > 0:
                self.visible_rate = self.visible_characters / elapsed

    def record_visible_complete(self):
        now = time.monotonic()
        with self.condition:
            self.visible_completed = now
            key = self.config.get("llm_performance_key")
            if key and self.visible_characters:
                with _PERFORMANCE_LOCK:
                    previous = _LLM_PERFORMANCE.get(key)
                    observed = float(self.visible_characters)
                    _LLM_PERFORMANCE[key] = observed if previous is None else previous * 0.7 + observed * 0.3

    def record_done(self, event: Dict, sent_text: str = ""):
        with self.condition:
            self.completed[event.get("idx")] = event
            if (
                self.sent_unit_counts.get(event.get("idx"), 1) > 1
                and event.get("alignmentStatus") != "aligned"
            ):
                # The capability may have gone stale after the aligner was
                # removed or became unavailable.  Estimated boundaries are
                # safe for audio already produced, but no later units in this
                # reply should continue to be packed without CTC timing.
                self.alignment_failed = True
            audio_seconds = event.get("audioSeconds")
            chars, words = _speech_measure(sent_text)
            if isinstance(audio_seconds, (int, float)) and audio_seconds > 0:
                if self.calibration is None and (chars or words):
                    self.calibration = (
                        chars / audio_seconds if chars else 14.0,
                        words / audio_seconds if words else 2.7,
                    )
                key = _performance_key(self.config)
                throughput = event.get("throughputX")
                if isinstance(throughput, (int, float)) and throughput > 0:
                    sample = {
                        "throughput": float(throughput),
                        "upscalePerAudio": max(0.0, float(event.get("upscaleMs") or 0) / 1000 / audio_seconds),
                        "alignmentPerAudio": max(0.0, float(event.get("alignmentMs") or 0) / 1000 / audio_seconds),
                    }
                    with _PERFORMANCE_LOCK:
                        previous = _PERFORMANCE.get(key)
                        _PERFORMANCE[key] = sample if previous is None else {
                            name: previous[name] * 0.7 + sample[name] * 0.3
                            for name in sample
                        }
            self.condition.notify_all()
        print(
            "[UniversalBatch] completed "
            f"idx={event.get('idx')} audio={event.get('audioSeconds')}s "
            f"estimated={event.get('estimatedSeconds')}s "
            f"throughput={event.get('throughputX')}x "
            f"upscale={event.get('upscaleMs')}ms alignment={event.get('alignmentMs')}ms "
            f"status={event.get('alignmentStatus')}"
        )

    def wait_done(self, idx: int, abort_check=None) -> bool:
        with self.condition:
            while idx not in self.completed:
                if self.cancelled or (abort_check and abort_check()):
                    return False
                self.condition.wait(0.05)
            return True

    def eos_likely_before_deadline(self, predicted_audio: float) -> bool:
        """Use historical completion length only to decide whether waiting is safe."""
        key = self.config.get("llm_performance_key")
        if not key or not self.get_headroom or not self.visible_rate:
            return False
        with _PERFORMANCE_LOCK:
            expected_characters = _LLM_PERFORMANCE.get(key)
        if not expected_characters or expected_characters <= self.visible_characters:
            return False
        try:
            headroom = max(0.0, float(self.get_headroom()))
        except Exception:
            return False
        remaining_generation = (
            expected_characters - self.visible_characters
        ) / max(1.0, self.visible_rate)
        key = _performance_key(self.config)
        with _PERFORMANCE_LOCK:
            observed = _PERFORMANCE.get(key)
        processing = (
            max(0.5, predicted_audio / 4.0)
            if observed is None
            else predicted_audio * (
                1.0 / max(0.01, observed["throughput"])
                + observed["upscalePerAudio"]
                + observed["alignmentPerAudio"]
            )
        )
        return remaining_generation + processing * 1.5 + 0.5 < headroom

    def deadline_reached(self, predicted_audio: float) -> bool:
        if not self.get_headroom:
            return False
        try:
            headroom = max(0.0, float(self.get_headroom()))
        except Exception:
            return False
        key = _performance_key(self.config)
        with _PERFORMANCE_LOCK:
            observed = _PERFORMANCE.get(key)
        if observed is None:
            predicted_processing = max(0.5, predicted_audio / 4.0)
        else:
            predicted_processing = predicted_audio * (
                1.0 / max(0.01, observed["throughput"])
                + observed["upscalePerAudio"]
                + observed["alignmentPerAudio"]
            )
        return headroom <= predicted_processing * 1.5 + 0.5

_GAME_LANGUAGES = {
    "EN_US": "English",
    "EN_GB": "English",
    "DE_DE": "German",
    "FR_FR": "French",
    "ES_ES": "Spanish",
    "ES_MX": "Spanish",
    "IT_IT": "Italian",
    "PT_BR": "Portuguese",
    "JA_JP": "Japanese",
    "KO_KR": "Korean",
    "ZH_CN": "Chinese",
    "ZH_TW": "Chinese",
    "PL_PL": "Polish",
    "RU_RU": "Russian",
    "AR_AE": "Arabic",
    "NL_NL": "Dutch",
    "TR_TR": "Turkish",
}


def _get_config() -> Dict:
    settings = load_settings()
    tts = settings.get("tts", {})
    universal = tts.get("universal", {})
    connection = settings.get("speech_server", {})
    api_url = (connection.get("api_url") or "http://127.0.0.1:8100").strip()
    game_language = settings.get("setup", {}).get("language", "EN_US")
    client = UniversalSpeechClient(api_url, (connection.get("api_key") or "").strip())
    llm = settings.get("llm", {})
    llm_provider = str(llm.get("provider") or "unknown")
    llm_provider_settings = llm.get(llm_provider, {})
    llm_model = (
        llm_provider_settings.get("model")
        if isinstance(llm_provider_settings, dict)
        else None
    )
    return {
        "api_url": client.api_url,
        "ws_url": client.ws_url,
        "api_key": client.api_key,
        "model": universal.get("model", "omnivoice"),
        "model_settings": universal.get("model_settings", {}),
        "silence_min_ms": float(universal.get("silence_min_ms", 250)),
        "silence_max_ms": float(universal.get("silence_max_ms", 1000)),
        "language": _GAME_LANGUAGES.get(game_language, "English"),
        "game_language": game_language,
        "llm_performance_key": (llm_provider, str(llm_model or "unknown")),
    }


def _headers(config: Dict) -> Dict[str, str]:
    headers = dict(UniversalSpeechClient(config["api_url"], config["api_key"]).headers)
    headers["Content-Type"] = "application/json"
    return headers


def _get_capabilities(config: Optional[Dict] = None, force: bool = False) -> Dict:
    config = config or _get_config()
    client = UniversalSpeechClient(config["api_url"], config["api_key"])
    return client.enriched_capabilities(config["game_language"], force=force)


def _model_caps(model_id: str, config: Optional[Dict] = None) -> Dict:
    for model in _get_capabilities(config).get("compatibleModels", []):
        if model["id"] == model_id:
            return model
    raise Exception(f"Model {model_id!r} not offered by universal TTS server")


class UniversalVoiceCache(VoiceCache):
    def _make_cache_key(self, name: str, lang: Optional[str] = None) -> str:
        if lang and lang != "EN_US":
            return f"{name}_{lang}"
        return name

    def load(self) -> bool:
        config = _get_config()
        try:
            payload = UniversalSpeechClient(
                config["api_url"], config["api_key"]
            ).voices()
        except UniversalAPIError as exc:
            print(f"[Universal] Voice list failed: {exc}")
            return False

        self._voices.clear()
        self._by_id.clear()
        self._duplicates_to_delete = []
        for raw_voice in payload.get("voices", []):
            voice_id = str(raw_voice.get("voiceId") or "")
            if not voice_id:
                continue
            encoded_name = voice_id.split("__", 1)[-1]
            base_name, detected_lang, detected_hash = parse_hashed_voice_name(encoded_name)
            language = raw_voice.get("langCode") or detected_lang or "EN_US"
            voice = {
                "voiceId": voice_id,
                "displayName": base_name,
                "langCode": language,
                "referenceHash": raw_voice.get("referenceHash") or detected_hash,
                "tags": raw_voice.get("tags", []),
                "hasTranscript": bool(raw_voice.get("hasTranscript", False)),
                "transcriptHash": raw_voice.get("transcriptHash"),
                "audioHash": raw_voice.get("audioHash"),
                "preparedModels": (
                    raw_voice.get("preparedModels")
                    if isinstance(raw_voice.get("preparedModels"), dict)
                    else {}
                ),
            }
            self.add(voice, lang=language)
        self._loaded = True
        return True


def _adaptive_sender(ws, segments, config, state, abort_check, record_error):
    """Queue-backed, one-batch-at-a-time Universal coherence scheduler."""
    incoming = queue.Queue()
    sentinel = object()

    def produce():
        sequence = 0
        completed_normally = False
        try:
            for item in segments:
                if state.cancelled:
                    break
                if isinstance(item, tuple) and len(item) == 3:
                    text, segment_voice, is_narration = item
                elif isinstance(item, tuple):
                    text, segment_voice = item
                    is_narration = False
                else:
                    text, segment_voice = item, None
                    is_narration = False
                if text and text.strip():
                    state.record_visible_unit(text)
                    incoming.put(
                        {
                            "id": f"s{sequence}",
                            "sequence": sequence,
                            "text": text,
                            "voice": segment_voice,
                            "is_narration": bool(is_narration),
                        }
                    )
                    sequence += 1
            completed_normally = not state.cancelled
        except Exception as exc:
            record_error(str(exc))
            incoming.put(exc)
        finally:
            if completed_normally:
                state.record_visible_complete()
            incoming.put(sentinel)

    producer = threading.Thread(
        target=produce, name="UniversalSentenceProducer", daemon=True
    )
    producer.start()
    pending = []
    source_done = False
    batch_idx = 0
    previous_idx = None
    previous_route = None
    first_sent = False
    adaptive = True
    policy = config.get("segmentation") or {}
    target = float(policy.get("targetSeconds", 20.0))
    maximum = float(policy.get("maxSeconds", 28.0))
    minimum = float(policy.get("minSeconds", 8.0))

    def receive(timeout=0.05):
        nonlocal source_done
        if source_done:
            return None
        try:
            item = incoming.get(timeout=timeout)
        except queue.Empty:
            return None
        if item is sentinel:
            source_done = True
            return None
        if isinstance(item, Exception):
            raise item
        pending.append(item)
        return item

    def send_batch(batch, reason):
        nonlocal batch_idx, previous_idx, previous_route, first_sent
        message = {
            "type": "segment",
            "idx": batch_idx,
            "units": [{"id": item["id"], "text": item["text"]} for item in batch],
        }
        voice = batch[0].get("voice")
        route = (voice, bool(batch[0].get("is_narration")))
        if voice and voice != config.get("base_voice_id"):
            message["voiceId"] = voice
        state.sent_texts[batch_idx] = " ".join(item["text"] for item in batch)
        state.sent_unit_counts[batch_idx] = len(batch)
        predicted = sum(
            _estimate_text_seconds(item["text"], policy, state.calibration)
            for item in batch
        )
        print(
            f"[UniversalBatch] dispatch idx={batch_idx} reason={reason} "
            f"units={[item['id'] for item in batch]} "
            f"narration={route[1]} predicted={predicted:.2f}s"
        )
        ws.send(json.dumps(message))
        previous_idx = batch_idx
        previous_route = route
        first_sent = True
        batch_idx += 1

    try:
        while True:
            if abort_check and abort_check():
                ws.send(json.dumps({"type": "abort"}))
                return
            if state.cancelled:
                return

            # Collect freely while the preceding generation occupies the server.
            if previous_idx is not None and previous_idx not in state.completed:
                receive(0.05)
                continue
            if previous_idx is not None:
                if not state.wait_done(previous_idx, abort_check):
                    ws.send(json.dumps({"type": "abort"}))
                    return
                if state.alignment_failed:
                    adaptive = False
                previous_idx = None

            if not pending and not source_done:
                receive(0.05)
                continue
            if not pending and source_done:
                break

            first = pending[0]
            first_route = (first.get("voice"), bool(first.get("is_narration")))
            if not first_sent or first_route != previous_route or not adaptive:
                reason = (
                    "first-run-unit" if not first_sent or first_route != previous_route
                    else "alignment-fallback"
                )
                send_batch([pending.pop(0)], reason)
                continue

            group = [first]
            estimate = _estimate_text_seconds(first["text"], policy, state.calibration)
            index = 1
            maximum_waiting = False
            while index < len(pending):
                candidate = pending[index]
                candidate_route = (
                    candidate.get("voice"), bool(candidate.get("is_narration"))
                )
                if candidate_route != first_route:
                    break
                candidate_estimate = _estimate_text_seconds(
                    candidate["text"], policy, state.calibration
                )
                if estimate + candidate_estimate > maximum:
                    maximum_waiting = True
                    break
                group.append(candidate)
                estimate += candidate_estimate
                index += 1
                if estimate >= target:
                    break

            boundary_waiting = index < len(pending) and (
                pending[index].get("voice"),
                bool(pending[index].get("is_narration")),
            ) != first_route
            if (
                source_done
                or boundary_waiting
                or maximum_waiting
                or estimate >= target
                or state.deadline_reached(estimate)
            ):
                del pending[: len(group)]
                reason = (
                    "eos" if source_done else
                    "routing-boundary" if boundary_waiting else
                    "maximum" if maximum_waiting else
                    "target" if estimate >= target else
                    "playback-deadline"
                )
                send_batch(group, reason)
                continue

            # Prefer reaching the minimum coherent tail, but never wait beyond
            # playback headroom. With no playback callback, cap idle collection.
            wait_started = time.monotonic()
            should_flush = False
            while True:
                receive(0.05)
                if source_done or len(pending) > len(group):
                    break
                if state.deadline_reached(estimate):
                    should_flush = True
                    break
                if not state.get_headroom and time.monotonic() - wait_started >= 0.6:
                    should_flush = True
                    break
                if (
                    estimate >= minimum
                    and time.monotonic() - wait_started >= 0.15
                    and not state.eos_likely_before_deadline(estimate)
                ):
                    should_flush = True
                    break
            if should_flush:
                del pending[: len(group)]
                send_batch(group, "collection-deadline")
            # Re-evaluate the enlarged queue or deadline on the next pass.

        ws.send(json.dumps({"type": "end"}))
    except Exception as exc:
        record_error(str(exc))
        print(f"[Universal] Adaptive segment sender failed: {exc}")
        try:
            ws.send(json.dumps({"type": "abort"}))
        except Exception:
            pass
    finally:
        state.cancel()
        producer.join(timeout=1)


def _default_ws_connect(url: str, headers: Dict[str, str]):
    connection = websocket.create_connection(
        url,
        header=[f"{key}: {value}" for key, value in headers.items()],
        timeout=180,
        redirect_limit=0,
    )
    status = getattr(getattr(connection, "handshake_response", None), "status", 101)
    if not isinstance(status, int) or status != 101:
        connection.close()
        raise UniversalAPIError(
            "redirect_rejected"
            if isinstance(status, int) and 300 <= status < 400
            else "malformed_response",
            "The speech server did not complete a direct WebSocket upgrade.",
        )
    return connection


def _timing_payload(event: Dict) -> Dict:
    """Validate additive timing data before it reaches subtitle state."""
    status = event.get("status")
    if status not in {"aligned", "failed", "unavailable", "skipped"}:
        raise ValueError("server timing event has an invalid status")
    word_alignment = event.get("wordAlignment") or {}
    if not isinstance(word_alignment, dict):
        raise ValueError("server timing event has invalid word alignment")
    words = word_alignment.get("words", [])
    starts = word_alignment.get("wordStartTimeSeconds", [])
    ends = word_alignment.get("wordEndTimeSeconds", [])
    if (
        not isinstance(words, list)
        or not isinstance(starts, list)
        or not isinstance(ends, list)
        or not len(words) == len(starts) == len(ends)
        or any(not isinstance(word, str) for word in words)
    ):
        raise ValueError("server timing event has inconsistent word arrays")
    previous_start = 0.0
    previous_end = 0.0
    for start, end in zip(starts, ends):
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, (int, float))
            or not isinstance(end, (int, float))
            or not math.isfinite(float(start))
            or not math.isfinite(float(end))
            or start < 0
            or end < start
            or start < previous_start
            or end < previous_end
        ):
            raise ValueError("server timing event has invalid word timestamps")
        previous_start = float(start)
        previous_end = float(end)

    units = event.get("units") or []
    audio_events = event.get("events") or []
    if not isinstance(units, list) or not isinstance(audio_events, list):
        raise ValueError("server timing event has invalid unit data")
    seen_ids = set()
    normalized_units = []
    for unit in units:
        if not isinstance(unit, dict) or not isinstance(unit.get("id"), str):
            raise ValueError("server timing event has an invalid unit")
        unit_id = unit["id"]
        start = unit.get("startTimeSeconds")
        end = unit.get("endTimeSeconds")
        if (
            not unit_id
            or unit_id in seen_ids
            or isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, (int, float))
            or not isinstance(end, (int, float))
            or not math.isfinite(float(start))
            or not math.isfinite(float(end))
            or start < 0
            or end < start
        ):
            raise ValueError("server timing event has invalid unit timestamps")
        seen_ids.add(unit_id)
        source = unit.get("source", "unknown")
        confidence = unit.get("confidence")
        if not isinstance(source, str) or (
            confidence is not None and not isinstance(confidence, str)
        ):
            raise ValueError("server timing event has invalid unit metadata")
        normalized_unit = {
            "id": unit_id,
            "startTimeSeconds": float(start),
            "endTimeSeconds": float(end),
            "source": source,
        }
        if confidence is not None:
            normalized_unit["confidence"] = confidence
        normalized_units.append(normalized_unit)
    normalized_events = []
    for audio_event in audio_events:
        timestamp = audio_event.get("timeSeconds") if isinstance(audio_event, dict) else None
        if (
            not isinstance(audio_event, dict)
            or not isinstance(audio_event.get("unitId"), str)
            or not audio_event.get("unitId")
            or not isinstance(audio_event.get("tag"), str)
            or not audio_event.get("tag")
            or isinstance(timestamp, bool)
            or not isinstance(timestamp, (int, float))
            or not math.isfinite(float(timestamp))
            or timestamp < 0
        ):
            raise ValueError("server timing event has an invalid audio-tag event")
        if seen_ids and audio_event["unitId"] not in seen_ids:
            raise ValueError("server timing event references an unknown unit")
        source = audio_event.get("source", "unknown")
        if not isinstance(source, str):
            raise ValueError("server timing event has invalid audio-tag metadata")
        normalized_events.append(
            {
                "unitId": audio_event["unitId"],
                "tag": audio_event["tag"],
                "timeSeconds": float(timestamp),
                "source": source,
            }
        )
    return {
        "words": list(words),
        "wordStartTimeSeconds": [float(value) for value in starts],
        "wordEndTimeSeconds": [float(value) for value in ends],
        "unitTimings": normalized_units,
        "audioTagEvents": normalized_events,
        "alignmentStatus": status,
    }


def _segment_done_payload(event: Dict) -> Dict:
    idx = event.get("idx")
    if not isinstance(idx, int) or isinstance(idx, bool) or idx < 0:
        raise ValueError("server segment_done has an invalid index")
    normalized = dict(event)
    for field in ("bytes", "physicalChunks"):
        value = event.get(field)
        if value is None:
            continue
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"server segment_done has invalid {field}")
    for field in (
        "audioSeconds",
        "synthesisMs",
        "upscaleMs",
        "alignmentMs",
        "processingMs",
        "estimatedSeconds",
        "throughputX",
    ):
        value = event.get(field)
        if value is None and field == "throughputX":
            continue
        if value is None:
            continue
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or value < 0
        ):
            raise ValueError(f"server segment_done has invalid {field}")
        normalized[field] = float(value)
    status = event.get("alignmentStatus", "skipped")
    if status not in {"aligned", "failed", "unavailable", "skipped"}:
        raise ValueError("server segment_done has invalid alignment status")
    normalized["alignmentStatus"] = status
    return normalized


def run_ws_request(
    config: Dict,
    voice_id: str,
    segments,
    on_chunk: Callable[[bytes, Optional[Dict]], None],
    on_voice_switch: Optional[Callable] = None,
    abort_check: Optional[Callable[[], bool]] = None,
    first_sentence_steps: Optional[int] = None,
    guidance_scale: Optional[float] = None,
    on_sentence_flushed: Optional[Callable] = None,
    record_error: Callable[[str], None] = lambda message: None,
    ws_connect: Callable = _default_ws_connect,
    get_playback_headroom: Optional[Callable[[], float]] = None,
) -> bool:
    """Drive one protocol-v2 synthesis request and return whether PCM arrived."""
    ws = None
    produced_audio = False
    total_pcm_bytes = 0
    sender_thread = None
    started = False
    state = _BatchState(config, get_playback_headroom)
    try:
        ws = ws_connect(f"{config['ws_url']}/v2/synthesize", _headers(config))
        wire_options = dict(config.get("options") or {})
        # Transitional support for pre-profile callers; runtime uses advertised options.
        if not wire_options and "num_steps" in config:
            wire_options = {
                "numSteps": config["num_steps"],
                "firstSegmentSteps": config.get("first_sentence_steps"),
                "guidanceScale": config.get("guidance_scale"),
            }
        if first_sentence_steps is not None and "firstSegmentSteps" in wire_options:
            wire_options["firstSegmentSteps"] = first_sentence_steps
        if guidance_scale is not None and "guidanceScale" in wire_options:
            wire_options["guidanceScale"] = guidance_scale
        wire_options = {key: value for key, value in wire_options.items() if value is not None}
        wire_options["upscale"] = bool(config.get("upscale", False))
        adaptive_batching = bool(config.get("adaptive_batching", False))
        if adaptive_batching:
            wire_options["timing"] = "auto"
        wire_options["silence"] = {
            "minMs": config["silence_min_ms"],
            "maxMs": config["silence_max_ms"],
        }
        ws.send(
            json.dumps(
                {
                    "type": "start",
                    "requestId": f"req_{int(time.time() * 1000)}",
                    "model": config["model"],
                    "voiceId": voice_id,
                    "language": config.get("language", "English"),
                    "options": wire_options,
                }
            )
        )

        def sender():
            if adaptive_batching:
                adaptive_config = dict(config)
                adaptive_config["base_voice_id"] = voice_id
                _adaptive_sender(
                    ws,
                    segments,
                    adaptive_config,
                    state,
                    abort_check,
                    record_error,
                )
                return
            index = 0
            try:
                for item in segments:
                    if abort_check and abort_check():
                        ws.send(json.dumps({"type": "abort"}))
                        return
                    if isinstance(item, tuple) and len(item) == 3:
                        text, segment_voice, _is_narration = item
                    elif isinstance(item, tuple):
                        text, segment_voice = item
                    else:
                        text, segment_voice = item, None
                    if not text or not text.strip():
                        continue
                    message = {"type": "segment", "idx": index, "text": text}
                    if segment_voice and segment_voice != voice_id:
                        message["voiceId"] = segment_voice
                    ws.send(json.dumps(message))
                    state.sent_texts[index] = text
                    index += 1
                ws.send(json.dumps({"type": "end"}))
            except Exception as exc:
                record_error(str(exc))
                print(f"[Universal] Segment sender failed: {exc}")
                try:
                    ws.send(json.dumps({"type": "abort"}))
                except Exception:
                    pass

        sender_thread = threading.Thread(
            target=sender, name="UniversalTTSSender", daemon=True
        )
        sender_thread.start()

        while True:
            frame = ws.recv()
            if isinstance(frame, (bytes, bytearray)):
                if not started:
                    raise ValueError("server sent PCM before the started event")
                if len(frame) % 2:
                    raise ValueError("server sent an invalid odd-length PCM16 frame")
                on_chunk(bytes(frame), None)
                produced_audio = True
                total_pcm_bytes += len(frame)
                continue
            event = json.loads(frame)
            if not isinstance(event, dict):
                raise ValueError("server sent a non-object event")
            event_type = event.get("type")
            if event_type not in {"started", "error"} and not started:
                raise ValueError(f"server sent {event_type!r} before the started event")
            if event_type == "started":
                if started:
                    raise ValueError("server sent a duplicate started event")
                announced_rate = event.get("sampleRate")
                expected_rate = config.get("expected_sample_rate")
                if (
                    not isinstance(announced_rate, int)
                    or isinstance(announced_rate, bool)
                    or announced_rate <= 0
                ):
                    raise ValueError("server started event has an invalid sampleRate")
                if expected_rate is not None and announced_rate != expected_rate:
                    raise ValueError(
                        f"server announced {announced_rate} Hz; expected {expected_rate} Hz"
                    )
                started = True
            elif event_type == "segment_start":
                byte_offset = event.get("byteOffset")
                if (
                    not isinstance(byte_offset, int)
                    or isinstance(byte_offset, bool)
                    or byte_offset < 0
                ):
                    raise ValueError("server segment_start has an invalid byteOffset")
                unit_ids = event.get("unitIds") or []
                if not isinstance(unit_ids, list) or any(
                    not isinstance(unit_id, str) for unit_id in unit_ids
                ):
                    raise ValueError("server segment_start has invalid unit IDs")
                sentence_idx = event.get("idx", 0)
                if unit_ids and isinstance(unit_ids[0], str) and unit_ids[0].startswith("s"):
                    try:
                        sentence_idx = int(unit_ids[0][1:])
                    except ValueError:
                        pass
                if on_voice_switch and sentence_idx > 0:
                    on_voice_switch(byte_offset, sentence_idx)
            elif event_type == "timing":
                on_chunk(b"", _timing_payload(event))
            elif event_type == "segment_done":
                event = _segment_done_payload(event)
                if event["idx"] in state.completed:
                    raise ValueError("server sent duplicate segment_done event")
                state.record_done(
                    event, state.sent_texts.get(event.get("idx"), "")
                )
                if on_sentence_flushed:
                    on_sentence_flushed()
            elif event_type == "done":
                total_bytes = event.get("totalBytes")
                aborted = event.get("aborted")
                if (
                    not isinstance(total_bytes, int)
                    or isinstance(total_bytes, bool)
                    or total_bytes < 0
                    or not isinstance(aborted, bool)
                    or total_bytes != total_pcm_bytes
                ):
                    raise ValueError("server done event has invalid stream totals")
                if sender_thread:
                    sender_thread.join(timeout=1)
                return produced_audio
            elif event_type == "error":
                message = f"{event.get('code')}: {event.get('message')}"
                record_error(message)
                print(f"[Universal] Server error {message}")
                return False
            else:
                raise ValueError(f"server sent an unknown event type {event_type!r}")
    except Exception as exc:
        record_error(str(exc))
        print(f"[Universal] WebSocket request failed: {exc}")
        return False
    finally:
        state.cancel()
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        if sender_thread and sender_thread.is_alive():
            sender_thread.join(timeout=1)


class UniversalProvider(BaseTTSProvider):
    def __init__(self):
        self._cache = UniversalVoiceCache()
        self._last_error = ""

    @property
    def name(self) -> str:
        return "Universal"

    def get_config(self) -> Dict:
        return _get_config()

    def get_sample_rate(self) -> int:
        return self.get_sample_rate_for_speaker(None)

    def get_sample_rate_for_speaker(self, speaker_id: Optional[str] = None) -> int:
        try:
            return int(self._request_config(speaker_id)["expected_sample_rate"])
        except Exception as exc:
            print(f"[Universal] Capabilities unavailable, using 24000 Hz: {exc}")
            return 24000

    def get_buffer_seconds(self) -> float:
        return 1.0

    def get_voice_cache(self) -> VoiceCache:
        return self._cache

    def init(self):
        _get_capabilities(force=True)
        return self._cache.load()

    def get_default_language(self) -> Optional[str]:
        return None

    def _record_synthesis_error(self, message: str):
        self._last_error = message or ""

    def should_reclone_after_synthesis_failure(self, voice_id: str) -> bool:
        return "voice_not_found" in self._last_error.lower()

    def _request_config(self, speaker_id: Optional[str]) -> Dict:
        config = dict(_get_config())
        capabilities = _get_capabilities(config)
        model = select_tts_model(capabilities, config["model"])
        if model is None:
            raise UniversalAPIError(
                "no_compatible_models",
                "The speech server has no voice-cloning model for the game language.",
                status=409,
            )
        config["model"] = model["id"]
        if model.get("installed", model.get("available", True)) is not True:
            raise UniversalAPIError(
                "model_not_installed",
                "The selected Universal TTS model must be installed before synthesis.",
                status=409,
            )
        saved_profiles = config.get("model_settings", {})
        saved_profile = saved_profiles.get(config["model"], {}) if isinstance(saved_profiles, dict) else {}
        profile = model_profile(model, saved_profile, capabilities.get("upscaler"))
        config["options"] = profile["options"]
        config["upscale"] = bool(profile.get("upscale"))
        config["adaptive_batching"] = bool(profile.get("adaptive_batching", False))
        upscaler = capabilities.get("upscaler")
        if config["upscale"] and (
            not upscaler
            or upscaler.get("installed", upscaler.get("available", True)) is not True
        ):
            raise UniversalAPIError(
                "upscaler_not_installed",
                "The selected Universal upscaler must be installed before synthesis.",
                status=409,
            )
        alignment = capabilities.get("alignment")
        if config["adaptive_batching"] and (
            not alignment
            or alignment.get("installed", alignment.get("available", True)) is not True
        ):
            raise UniversalAPIError(
                "aligner_not_installed",
                "The Universal aligner must be installed before adaptive batching.",
                status=409,
            )
        config["segmentation"] = model.get("segmentation")
        config["capabilities_version"] = int(capabilities.get("capabilitiesVersion", 1))
        config["registry_revision"] = capabilities.get("registryRevision")
        config["alignment"] = alignment
        config["expression_control"] = model.get("expressionControl")
        config["paralinguistic_tags"] = list(model.get("paralinguisticTags", []))
        config["voice_reference"] = model.get("voiceReference") or {
            "transcript": "unused",
            "preparation": {"mode": "lazy", "inputs": ["audio"], "revision": None},
        }
        config["control_schemas"] = {
            control["id"]: control for control in model.get("controls", [])
        }
        config["expected_sample_rate"] = int(
            upscaler["sampleRate"]
            if config["upscale"] and upscaler
            else model["sampleRate"]
        )
        return config

    @staticmethod
    def _preparation_is_current(voice: Dict, model_id: str, policy: Dict) -> bool:
        preparation = policy.get("preparation") or {}
        if preparation.get("mode") != "persistent":
            return True
        prepared_models = voice.get("preparedModels")
        if not isinstance(prepared_models, dict):
            return False
        marker = prepared_models.get(model_id)
        if not isinstance(marker, dict) or marker.get("revision") != preparation.get("revision"):
            return False
        hashes = marker.get("inputHashes")
        if not isinstance(hashes, dict):
            return False
        if "audio" in preparation.get("inputs", []) and hashes.get("audio") != voice.get("audioHash"):
            return False
        if (
            "transcript" in preparation.get("inputs", [])
            and hashes.get("transcript") != voice.get("transcriptHash")
        ):
            return False
        return True

    def _prepare_remote_voice(self, voice: Dict, model_id: str, policy: Dict) -> Dict:
        preparation = policy.get("preparation") or {}
        if preparation.get("mode") != "persistent" or self._preparation_is_current(
            voice, model_id, policy
        ):
            return voice
        config = _get_config()
        result = UniversalSpeechClient(
            config["api_url"], config["api_key"]
        ).prepare_voice(model_id, voice["voiceId"])
        existing_prepared = voice.get("preparedModels")
        prepared = dict(existing_prepared) if isinstance(existing_prepared, dict) else {}
        prepared[model_id] = result.get("preparation", {})
        voice["preparedModels"] = prepared
        return voice

    def prepare_reference_for_clone(
        self, character_name: str, lang: str, reference_path: str
    ) -> None:
        policy = self._request_config(character_name)["voice_reference"]
        if policy.get("transcript") == "required":
            ensure_reference_transcript(reference_path)

    def ensure_cached_voice_ready(
        self,
        voice: Dict,
        character_name: str,
        lang: str,
        reference_path: Optional[str],
    ) -> Optional[Dict]:
        request_config = self._request_config(character_name)
        policy = request_config["voice_reference"]
        transcript_policy = policy.get("transcript")
        local_transcript = None
        if reference_path and transcript_policy == "required":
            local_transcript = ensure_reference_transcript(reference_path)
        elif reference_path and transcript_policy == "optional":
            local_transcript = read_reference_transcript(reference_path)
        if local_transcript:
            local_hash = reference_transcript_hash(local_transcript)
            remote_hash = voice.get("transcriptHash")
            if not voice.get("hasTranscript") or (
                remote_hash is not None and remote_hash != local_hash
            ):
                return None
        elif transcript_policy == "required":
            return None
        return self._prepare_remote_voice(voice, request_config["model"], policy)

    def finalize_cloned_voice(
        self,
        voice: Dict,
        character_name: str,
        lang: str,
        reference_path: str,
    ) -> Dict:
        request_config = self._request_config(character_name)
        return self._prepare_remote_voice(
            voice, request_config["model"], request_config["voice_reference"]
        )

    def _apply_expression_modifier(self, config: Dict, speaker_id: Optional[str]) -> None:
        control_id = config.get("expression_control")
        if not speaker_id or control_id != "guidanceScale":
            return
        modifier = (
            load_settings()
            .get("tts", {})
            .get("npc_temp_modifiers", {})
            .get(speaker_id, 0.0)
        )
        if modifier > 0:
            schema = config.get("control_schemas", {}).get(control_id, {})
            maximum = float(schema.get("maximum", 10.0))
            base = float(config.get("options", {}).get(control_id, 0.0))
            config["options"] = dict(config.get("options", {}))
            config["options"][control_id] = min(base + modifier * 10.0, maximum)

    def clone_voice(
        self,
        display_name: str,
        reference_wav_path: str,
        lang: Optional[str] = None,
    ) -> Optional[Dict]:
        config = _get_config()
        transcript_policy = (
            self._request_config(display_name)
            .get("voice_reference", {})
            .get("transcript", "unused")
        )
        if transcript_policy == "required":
            # Required transcripts may already live in OmniVoice's .tokens.pt
            # sidecar.  Use the shared resolver so lazy cloning follows the
            # exact same .txt -> .tokens.pt -> STT policy as bulk setup.
            transcript = ensure_reference_transcript(reference_wav_path)
        elif transcript_policy == "optional":
            transcript = read_reference_transcript(reference_wav_path)
        else:
            transcript = None
        try:
            with open(reference_wav_path, "rb") as wav:
                audio_b64 = base64.b64encode(wav.read()).decode()
        except OSError as exc:
            print(f"[Universal] Cannot read voice reference: {exc}")
            return None
        payload = {
            "displayName": display_name,
            "langCode": lang or "EN_US",
            "audioData": audio_b64,
            "refText": transcript,
            "referenceHash": compute_reference_hash(reference_wav_path),
            "tags": ["hogwarts-legacy", "auto-cloned"],
        }
        try:
            voice = UniversalSpeechClient(
                config["api_url"], config["api_key"]
            ).clone_voice(payload)
            return {
                "voiceId": voice["voiceId"],
                "displayName": display_name,
                "langCode": voice.get("langCode", lang or "EN_US"),
                "referenceHash": voice.get("referenceHash"),
                "hasTranscript": bool(voice.get("hasTranscript", transcript)),
                "transcriptHash": voice.get("transcriptHash"),
                "audioHash": voice.get("audioHash"),
                "preparedModels": (
                    voice.get("preparedModels")
                    if isinstance(voice.get("preparedModels"), dict)
                    else {}
                ),
            }
        except UniversalAPIError as exc:
            print(f"[Universal] Clone request failed: {exc}")
            return None

    def delete_voice(self, voice_id: str) -> bool:
        config = _get_config()
        try:
            UniversalSpeechClient(config["api_url"], config["api_key"]).delete_voice(voice_id)
            return True
        except UniversalAPIError:
            return False

    def _eq_wrap(self, on_chunk, config: Dict):
        if not config.get("upscale", False):
            return on_chunk
        eq = _OmniVoiceEQ(int(config["expected_sample_rate"]))

        def wrapped(pcm, timing):
            on_chunk(eq.process_pcm16(pcm), timing)

        return wrapped

    def _resolve_remote_segment_voice(
        self, voice_name_or_id: str, fallback_voice_id: str, game_language: str
    ) -> str:
        """Resolve narrator/display names to the server's exact voice ID."""
        if not voice_name_or_id or voice_name_or_id == fallback_voice_id:
            return fallback_voice_id
        cached = self._cache.get_by_id(voice_name_or_id) or self._cache.get(
            voice_name_or_id, game_language
        )
        if cached and cached.get("voiceId"):
            return cached["voiceId"]
        try:
            voice = self.get_or_create_voice(voice_name_or_id, game_language)
        except Exception as exc:
            print(
                f"[Universal] Could not prepare segment voice {voice_name_or_id!r}; "
                f"using the speaker voice: {exc}"
            )
            return fallback_voice_id
        return voice.get("voiceId", fallback_voice_id) if voice else fallback_voice_id

    def _resolve_segment_voices(self, segments, fallback_voice_id: str, config: Dict):
        for item in segments:
            if not isinstance(item, tuple):
                yield item
                continue
            if len(item) == 3:
                text, segment_voice, is_narration = item
            else:
                text, segment_voice = item
                is_narration = segment_voice != fallback_voice_id
            yield (
                text,
                self._resolve_remote_segment_voice(
                    segment_voice, fallback_voice_id, config["game_language"]
                ),
                bool(is_narration),
            )

    def synthesize_stream(
        self,
        text: str,
        voice_id: str,
        on_chunk: Callable[[bytes, Optional[Dict]], None],
        speaker_id: Optional[str] = None,
    ) -> bool:
        with _synth_lock:
            self._record_synthesis_error("")
            config = self._request_config(speaker_id)
            config["adaptive_batching"] = False
            self._apply_expression_modifier(config, speaker_id)
            text = _filter_model_tags(text, config["paralinguistic_tags"])
            return run_ws_request(
                config,
                voice_id,
                iter([text]),
                self._eq_wrap(on_chunk, config),
                record_error=self._record_synthesis_error,
            )

    def synthesize_stream_sentences(
        self,
        sentences,
        voice_id: str,
        on_chunk: Callable[[bytes, Optional[Dict]], None],
        speaker_id: Optional[str] = None,
        on_sentence_flushed: Callable = None,
        abort_check: Callable = None,
        on_voice_switch: Callable = None,
        get_playback_headroom: Callable = None,
    ) -> bool:
        with _synth_lock:
            self._record_synthesis_error("")
            config = self._request_config(speaker_id)
            self._apply_expression_modifier(config, speaker_id)
            sentences = _filter_segment_tags(
                sentences, config["paralinguistic_tags"]
            )
            return run_ws_request(
                config,
                voice_id,
                self._resolve_segment_voices(sentences, voice_id, config),
                self._eq_wrap(on_chunk, config),
                on_voice_switch=on_voice_switch,
                abort_check=abort_check,
                on_sentence_flushed=on_sentence_flushed,
                record_error=self._record_synthesis_error,
                get_playback_headroom=get_playback_headroom,
            )


def clear_voice_cache():
    UniversalSpeechClient.invalidate_cache()
