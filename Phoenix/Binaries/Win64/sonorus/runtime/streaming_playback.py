"""Shared streaming playback helpers for chat and commentary runtimes."""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, Iterator, Optional

from utils.narration import (
    StreamingNarrationParser,
    normalize_narration_response,
    parse_segments,
)


def iter_completed_response_sentences(
    response_text: str,
    narration_enabled: bool = False,
    log_prefix: str = "[RespStream]",
) -> Iterator:
    """Yield sentence chunks for an already-generated response."""
    from utils.text_utils import split_into_sentences_safe as _split_sentences_safe

    sent_count = 0
    stream_start = time.time()

    if narration_enabled:
        normalized = normalize_narration_response(response_text or "")
        for seg in parse_segments(normalized):
            seg_text = (seg.text or "").strip()
            if not seg_text:
                continue
            sub_chunks = _split_sentences_safe(seg_text) or [seg_text]
            for text in sub_chunks:
                text = (text or "").strip()
                if not text:
                    continue
                sent_count += 1
                elapsed = (time.time() - stream_start) * 1000
                tag = " [narration]" if seg.is_narration else ""
                print(f'{log_prefix} Sentence #{sent_count} at {elapsed:.0f}ms{tag}: "{text}"')
                yield (text, seg.is_narration)
    else:
        sub_chunks = _split_sentences_safe(response_text or "") or [response_text]
        for chunk in sub_chunks:
            chunk = (chunk or "").strip()
            if not chunk:
                continue
            sent_count += 1
            elapsed = (time.time() - stream_start) * 1000
            print(f'{log_prefix} Sentence #{sent_count} at {elapsed:.0f}ms: "{chunk}"')
            yield chunk


def build_live_sentence_stream(
    prompt: str = None,
    user_input: str = None,
    *,
    messages: list = None,
    full_text_holder: Dict[str, str],
    llm_done: threading.Event,
    stream_sentences_func: Callable,
    strip_action_tag_func: Callable[[str], str],
    narration_enabled: bool = False,
    log_prefix: str = "[StreamGen]",
    speaker_id: str = None,
    kv_cache_prefix: Any = None,
    kv_cache_context: str = None,
) -> Iterator:
    """Stream LLM output and yield spoken sentence chunks.

    Accepts either (prompt, user_input) for legacy callers or (messages)
    for the new multi-message chat path.
    """
    from utils.text_utils import split_into_sentences_safe as _split_sentences_safe
    from utils.llm_utils import strip_response_metadata

    sent_count = 0
    accepted_parts = []
    stream_start = time.time()
    narration_parser = (
        StreamingNarrationParser()
        if narration_enabled
        else None
    )

    def emit_narration_segments(segments):
        nonlocal sent_count
        for seg in segments:
            seg_text = (seg.text or "").strip()
            if not seg_text:
                continue
            sub_chunks = _split_sentences_safe(seg_text) or [seg_text]
            for text in sub_chunks:
                text = (text or "").strip()
                if not text:
                    continue
                sent_count += 1
                tag = " [narration]" if seg.is_narration else ""
                elapsed = (time.time() - stream_start) * 1000
                print(
                    f'{log_prefix} Sentence #{sent_count} at '
                    f'{elapsed:.0f}ms{tag}: "{text}"'
                )
                accepted_parts.append(
                    f"*{text}*" if seg.is_narration else f'"{text}"'
                )
                full_text_holder["text"] = " ".join(accepted_parts)
                yield (text, seg.is_narration)

    # Choose sentence source based on arguments
    if messages is not None:
        from utils.llm_utils import stream_sentences_messages
        sentence_source = stream_sentences_messages(
            messages,
            speaker_id=speaker_id,
            kv_cache_prefix=kv_cache_prefix,
            kv_cache_context=kv_cache_context,
        )
    else:
        sentence_source = stream_sentences_func(
            prompt,
            user_input,
            speaker_id=speaker_id,
            kv_cache_prefix=kv_cache_prefix,
            kv_cache_context=kv_cache_context,
        )

    try:
        for sentence, accumulated, _is_final in sentence_source:
            full_text_holder["raw"] = accumulated
            if not narration_enabled:
                full_text_holder["text"] = strip_response_metadata(
                    strip_action_tag_func(accumulated)
                )

            clean = strip_response_metadata(strip_action_tag_func(sentence))
            if not clean:
                continue

            if narration_enabled:
                yield from emit_narration_segments(narration_parser.parse(clean))
            else:
                sub_chunks = _split_sentences_safe(clean) or [clean]
                for chunk in sub_chunks:
                    chunk = chunk.strip()
                    if not chunk:
                        continue
                    sent_count += 1
                    elapsed = (time.time() - stream_start) * 1000
                    print(f'{log_prefix} Sentence #{sent_count} at {elapsed:.0f}ms: "{chunk}"')
                    yield chunk

        if narration_parser is not None:
            yield from emit_narration_segments(narration_parser.finish())
    finally:
        elapsed = (time.time() - stream_start) * 1000
        print(f"{log_prefix} Generator exhausted ({sent_count} sentences) at {elapsed:.0f}ms")
        llm_done.set()


