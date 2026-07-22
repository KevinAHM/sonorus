"""
Inworld WebSocket TTS Transport

Manages a persistent WebSocket connection to Inworld's bidirectional TTS API.
Creates ephemeral contexts per synthesis call (unique ID each time, closed when done).
Audio chunks dispatched to per-synthesis callbacks.

Protocol:
  Client sends: create (context), send_text, flush_context, close_context
  Server sends: contextCreated, audioChunk, flushCompleted, contextClosed
"""
import json
import time
import base64
import random
import threading
from typing import Dict, Optional, Callable

try:
    import websocket
    WS_AVAILABLE = True
except ImportError:
    WS_AVAILABLE = False
    print("[InworldWS] websocket-client not installed - WebSocket TTS unavailable")


WS_ENDPOINT = "wss://api.inworld.ai/tts/v1/voice:streamBidirectional"


class InworldWSContext:
    """State for a single TTS context on the WebSocket connection."""

    def __init__(self, context_id: str, voice_id: str, config: dict):
        self.context_id = context_id
        self.voice_id = voice_id
        self.config = config  # create message config (for reconnection)
        self.created = threading.Event()

        # Per-synthesis callbacks (set by synthesize call, cleared on flush)
        self._on_chunk: Optional[Callable] = None
        self._on_flush: Optional[Callable] = None
        self._on_error: Optional[Callable] = None
        self._lock = threading.Lock()

        # Note: no synthesis_lock needed — contexts are ephemeral (one per call)

        # Diagnostics
        self.chunks_received = 0
        self.bytes_received = 0
        self.stream_start_time = 0.0
        self.chunk_recv_times = []

    def set_handlers(self, on_chunk: Callable = None,
                     on_flush: Callable = None,
                     on_error: Callable = None,
                     sample_rate: int = 48000):
        """Set callbacks for the current synthesis request."""
        with self._lock:
            self._on_chunk = on_chunk
            self._on_flush = on_flush
            self._on_error = on_error
            self.chunks_received = 0
            self.bytes_received = 0
            self.stream_start_time = time.time()
            self.chunk_recv_times = []
            # Track cumulative audio for word alignment timestamp offsetting.
            # Each sentence's timestamps reset to 0, but audio bytes accumulate.
            self._completed_sentences_bytes = 0  # PCM bytes from finished sentences
            self._current_sentence_bytes = 0     # PCM bytes in current sentence
            self._sample_rate = sample_rate

    def handle_audio_chunk(self, pcm_bytes: bytes, word_alignment: Optional[dict]):
        """Called by receive loop when audio chunk arrives for this context."""
        self.chunks_received += 1
        self.bytes_received += len(pcm_bytes)
        self._current_sentence_bytes += len(pcm_bytes)
        self.chunk_recv_times.append(time.time())

        if self.chunks_received == 1:
            elapsed = (time.time() - self.stream_start_time) * 1000
            words = len(word_alignment.get("words", [])) if word_alignment else 0
            # Measure actual Inworld latency from when first sentence was sent
            inworld_latency = ""
            if hasattr(self, '_first_send_time') and self._first_send_time:
                inworld_latency = f", inworld_latency={(time.time() - self._first_send_time) * 1000:.0f}ms"
            print(f"[InworldWS] First chunk at {elapsed:.0f}ms: {len(pcm_bytes)} bytes, {words} words{inworld_latency}")

        # Offset word alignment timestamps for sentence pipelining.
        # Inworld timestamps are absolute from sentence start (reset per flush).
        # We offset by completed sentences' duration so visemes align with
        # the concatenated audio stream.
        if word_alignment and self._completed_sentences_bytes > 0:
            offset_secs = self._completed_sentences_bytes / (self._sample_rate * 2)
            starts = word_alignment.get("wordStartTimeSeconds")
            ends = word_alignment.get("wordEndTimeSeconds")
            if starts:
                word_alignment["wordStartTimeSeconds"] = [t + offset_secs for t in starts]
            if ends:
                word_alignment["wordEndTimeSeconds"] = [t + offset_secs for t in ends]

        if word_alignment and word_alignment.get("words"):
            starts = word_alignment.get("wordStartTimeSeconds", []) or []
            ends = word_alignment.get("wordEndTimeSeconds", []) or []
            first_start = starts[0] if starts else None
            last_end = ends[-1] if ends else None
            print(f"[InworldWS] Chunk timestamps: words={len(word_alignment.get('words', []))} "
                  f"range={first_start if first_start is not None else 'n/a'}-"
                  f"{last_end if last_end is not None else 'n/a'} "
                  f"completed_bytes={self._completed_sentences_bytes} "
                  f"current_sentence_bytes={self._current_sentence_bytes} "
                  f"total_bytes={self.bytes_received}")

        with self._lock:
            handler = self._on_chunk
        if handler:
            try:
                handler(pcm_bytes, word_alignment)
            except Exception as e:
                print(f"[InworldWS] Error in on_chunk handler: {e}")

    def handle_flush_completed(self):
        """Called by receive loop when flushCompleted arrives for this context."""
        # Accumulate completed sentence bytes for timestamp offsetting
        self._completed_sentences_bytes += self._current_sentence_bytes
        self._current_sentence_bytes = 0

        with self._lock:
            handler = self._on_flush
        if handler:
            try:
                handler()
            except Exception as e:
                print(f"[InworldWS] Error in on_flush handler: {e}")

    def handle_async_timestamps(self, word_alignment: dict):
        """Called when async word alignment arrives (ASYNC transport strategy)."""
        # Offset timestamps for sentence pipelining (same as sync path)
        if self._completed_sentences_bytes > 0:
            offset_secs = self._completed_sentences_bytes / (self._sample_rate * 2)
            starts = word_alignment.get("wordStartTimeSeconds")
            ends = word_alignment.get("wordEndTimeSeconds")
            if starts:
                word_alignment["wordStartTimeSeconds"] = [t + offset_secs for t in starts]
            if ends:
                word_alignment["wordEndTimeSeconds"] = [t + offset_secs for t in ends]

        words = len(word_alignment.get("words", []))
        starts = word_alignment.get("wordStartTimeSeconds", []) or []
        ends = word_alignment.get("wordEndTimeSeconds", []) or []
        first_start = starts[0] if starts else None
        last_end = ends[-1] if ends else None
        print(f"[InworldWS] Async timestamps: {words} words "
              f"range={first_start if first_start is not None else 'n/a'}-"
              f"{last_end if last_end is not None else 'n/a'} "
              f"completed_bytes={self._completed_sentences_bytes} "
              f"current_sentence_bytes={self._current_sentence_bytes} "
              f"total_bytes={self.bytes_received}")

        with self._lock:
            handler = self._on_chunk
        if handler:
            try:
                # Send timestamps with empty audio — base.py handles 0-byte chunks
                handler(b'', word_alignment)
            except Exception as e:
                print(f"[InworldWS] Error in async timestamp handler: {e}")

    def handle_error(self, error_msg: str):
        """Called on stream errors."""
        with self._lock:
            handler = self._on_error
        if handler:
            try:
                handler(error_msg)
            except Exception:
                pass

    def log_summary(self):
        """Print synthesis summary stats."""
        if self.chunks_received == 0:
            return
        elapsed = time.time() - self.stream_start_time
        print(f"[InworldWS] ctx={self.context_id}: {self.chunks_received} chunks, "
              f"{self.bytes_received} bytes in {elapsed:.2f}s")
        if len(self.chunk_recv_times) > 1:
            gaps = [self.chunk_recv_times[i] - self.chunk_recv_times[i - 1]
                    for i in range(1, len(self.chunk_recv_times))]
            print(f"[InworldWS] Inter-chunk gaps: min={min(gaps)*1000:.0f}ms, "
                  f"max={max(gaps)*1000:.0f}ms, avg={sum(gaps)/len(gaps)*1000:.0f}ms")


