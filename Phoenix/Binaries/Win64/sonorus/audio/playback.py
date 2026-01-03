"""
Playback Coordinator

Synchronizes TTS audio playback with lipsync visemes.
Handles:
- Per-turn viseme accumulation during pre-buffering
- Handshake with Lua before audio starts (lipsync_start → lipsync_ready)
- Continuous viseme streaming during playback
- Audio position sync for drift correction
"""
import os
import sys
import time
import threading
from typing import List, Dict, Optional, Callable

# Add parent to path for utils imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.settings import load_settings


class TurnState:
    """State for a single conversation turn's playback."""

    def __init__(self, turn_id: str, speaker_id: str = None, use_3d: bool = True):
        self.turn_id = turn_id
        self.speaker_id = speaker_id         # Character ID for Lua
        self.use_3d = use_3d                 # False for player voice (centered stereo)
        self.viseme_buffer: List[Dict] = []  # Accumulated visemes
        self.visemes_sent_idx: int = 0       # How many sent to Lua
        self.audio_stream = None             # TTSStream reference
        self.playback_started: bool = False
        self.playback_start_time: float = 0  # Wall clock time when audio started
        self.audio_position: float = 0.0     # Current playback position (seconds)
        self.created_at: float = time.time()

    def add_visemes(self, visemes: List[Dict]):
        """Add visemes to buffer (called as TTS chunks arrive)."""
        self.viseme_buffer.extend(visemes)

    def get_unsent_visemes(self) -> List[Dict]:
        """Get visemes that haven't been sent to Lua yet."""
        unsent = self.viseme_buffer[self.visemes_sent_idx:]
        self.visemes_sent_idx = len(self.viseme_buffer)
        return unsent

    def get_all_visemes(self) -> List[Dict]:
        """Get all visemes (for initial send with lipsync_start)."""
        self.visemes_sent_idx = len(self.viseme_buffer)
        return list(self.viseme_buffer)


