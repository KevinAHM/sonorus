"""
3D Audio Module using libaudioverse

Supports streaming TTS with real-time 3D positioning and reverb.
"""
import os
import sys
import time
import queue
import threading
import math
import wave
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.settings import SONORUS_DIR, load_settings

# Fix Python 3.10+ compatibility for libaudioverse
import collections
import collections.abc
if not hasattr(collections, 'Sized'):
    collections.Sized = collections.abc.Sized
if not hasattr(collections, 'Iterable'):
    collections.Iterable = collections.abc.Iterable
if not hasattr(collections, 'Mapping'):
    collections.Mapping = collections.abc.Mapping

# libaudioverse imports
try:
    import libaudioverse as lav
    LAV_AVAILABLE = True
    _lav_initialized = False
except ImportError:
    LAV_AVAILABLE = False
    _lav_initialized = False
    print("[WARN] libaudioverse not available")

# Reverb presets
try:
    from audio.reverb import get_preset_for_auxbus, get_preset_name_for_auxbus
    REVERB_PRESETS_AVAILABLE = True
except ImportError:
    REVERB_PRESETS_AVAILABLE = False

class PositionReader:
    """Reads camera and NPC positions from socket server."""

    def __init__(self, lua_socket=None):
        self.cam_pos = (0, 0, 0)
        self.cam_yaw = 0
        self.npc_pos = (0, 0, 0)
        self.vr_cam_yaw = None    # Actual in-game camera yaw from UEVR (replaces cam_yaw when set)
        self.vr_cam_pitch = None  # Actual in-game camera pitch from UEVR
        self._lua_socket = lua_socket
        self._initialized = False

    def set_socket(self, lua_socket):
        self._lua_socket = lua_socket

    def set_initial_positions(self, cam_pos, cam_yaw, npc_pos):
        self.cam_pos = cam_pos
        self.npc_pos = npc_pos
        self.cam_yaw = cam_yaw
        self._initialized = True

        if self._lua_socket:
            with self._lua_socket._context_lock:
                self._lua_socket._positions = {
                    "camX": cam_pos[0], "camY": cam_pos[1], "camZ": cam_pos[2],
                    "camYaw": cam_yaw, "camPitch": 0,
                    "npcX": npc_pos[0], "npcY": npc_pos[1], "npcZ": npc_pos[2],
                }

        print(f"[PositionReader] Initial positions: npc={npc_pos}, cam={cam_pos}")

    def update(self):
        if not self._lua_socket:
            return
        try:
            pos = self._lua_socket.get_positions()
            if not pos:
                return
            self.cam_pos = (float(pos.get("camX", 0)), float(pos.get("camY", 0)), float(pos.get("camZ", 0)))
            self.npc_pos = (float(pos.get("npcX", 0)), float(pos.get("npcY", 0)), float(pos.get("npcZ", 0)))
            self.cam_yaw = float(pos.get("camYaw", 0))
        except Exception as e:
            print(f"[PositionReader] Update error: {e}")

    def get_source_position(self):
        """Get NPC position in listener-relative coordinates."""
        dx = self.npc_pos[0] - self.cam_pos[0]
        dy = self.npc_pos[1] - self.cam_pos[1]
        dz = self.npc_pos[2] - self.cam_pos[2]

        # In VR: use actual camera rotation from UEVR (already includes HMD)
        # Non-VR: use game camera yaw from Lua
        total_yaw = self.vr_cam_yaw if self.vr_cam_yaw is not None else self.cam_yaw
        yaw_rad = math.radians(-total_yaw)
        cos_yaw = math.cos(yaw_rad)
        sin_yaw = math.sin(yaw_rad)

        rx = dx * cos_yaw - dy * sin_yaw
        ry = dx * sin_yaw + dy * cos_yaw

        scale = 0.01  # Unreal units to meters
        # libaudioverse coords: x=right, y=up, z=backward
        lav_x = ry * scale
        lav_y = dz * scale
        lav_z = -rx * scale

        # Apply camera pitch (VR: actual camera pitch, non-VR: none)
        total_pitch = self.vr_cam_pitch if self.vr_cam_pitch is not None else 0
        if total_pitch != 0:
            pitch_rad = math.radians(-total_pitch)
            cos_p = math.cos(pitch_rad)
            sin_p = math.sin(pitch_rad)
            lav_y, lav_z = lav_y * cos_p - lav_z * sin_p, lav_y * sin_p + lav_z * cos_p

        return (lav_x, lav_y, lav_z)


