"""
Conversation state management for Sonorus.
Handles conversation flow, queue management, and pre-buffering.
"""

import threading
import time


class ConversationState:
    """State machine for multi-NPC conversations with interruption support"""

    def __init__(self):
        self.state = "idle"  # idle | processing | playing
        self.queue = []  # [{id, speaker, target, full_text, segments, current_segment, status}]
        self.current_index = 1  # Which queue item is playing (1-indexed for Lua compatibility)
        self.turn_count = 0  # How many NPC responses this conversation
        self.max_turns = 6  # Hard limit (configurable)
        self.pending_player_input = None  # Player interrupted - handle after current
        self.interrupted = False  # Flag to stop interjection chain
        self.pending_history_entries = []  # History entries waiting for audio to complete
        # Memory system: track conversation participants for chapter evaluation at end
        self.conversation_speakers = set()  # NPC IDs who spoke this conversation
        self.conversation_location = None  # Location where conversation started
        self.conversation_context = None  # Game context snapshot for chapter eval
        # Director/Prompt mode: NPC-to-NPC conversations without player involvement
        self.prompt_mode = False  # True if this conversation was started from a director prompt
        self.prompt_participants = []  # List of NPC IDs participating in director-prompted conversation
        self.prompt_scenario = ""  # Scenario/topic from the director prompt
        self.prompt_include_player = False  # True if player was mentioned in the prompt

    def reset(self):
        """Reset for new conversation"""
        self.state = "idle"
        self.queue = []
        self.current_index = 1  # 1-indexed for Lua compatibility
        self.turn_count = 0
        self.pending_player_input = None
        self.interrupted = False
        self.pending_history_entries = []  # Discard uncommitted entries
        self.conversation_speakers = set()
        self.conversation_location = None
        self.conversation_context = None
        # Reset director/prompt mode state
        self.prompt_mode = False
        self.prompt_participants = []
        self.prompt_scenario = ""
        self.prompt_include_player = False
        return self

    def track_speaker(self, speaker_id: str, location: str = None, context: dict = None):
        """Track an NPC who spoke in this conversation."""
        self.conversation_speakers.add(speaker_id)
        # Store location from first speaker (conversation start)
        if self.conversation_location is None and location:
            self.conversation_location = location
        # Store context snapshot (update each turn for freshest data)
        if context:
            self.conversation_context = context

    def add_pending_history(self, entry):
        """Add a history entry that will be committed when audio completes"""
        self.pending_history_entries.append(entry)

    def commit_pending_history(self):
        """Commit all pending history entries directly to database.

        Uses O(1) append_entry() for each entry instead of O(n) replace-all.
        Updates timestamps to reflect when audio actually played (not when generated).
        """
        if self.pending_history_entries:
            from .dialogue_db import append_entry
            now = int(time.time())
            for entry in self.pending_history_entries:
                entry["timestamp"] = now  # Update to when audio finished
                append_entry(entry)  # O(1) per entry
            count = len(self.pending_history_entries)
            self.pending_history_entries = []
            return count
        return 0

    def add_to_queue(self, speaker, target, full_text, segments=None, speaker_id=None):
        """Add a response to the queue with optional sentence segments

        Args:
            speaker: Display name (e.g., "Nellie Oggspire") for UI/prompts
            target: Who they're speaking to
            full_text: The dialogue text
            segments: Optional sentence segments for chunked TTS
            speaker_id: Internal ID (e.g., "NellieOggspire") for actor lookups
        """
        msg_id = f"msg_{len(self.queue):03d}"

        if segments is None:
            # Single segment (no chunking)
            segments = [{
                "text": full_text,
                "audio_file": None,
                "status": "pending"
            }]

        self.queue.append({
            "id": msg_id,
            "speaker": speaker,
            "speakerId": speaker_id or speaker.replace(" ", ""),  # Fallback: remove spaces
            "target": target,
            "full_text": full_text,
            "segments": segments,
            "current_segment": 1,  # 1-indexed for Lua compatibility
            "status": "pending"
        })
        return msg_id