class PlaybackCoordinator:
    """
    Coordinates TTS audio playback with lipsync visemes.

    Usage:
        coordinator = PlaybackCoordinator(lua_socket)

        # Pre-buffer phase (in TTS callback):
        turn = coordinator.create_turn(turn_id)
        turn.add_visemes(visemes_from_chunk)
        turn.audio_stream = tts_stream

        # Playback phase:
        coordinator.play_turn(turn_id)
    """

    def __init__(self, lua_socket):
        self.lua_socket = lua_socket
        self.turns: Dict[str, TurnState] = {}
        self.current_turn_id: Optional[str] = None

        # Handshake synchronization
        self._lipsync_ready_event = threading.Event()
        self._lipsync_ready_turn_id: Optional[str] = None
        self._lipsync_ready_lock = threading.Lock()

        # Sync loop control
        self._sync_thread: Optional[threading.Thread] = None
        self._stop_sync = threading.Event()

        # Audio position callback (set by audio player)
        self._get_audio_position: Optional[Callable[[], float]] = None

    def create_turn(self, turn_id: str, speaker_id: str = None, use_3d: bool = True) -> TurnState:
        """Create a new turn for pre-buffering."""
        turn = TurnState(turn_id, speaker_id, use_3d=use_3d)
        self.turns[turn_id] = turn
        # Cleanup old turns (keep last 5)
        if len(self.turns) > 5:
            oldest = sorted(self.turns.values(), key=lambda t: t.created_at)[0]
            del self.turns[oldest.turn_id]
        return turn

    def get_turn(self, turn_id: str) -> Optional[TurnState]:
        """Get existing turn state."""
        return self.turns.get(turn_id)

    def on_lipsync_ready(self, turn_id: str):
        """Called by socket handler when Lua acknowledges lipsync_start."""
        with self._lipsync_ready_lock:
            self._lipsync_ready_turn_id = turn_id
        self._lipsync_ready_event.set()
        print(f"[Coordinator] Received lipsync_ready for turn {turn_id}")

    def set_audio_position_callback(self, callback: Callable[[], float]):
        """Set callback to get current audio playback position."""
        self._get_audio_position = callback

    def play_turn(self, turn_id: str, audio_player, blocking: bool = True) -> bool:
        """
        Play a turn with synchronized lipsync.

        Args:
            turn_id: Turn to play
            audio_player: Audio3DPlayer instance
            blocking: If True, blocks until playback complete

        Returns:
            True if playback started successfully
        """
        turn = self.turns.get(turn_id)
        if not turn:
            print(f"[Coordinator] Unknown turn: {turn_id}")
            return False

        if not turn.audio_stream:
            print(f"[Coordinator] No audio stream for turn: {turn_id}")
            return False

        self.current_turn_id = turn_id

        def do_playback():
            try:
                # 1. Get initial visemes (whatever we have buffered)
                initial_visemes = turn.get_all_visemes()
                print(f"[Coordinator] Starting turn {turn_id} with {len(initial_visemes)} initial visemes")

                # 2. Wait for previous turn's mouth animation to complete
                # This prevents the new lipsync_start from interrupting the closing animation
                self.lua_socket.wait_for_turn_complete(timeout=1.0)

                # 3. Look up per-character lipsync scale
                settings = load_settings()
                lipsync_settings = settings.get('lipsync', {})
                npc_scales = lipsync_settings.get('npc_scales', {})
                default_scale = lipsync_settings.get('default_scale', 1.0)
                scale = npc_scales.get(turn.speaker_id, default_scale)

                # 4. Mark new turn starting and send lipsync_start
                self.lua_socket.mark_turn_started()
                self._lipsync_ready_event.clear()
                self.lua_socket.send_lipsync_start(
                    speaker=turn.speaker_id,
                    turn_id=turn_id,
                    visemes=self._format_visemes(initial_visemes),
                    scale=scale
                )

                # 3. Wait for Lua acknowledgment
                ack_timeout = 0.15  # 150ms max wait
                if not self._lipsync_ready_event.wait(timeout=ack_timeout):
                    print(f"[Coordinator] Warning: No lipsync_ready within {ack_timeout*1000:.0f}ms, starting anyway")
                else:
                    with self._lipsync_ready_lock:
                        if self._lipsync_ready_turn_id != turn_id:
                            print(f"[Coordinator] Warning: lipsync_ready for wrong turn "
                                  f"(got {self._lipsync_ready_turn_id}, expected {turn_id})")

                # 4. Mark playback started (store in turn for sync loop access)
                turn.playback_started = True
                turn.playback_start_time = time.time()
                playback_start_time = turn.playback_start_time

                # 5. Start sync loop (sends new visemes + audio position)
                self._stop_sync.clear()
                self._sync_thread = threading.Thread(
                    target=self._sync_loop,
                    args=(turn,),
                    daemon=True
                )
                self._sync_thread.start()

                # 6. Play audio (blocks until done)
                print(f"[Coordinator] Starting audio playback for turn {turn_id}")
                success = audio_player.play_stream(turn.audio_stream, use_3d=turn.use_3d)

                # 7. Stop sync loop
                self._stop_sync.set()
                if self._sync_thread:
                    self._sync_thread.join(timeout=1.0)

                playback_duration = time.time() - playback_start_time
                print(f"[Coordinator] Turn {turn_id} complete: {playback_duration:.2f}s, "
                      f"{len(turn.viseme_buffer)} total visemes")

                return success

            except Exception as e:
                print(f"[Coordinator] Playback error: {e}")
                import traceback
                traceback.print_exc()
                return False
            finally:
                self.current_turn_id = None

        if blocking:
            return do_playback()
        else:
            thread = threading.Thread(target=do_playback, daemon=True)
            thread.start()
            return True

    def add_visemes_to_current(self, visemes: List[Dict]):
        """Add visemes to currently playing turn (for streaming)."""
        if self.current_turn_id:
            turn = self.turns.get(self.current_turn_id)
            if turn:
                turn.add_visemes(visemes)

    def _sync_loop(self, turn: TurnState):
        """Background loop: sends new visemes and audio position sync."""
        last_sync_time = 0
        sync_interval = 0.1  # 100ms between audio_sync messages

        while not self._stop_sync.is_set():
            now = time.time()

            # Send any new visemes that arrived during playback
            new_visemes = turn.get_unsent_visemes()
            if new_visemes:
                self.lua_socket.send({
                    "type": "visemes",
                    "turn_id": turn.turn_id,
                    "frames": self._format_visemes(new_visemes)
                })
                print(f"[Coordinator] Sent {len(new_visemes)} streaming visemes")

            # Send audio position sync
            if now - last_sync_time >= sync_interval:
                audio_pos = self._get_audio_position_safe(turn)
                if audio_pos is not None:
                    self.lua_socket.send({
                        "type": "audio_sync",
                        "turn_id": turn.turn_id,
                        "position": audio_pos
                    })
                    turn.audio_position = audio_pos
                last_sync_time = now

            time.sleep(0.02)  # 50Hz check rate

    def _get_audio_position_safe(self, turn: TurnState) -> Optional[float]:
        """Get audio position - uses wall clock since playback start."""
        # Try callback first (if set by audio player)
        if self._get_audio_position:
            try:
                return self._get_audio_position()
            except:
                pass

        # Primary: Use wall clock time since playback started
        # This is accurate because playback_start_time is set right before audio.play()
        if turn.playback_started and turn.playback_start_time > 0:
            return time.time() - turn.playback_start_time

        return None

    def _format_visemes(self, visemes: List[Dict]) -> List[List]:
        """Format visemes for socket transmission: [t, jaw, smile, funnel]"""
        formatted = []
        for v in visemes:
            if isinstance(v, dict):
                formatted.append([
                    v.get('t', 0),
                    v.get('jaw', 0),
                    v.get('smile', 0),
                    v.get('funnel', 0)
                ])
            elif isinstance(v, (list, tuple)) and len(v) >= 4:
                formatted.append(list(v[:4]))
        return formatted


# Global coordinator instance (set by server.py)
_coordinator: Optional[PlaybackCoordinator] = None


def get_coordinator() -> Optional[PlaybackCoordinator]:
    """Get the global coordinator instance."""
    return _coordinator


def init_coordinator(lua_socket) -> PlaybackCoordinator:
    """Initialize the global coordinator."""
    global _coordinator
    _coordinator = PlaybackCoordinator(lua_socket)
    return _coordinator