class TTSStream:
    """Stream adapter for TTS PCM chunks."""

    CHUNK_SIZE = 4096

    def __init__(self, sample_rate=44100, channels=1, bits=16):
        self.sample_rate = sample_rate
        self.frequency = sample_rate  # Alias
        self.channels = channels
        self.bits = bits
        self.buffer_queue = queue.Queue()
        self.stream_complete = False
        self.playback_started = False
        self._total_fed = 0

    def feed(self, pcm_bytes):
        """Feed PCM data chunk."""
        if pcm_bytes:
            self._total_fed += len(pcm_bytes)
            # Split large chunks
            if len(pcm_bytes) > self.CHUNK_SIZE:
                for i in range(0, len(pcm_bytes), self.CHUNK_SIZE):
                    self.buffer_queue.put(pcm_bytes[i:i + self.CHUNK_SIZE])
            else:
                self.buffer_queue.put(pcm_bytes)

    def finish(self):
        """Signal end of stream, adding trailing silence to prevent clipped playback."""
        # Add ~150ms of silence to prevent audio cutoff at end
        # (playback systems often clip the final samples)
        silence_duration = 0.15  # seconds
        silence_samples = int(self.sample_rate * silence_duration)
        bytes_per_sample = self.bits // 8 * self.channels
        silence_bytes = bytes(silence_samples * bytes_per_sample)
        self.feed(silence_bytes)

        self.stream_complete = True
        self.buffer_queue.put(None)  # Sentinel

    def clean_up(self):
        self.stream_complete = True


def create_tts_stream(sample_rate=44100, channels=1):
    """Create a new TTS stream."""
    return TTSStream(sample_rate, channels)


def _is_narration_at(byte_offset, boundaries):
    """Check if a byte offset falls within a narration segment.

    Walks sentence_boundaries (populated concurrently during streaming)
    and uses start_bytes (set at voice switch points) to determine if the
    given byte position is narration or dialogue.

    Boundaries without start_bytes are skipped (only voice-switch boundaries
    have start_bytes). The first boundary implicitly starts at byte 0.
    """
    if not boundaries:
        return False
    # First boundary implicitly starts at byte 0
    narrating = boundaries[0].get('is_narration', False)
    for b in boundaries[1:]:
        sb = b.get('start_bytes')
        if sb is not None:
            if sb <= byte_offset:
                narrating = b.get('is_narration', False)
            else:
                break
    return narrating