@dataclass
class StreamingPlaybackSession:
    speaker_id: str
    epoch: Optional[int]
    lua_socket: Any
    is_conversation_valid: Callable[[Optional[int]], bool]
    setup_event: threading.Event
    setup_data: Dict[str, Any]
    buffer_ready_event: threading.Event
    full_text_holder: Dict[str, str]
    sentence_subtitles: bool
    narration_enabled: bool
    voice_id: Optional[str]
    tts_thread: threading.Thread

    def wait_for_buffer(self, timeout: float = 120.0) -> bool:
        return self.buffer_ready_event.wait(timeout=timeout)

    def abort(self) -> None:
        self.setup_data["_abort"] = True
        self.setup_event.set()

    def start_playback(
        self,
        *,
        display_name: str,
        text: str,
        turn_index: int,
        target_id: str,
        remove_unpaired_double_quotes: Callable[[str], str],
        pending_history_entry: Optional[Dict[str, Any]] = None,
        add_pending_history: Optional[Callable[[Dict[str, Any]], None]] = None,
        action: str = "None",
        house_point_actions: Optional[list] = None,
    ) -> Dict[str, Any]:
        if self.epoch is not None and not self.is_conversation_valid(self.epoch):
            print(f"[RespStream] Aborting stale playback before play_turn (epoch={self.epoch})")
            self.abort()
            return {
                "success": False,
                "status": "aborted",
                "message": "Conversation became stale before playback",
            }

        subtitle_seed_text = remove_unpaired_double_quotes(text)
        turn_result = self.lua_socket.send_play_turn(
            speaker_id=self.speaker_id,
            display_name=display_name,
            text=subtitle_seed_text,
            turn_index=turn_index,
            target_id=target_id,
            action=action,
            house_point_actions=house_point_actions if house_point_actions else None,
            streaming_subtitles=True,
        )

        if not turn_result.get("success"):
            self.abort()
            return {"success": False, "error": "play_turn failed", "turn_result": turn_result}

        if pending_history_entry and add_pending_history:
            add_pending_history(pending_history_entry)

        self.setup_data["positions"] = turn_result.get("positions")
        self.setup_data["turn_id"] = turn_result.get("turn_id")
        self.setup_event.set()

        return {"success": True, "turn_result": turn_result}


def start_streaming_playback_session(
    *,
    sentence_iterable: Iterable,
    speaker_id: str,
    full_text_holder: Dict[str, str],
    epoch: Optional[int],
    run_streaming_tts_async: Callable,
    tts_service: Any,
    lua_socket: Any,
    load_settings_func: Callable[[], Dict[str, Any]],
    can_use_streaming_tts_func: Callable[[], bool],
    is_conversation_valid_func: Callable[[Optional[int]], bool],
    pending_history_entry: Optional[Dict[str, Any]] = None,
    require_voice_prefetch: bool = False,
) -> Dict[str, Any]:
    """Create and start a shared streaming playback session."""
    if not speaker_id or not can_use_streaming_tts_func():
        return {"success": False, "fallback_required": True}

    lua_socket.send({"type": "fast_poll", "duration": 5.0})

    voice_id = None
    try:
        voice = tts_service.get_or_create_voice(speaker_id, lua_socket=lua_socket)
        voice_id = voice.get("voiceId") if voice else None
    except Exception as e:
        print(f"[RespStream] Voice pre-fetch failed for {speaker_id}: {e}")
        if require_voice_prefetch:
            return {
                "success": False,
                "fallback_required": True,
                "error": "voice prefetch failed",
            }

    conv_settings = load_settings_func().get("conversation", {})
    sentence_subtitles = conv_settings.get("sentence_subtitles", True)
    narration_enabled = conv_settings.get("narration_enabled", False)

    setup_event = threading.Event()
    buffer_ready_event = threading.Event()
    setup_data = {
        "_buffer_ready": buffer_ready_event,
        "_sentence_subtitles": sentence_subtitles,
        "_history_entry": pending_history_entry,
    }

    tts_thread = threading.Thread(
        target=run_streaming_tts_async,
        args=(sentence_iterable, speaker_id, setup_event, setup_data, full_text_holder, epoch),
        daemon=True,
    )
    tts_thread.start()

    return {
        "success": True,
        "voice_id": voice_id,
        "session": StreamingPlaybackSession(
            speaker_id=speaker_id,
            epoch=epoch,
            lua_socket=lua_socket,
            is_conversation_valid=is_conversation_valid_func,
            setup_event=setup_event,
            setup_data=setup_data,
            buffer_ready_event=buffer_ready_event,
            full_text_holder=full_text_holder,
            sentence_subtitles=sentence_subtitles,
            narration_enabled=narration_enabled,
            voice_id=voice_id,
            tts_thread=tts_thread,
        ),
    }


