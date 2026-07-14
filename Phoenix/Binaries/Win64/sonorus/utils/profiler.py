"""
Simple profiler for tracing timing of operations.

Usage:
    from utils.profiler import Profiler

    prof = Profiler.get("my_operation")
    prof.start("operation_name")
    prof.mark("step 1 done")
    prof.mark("step 2 done")
    prof.summary()

Or use the default instance:
    from utils.profiler import profiler
    profiler.start("my_operation")
    ...

Dev mode is controlled by settings.json -> dev.enabled
"""

import time
import datetime
import threading

from .settings import is_dev_mode, dev_print


class Profiler:
    """
    Simple profiler to trace timing of operations.

    Thread-safe and supports multiple named instances.
    """
    _instances = {}
    _lock = threading.Lock()

    def __init__(self, name="default"):
        self.name = name
        self.start_time = None
        self.events = []
        self._event_lock = threading.Lock()

    @classmethod
    def get(cls, name="default"):
        """Get or create a named profiler instance."""
        with cls._lock:
            if name not in cls._instances:
                cls._instances[name] = cls(name)
            return cls._instances[name]

    def start(self, label=None):
        """Start a new profiling session."""
        if not is_dev_mode():
            return
        with self._event_lock:
            self.start_time = time.perf_counter()
            self.events = []
            start_label = f"[START] {label}" if label else f"[START] {self.name}"
            self._record(start_label)

    def mark(self, label):
        """Record a timing mark."""
        if not is_dev_mode() or self.start_time is None:
            return
        with self._event_lock:
            self._record(label)

    def _record(self, label):
        """Internal: record event (must hold lock)."""
        elapsed = (time.perf_counter() - self.start_time) * 1000
        wall = datetime.datetime.now()
        self.events.append((elapsed, label, wall))
        wall_str = wall.strftime("%H:%M:%S.%f")[:-3]
        dev_print(f"[PROFILE:{self.name}] +{elapsed:7.1f}ms {wall_str} | {label}")

    def summary(self):
        """Print a summary of all recorded events with deltas."""
        if not is_dev_mode():
            return
        with self._event_lock:
            if not self.events:
                return
            dev_print("\n" + "=" * 80)
            dev_print(f"[PROFILE:{self.name}] Timeline Summary:")
            dev_print("=" * 80)
            prev = 0
            for event in self.events:
                elapsed, label = event[0], event[1]
                wall = event[2] if len(event) > 2 else None
                delta = elapsed - prev
                wall_str = wall.strftime("%H:%M:%S.%f")[:-3] if wall else "??:??:??.???"
                dev_print(f"  +{elapsed:7.1f}ms ({delta:6.1f}ms) {wall_str} | {label}")
                prev = elapsed
            total = self.events[-1][0] if self.events else 0
            dev_print("-" * 80)
            dev_print(f"  Total: {total:.1f}ms")
            dev_print("=" * 80 + "\n")

    def reset(self):
        """Reset the profiler state."""
        with self._event_lock:
            self.start_time = None
            self.events = []

    def elapsed(self):
        """Get elapsed time in ms since start, or None if not started."""
        if self.start_time is None:
            return None
        return (time.perf_counter() - self.start_time) * 1000

    def _get_duration_unlocked(self, start_label: str, end_label: str) -> float:
        """
        Get duration between two marks by label substring matching.
        Returns duration in ms, or 0 if marks not found.
        NOTE: Caller must hold _event_lock.
        """
        start_time = None
        end_time = None
        for event in self.events:
            elapsed, label = event[0], event[1]
            if start_label in label and start_time is None:
                start_time = elapsed
            if end_label in label:
                end_time = elapsed
        if start_time is not None and end_time is not None:
            return end_time - start_time
        return 0

    def get_duration(self, start_label: str, end_label: str) -> float:
        """
        Get duration between two marks by label substring matching.
        Returns duration in ms, or 0 if marks not found.
        """
        with self._event_lock:
            return self._get_duration_unlocked(start_label, end_label)

    def conversation_summary(self):
        """
        Print a quick status after LLM response.
        The full breakdown is printed by print_time_to_audio() when audio starts.
        """
        if not is_dev_mode():
            return
        with self._event_lock:
            if not self.events:
                return

            # Get time to LLM response
            llm_done_time = None
            for event in self.events:
                elapsed, label = event[0], event[1]
                if "llm_response done" in label:
                    llm_done_time = elapsed
                    break

            if llm_done_time:
                print(f"[PROFILE] LLM response ready at {llm_done_time:.0f}ms - starting TTS...")

    def print_time_to_audio(self):
        """
        Print the total time from conversation start to NPC audio playback.
        Called from playback coordinator when audio actually starts.
        Shows full breakdown since all marks are now available.
        """
        if not is_dev_mode():
            return
        with self._event_lock:
            # Find npc_audio_start timestamp
            audio_start_time = None
            audio_start_wall = None
            for event in self.events:
                elapsed, label = event[0], event[1]
                wall = event[2] if len(event) > 2 else None
                if "npc_audio_start" in label:
                    audio_start_time = elapsed
                    audio_start_wall = wall
                    break

            if audio_start_time is None:
                return

            # Get all stage durations (support both streaming and non-streaming mark names)
            context_refresh = self._get_duration_unlocked("context_refresh start", "context_refresh done")
            target_sel = self._get_duration_unlocked("target_selection start", "target_selection done")
            search_intent = self._get_duration_unlocked("search_intent start", "search_intent done")
            graph_lookup = self._get_duration_unlocked("graph_lookup start", "graph_lookup done")
            memory_total = self._get_duration_unlocked("memory_ops start", "memory_ops done")
            # LLM: streaming path uses "llm_stream", non-streaming uses "llm_response"
            llm_response = self._get_duration_unlocked("llm_response start", "llm_response done")
            llm_stream = self._get_duration_unlocked("llm_stream start", "llm_stream done")
            llm_total = llm_stream if llm_stream > 0 else llm_response
            is_streaming = llm_stream > 0
            # TTS first chunk (streaming) or buffer ready (non-streaming)
            tts_first_chunk = self._get_duration_unlocked("llm_stream start", "tts_first_chunk")
            tts_buffer = self._get_duration_unlocked("llm_response done", "tts_buffer_ready")
            lua_handshake = self._get_duration_unlocked("tts_buffer_ready", "npc_audio_start")
            stream_setup = self._get_duration_unlocked("tts_first_chunk", "npc_audio_start")

            # Build wall-clock lookup from events
            wall_lookup = {}
            for event in self.events:
                elapsed, label = event[0], event[1]
                wall = event[2] if len(event) > 2 else None
                if wall:
                    wall_lookup[label] = wall

            def _wall_for(label_substr):
                """Find wall-clock time for first event matching substring."""
                for event in self.events:
                    if label_substr in event[1] and len(event) > 2 and event[2]:
                        return event[2].strftime("%H:%M:%S.%f")[:-3]
                return ""

            print(f"\n{'='*75}")
            print("  CONVERSATION TURN PROFILING (Complete)")
            print("=" * 75)
            print(f"  {'Stage':<35} {'Time':>10} {'Cumul.':>10}  {'Wall':>12}")
            print("-" * 75)

            cumulative = 0

            if context_refresh > 0:
                cumulative += context_refresh
                print(f"  {'1. Game Context Refresh':<35} {context_refresh:>9.0f}ms {cumulative:>9.0f}ms  {_wall_for('context_refresh done'):>12}")

            if target_sel > 0:
                cumulative += target_sel
                print(f"  {'2. Target Selection (LLM)':<35} {target_sel:>9.0f}ms {cumulative:>9.0f}ms  {_wall_for('target_selection done'):>12}")

            if memory_total > 0:
                print(f"  {'3. Memory Operations':<35} {memory_total:>9.0f}ms")
                if search_intent > 0:
                    print(f"     {'└─ Search Intent (LLM)':<32} {search_intent:>9.0f}ms")
                if graph_lookup > 0:
                    print(f"     {'└─ Graph Lookup':<32} {graph_lookup:>9.0f}ms")
                cumulative += memory_total

            if is_streaming:
                # Streaming path: LLM + TTS run in parallel
                if llm_total > 0:
                    cumulative += llm_total
                    print(f"  {'4. LLM Stream + TTS (parallel)':<35} {llm_total:>9.0f}ms {cumulative:>9.0f}ms  {_wall_for('llm_stream done'):>12}")
                if tts_first_chunk > 0:
                    print(f"     {'└─ First audio chunk':<32} {tts_first_chunk:>9.0f}ms            {_wall_for('tts_first_chunk'):>12}")
                if stream_setup > 0:
                    cumulative += stream_setup
                    print(f"  {'5. Play Turn + Setup':<35} {stream_setup:>9.0f}ms {cumulative:>9.0f}ms  {_wall_for('npc_audio_start'):>12}")
                    # Show sub-steps if available
                    setup_wait = self._get_duration_unlocked("tts_first_chunk", "setup_received")
                    viseme_gen = self._get_duration_unlocked("setup_received", "viseme_gen_done")
                    prev_turn_wait = self._get_duration_unlocked("waiting_prev_turn", "prev_turn_done")
                    lipsync_send = self._get_duration_unlocked("prev_turn_done", "lipsync_sent")
                    lipsync_ack = self._get_duration_unlocked("lipsync_sent", "npc_audio_start")
                    if setup_wait > 0:
                        print(f"     {'└─ Wait for setup_event':<32} {setup_wait:>9.0f}ms            {_wall_for('setup_received'):>12}")
                    if viseme_gen > 0:
                        print(f"     {'└─ Viseme generation':<32} {viseme_gen:>9.0f}ms            {_wall_for('viseme_gen_done'):>12}")
                    if prev_turn_wait > 0:
                        print(f"     {'└─ Wait prev turn done':<32} {prev_turn_wait:>9.0f}ms            {_wall_for('prev_turn_done'):>12}")
                    if lipsync_send > 0:
                        print(f"     {'└─ Lipsync start send':<32} {lipsync_send:>9.0f}ms            {_wall_for('lipsync_sent'):>12}")
                    if lipsync_ack > 0:
                        print(f"     {'└─ Lipsync ack wait':<32} {lipsync_ack:>9.0f}ms            {_wall_for('npc_audio_start'):>12}")
            else:
                # Non-streaming path
                if llm_total > 0:
                    cumulative += llm_total
                    print(f"  {'4. AI Response (LLM)':<35} {llm_total:>9.0f}ms {cumulative:>9.0f}ms  {_wall_for('llm_response done'):>12}")
                if tts_buffer > 0:
                    cumulative += tts_buffer
                    print(f"  {'5. TTS Synthesis + Buffer':<35} {tts_buffer:>9.0f}ms {cumulative:>9.0f}ms  {_wall_for('tts_buffer_ready'):>12}")
                if lua_handshake > 0:
                    cumulative += lua_handshake
                    print(f"  {'6. Lua Handshake + Setup':<35} {lua_handshake:>9.0f}ms {cumulative:>9.0f}ms  {_wall_for('npc_audio_start'):>12}")

            wall_str = audio_start_wall.strftime("%H:%M:%S.%f")[:-3] if audio_start_wall else ""
            print("-" * 75)
            print(f"  {'TOTAL TIME TO FIRST AUDIO':<35} {'':<10} {audio_start_time:>9.0f}ms  {wall_str:>12}")
            print("=" * 75 + "\n")


# Default profiler instance for convenience
profiler = Profiler.get("default")