class PreBuffer:
    """
    Manages one-ahead TTS buffering for smooth conversation flow.
    Buffers the next NPC response while current audio is playing.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.state = "idle"  # idle | buffering | ready
        self.ready_event = threading.Event()
        self.abort_flag = False
        # Buffered data
        self.speaker = None
        self.speaker_id = None
        self.target = None
        self.text = None
        self.tts_stream = None
        self.word_timings = []
        self.visemes = []  # Pre-computed visemes with gap filling
        self.sentence_boundaries = []  # Subtitle/narration timing boundaries
        self.positions = None  # Initial 3D positions from turn_ready
        self.turn_id = None  # Turn ID for lipsync_start (prevents race condition)
        self.reverb_auxbus = None  # Reverb preset name from game
        self.reverb_send = 1.0  # Reverb wet/dry mix

    def start_buffering(self, speaker, speaker_id, target, text, positions=None, turn_id=None,
                        reverb_auxbus=None, reverb_send=1.0):
        """Begin buffering a new response."""
        with self.lock:
            self._reset_unlocked()
            self.state = "buffering"
            self.abort_flag = False
            self.ready_event.clear()
            self.speaker = speaker
            self.speaker_id = speaker_id
            self.target = target
            self.text = text
            self.positions = positions  # Store initial 3D positions
            self.turn_id = turn_id  # Store turn_id to use at playback time
            self.reverb_auxbus = reverb_auxbus  # Store reverb preset
            self.reverb_send = reverb_send  # Store reverb send level
        print(f"[PreBuffer] Started buffering: {speaker}")

    def mark_ready(self, tts_stream, word_timings, visemes=None, sentence_boundaries=None):
        """Mark buffer as ready with downloaded TTS and pre-computed visemes."""
        with self.lock:
            if self.abort_flag:
                if tts_stream:
                    tts_stream.clean_up()
                print(f"[PreBuffer] Discarded (aborted)")
                return False
            self.tts_stream = tts_stream
            self.word_timings = word_timings
            self.visemes = visemes or []
            self.sentence_boundaries = sentence_boundaries or []
            self.state = "ready"
            self.ready_event.set()
        viseme_count = len(self.visemes)
        print(f"[PreBuffer] Ready: {self.speaker} ({viseme_count} visemes, {len(self.sentence_boundaries)} boundaries)")
        return True

    def consume(self):
        """Get buffered data and reset to idle. Returns dict or None."""
        with self.lock:
            if self.state != "ready":
                return None
            data = {
                "speaker": self.speaker,
                "speaker_id": self.speaker_id,
                "target": self.target,
                "text": self.text,
                "tts_stream": self.tts_stream,
                "word_timings": self.word_timings,
                "visemes": self.visemes,  # Pre-computed visemes with gap filling
                "sentence_boundaries": self.sentence_boundaries,
                "positions": self.positions,  # Include initial 3D positions
                "turn_id": self.turn_id,  # Include turn_id for lipsync_start
                "reverb_auxbus": self.reverb_auxbus,  # Reverb preset name
                "reverb_send": self.reverb_send  # Reverb wet/dry mix
            }
            # Don't cleanup tts_stream - caller owns it now
            self.tts_stream = None
            self._reset_unlocked()
        print(f"[PreBuffer] Consumed: {data['speaker']} ({len(data['visemes'])} visemes)")
        return data

    def abort(self):
        """Abort any in-progress buffering."""
        with self.lock:
            if self.state != "idle":
                print(f"[PreBuffer] Aborting (was {self.state})")
            self.abort_flag = True
            if self.tts_stream:
                self.tts_stream.clean_up()
            self._reset_unlocked()

    def _reset_unlocked(self):
        """Reset state (must be called with lock held)."""
        self.speaker = None
        self.speaker_id = None
        self.target = None
        self.text = None
        self.tts_stream = None
        self.word_timings = []
        self.visemes = []
        self.sentence_boundaries = []
        self.positions = None
        self.turn_id = None
        self.state = "idle"