class Audio3DPlayer:
    """3D audio player using libaudioverse with PushNode streaming and reverb."""

    def __init__(self):
        self.initialized = False
        self.server = None
        self.environment = None
        self.reverb = None
        self.reverb_send_index = None
        self.position_reader = PositionReader()
        self._update_thread = None
        self._stop_event = threading.Event()
        self.abort_flag = False
        self._current_reverb = None
        # Keep references to prevent GC
        self._push_node = None
        self._source = None
        self._output_cleared = False  # Track if output device was cleared
        # Playback lock to prevent concurrent play_stream() calls
        self._playback_lock = threading.Lock()
        # Pause state for soft interrupt
        self._paused = False
        self._pause_start: float = 0
        self._total_pause_duration: float = 0

    def abort(self, fade_ms=100):
        """Stop audio playback with a short fade out.

        Args:
            fade_ms: Fade duration in milliseconds (default 100ms)
        """
        # Capture current nodes - they may be replaced by new play_stream
        source = self._source
        push_node = self._push_node

        # Only set abort_flag if there's actually something to abort
        # Otherwise we'd poison future playbacks that start after this call
        if not source and not push_node:
            return

        self.abort_flag = True

        # Clean up pause state
        was_paused = self._paused
        self._paused = False
        self._pause_start = 0
        self._total_pause_duration = 0

        # Only fade if not already paused (source.mul is already 0 when paused)
        if source and fade_ms > 0 and not was_paused:
            steps = max(1, fade_ms // 10)  # ~10ms per step
            step_time = (fade_ms / 1000.0) / steps
            for i in range(steps - 1, -1, -1):  # Start at steps-1 (skip 1.0, already there)
                try:
                    source.mul = i / steps
                except Exception:
                    break  # Source may have been destroyed
                time.sleep(step_time)

        # Now stop the nodes
        if push_node:
            try:
                push_node.state = lav.NodeStates.paused
                push_node.reset()
            except Exception as e:
                print(f"[Audio3D] PushNode stop error: {e}")

        if source:
            try:
                source.state = lav.NodeStates.paused
            except Exception as e:
                print(f"[Audio3D] Source pause error: {e}")

        print(f"[Audio3D] Playback aborted (fade={fade_ms}ms)")

    def pause(self):
        """Pause audio with fade-out (soft interrupt). Feed loop keeps running,
        PushNode queues audio internally — nothing lost."""
        if self._paused:
            return
        target = self._source if self._source else self._push_node
        if not target:
            return
        self._pause_start = time.time()
        try:
            target.mul.linear_ramp_to_value(0.05, 0.0)  # 50ms per-sample fade-out
        except Exception:
            try:
                target.mul = 0.0
            except Exception:
                pass
        time.sleep(0.05)  # Wait for ramp to complete
        # Native PushNode pause: stops output, queues fed audio internally
        if self._push_node:
            try:
                self._push_node.state = lav.NodeStates.paused
            except Exception:
                pass
        self._paused = True
        print("[Audio3D] Paused (soft interrupt)")

    def resume(self) -> float:
        """Resume audio with fade-in. Returns total pause duration."""
        if not self._paused:
            return 0.0
        if self._pause_start > 0:
            self._total_pause_duration += time.time() - self._pause_start
            self._pause_start = 0
        self._paused = False
        target = self._source if self._source else self._push_node
        if self.server:
            try:
                with self.server:  # Atomic: unpause + anchor at 0 before any block mixes
                    if self._push_node:
                        self._push_node.state = lav.NodeStates.playing
                    if target:
                        target.mul = 0.0  # Anchor at 0 (cancels stale automators)
                        target.mul.linear_ramp_to_value(0.05, 1.0)  # 50ms fade-in
            except Exception:
                # Fallback: just unpause and set volume
                try:
                    if self._push_node:
                        self._push_node.state = lav.NodeStates.playing
                    if target:
                        target.mul = 1.0
                except Exception:
                    pass
        print(f"[Audio3D] Resumed (total paused: {self._total_pause_duration:.1f}s)")
        return self._total_pause_duration

    @property
    def is_paused(self):
        return self._paused

    def init(self):
        if not LAV_AVAILABLE:
            print("[Audio3D] libaudioverse not available")
            return False

        try:
            global _lav_initialized
            if not _lav_initialized:
                lav.initialize()
                _lav_initialized = True

            # Create server and set output device EARLY
            self.server = lav.Server()
            self.server.set_output_device()

            # Create environment with HRTF
            self.environment = lav.EnvironmentNode(self.server, "default")
            self.environment.panning_strategy = lav.PanningStrategies.hrtf

            # Distance attenuation - linear rolloff, tighter range for noticeable effect
            self.environment.distance_model = lav.DistanceModels.linear
            self.environment.max_distance = 15.0  # silent beyond 15m

            # Add reverb
            self.reverb = lav.FdnReverbNode(self.server)
            self.reverb.t60 = 1.5

            # Add effect send (4 channels, is_reverb=True, connect_by_default=True)
            self.reverb_send_index = self.environment.add_effect_send(4, True, True)

            # Connect effect send to reverb
            self.environment.connect(self.reverb_send_index, self.reverb, 0)

            # Connect: environment + reverb -> server
            self.environment.connect(0, self.server)
            self.reverb.connect(0, self.server)

            # Default reverb levels - subtle at close range
            self.environment.min_reverb_level = 0.05  # 5% wet at close range
            self.environment.max_reverb_level = 0.2   # 20% wet at far range
            self.environment.default_reverb_distance = 75.0

            self.initialized = True
            print("[Audio3D] libaudioverse initialized (HRTF + reverb)")
            return True

        except Exception as e:
            print(f"[Audio3D] Failed to initialize: {e}")
            import traceback
            traceback.print_exc()
            return False

    def set_reverb(self, auxbus_name: str, send_level: float = 1.0):
        """Set reverb based on game AuxBus name."""
        if not self.reverb or not REVERB_PRESETS_AVAILABLE:
            return

        try:
            preset = get_preset_for_auxbus(auxbus_name)
            preset_name = get_preset_name_for_auxbus(auxbus_name)

            self.reverb.t60 = preset.get("decay_time", 1.5)
            self.reverb.density = preset.get("density", 0.5)

            # Cutoff from gain - higher gain = brighter reverb
            gain = preset.get("gain", 0.32) * send_level
            cutoff = 2000 + (gain * 6000)
            self.reverb.cutoff_frequency = cutoff

            # Modulation from preset - reduces metallic sound
            mod_depth = preset.get("modulation_depth", 0.0)
            mod_time = preset.get("modulation_time", 0.25)
            self.reverb.delay_modulation_depth = mod_depth
            self.reverb.delay_modulation_frequency = 1.0 / mod_time if mod_time > 0 else 4.0

            # Reverb wet levels - scale by preset's late_reverb_gain for environment-appropriate wetness
            # Caves/dungeons have high late_reverb_gain (~0.35-0.4), outdoor has low (~0.05-0.1)
            late_gain = preset.get("late_reverb_gain", 0.1)
            min_rev = 0.03 + (late_gain * 0.5)  # 3-23% at close (outdoor ~8%, dungeon ~20%)
            max_rev = 0.10 + (late_gain * 1.2)  # 10-58% at far (outdoor ~22%, dungeon ~52%)
            self.environment.min_reverb_level = min_rev
            self.environment.max_reverb_level = max_rev
            self.environment.default_reverb_distance = 75.0

            self._current_reverb = preset_name
            print(f"[Audio3D] Reverb: {preset_name} (t60={preset['decay_time']:.1f}s, wet={min_rev:.0%}-{max_rev:.0%})")

        except Exception as e:
            print(f"[Audio3D] Failed to set reverb: {e}")

    def play_stream(self, tts_stream, on_chunk_callback=None, on_start=None, use_3d=True,
                    reverb_auxbus=None, reverb_send=1.0, abort_check=None,
                    sentence_boundaries=None, centered=False):
        """Play streaming TTS audio with 3D positioning and reverb using PushNode.

        Args:
            abort_check: Callback that returns True if playback should abort (epoch stale).
                        This is the primary abort mechanism - checked throughout playback.
            sentence_boundaries: Optional list of dicts with 'is_narration' and 'start_bytes'
                keys. When provided, narration segments play non-spatially (centered)
                while dialogue plays 3D. Only meaningful when use_3d=True.
            centered: If True with use_3d=True, routes through EnvironmentNode for reverb
                but positions source fixed in front of listener (no tracking). For FPV/VR player voice.
        """
        if not self.initialized:
            if not self.init():
                return False

        # Check abort before acquiring lock
        if abort_check and abort_check():
            print("[Audio3D] Abort before start (epoch stale)")
            return False

        # Acquire lock to prevent concurrent playbacks from interfering
        # Use timeout to avoid deadlock if something goes wrong
        if not self._playback_lock.acquire(timeout=5.0):
            print("[Audio3D] Warning: Playback lock timeout - another playback may be stuck")
            return False

        try:
            return self._play_stream_locked(tts_stream, on_chunk_callback, on_start, use_3d,
                                            reverb_auxbus, reverb_send, abort_check,
                                            sentence_boundaries, centered)
        finally:
            self._playback_lock.release()

    def _play_stream_locked(self, tts_stream, on_chunk_callback=None, on_start=None, use_3d=True,
                            reverb_auxbus=None, reverb_send=1.0, abort_check=None,
                            sentence_boundaries=None, centered=False):
        """Internal play_stream with lock already held."""
        # Reset pause state for new playback
        self._paused = False
        self._pause_start = 0
        self._total_pause_duration = 0

        # Re-enable output device if it was cleared after previous playback
        if self._output_cleared and self.server:
            try:
                self.server.set_output_device()
                self._output_cleared = False
            except Exception as e:
                print(f"[Audio3D] Failed to re-enable output: {e}")

        # Stop any previous playback BEFORE creating new nodes (prevents overlap)
        if self._push_node:
            try:
                self._push_node.state = lav.NodeStates.paused
                self._push_node.reset()
            except Exception:
                pass
        # Reset narration routing state
        self._narr_active = False
        if self._source:
            try:
                self._source.state = lav.NodeStates.paused
            except Exception:
                pass

        # Note: abort_flag is NOT reset here - epoch-based abort_check is the primary mechanism
        # Resetting abort_flag here was the source of a race condition where new playbacks
        # would clear the flag while old playbacks were still checking it

        try:
            # Get audio params
            sample_rate = getattr(tts_stream, 'sample_rate', 24000)
            channels = getattr(tts_stream, 'channels', 1)

            # Load audio settings
            settings = load_settings()
            audio_cfg = settings.get("audio", {})
            reverb_enabled = audio_cfg.get("reverb", True)
            camera_offset = audio_cfg.get("camera_offset", 0.0)
            master_volume = audio_cfg.get("volume", 100) / 100.0
            try:
                narration_volume = float(audio_cfg.get("narration_volume", 80)) / 100.0
            except Exception:
                narration_volume = 0.8
            narration_volume = max(0.0, min(1.0, narration_volume))

            # Apply master volume (0-100% maps to 0.0-1.0)
            self.server.mul = master_volume

            # Get reverb from global state if not explicitly passed
            if reverb_enabled and not reverb_auxbus and self.position_reader._lua_socket:
                try:
                    reverb = self.position_reader._lua_socket.get_current_reverb()
                    if reverb:
                        reverb_auxbus = reverb.get("auxbus")
                        reverb_send = reverb.get("send", 1.0)
                except Exception:
                    pass

            # Apply reverb if enabled
            if reverb_enabled and reverb_auxbus:
                self.set_reverb(reverb_auxbus, reverb_send)
            elif not reverb_enabled:
                # Disable reverb sends
                self.environment.min_reverb_level = 0.0
                self.environment.max_reverb_level = 0.0

            # Create PushNode for streaming
            push_node = lav.PushNode(self.server, sr=sample_rate, channels=1)
            push_node.threshold = 0.1  # Fire low callback when < 100ms buffered

            # Keep references to prevent GC
            self._push_node = push_node

            if use_3d and centered:
                # FPV/VR player voice: route through EnvironmentNode for reverb
                # but fixed position in front of listener — no tracking needed
                # Disconnect position reader so stale socket updates can't move the source
                self.position_reader.set_socket(None)
                self.position_reader.cam_pos = (0, 0, 0)
                self.position_reader.npc_pos = (0, 0, 0)
                self.position_reader.cam_yaw = 0
                self.position_reader.vr_cam_yaw = None
                self.position_reader.vr_cam_pitch = None

                source = lav.SourceNode(self.server, self.environment)
                push_node.connect(0, source, 0)
                self._source = source
                source.position.value = (0, 0, -0.5)  # 0.5m directly in front of listener
                print(f"[Audio3D] CENTERED branch hit: source=(0,0,-0.5), socket disconnected, no tracking thread")

            elif use_3d:
                # 3D audio: route through EnvironmentNode for HRTF spatialization
                # PushNode -> SourceNode -> Environment (HRTF) -> Server
                print(f"[Audio3D] NORMAL 3D branch hit: use_3d={use_3d}, centered={centered}")
                source = lav.SourceNode(self.server, self.environment)
                push_node.connect(0, source, 0)
                self._source = source

                # Use actual HMD direction for audio listener orientation
                from vr import get_vr_tracker as _get_vr
                vr = _get_vr()
                if vr and vr.initialized:
                    self.position_reader.vr_cam_yaw = vr.head_yaw
                    self.position_reader.vr_cam_pitch = vr.head_pitch

                pos = self.position_reader.get_source_position()
                # Apply camera height offset (positive = listener higher = NPC appears lower)
                pos = (pos[0], pos[1] - camera_offset, pos[2])
                source.position.value = pos
                print(f"[Audio3D] Source position: {pos} (cam yaw={vr.cam_yaw:.1f} pitch={vr.cam_pitch:.1f})" if vr and vr.initialized else f"[Audio3D] Source position: {pos}")

                # Start position update thread
                self._stop_event.clear()
                self._update_thread = threading.Thread(
                    target=self._update_positions,
                    args=(source, camera_offset),
                    daemon=True
                )
                self._update_thread.start()

            else:
                # Non-3D audio (player voice in third person): bypass HRTF entirely
                # Connect PushNode directly to server for clean centered playback
                # This avoids HRTF processing issues on some audio configurations
                push_node.connect(0, self.server)
                push_node.mul = 1.0  # Unity gain (master volume already applied to server)
                self._source = None
                print(f"[Audio3D] Non-3D mode: direct to server (bypassing HRTF)")

            print(f"[Audio3D] Streaming {sample_rate}Hz {'3D' if use_3d else 'stereo'}...")

            tts_stream.playback_started = True
            playback_start = time.time()
            self._underrun_time = None  # set below after underrun_time list is created
            if on_start:
                try:
                    on_start(playback_start)
                except Exception as e:
                    print(f"[Audio3D] on_start error: {e}")

            # Stream chunks as they arrive - playback starts immediately
            total_samples = 0
            total_bytes_fed = 0  # Track raw PCM bytes for narration routing
            aborted = False
            bytes_per_second = max(1, sample_rate * 2 * channels)
            # Track push_node buffer underruns.  When the push_node runs dry
            # (TTS still generating the next sentence), it outputs silence but
            # the wall clock keeps ticking.  Without correction, audio_pos
            # drifts ahead of reality — subtitles advance early and the drain
            # wait at the end is too short, cutting off the tail.
            underrun_time = [0.0]  # mutable so closures can read it
            self._underrun_time = underrun_time
            def _switch_push_route(is_narration):
                """Switch route atomically to avoid dual-path loudness artifacts."""
                current_is_narr = bool(getattr(self, '_narr_active', False))
                if is_narration == current_is_narr:
                    return

                # Libaudioverse has no targeted disconnect for server connections.
                # disconnect(output) clears ALL connections on that output, which
                # is safe here since we only maintain one route at a time.
                push_node.disconnect(0)

                if is_narration:
                    push_node.mul = narration_volume
                    push_node.connect(0, self.server)
                else:
                    push_node.mul = 1.0
                    push_node.connect(0, source, 0)

            def _estimated_played_bytes():
                """Estimate played byte position from playback wall clock."""
                total_pause = self._total_pause_duration
                if self._pause_start > 0:
                    total_pause += time.time() - self._pause_start
                elapsed = max(0.0, time.time() - playback_start - total_pause - underrun_time[0])
                est = int(elapsed * bytes_per_second)
                # Can't have played more than we've fed.
                return min(est, total_bytes_fed)

            def _update_route_if_needed():
                # Route by PLAYED position (not fed), so pre-buffering doesn't
                # preemptively flip spatialization for still-playing dialogue.
                if not use_3d or not sentence_boundaries:
                    return
                # Boundaries are populated concurrently while streaming; do not
                # latch narration presence once at startup.
                if not any(b.get('is_narration') for b in sentence_boundaries):
                    return
                playback_offset = _estimated_played_bytes()
                is_narr = _is_narration_at(playback_offset, sentence_boundaries)
                if not hasattr(self, '_narr_active') or self._narr_active != is_narr:
                    try:
                        _switch_push_route(is_narr)
                    except Exception as e:
                        print(f"[Audio3D] Route switch error: {e}")
                    else:
                        self._narr_active = is_narr
                        label = "NARRATION (centered)" if is_narr else "DIALOGUE (3D)"
                        print(f"[Audio3D] Routing -> {label} at played_byte {playback_offset} (fed={total_bytes_fed})")
            while True:
                # Check epoch-based abort (primary mechanism)
                if abort_check and abort_check():
                    print("[Audio3D] Playback aborted (epoch stale)")
                    aborted = True
                    break

                # Also check legacy abort_flag for backwards compatibility
                if self.abort_flag:
                    print("[Audio3D] Playback aborted (flag)")
                    aborted = True
                    break

                # Route updates are based on what is currently being played.
                _update_route_if_needed()

                try:
                    chunk = tts_stream.buffer_queue.get(timeout=0.1)
                    if chunk is None:
                        break

                    # Convert int16 PCM to float32 [-1, 1]
                    samples = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0

                    # If stereo, downmix to mono for 3D source
                    if channels == 2:
                        samples = (samples[0::2] + samples[1::2]) / 2.0

                    push_node.feed(len(samples), samples.tolist())
                    total_samples += len(samples)
                    total_bytes_fed += len(chunk)

                    if on_chunk_callback:
                        on_chunk_callback(len(chunk))

                except queue.Empty:
                    if tts_stream.stream_complete:
                        break
                    # Detect underrun: if wall clock has passed all fed audio,
                    # the push_node is outputting silence right now.
                    fed_duration = total_samples / sample_rate if total_samples > 0 else 0
                    total_pause = self._total_pause_duration
                    if self._pause_start > 0:
                        total_pause += time.time() - self._pause_start
                    wall = time.time() - playback_start - total_pause
                    if wall > fed_duration:
                        underrun_time[0] = wall - fed_duration
                    continue

            # Wait for remaining audio to play out (skip if aborted)
            if not aborted:
                total_duration = total_samples / sample_rate
                # Account for in-progress pause (not yet added to _total_pause_duration)
                total_pause = self._total_pause_duration
                if self._pause_start > 0:
                    total_pause += time.time() - self._pause_start
                elapsed = time.time() - playback_start - total_pause - underrun_time[0]
                remaining = total_duration - elapsed + 0.3
                if underrun_time[0] > 0.05:
                    print(f"[Audio3D] Underrun correction: {underrun_time[0]:.2f}s")
                if remaining > 0:
                    print(f"[Audio3D] Fed {total_samples} samples ({total_duration:.2f}s audio) "
                          f"in {elapsed:.1f}s, waiting {remaining:.1f}s more...")
                    # Wait in small increments so we can check abort
                    wait_end = time.time() + remaining
                    while time.time() < wait_end:
                        # Keep route switching active while buffered audio drains.
                        # Narration often starts long after feeding has finished.
                        _update_route_if_needed()
                        if (abort_check and abort_check()) or self.abort_flag:
                            print("[Audio3D] Aborted during wait")
                            aborted = True
                            break
                        # Extend wait while paused (PushNode isn't playing)
                        if self._paused:
                            wait_end += 0.05
                        time.sleep(0.05)  # 50ms check interval
                else:
                    print(f"[Audio3D] Fed {total_samples} samples, playback complete")
            else:
                print("[Audio3D] Skipping wait (aborted)")

            # Cleanup
            self._underrun_time = None
            self._stop_event.set()
            if self._update_thread:
                self._update_thread.join(timeout=1.0)

            # Clean up per-playback nodes to stop libaudioverse processing
            if self._push_node:
                try:
                    self._push_node.state = lav.NodeStates.paused
                    self._push_node.reset()
                except Exception:
                    pass
                self._push_node = None
            if self._source:
                try:
                    self._source.state = lav.NodeStates.paused
                except Exception:
                    pass
                self._source = None

            # Stop audio output to reduce idle CPU (will re-enable on next playback)
            if self.server:
                try:
                    self.server.clear_output_device()
                    self._output_cleared = True
                except Exception:
                    pass

            # Reset abort_flag at END of playback (not at start - that caused race condition)
            # This ensures next playback starts clean after this one finishes
            self.abort_flag = False

            print("[Audio3D] Playback complete")
            return not aborted  # Return False if we aborted

        except Exception as e:
            print(f"[Audio3D] Playback error: {e}")
            import traceback
            traceback.print_exc()
            return False

    def _update_positions(self, source, camera_offset=0.0):
        from vr import get_vr_tracker as _get_vr
        while not self._stop_event.is_set():
            try:
                self.position_reader.update()
                # Poll VR at 50ms for smooth audio spatialization
                vr = _get_vr()
                if vr and vr.initialized:
                    vr.update()
                    self.position_reader.vr_cam_yaw = vr.head_yaw
                    self.position_reader.vr_cam_pitch = vr.head_pitch
                pos = self.position_reader.get_source_position()
                # Apply camera height offset
                pos = (pos[0], pos[1] - camera_offset, pos[2])
                source.position.value = pos
            except Exception as e:
                print(f"[Audio3D] Position error: {e}")
            time.sleep(0.05)

    def shutdown(self):
        self._stop_event.set()
        if self._update_thread:
            self._update_thread.join(timeout=1.0)
        # VR tracker is module-level, don't shut it down here
        self._push_node = None
        self._source = None
        # Clean up libaudioverse resources (server runs background audio thread)
        if self.reverb:
            self.reverb = None
        if self.environment:
            self.environment = None
        if self.server:
            self.server = None
        self.initialized = False


_player = None


def get_player():
    global _player
    if _player is None:
        _player = Audio3DPlayer()
    return _player


def shutdown():
    global _player
    if _player:
        _player.shutdown()
        _player = None