class InworldWebSocket:
    """
    Persistent WebSocket connection to Inworld TTS bidirectional API.

    Creates ephemeral contexts per synthesis call — each call gets a unique
    context ID, closed when done. Thread-safe for concurrent calls.

    Usage:
        ws = InworldWebSocket(auth_header="Basic ...")
        ws.connect()

        # Synchronous synthesis (blocks until flushCompleted)
        ws.synthesize("Hello world", voice_id, model_id, temp, on_chunk)

        # Sentence-level streaming (blocks until all flushes complete)
        ws.synthesize_sentences(sentence_iter, voice_id, model_id, temp, on_chunk)

        ws.disconnect()
    """

    RECV_TIMEOUT = 1.0  # seconds, for shutdown check in receive loop

    def __init__(self, auth_header: str, ws_url: str = None, log: bool = False):
        self._ws_url = ws_url or WS_ENDPOINT
        self._auth_header = auth_header
        self._log = log

        self._ws = None
        self._contexts: Dict[str, InworldWSContext] = {}
        self._context_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._receive_thread: Optional[threading.Thread] = None
        self._connected = False
        self._shutdown = threading.Event()
        self._reconnect_lock = threading.Lock()
        self._generation = 0
        self.last_error = ""

        # Monotonic counter for unique context IDs
        self._next_ctx_id = 0

    @property
    def connected(self) -> bool:
        return self._connected and self._ws is not None

    def connect(self):
        """Connect to WebSocket endpoint."""
        if not WS_AVAILABLE:
            raise ImportError("websocket-client package not installed")

        if self._connected:
            return

        self._shutdown.clear()

        try:
            self._ws = websocket.create_connection(
                self._ws_url,
                header={"Authorization": self._auth_header},
                timeout=15
            )
            self._ws.settimeout(self.RECV_TIMEOUT)
            self._connected = True

            # Start receive loop
            self._receive_thread = threading.Thread(
                target=self._receive_loop, daemon=True,
                name="InworldWS-recv"
            )
            self._receive_thread.start()

            if self._log:
                print(f"[InworldWS] Connected to {self._ws_url}")
        except Exception as e:
            print(f"[InworldWS] Connection failed: {e}")
            self._ws = None
            self._connected = False
            raise

    def disconnect(self):
        """Close all contexts and disconnect."""
        self._shutdown.set()

        # Close all contexts
        with self._context_lock:
            for ctx_id in list(self._contexts.keys()):
                try:
                    self._send_close_context(ctx_id)
                except Exception:
                    pass
            self._contexts.clear()

        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None

        self._connected = False

        if self._receive_thread:
            self._receive_thread.join(timeout=5.0)
            self._receive_thread = None

        if self._log:
            print("[InworldWS] Disconnected")

    def synthesize(self, text: str, voice_id: str, model_id: str,
                   temperature: float, on_chunk: Callable,
                   speaker_id: str = None,
                   sample_rate: int = 48000, speed: float = 1.0,
                   timeout: float = 60.0) -> bool:
        """
        Synthesize text via WebSocket. Blocks until flushCompleted.

        Args:
            text: Text to synthesize
            voice_id: Inworld voice ID
            model_id: Model ID (e.g. "inworld-tts-1.5-max")
            temperature: Sampling temperature
            on_chunk: Callback(pcm_bytes, word_alignment) per audio chunk
            speaker_id: Speaker name for logging
            sample_rate: Audio sample rate
            speed: Speaking rate
            timeout: Max wait time for flush

        Returns:
            True on success, False on error
        """
        self.last_error = ""
        if not self.connected:
            return False

        flush_done = threading.Event()
        error_msg = [None]

        def on_flush():
            flush_done.set()

        def on_error(msg):
            error_msg[0] = msg
            flush_done.set()

        ctx = self._create_new_context(
            voice_id, model_id, temperature, sample_rate, speed
        )

        try:
            ctx.set_handlers(on_chunk=on_chunk, on_flush=on_flush, on_error=on_error,
                            sample_rate=sample_rate)

            # Send text with flush
            try:
                self._send_text(ctx.context_id, text, flush=True)
            except Exception as e:
                print(f"[InworldWS] Send failed: {e}")
                return False

            # Block until flushCompleted
            if not flush_done.wait(timeout=timeout):
                print(f"[InworldWS] Timeout waiting for flush ({timeout}s)")
                return False

            ctx.log_summary()

            if error_msg[0]:
                print(f"[InworldWS] Synthesis error: {error_msg[0]}")
                self.last_error = error_msg[0]
                return False

            return ctx.chunks_received > 0
        finally:
            self._destroy_context(ctx.context_id)

    def synthesize_sentences(self, sentences, voice_id: str, model_id: str,
                             temperature: float, on_chunk: Callable,
                             speaker_id: str = None,
                             sample_rate: int = 48000, speed: float = 1.0,
                             on_sentence_flushed: Callable = None,
                             timeout_per_flush: float = 30.0,
                             abort_check: Callable = None) -> bool:
        """
        Synthesize sentences one at a time via WebSocket.
        Each sentence is sent with flush_context, so audio starts arriving
        before all sentences are submitted. Blocks until all flushes complete.

        Args:
            sentences: Iterable of sentence strings (can be a generator that
                       blocks on LLM streaming)
            voice_id: Inworld voice ID
            model_id: Model ID
            temperature: Sampling temperature
            on_chunk: Callback(pcm_bytes, word_alignment) per audio chunk
            speaker_id: Speaker name for logging
            sample_rate: Audio sample rate
            speed: Speaking rate
            on_sentence_flushed: Optional callback when a sentence's audio is done.
                WARNING: NOT called once per sentence. Inworld batches flushes,
                so 4 sentences may produce only 2 callbacks. Do NOT use this to
                count sentences or index into per-sentence data structures.
            timeout_per_flush: Max wait per sentence flush

        Returns:
            True on success, False on error
        """
        self.last_error = ""
        if not self.connected:
            return False

        error_msg = [None]
        flush_event = threading.Event()
        flush_count = [0]
        chunk_activity = threading.Event()  # Signaled on every audio chunk to reset idle timeout

        def on_flush():
            flush_count[0] += 1
            # WARNING: This fires per Inworld flush response, NOT per sentence.
            # Inworld batches flushes — 4 sentences may only trigger 2 callbacks.
            # Do NOT rely on flush_count matching the number of sentences pushed.
            if on_sentence_flushed:
                on_sentence_flushed()
            elapsed = (time.time() - ctx.stream_start_time) * 1000
            print(f"[InworldWS] Flush completed at {elapsed:.0f}ms "
                  f"(flushes={flush_count[0]}, sent_sentences={sent_count}, "
                  f"bytes_received={ctx.bytes_received}, "
                  f"completed_bytes={ctx._completed_sentences_bytes})")
            flush_event.set()

        def on_error(msg):
            error_msg[0] = msg
            flush_event.set()

        ctx = self._create_new_context(
            voice_id, model_id, temperature, sample_rate, speed
        )

        # Wrap on_chunk to signal activity (resets idle timeout in completion loop)
        def on_chunk_with_activity(pcm_bytes, word_alignment):
            chunk_activity.set()
            if on_chunk:
                on_chunk(pcm_bytes, word_alignment)

        try:
            ctx.set_handlers(on_chunk=on_chunk_with_activity, on_flush=on_flush, on_error=on_error,
                            sample_rate=sample_rate)

            _synth_start = time.time()
            sent_count = 0
            aborted = False
            for sentence in sentences:
                if not sentence or not sentence.strip():
                    continue
                if error_msg[0]:
                    break
                if not self.connected:
                    error_msg[0] = "Connection lost"
                    break
                if abort_check and abort_check():
                    aborted = True
                    print(f"[InworldWS] Aborted before sending sentence #{sent_count + 1}")
                    break

                try:
                    send_t = time.time()
                    self._send_text(ctx.context_id, sentence.strip(), flush=True)
                    sent_count += 1
                    elapsed = (time.time() - _synth_start) * 1000
                    print(f"[InworldWS] Sent sentence #{sent_count} at {elapsed:.0f}ms: \"{sentence.strip()}\"")
                    if sent_count == 1:
                        ctx._first_send_time = send_t  # Track for latency measurement
                except Exception as e:
                    print(f"[InworldWS] Send failed: {e}")
                    error_msg[0] = str(e)
                    break

            elapsed = (time.time() - _synth_start) * 1000
            print(f"[InworldWS] All {sent_count} sentences sent at {elapsed:.0f}ms, waiting for completion...")

            if sent_count == 0 and not aborted:
                print("[InworldWS] No sentences to synthesize")
                return False

            if not aborted:
                idle_timeout = 5.0
                poll_interval = 0.5 if abort_check else idle_timeout
                idle_start = time.time()
                while True:
                    if error_msg[0]:
                        break
                    if flush_count[0] >= sent_count:
                        break
                    if abort_check and abort_check():
                        print(f"[InworldWS] Aborted during flush wait ({flush_count[0]}/{sent_count} flushes)")
                        aborted = True
                        break
                    flush_event.clear()
                    chunk_activity.clear()
                    if not flush_event.wait(timeout=poll_interval):
                        if abort_check and abort_check():
                            print(f"[InworldWS] Aborted during flush wait ({flush_count[0]}/{sent_count} flushes)")
                            aborted = True
                            break
                        if chunk_activity.is_set():
                            chunk_activity.clear()
                            idle_start = time.time()
                            continue
                        if time.time() - idle_start >= idle_timeout:
                            print(f"[InworldWS] Idle timeout ({flush_count[0]}/{sent_count} flushes received, "
                                  f"bytes_received={ctx.bytes_received}, "
                                  f"completed_bytes={ctx._completed_sentences_bytes})")
                            break
                    else:
                        idle_start = time.time()

            print(f"[InworldWS] Synthesis {'aborted' if aborted else 'complete'} "
                  f"({flush_count[0]} flushes, {sent_count} sentences, "
                  f"bytes_received={ctx.bytes_received}, "
                  f"completed_bytes={ctx._completed_sentences_bytes})")
            ctx.log_summary()

            if aborted:
                return False

            if error_msg[0]:
                print(f"[InworldWS] Synthesis error: {error_msg[0]}")
                self.last_error = error_msg[0]
                return False

            return ctx.chunks_received > 0
        finally:
            self._destroy_context(ctx.context_id)

    def synthesize_sentences_multi_voice(self, sentences, default_voice_id: str,
                                          model_id: str, temperature: float,
                                          on_chunk: Callable,
                                          speaker_id: str = None,
                                          sample_rate: int = 48000,
                                          speed: float = 1.0,
                                          on_sentence_flushed: Callable = None,
                                          abort_check: Callable = None,
                                          on_voice_switch: Callable = None) -> bool:
        """
        Synthesize sentences with per-sentence voice selection using multiple WS contexts.
        Used for narration: character voice for dialogue, narrator voice for narration.

        Sentences are (text, voice_id) tuples. Creates contexts lazily per unique voice_id.
        At voice boundaries, flushes the current context and waits for completion before
        switching to ensure correct audio order. Within same-voice runs, pipelines normally.
        """
        self.last_error = ""
        if not self.connected:
            return False

        contexts = {}  # voice_id -> InworldWSContext
        error_msg = [None]
        total_chunks = [0]
        global_pcm_bytes = [0]  # Total PCM bytes across ALL contexts (for timestamp offsetting)

        # Per-context flush tracking
        flush_counts = {}   # voice_id -> [sent, received]
        flush_events = {}   # voice_id -> threading.Event
        chunk_activity = threading.Event()

        def _make_on_flush(vid):
            def on_flush():
                flush_counts[vid][1] += 1
                # WARNING: This fires per Inworld flush response, NOT per sentence.
                # Inworld batches flushes — 4 sentences may only trigger 2 callbacks.
                if on_sentence_flushed:
                    on_sentence_flushed()
                flush_events[vid].set()
            return on_flush

        def _make_on_error(vid):
            def on_error(msg):
                error_msg[0] = msg
                flush_events[vid].set()
            return on_error

        def _make_on_chunk(vid):
            def on_chunk_wrapper(pcm_bytes, word_alignment):
                total_chunks[0] += 1
                if pcm_bytes:
                    global_pcm_bytes[0] += len(pcm_bytes)
                chunk_activity.set()
                if on_chunk:
                    on_chunk(pcm_bytes, word_alignment)
            return on_chunk_wrapper

        def _get_or_create_context(vid):
            if vid in contexts:
                return contexts[vid]
            ctx = self._create_new_context(vid, model_id, temperature, sample_rate, speed)
            contexts[vid] = ctx
            flush_counts[vid] = [0, 0]  # [sent, received]
            flush_events[vid] = threading.Event()
            ctx.set_handlers(
                on_chunk=_make_on_chunk(vid),
                on_flush=_make_on_flush(vid),
                on_error=_make_on_error(vid),
                sample_rate=sample_rate
            )
            return ctx

        def _inject_silence(seconds: float):
            """Inject explicit PCM silence and keep global timing/bytes aligned."""
            if seconds <= 0:
                return
            # Inworld streams 16-bit mono PCM.
            silence_samples = int(sample_rate * seconds)
            silence_bytes = bytes(max(0, silence_samples) * 2)
            if not silence_bytes:
                return
            total_chunks[0] += 1
            global_pcm_bytes[0] += len(silence_bytes)
            chunk_activity.set()
            if on_chunk:
                on_chunk(silence_bytes, None)

        def _wait_for_flushes(vid, timeout=30.0):
            """Wait for all sent flushes on a context to complete."""
            sent, received = flush_counts[vid]
            if received >= sent:
                return True
            idle_timeout = 5.0
            poll_interval = 0.5 if abort_check else idle_timeout
            idle_start = time.time()
            while received < sent:
                if error_msg[0]:
                    return False
                if abort_check and abort_check():
                    return False
                flush_events[vid].clear()
                chunk_activity.clear()
                if not flush_events[vid].wait(timeout=poll_interval):
                    if abort_check and abort_check():
                        return False
                    if chunk_activity.is_set():
                        idle_start = time.time()
                        continue
                    if time.time() - idle_start >= idle_timeout:
                        print(f"[InworldWS] Multi-voice idle timeout for {vid}")
                        return False
                else:
                    idle_start = time.time()
                received = flush_counts[vid][1]
            return True

        try:
            sent_count = 0
            prev_vid = None
            _synth_start = time.time()

            for item in sentences:
                if isinstance(item, tuple):
                    text, vid = item
                else:
                    text, vid = item, default_voice_id

                if not text or not text.strip():
                    continue
                if error_msg[0]:
                    break
                if not self.connected:
                    error_msg[0] = "Connection lost"
                    break
                if abort_check and abort_check():
                    break

                # At voice boundary: wait for previous context to flush
                if prev_vid is not None and vid != prev_vid:
                    if not _wait_for_flushes(prev_vid):
                        break
                    # Add a short natural pause before narration starts.
                    # Must happen BEFORE on_voice_switch so sentence start_bytes
                    # include the injected gap and all downstream timing stays aligned.
                    if vid != default_voice_id:
                        pause_s = random.uniform(0.2, 0.5)
                        _inject_silence(pause_s)
                        print(f"[InworldWS] Injected narration pause: {pause_s:.3f}s")
                    # Notify caller of voice switch with accurate byte position.
                    # sent_count == index of the sentence about to be sent (0-based),
                    # and global_pcm_bytes is accurate because _wait_for_flushes blocked
                    # until all audio from the previous voice arrived.
                    if on_voice_switch:
                        on_voice_switch(global_pcm_bytes[0], sent_count)
                    # Sync global byte offset into the next context so word timestamps
                    # are offset correctly relative to the combined audio stream
                    next_ctx = _get_or_create_context(vid)
                    next_ctx._completed_sentences_bytes = global_pcm_bytes[0]
                    next_ctx._current_sentence_bytes = 0

                ctx = _get_or_create_context(vid)
                try:
                    send_t = time.time()
                    self._send_text(ctx.context_id, text.strip(), flush=True)
                    flush_counts[vid][0] += 1
                    sent_count += 1
                    if sent_count == 1:
                        ctx._first_send_time = send_t
                    elapsed = (time.time() - _synth_start) * 1000
                    print(f"[InworldWS] Multi-voice sent #{sent_count} [{vid[:20]}] at {elapsed:.0f}ms: \"{text.strip()}\"")
                except Exception as e:
                    print(f"[InworldWS] Multi-voice send failed: {e}")
                    error_msg[0] = str(e)
                    break

                prev_vid = vid

            # Wait for all contexts to complete
            for vid in flush_counts:
                if not _wait_for_flushes(vid):
                    break

            elapsed = (time.time() - _synth_start) * 1000
            print(f"[InworldWS] Multi-voice complete: {sent_count} sentences, "
                  f"{len(contexts)} contexts, {elapsed:.0f}ms")

            if error_msg[0]:
                print(f"[InworldWS] Multi-voice error: {error_msg[0]}")
                self.last_error = error_msg[0]
                return False

            return total_chunks[0] > 0

        finally:
            for vid, ctx in contexts.items():
                ctx.log_summary()
                self._destroy_context(ctx.context_id)

    def close_all_contexts(self):
        """Close all contexts (e.g. on settings change)."""
        with self._context_lock:
            for ctx_id in list(self._contexts.keys()):
                try:
                    self._send_close_context(ctx_id)
                except Exception:
                    pass
            self._contexts.clear()

    # ─── Internal helpers ──────────────────────────────────────────────

    def _create_new_context(self, voice_id: str, model_id: str,
                             temperature: float, sample_rate: int,
                             speed: float) -> InworldWSContext:
        """Create a fresh ephemeral context. Never reuses — unique ID each time."""
        safe = voice_id.replace("__", "-").replace("/", "-")

        with self._context_lock:
            self._next_ctx_id += 1
            context_id = f"ctx-{safe}-{self._next_ctx_id}"

            config = {
                "voiceId": voice_id,
                "modelId": model_id,
                "autoMode": True,
                "bufferCharThreshold": 100,
                "maxBufferDelayMs": 0,
                "timestampType": "WORD",
                "timestampTransportStrategy": "ASYNC",
                "temperature": temperature,
                "applyTextNormalization": "OFF",
                "audioConfig": {
                    "audioEncoding": "LINEAR16",
                    "sampleRateHertz": sample_rate,
                    "speakingRate": speed
                }
            }

            ctx = InworldWSContext(context_id, voice_id, config)
            self._contexts[context_id] = ctx

        # Send create context message (outside lock to avoid deadlock with recv)
        create_start = time.time()
        try:
            self._send_create_context(context_id, config)
        except Exception:
            # Clean up leaked context if send fails
            with self._context_lock:
                self._contexts.pop(context_id, None)
            raise

        if not ctx.created.wait(timeout=10.0):
            print(f"[InworldWS] Warning: context {context_id} creation not confirmed")
        else:
            create_ms = (time.time() - create_start) * 1000
            print(f"[InworldWS] Context {context_id} created in {create_ms:.0f}ms")

        return ctx

    def _destroy_context(self, context_id: str):
        """Remove context from tracking and close on server."""
        with self._context_lock:
            self._contexts.pop(context_id, None)
        try:
            self._send_close_context(context_id)
        except Exception:
            pass

    def _send_create_context(self, context_id: str, config: dict):
        msg = {
            "contextId": context_id,
            "create": config
        }
        self._send_json(msg, _allow_reconnect=False)
        if self._log:
            print(f"[InworldWS] Creating context {context_id} voice={config['voiceId']}")

    def _send_close_context(self, context_id: str):
        msg = {
            "contextId": context_id,
            "close_context": {}
        }
        self._send_json(msg, _allow_reconnect=False)
        if self._log:
            print(f"[InworldWS] Closing context {context_id}")

    def _send_text(self, context_id: str, text: str, flush: bool = True):
        msg = {
            "contextId": context_id,
            "send_text": {
                "text": text
            }
        }
        if flush:
            msg["send_text"]["flush_context"] = {}
        self._send_json(msg)

    def _send_json(self, msg: dict, _allow_reconnect: bool = True):
        """Send JSON message. Reconnects and retries once on failure."""
        if not self._ws:
            if _allow_reconnect and not self._shutdown.is_set():
                print("[InworldWS] Not connected, attempting reconnect...")
                if self._reconnect():
                    return self._send_json(msg, _allow_reconnect=False)
            raise ConnectionError("WebSocket not connected")
        data = json.dumps(msg)
        try:
            with self._send_lock:
                self._ws.send(data)
        except Exception as e:
            if not _allow_reconnect or self._shutdown.is_set():
                raise
            print(f"[InworldWS] Send error ({e}), reconnecting...")
            self._connected = False
            if self._reconnect():
                try:
                    with self._send_lock:
                        self._ws.send(data)
                except Exception as e2:
                    raise ConnectionError(f"Send failed after reconnect: {e2}") from e2
            else:
                raise ConnectionError(f"Send failed and reconnect failed: {e}") from e

    # ─── Receive loop ──────────────────────────────────────────────────

    def _receive_loop(self):
        """Background thread receiving and dispatching WebSocket messages."""
        my_gen = self._generation
        while not self._shutdown.is_set():
            if self._generation != my_gen:
                return  # Connection was replaced by reconnection
            try:
                ws = self._ws
                if not ws:
                    break
                raw = ws.recv()
                if not raw:
                    continue
                self._dispatch_message(raw)
            except websocket.WebSocketTimeoutException:
                continue  # timeout is expected, lets us check shutdown flag
            except websocket.WebSocketConnectionClosedException:
                if self._generation != my_gen or self._shutdown.is_set():
                    return  # Already reconnected or shutting down
                print("[InworldWS] Connection lost, reconnecting...")
                self._connected = False
                self._reconnect()
                return
            except Exception as e:
                if self._generation != my_gen or self._shutdown.is_set():
                    return  # Already reconnected or shutting down
                print(f"[InworldWS] Receive error: {e}, reconnecting...")
                self._connected = False
                self._reconnect()
                return

    def _dispatch_message(self, raw: str):
        """Parse and dispatch a received WebSocket message."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            print(f"[InworldWS] Invalid JSON received")
            return

        result = msg.get("result")
        if not result:
            # Check for top-level error
            error = msg.get("error")
            if error:
                print(f"[InworldWS] Server error: {error.get('message', error)}")
            return

        context_id = result.get("contextId", "")

        # Check for status errors
        status = result.get("status")
        if status and status.get("code", 0) != 0:
            err_msg = f"code={status['code']}: {status.get('message', '')}"
            print(f"[InworldWS] Error in {context_id}: {err_msg}")
            with self._context_lock:
                ctx = self._contexts.get(context_id)
            if ctx:
                ctx.handle_error(err_msg)
            return

        # Context created
        if "contextCreated" in result:
            with self._context_lock:
                ctx = self._contexts.get(context_id)
            if ctx:
                ctx.created.set()
            if self._log:
                print(f"[InworldWS] Context created: {context_id}")
            return

        # Audio chunk
        audio_chunk = result.get("audioChunk")
        if audio_chunk:
            self._handle_audio_chunk(context_id, audio_chunk)
            return

        # Flush completed
        if "flushCompleted" in result:
            with self._context_lock:
                ctx = self._contexts.get(context_id)
            if ctx:
                ctx.handle_flush_completed()
            if self._log:
                print(f"[InworldWS] Flush completed: {context_id}")
            return

        # Async timestamp info (when timestampTransportStrategy=ASYNC)
        ts_info = result.get("timestampInfo")
        if ts_info:
            word_alignment = ts_info.get("wordAlignment")
            if word_alignment:
                with self._context_lock:
                    ctx = self._contexts.get(context_id)
                if ctx:
                    ctx.handle_async_timestamps(word_alignment)
            else:
                print(f"[InworldWS] Async timestamp (non-word): {list(ts_info.keys())}")
            return

        # Context closed (server confirms close — context already removed by _destroy_context)
        if "contextClosed" in result:
            with self._context_lock:
                self._contexts.pop(context_id, None)
            if self._log:
                print(f"[InworldWS] Context closed: {context_id}")
            return

    def _handle_audio_chunk(self, context_id: str, audio_chunk: dict):
        """Process an audioChunk message."""
        audio_b64 = audio_chunk.get("audioContent", "")
        word_alignment = None
        ts_info = audio_chunk.get("timestampInfo")
        if ts_info:
            word_alignment = ts_info.get("wordAlignment")

        if not audio_b64:
            # ASYNC transport: timestamps may arrive as audioChunk with no audio
            if word_alignment:
                with self._context_lock:
                    ctx = self._contexts.get(context_id)
                if ctx:
                    ctx.handle_async_timestamps(word_alignment)
            return

        try:
            audio_bytes = base64.b64decode(audio_b64)
        except Exception as e:
            print(f"[InworldWS] Base64 decode error: {e}")
            return

        # Strip WAV header if present
        if audio_bytes[:4] == b'RIFF':
            data_pos = audio_bytes.find(b'data')
            if data_pos != -1:
                pcm_bytes = audio_bytes[data_pos + 8:]
            else:
                pcm_bytes = audio_bytes[44:]
        else:
            pcm_bytes = audio_bytes

        # Log emote detection (same as HTTP path)
        if word_alignment:
            words = word_alignment.get("words", [])
            starts = word_alignment.get("wordStartTimeSeconds", [])
            ends = word_alignment.get("wordEndTimeSeconds", [])
            for i, word in enumerate(words):
                if (word.startswith('[') or word.startswith('<') or word.startswith('*') or
                        word.endswith(']') or word.endswith('>') or word.endswith('*')):
                    start_t = starts[i] if i < len(starts) else -1
                    end_t = ends[i] if i < len(ends) else -1
                    print(f"[InworldWS] Emote detected: '{word}' at {start_t:.3f}s-{end_t:.3f}s")

        with self._context_lock:
            ctx = self._contexts.get(context_id)

        if ctx:
            ctx.handle_audio_chunk(pcm_bytes, word_alignment)

    def _reconnect(self) -> bool:
        """Attempt to reconnect. Thread-safe, deduplicates concurrent calls.

        Returns True if reconnected successfully, False otherwise.
        Starts a new receive thread on success.
        """
        with self._reconnect_lock:
            # Another thread may have already reconnected
            if self._connected and self._ws:
                return True
            return self._do_reconnect()

    def _do_reconnect(self) -> bool:
        """Internal reconnection logic. Caller must hold _reconnect_lock."""
        max_attempts = 3
        backoff = 1.0

        # Close old socket to unblock any pending recv()
        old_ws = self._ws
        self._ws = None
        self._connected = False
        if old_ws:
            try:
                old_ws.close()
            except Exception:
                pass

        for attempt in range(1, max_attempts + 1):
            if self._shutdown.is_set():
                return False
            print(f"[InworldWS] Reconnect attempt {attempt}/{max_attempts}...")
            time.sleep(backoff)
            backoff *= 2

            try:
                self._ws = websocket.create_connection(
                    self._ws_url,
                    header={"Authorization": self._auth_header},
                    timeout=15
                )
                self._ws.settimeout(self.RECV_TIMEOUT)
                self._connected = True
                self._generation += 1
                print(f"[InworldWS] Reconnected (gen={self._generation})")

                # Recreate contexts that were active
                with self._context_lock:
                    for ctx_id, ctx in self._contexts.items():
                        ctx.created = threading.Event()
                        try:
                            self._send_create_context(ctx_id, ctx.config)
                        except Exception as e:
                            print(f"[InworldWS] Failed to recreate context {ctx_id}: {e}")

                # Start new receive thread
                self._receive_thread = threading.Thread(
                    target=self._receive_loop, daemon=True,
                    name="InworldWS-recv"
                )
                self._receive_thread.start()

                return True
            except Exception as e:
                print(f"[InworldWS] Reconnect failed: {e}")

        print("[InworldWS] All reconnect attempts failed")
        self._connected = False
        return False