def play_completed_response_streaming(
    response: str,
    speaker_id: str,
    speaker_name: str,
    target_id: str,
    turn_index: int,
    epoch: Optional[int],
    *,
    raw_response: Optional[str] = None,
    action: str = "None",
    house_point_actions: Optional[list] = None,
    pending_history_entry: Optional[Dict[str, Any]] = None,
    wait_for_prior_tts: Optional[threading.Event] = None,
    before_play_callback: Optional[Callable[[], Optional[Dict[str, Any]]]] = None,
    remove_unpaired_double_quotes: Callable[[str], str],
    add_pending_history: Callable[[Dict[str, Any]], None],
    run_streaming_tts_async: Callable,
    tts_service: Any,
    lua_socket: Any,
    load_settings_func: Callable[[], Dict[str, Any]],
    can_use_streaming_tts_func: Callable[[], bool],
    is_conversation_valid_func: Callable[[Optional[int]], bool],
) -> Dict[str, Any]:
    """Play a completed response through the sentence-streaming subtitle/TTS path."""
    if not response or not response.strip() or not speaker_id:
        return {"success": False, "fallback_required": True}

    narration_enabled = load_settings_func().get("conversation", {}).get("narration_enabled", False)
    full_text_holder = {
        "text": response,
        "raw": raw_response if raw_response is not None else response,
    }

    session_result = start_streaming_playback_session(
        sentence_iterable=iter_completed_response_sentences(
            response,
            narration_enabled=narration_enabled,
            log_prefix="[RespStream]",
        ),
        speaker_id=speaker_id,
        full_text_holder=full_text_holder,
        epoch=epoch,
        run_streaming_tts_async=run_streaming_tts_async,
        tts_service=tts_service,
        lua_socket=lua_socket,
        load_settings_func=load_settings_func,
        can_use_streaming_tts_func=can_use_streaming_tts_func,
        is_conversation_valid_func=is_conversation_valid_func,
        pending_history_entry=pending_history_entry,
    )
    if not session_result.get("success"):
        return session_result

    session = session_result["session"]
    if not session.wait_for_buffer(timeout=120.0):
        print(f"[RespStream] Buffer ready timeout for {speaker_id}")
        session.abort()
        return {"success": False, "error": "TTS buffer ready timeout"}

    if wait_for_prior_tts is not None:
        wait_for_prior_tts.wait(timeout=60.0)

    if before_play_callback:
        abort_result = before_play_callback()
        if abort_result:
            session.abort()
            return abort_result

    playback_result = session.start_playback(
        display_name=speaker_name,
        text=response,
        turn_index=turn_index,
        target_id=target_id,
        remove_unpaired_double_quotes=remove_unpaired_double_quotes,
        pending_history_entry=pending_history_entry,
        add_pending_history=add_pending_history,
        action=action,
        house_point_actions=house_point_actions,
    )
    if not playback_result.get("success"):
        return playback_result

    if not session.sentence_subtitles and response.strip():
        subtitle_text = response
        if session.narration_enabled:
            subtitle_text = re.sub(r"\*([^*]+)\*", r"\1", subtitle_text)
        subtitle_text = remove_unpaired_double_quotes(subtitle_text)
        lua_socket.send(
            {
                "type": "subtitle_update",
                "turn_id": playback_result["turn_result"]["turn_id"],
                "text": subtitle_text,
                "sentence_idx": 0,
                "total_sentences": 1,
                "is_narration": False,
            }
        )

    return {
        "success": True,
        "turn_result": playback_result["turn_result"],
        "voice_id": session.voice_id,
        "tts_thread": session.tts_thread,
        "session": session,
    }
