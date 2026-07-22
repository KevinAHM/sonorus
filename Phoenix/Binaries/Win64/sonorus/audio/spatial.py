"""
3D Audio Module using PyOpenAL

Supports both file playback and streaming TTS with real-time 3D positioning.
"""
import os
import sys
import time
import queue
import threading
import math

# Add parent to path for utils imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.settings import SONORUS_DIR, load_settings

# TESTING: Disable 3D positioning to diagnose lag (plays mono audio at center)
DISABLE_3D_POSITIONING = False

# OpenAL imports
try:
    from openal import (
        oalInit, oalQuit, oalGetListener, oalSetStreamBufferCount,
        oalInitHRTF,  # PyOpenAL-HRTF extension
        Source, SourceStream, Buffer, WaveFile, AL_PLAYING, AL_STOPPED
    )
    OPENAL_AVAILABLE = True
    HRTF_AVAILABLE = True
except ImportError:
    try:
        # Fallback to standard PyOpenAL without HRTF
        from openal import (
            oalInit, oalQuit, oalGetListener, oalSetStreamBufferCount,
            Source, SourceStream, Buffer, WaveFile, AL_PLAYING, AL_STOPPED
        )
        OPENAL_AVAILABLE = True
        HRTF_AVAILABLE = False
        print("[WARN] PyOpenAL-HRTF not available, using standard PyOpenAL")
    except ImportError:
        OPENAL_AVAILABLE = False
        HRTF_AVAILABLE = False
        AL_STOPPED = 0x4114  # Fallback value
        print("[WARN] PyOpenAL not available")

# OpenAL EFX imports for reverb (via ctypes - PyOpenAL doesn't expose EFX)
EFX_AVAILABLE = False
_efx = None  # Module-level EFX function container

try:
    import ctypes
    from openal import al

    # EFX constants
    AL_EFFECTSLOT_EFFECT = 0x0001
    AL_EFFECT_TYPE = 0x8001
    AL_EFFECT_EAXREVERB = 0x8000
    AL_EFFECT_REVERB = 0x0001
    AL_AUXILIARY_SEND_FILTER = 0x10005
    AL_EFFECTSLOT_NULL = 0

    # EAX Reverb parameters
    AL_EAXREVERB_DENSITY = 0x0001
    AL_EAXREVERB_DIFFUSION = 0x0002
    AL_EAXREVERB_GAIN = 0x0003
    AL_EAXREVERB_GAINHF = 0x0004
    AL_EAXREVERB_GAINLF = 0x0005
    AL_EAXREVERB_DECAY_TIME = 0x0006
    AL_EAXREVERB_DECAY_HFRATIO = 0x0007
    AL_EAXREVERB_DECAY_LFRATIO = 0x0008
    AL_EAXREVERB_REFLECTIONS_GAIN = 0x0009
    AL_EAXREVERB_REFLECTIONS_DELAY = 0x000A
    AL_EAXREVERB_LATE_REVERB_GAIN = 0x000C
    AL_EAXREVERB_LATE_REVERB_DELAY = 0x000D
    AL_EAXREVERB_ECHO_TIME = 0x000F
    AL_EAXREVERB_ECHO_DEPTH = 0x0010
    AL_EAXREVERB_MODULATION_TIME = 0x0011
    AL_EAXREVERB_MODULATION_DEPTH = 0x0012
    AL_EAXREVERB_AIR_ABSORPTION_GAINHF = 0x0013
    AL_EAXREVERB_HFREFERENCE = 0x0014
    AL_EAXREVERB_LFREFERENCE = 0x0015
    AL_EAXREVERB_ROOM_ROLLOFF_FACTOR = 0x0016

    class EFXFunctions:
        """Container for EFX functions loaded via ctypes"""
        def __init__(self):
            self.available = False
            self.alGenEffects = None
            self.alDeleteEffects = None
            self.alEffecti = None
            self.alEffectf = None
            self.alGenAuxiliaryEffectSlots = None
            self.alDeleteAuxiliaryEffectSlots = None
            self.alAuxiliaryEffectSloti = None
            self.alSource3i = None
            self.alGetError = None

    _efx = EFXFunctions()

    def _init_efx_functions():
        """Initialize EFX functions from OpenAL-Soft DLL"""
        global _efx, EFX_AVAILABLE

        # Find OpenAL DLL - try common locations
        dll_paths = [
            # Embedded Python location
            os.path.join(SONORUS_DIR, "python", "Lib", "site-packages", "openal", "soft_oal_64.dll"),
            # Common names in PATH
            "soft_oal_64.dll",
            "OpenAL32.dll",
            "soft_oal.dll",
            "openal32.dll",
        ]
        openal_dll = None

        for path in dll_paths:
            try:
                openal_dll = ctypes.CDLL(path)
                print(f"[Audio3D] Loaded OpenAL DLL: {path}")
                break
            except OSError:
                continue

        if not openal_dll:
            print("[Audio3D] Could not load OpenAL DLL for EFX")
            return False

        try:
            # Get alGetProcAddress to load EFX extension functions
            alGetProcAddress = openal_dll.alGetProcAddress
            alGetProcAddress.restype = ctypes.c_void_p
            alGetProcAddress.argtypes = [ctypes.c_char_p]

            # Load EFX functions
            def get_func(name, restype, argtypes):
                addr = alGetProcAddress(name.encode('utf-8'))
                if not addr:
                    print(f"[Audio3D] alGetProcAddress failed for: {name}")
                    return None
                func_type = ctypes.CFUNCTYPE(restype, *argtypes)
                return func_type(addr)

            # alGenEffects(sizei n, uint *effects)
            _efx.alGenEffects = get_func("alGenEffects", None,
                [ctypes.c_int, ctypes.POINTER(ctypes.c_uint)])

            # alDeleteEffects(sizei n, uint *effects)
            _efx.alDeleteEffects = get_func("alDeleteEffects", None,
                [ctypes.c_int, ctypes.POINTER(ctypes.c_uint)])

            # alEffecti(uint effect, enum param, int value)
            _efx.alEffecti = get_func("alEffecti", None,
                [ctypes.c_uint, ctypes.c_int, ctypes.c_int])

            # alEffectf(uint effect, enum param, float value)
            _efx.alEffectf = get_func("alEffectf", None,
                [ctypes.c_uint, ctypes.c_int, ctypes.c_float])

            # alGenAuxiliaryEffectSlots(sizei n, uint *slots)
            _efx.alGenAuxiliaryEffectSlots = get_func("alGenAuxiliaryEffectSlots", None,
                [ctypes.c_int, ctypes.POINTER(ctypes.c_uint)])

            # alDeleteAuxiliaryEffectSlots(sizei n, uint *slots)
            _efx.alDeleteAuxiliaryEffectSlots = get_func("alDeleteAuxiliaryEffectSlots", None,
                [ctypes.c_int, ctypes.POINTER(ctypes.c_uint)])

            # alAuxiliaryEffectSloti(uint slot, enum param, int value)
            _efx.alAuxiliaryEffectSloti = get_func("alAuxiliaryEffectSloti", None,
                [ctypes.c_uint, ctypes.c_int, ctypes.c_int])

            # alSource3i(uint source, enum param, int v1, int v2, int v3)
            _efx.alSource3i = get_func("alSource3i", None,
                [ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int])

            # alSourceiv(uint source, enum param, int *values) - for aux send filter
            _efx.alSourceiv = get_func("alSourceiv", None,
                [ctypes.c_uint, ctypes.c_int, ctypes.POINTER(ctypes.c_int)])

            # alGetError()
            _efx.alGetError = openal_dll.alGetError
            _efx.alGetError.restype = ctypes.c_int
            _efx.alGetError.argtypes = []

            # Check all required functions loaded
            func_names = ["alGenEffects", "alEffecti", "alEffectf",
                         "alGenAuxiliaryEffectSlots", "alAuxiliaryEffectSloti", "alSource3i"]
            required = [_efx.alGenEffects, _efx.alEffecti, _efx.alEffectf,
                       _efx.alGenAuxiliaryEffectSlots, _efx.alAuxiliaryEffectSloti,
                       _efx.alSource3i]

            missing = [name for name, func in zip(func_names, required) if func is None]

            if not missing:
                _efx.available = True
                EFX_AVAILABLE = True
                print("[Audio3D] EFX functions loaded via ctypes")
                return True
            else:
                print(f"[Audio3D] Missing EFX functions: {missing}")
                return False

        except Exception as e:
            print(f"[Audio3D] Failed to load EFX functions: {e}")
            return False

except ImportError as e:
    print(f"[WARN] OpenAL EFX not available: {e}")

# Reverb presets
try:
    from audio.reverb import get_preset_for_auxbus, get_preset_name_for_auxbus
    REVERB_PRESETS_AVAILABLE = True
except ImportError:
    REVERB_PRESETS_AVAILABLE = False
    print("[WARN] Reverb presets not available")

# ============================================
# Position Reader - reads from socket with interpolation
# ============================================
class PositionReader:
    """
    Reads camera and NPC positions from socket server (updated by Lua via TCP).
    Provides smooth interpolation between position updates to avoid stepping artifacts.
    """

    def __init__(self, lua_socket=None):
        # Current interpolated values (output)
        self.cam_pos = (0, 0, 0)
        self.cam_yaw = 0
        self.npc_pos = (0, 0, 0)
        self._lua_socket = lua_socket

        # Interpolation state - previous and current snapshots
        self._prev_cam = (0, 0, 0)
        self._prev_npc = (0, 0, 0)
        self._prev_yaw = 0
        self._curr_cam = (0, 0, 0)
        self._curr_npc = (0, 0, 0)
        self._curr_yaw = 0
        self._last_update_time = 0      # When we received last position
        self._update_interval = 0.1     # Expected interval (100ms default)
        self._initialized = False       # First position flag

    def set_socket(self, lua_socket):
        """Set the socket server reference (for lazy initialization)"""
        self._lua_socket = lua_socket

    def set_initial_positions(self, cam_pos, cam_yaw, npc_pos):
        """
        Directly set initial positions (bypasses socket, eliminates race condition).
        Call this BEFORE play_stream() to ensure first speaker has correct 3D position.
        """
        self._prev_cam = self._curr_cam = cam_pos
        self._prev_npc = self._curr_npc = npc_pos
        self._prev_yaw = self._curr_yaw = cam_yaw
        self.cam_pos = cam_pos
        self.npc_pos = npc_pos
        self.cam_yaw = cam_yaw
        self._initialized = True
        self._last_update_time = time.time()

        # CRITICAL: Also update socket's cached positions to prevent stale data
        # from overwriting these on the next update() call. Without this, the
        # position briefly jumps to the previous speaker's location.
        if self._lua_socket:
            with self._lua_socket._context_lock:
                self._lua_socket._positions = {
                    "camX": cam_pos[0], "camY": cam_pos[1], "camZ": cam_pos[2],
                    "camYaw": cam_yaw, "camPitch": 0,
                    "npcX": npc_pos[0], "npcY": npc_pos[1], "npcZ": npc_pos[2],
                }

        print(f"[PositionReader] Initial positions set: npc={npc_pos}, cam={cam_pos}, yaw={cam_yaw}")

    def _lerp(self, a, b, t):
        """Linear interpolate between tuples a and b by factor t (0-1)"""
        t = max(0.0, min(1.0, t))  # Clamp to [0,1]
        return tuple(a[i] + (b[i] - a[i]) * t for i in range(len(a)))

    def _lerp_angle(self, a, b, t):
        """Lerp angle with wraparound handling (e.g., 350 -> 10 goes through 0)"""
        t = max(0.0, min(1.0, t))
        diff = b - a
        # Handle wraparound
        if diff > 180:
            diff -= 360
        elif diff < -180:
            diff += 360
        return a + diff * t

    def update(self):
        """Fetch new position from socket and update interpolation state"""
        if not self._lua_socket:
            return  # No socket, keep last valid values

        try:
            pos = self._lua_socket.get_positions()
            if not pos:
                return

            new_cam = (
                float(pos.get("camX", 0)),
                float(pos.get("camY", 0)),
                float(pos.get("camZ", 0))
            )
            new_npc = (
                float(pos.get("npcX", 0)),
                float(pos.get("npcY", 0)),
                float(pos.get("npcZ", 0))
            )
            new_yaw = float(pos.get("camYaw", 0))

            # Check if anything actually changed (avoid redundant updates)
            if new_cam == self._curr_cam and new_npc == self._curr_npc and new_yaw == self._curr_yaw:
                return

            now = time.time()

            if not self._initialized:
                # First position: use immediately, no interpolation
                self._prev_cam = self._curr_cam = new_cam
                self._prev_npc = self._curr_npc = new_npc
                self._prev_yaw = self._curr_yaw = new_yaw
                self.cam_pos = new_cam
                self.npc_pos = new_npc
                self.cam_yaw = new_yaw
                self._initialized = True
            else:
                # Shift current -> previous
                self._prev_cam = self._curr_cam
                self._prev_npc = self._curr_npc
                self._prev_yaw = self._curr_yaw
                # Store new as current
                self._curr_cam = new_cam
                self._curr_npc = new_npc
                self._curr_yaw = new_yaw

            # Update timing (clamp to max 200ms to handle gaps gracefully)
            if self._last_update_time > 0:
                self._update_interval = min(now - self._last_update_time, 0.2)
            self._last_update_time = now

        except:
            pass  # Keep last valid values

    def interpolate(self):
        """Calculate interpolated positions for this frame (call after update)"""
        if not self._initialized:
            return  # Keep defaults

        now = time.time()
        elapsed = now - self._last_update_time

        # Calculate blend factor: 0 = at prev, 1 = at curr
        t = elapsed / self._update_interval if self._update_interval > 0 else 1.0

        # Clamp to 1.0 - don't extrapolate past current (causes overshoot)
        t = min(t, 1.0)

        # Interpolate positions
        self.cam_pos = self._lerp(self._prev_cam, self._curr_cam, t)
        self.npc_pos = self._lerp(self._prev_npc, self._curr_npc, t)
        self.cam_yaw = self._lerp_angle(self._prev_yaw, self._curr_yaw, t)

    def get_listener_position(self):
        """Get camera position for OpenAL listener"""
        # Convert UE4 coords to OpenAL (scale down from cm to m)
        # UE4: X=forward, Y=right, Z=up
        # OpenAL: X=right, Y=up, Z=backward
        scale = 0.01
        return (
            self.cam_pos[1] * scale,   # UE4 Y (right) -> OpenAL X (right)
            self.cam_pos[2] * scale,   # UE4 Z (up) -> OpenAL Y (up)
            -self.cam_pos[0] * scale   # UE4 X (forward) -> OpenAL -Z (forward)
        )

    def get_listener_orientation(self):
        """Get camera orientation for OpenAL listener (at, up vectors)"""
        # Convert yaw to forward vector
        # UE4 yaw: 0=+X(forward), 90=+Y(right)
        # OpenAL: forward=-Z, right=+X
        yaw_rad = math.radians(self.cam_yaw)
        # Forward vector (at) in OpenAL coords
        fx = math.sin(yaw_rad)   # right component
        fz = -math.cos(yaw_rad)  # forward component (negative Z)
        # Up vector
        return (fx, 0, fz, 0, 1, 0)

    def get_source_position(self):
        """Get NPC position for OpenAL source"""
        # Convert UE4 coords to OpenAL
        # UE4: X=forward, Y=right, Z=up
        # OpenAL: X=right, Y=up, Z=backward
        scale = 0.01
        return (
            self.npc_pos[1] * scale,   # UE4 Y (right) -> OpenAL X (right)
            self.npc_pos[2] * scale,   # UE4 Z (up) -> OpenAL Y (up)
            -self.npc_pos[0] * scale   # UE4 X (forward) -> OpenAL -Z (forward)
        )


# ============================================
# TTS Stream Adapter
# ============================================
class TTSStream:
    """
    Stream adapter for real-time TTS PCM chunks.
    Implements the interface expected by PyOpenAL's SourceStream.

    Large TTS chunks are split into smaller pieces (~4KB, ~42ms at 48kHz)
    for smoother OpenAL buffer management and to prevent playback gaps.
    """

    # Target chunk size for OpenAL buffers: ~4KB = ~42ms at 48kHz mono 16-bit
    # PyOpenAL recommends 20-50ms buffers for smooth streaming
    CHUNK_SIZE = 4096

    def __init__(self, sample_rate=44100, channels=1, bits=16):
        # Required by PyOpenAL
        self.sample_rate = sample_rate
        self.frequency = sample_rate  # PyOpenAL alias
        self.channels = channels
        self.bits = bits
        self.length = 0  # Unknown length for streaming

        # Internal state
        self.buffer_queue = queue.Queue()
        self.done = False
        self.stream_complete = False  # Alias for compatibility with spatial_lav
        self.exists = True
        self._total_fed = 0
        self.playback_started = False  # Set by Audio3DPlayer after source.play()

        # DIAGNOSTIC: Streaming metrics for debugging audio hitches
        self._chunk_count = 0
        self._silence_count = 0
        self._total_pulled = 0
        self._split_count = 0      # Number of sub-chunks created from splitting
        self._feed_times = []      # Timestamps of feed() calls
        self._pull_times = []      # Timestamps of get_buffer() calls
        self._chunk_sizes = []     # Size of each chunk fed (before splitting)
        self._queue_depths = []    # Queue depth at each pull
        self._underrun_times = []  # When silence was returned (elapsed seconds)
        self._first_feed_time = None
        self._first_pull_time = None

    def feed(self, pcm_bytes):
        """Feed PCM data chunk (called by TTS provider).

        Large chunks are automatically split into smaller ~4KB pieces
        for smoother OpenAL buffer management.
        """
        if self.exists and pcm_bytes:
            now = time.time()

            # DIAGNOSTIC: Track first chunk timing
            if self._first_feed_time is None:
                self._first_feed_time = now
                print(f"[TTSStream] FIRST CHUNK at t=0.000s, size={len(pcm_bytes)} bytes")

            self._chunk_count += 1
            self._total_fed += len(pcm_bytes)
            self._feed_times.append(now)
            self._chunk_sizes.append(len(pcm_bytes))

            # Calculate inter-chunk gap and log
            elapsed = now - self._first_feed_time
            if len(self._feed_times) > 1:
                gap = now - self._feed_times[-2]
                print(f"[TTSStream] FEED #{self._chunk_count}: {len(pcm_bytes)} bytes, "
                      f"gap={gap*1000:.1f}ms, elapsed={elapsed:.2f}s, queue={self.buffer_queue.qsize()}")

            # DIAGNOSTIC: Check for WAV header in chunk (shouldn't happen after stripping in inworld.py)
            if len(pcm_bytes) >= 4 and pcm_bytes[:4] == b'RIFF':
                print(f"[TTSStream] WARNING: Chunk #{self._chunk_count} contains WAV header! (not stripped)")

            # DIAGNOSTIC: Check PCM alignment (16-bit audio should have even byte count)
            if len(pcm_bytes) % 2 != 0:
                print(f"[TTSStream] WARNING: Chunk #{self._chunk_count} has ODD size {len(pcm_bytes)} (misaligned PCM)")

            # Split large chunks into smaller pieces for smoother OpenAL buffering
            # This prevents gaps caused by PyOpenAL struggling with huge buffers
            if len(pcm_bytes) > self.CHUNK_SIZE:
                num_pieces = 0
                for i in range(0, len(pcm_bytes), self.CHUNK_SIZE):
                    piece = pcm_bytes[i:i + self.CHUNK_SIZE]
                    self.buffer_queue.put(piece)
                    num_pieces += 1
                self._split_count += num_pieces
                # Log splitting on first large chunk only
                if self._chunk_count == 1:
                    print(f"[TTSStream] Split {len(pcm_bytes)} bytes into {num_pieces} x ~{self.CHUNK_SIZE} byte pieces")
            else:
                self.buffer_queue.put(pcm_bytes)

    def finish(self):
        """Signal end of TTS stream"""
        elapsed = time.time() - self._first_feed_time if self._first_feed_time else 0
        remaining = self.buffer_queue.qsize()
        print(f"[TTSStream] FINISH called at {elapsed:.2f}s, {remaining} chunks remaining in queue, total fed: {self._total_fed} bytes")
        self.done = True
        self.stream_complete = True

    def get_buffer(self):
        """Get next buffer (called by PyOpenAL SourceStream)"""
        if not self.exists:
            return None

        now = time.time()

        # DIAGNOSTIC: Track first pull timing
        if self._first_pull_time is None:
            self._first_pull_time = now
            print(f"[TTSStream] FIRST PULL at t=0.000s")

        queue_depth = self.buffer_queue.qsize()
        self._queue_depths.append(queue_depth)
        self._pull_times.append(now)
        elapsed = now - self._first_pull_time

        try:
            # Non-blocking get - return immediately
            data = self.buffer_queue.get_nowait()
            self._total_pulled += len(data)

            # DIAGNOSTIC: Log every 50th pull to avoid spam (but always log first few)
            pull_count = len(self._pull_times)
            if pull_count <= 5 or pull_count % 50 == 0:
                print(f"[TTSStream] PULL #{pull_count}: {len(data)} bytes, "
                      f"queue={queue_depth}, elapsed={elapsed:.2f}s")

            return (data, len(data))
        except queue.Empty:
            if self.done and self.buffer_queue.empty():
                self._print_summary()
                self.exists = False
                return None

            # Only log underruns during actual playback (not initial buffer fill)
            if self.playback_started:
                self._silence_count += 1
                self._underrun_times.append(elapsed)
                print(f"[TTSStream] *** UNDERRUN #{self._silence_count} *** "
                      f"at elapsed={elapsed:.2f}s, queue=EMPTY")

            # Return small silence chunk to keep stream alive
            silence = b'\x00\x00' * 512
            return (silence, len(silence))

    def _print_summary(self):
        """Print streaming summary on completion - helps diagnose audio issues"""
        duration = self._pull_times[-1] - self._first_pull_time if self._pull_times else 0
        print(f"\n[TTSStream] === STREAMING SUMMARY ===")
        print(f"[TTSStream] Total chunks fed: {self._chunk_count} (split into {self._split_count} pieces)")
        print(f"[TTSStream] Total bytes fed: {self._total_fed}")
        print(f"[TTSStream] Total bytes pulled: {self._total_pulled}")
        print(f"[TTSStream] Total underruns (silence gaps): {self._silence_count}")
        print(f"[TTSStream] Playback duration: {duration:.2f}s")

        if self._underrun_times:
            # Show first 10 underrun timestamps
            timestamps = [f'{t:.2f}s' for t in self._underrun_times[:10]]
            print(f"[TTSStream] Underrun timestamps: {timestamps}")
            if len(self._underrun_times) > 10:
                print(f"[TTSStream]   ... and {len(self._underrun_times) - 10} more")

        if self._chunk_sizes:
            print(f"[TTSStream] Chunk size range: {min(self._chunk_sizes)}-{max(self._chunk_sizes)} bytes")
            avg_size = sum(self._chunk_sizes) / len(self._chunk_sizes)
            print(f"[TTSStream] Avg chunk size: {avg_size:.0f} bytes")

        if self._feed_times and len(self._feed_times) > 1:
            gaps = [self._feed_times[i] - self._feed_times[i-1] for i in range(1, len(self._feed_times))]
            print(f"[TTSStream] Inter-chunk gaps: min={min(gaps)*1000:.1f}ms, max={max(gaps)*1000:.1f}ms, avg={sum(gaps)/len(gaps)*1000:.1f}ms")

        # Calculate buffer health
        if self._queue_depths:
            empty_count = sum(1 for d in self._queue_depths if d == 0)
            empty_pct = (empty_count / len(self._queue_depths)) * 100
            print(f"[TTSStream] Queue empty {empty_pct:.1f}% of pulls ({empty_count}/{len(self._queue_depths)})")

        print(f"[TTSStream] ========================\n")

    def clean_up(self):
        print("[TTSStream] Cleanup called")
        self.exists = False


# ============================================
# Audio3D Player
# ============================================
class Audio3DPlayer:
    """
    3D audio player using PyOpenAL.
    Handles both file playback and streaming.
    Supports EFX reverb effects.
    """

    def __init__(self):
        self.initialized = False
        self.source = None
        self.position_reader = PositionReader()
        self._update_thread = None
        self._stop_event = threading.Event()
        self.abort_flag = False  # For interruption support

        # EFX reverb state
        self._efx_effect = None
        self._efx_slot = None
        self._current_reverb = None  # Name of current preset

        # Pause state for soft interrupt
        self._pause_start: float = 0
        self._total_pause_duration: float = 0
        self._paused = False
        self._source_gain: float = 1.5  # Stored for restore on resume

    def pause(self):
        """Pause audio playback with gain fade (soft interrupt)."""
        if self._paused or not self.source:
            return
        self._pause_start = time.time()
        # Fade gain to 0
        gain = self._source_gain
        steps = 5
        step_time = 0.01
        for i in range(steps - 1, -1, -1):
            try:
                self.source.set_gain(gain * i / steps)
            except Exception:
                break
            time.sleep(step_time)
        try:
            self.source.pause()
        except Exception:
            pass
        self._paused = True
        print("[Audio3D] Paused (soft interrupt)")

    def resume(self) -> float:
        """Resume audio playback with gain fade. Returns total pause duration."""
        if not self._paused or not self.source:
            return 0.0
        if self._pause_start > 0:
            self._total_pause_duration += time.time() - self._pause_start
            self._pause_start = 0
        self._paused = False
        try:
            self.source.set_gain(0)
            self.source.play()
        except Exception:
            pass
        # Fade gain back to original
        gain = self._source_gain
        steps = 5
        step_time = 0.01
        for i in range(1, steps + 1):
            try:
                self.source.set_gain(gain * i / steps)
            except Exception:
                break
            if i < steps:
                time.sleep(step_time)
        print(f"[Audio3D] Resumed (total paused: {self._total_pause_duration:.1f}s)")
        return self._total_pause_duration

    @property
    def is_paused(self):
        return self._paused

    def abort(self, fade_ms=100):
        """Stop playback with a short fade out.

        Args:
            fade_ms: Fade duration in milliseconds (default 100ms)
        """
        if not self.source:
            return

        self.abort_flag = True

        # Clean up pause state
        was_paused = self._paused
        self._paused = False
        self._pause_start = 0
        self._total_pause_duration = 0

        # Only fade if not already paused (gain is already 0 when paused)
        if fade_ms > 0 and not was_paused:
            steps = max(1, fade_ms // 10)
            step_time = (fade_ms / 1000.0) / steps
            for i in range(steps - 1, -1, -1):
                try:
                    self.source.set_gain(i / steps)
                except Exception:
                    break
                time.sleep(step_time)

        try:
            self.source.stop()
            print(f"[Audio3D] Playback aborted (fade={fade_ms}ms)")
        except Exception as e:
            print(f"[Audio3D] Source stop error: {e}")

    def init(self):
        """Initialize OpenAL with HRTF and EFX"""
        if not OPENAL_AVAILABLE:
            print("[Audio3D] PyOpenAL not available")
            return False

        try:
            oalInit()
            oalSetStreamBufferCount(16)  # More buffers for streaming

            # Initialize HRTF for binaural 3D audio
            if HRTF_AVAILABLE:
                try:
                    oalInitHRTF()
                    print("[Audio3D] OpenAL initialized with HRTF")
                except Exception as e:
                    print(f"[Audio3D] HRTF init failed, using standard panning: {e}")
            else:
                print("[Audio3D] OpenAL initialized (no HRTF)")

            self.initialized = True

            # Initialize EFX functions (must be done after oalInit creates context)
            if _efx is not None and not _efx.available:
                _init_efx_functions()

            # Initialize EFX reverb effect and slot
            if _efx is not None and _efx.available:
                self._init_efx()

            return True
        except Exception as e:
            print(f"[Audio3D] Failed to initialize: {e}")
            return False

    def _init_efx(self):
        """Initialize EFX effect and slot for reverb"""
        global _efx
        if not _efx or not _efx.available:
            print("[Audio3D] EFX functions not available")
            return

        try:
            # Generate effect (ctypes needs pointer)
            effect = ctypes.c_uint(0)
            _efx.alGenEffects(1, ctypes.byref(effect))
            if effect.value == 0:
                print("[Audio3D] Failed to generate EFX effect")
                return

            # Clear any pending errors
            _efx.alGetError()

            # Set effect type to EAX Reverb
            _efx.alEffecti(effect.value, AL_EFFECT_TYPE, AL_EFFECT_EAXREVERB)
            err = _efx.alGetError()
            if err:
                print(f"[Audio3D] EAX Reverb not supported (error {hex(err)}), trying standard reverb")
                _efx.alEffecti(effect.value, AL_EFFECT_TYPE, AL_EFFECT_REVERB)
                err = _efx.alGetError()
                if err:
                    print(f"[Audio3D] Standard reverb also failed (error {hex(err)})")
                    _efx.alDeleteEffects(1, ctypes.byref(effect))
                    return

            # Generate effect slot
            slot = ctypes.c_uint(0)
            _efx.alGenAuxiliaryEffectSlots(1, ctypes.byref(slot))
            if slot.value == 0:
                print("[Audio3D] Failed to generate EFX slot")
                _efx.alDeleteEffects(1, ctypes.byref(effect))
                return

            # Attach effect to slot
            _efx.alAuxiliaryEffectSloti(slot.value, AL_EFFECTSLOT_EFFECT, effect.value)
            err = _efx.alGetError()
            if err:
                print(f"[Audio3D] Failed to attach effect to slot (error {hex(err)})")
                _efx.alDeleteAuxiliaryEffectSlots(1, ctypes.byref(slot))
                _efx.alDeleteEffects(1, ctypes.byref(effect))
                return

            self._efx_effect = effect.value
            self._efx_slot = slot.value
            print(f"[Audio3D] EFX reverb initialized (effect={effect.value}, slot={slot.value})")

        except Exception as e:
            print(f"[Audio3D] EFX init error: {e}")
            import traceback
            traceback.print_exc()

    def set_reverb(self, auxbus_name: str, send_level: float = 1.0):
        """
        Set reverb preset based on game AuxBus name.

        Args:
            auxbus_name: The AuxBus name from Lua (e.g., "OutdoorConvolution")
            send_level: Wet/dry mix (0.0-1.0), from game's SendLevel
        """
        global _efx
        if not _efx or not _efx.available or not self._efx_effect:
            return

        if not REVERB_PRESETS_AVAILABLE:
            return

        try:
            preset = get_preset_for_auxbus(auxbus_name)
            preset_name = get_preset_name_for_auxbus(auxbus_name)

            # Apply preset parameters using ctypes
            _efx.alEffectf(self._efx_effect, AL_EAXREVERB_DENSITY, preset["density"])
            _efx.alEffectf(self._efx_effect, AL_EAXREVERB_DIFFUSION, preset["diffusion"])
            _efx.alEffectf(self._efx_effect, AL_EAXREVERB_GAIN, preset["gain"] * send_level)
            _efx.alEffectf(self._efx_effect, AL_EAXREVERB_GAINHF, preset["gain_hf"])
            _efx.alEffectf(self._efx_effect, AL_EAXREVERB_GAINLF, preset["gain_lf"])
            _efx.alEffectf(self._efx_effect, AL_EAXREVERB_DECAY_TIME, preset["decay_time"])
            _efx.alEffectf(self._efx_effect, AL_EAXREVERB_DECAY_HFRATIO, preset["decay_hf_ratio"])
            _efx.alEffectf(self._efx_effect, AL_EAXREVERB_DECAY_LFRATIO, preset["decay_lf_ratio"])
            _efx.alEffectf(self._efx_effect, AL_EAXREVERB_REFLECTIONS_GAIN, preset["reflections_gain"])
            _efx.alEffectf(self._efx_effect, AL_EAXREVERB_REFLECTIONS_DELAY, preset["reflections_delay"])
            _efx.alEffectf(self._efx_effect, AL_EAXREVERB_LATE_REVERB_GAIN, preset["late_reverb_gain"])
            _efx.alEffectf(self._efx_effect, AL_EAXREVERB_LATE_REVERB_DELAY, preset["late_reverb_delay"])
            _efx.alEffectf(self._efx_effect, AL_EAXREVERB_ECHO_TIME, preset["echo_time"])
            _efx.alEffectf(self._efx_effect, AL_EAXREVERB_ECHO_DEPTH, preset["echo_depth"])
            _efx.alEffectf(self._efx_effect, AL_EAXREVERB_MODULATION_TIME, preset["modulation_time"])
            _efx.alEffectf(self._efx_effect, AL_EAXREVERB_MODULATION_DEPTH, preset["modulation_depth"])
            _efx.alEffectf(self._efx_effect, AL_EAXREVERB_AIR_ABSORPTION_GAINHF, preset["air_absorption_gain_hf"])
            _efx.alEffectf(self._efx_effect, AL_EAXREVERB_HFREFERENCE, preset["hf_reference"])
            _efx.alEffectf(self._efx_effect, AL_EAXREVERB_LFREFERENCE, preset["lf_reference"])
            _efx.alEffectf(self._efx_effect, AL_EAXREVERB_ROOM_ROLLOFF_FACTOR, preset["room_rolloff_factor"])

            # Update effect slot with new parameters
            _efx.alAuxiliaryEffectSloti(self._efx_slot, AL_EFFECTSLOT_EFFECT, self._efx_effect)

            self._current_reverb = preset_name

            # Detailed reverb debug output
            effective_gain = preset["gain"] * send_level
            print(f"[Audio3D] Reverb: {preset_name}")
            print(f"[Audio3D]   Gain: {effective_gain:.3f} (preset={preset['gain']:.3f} x send={send_level:.2f})")
            print(f"[Audio3D]   Decay: {preset['decay_time']:.2f}s | HF ratio: {preset['decay_hf_ratio']:.2f}")
            print(f"[Audio3D]   Reflections: gain={preset['reflections_gain']:.3f} delay={preset['reflections_delay']*1000:.1f}ms")
            print(f"[Audio3D]   Late reverb: gain={preset['late_reverb_gain']:.3f} delay={preset['late_reverb_delay']*1000:.1f}ms")
            print(f"[Audio3D]   Echo: depth={preset['echo_depth']:.2f} time={preset['echo_time']*1000:.0f}ms")

        except Exception as e:
            print(f"[Audio3D] Failed to set reverb: {e}")
            import traceback
            traceback.print_exc()

    def _connect_source_to_reverb(self, source_id: int, send_level: float = 1.0):
        """Connect a source to the reverb effect slot"""
        global _efx
        if not self._efx_slot:
            print("[Audio3D] No EFX slot available - reverb disabled")
            return

        if not _efx or not _efx.available:
            print("[Audio3D] EFX functions not available")
            return

        try:
            # Clear any pending errors first (OpenAL errors are sticky)
            _efx.alGetError()

            # Connect source's aux send 0 to our reverb slot
            # alSource3i(source, AL_AUXILIARY_SEND_FILTER, effectSlot, sendNumber, filter)
            print(f"[Audio3D] Connecting source {source_id} to slot {self._efx_slot}...")

            # Use alSource3i directly - it's a core function
            _efx.alSource3i(source_id, AL_AUXILIARY_SEND_FILTER, self._efx_slot, 0, 0)

            err = _efx.alGetError()
            if err:
                error_names = {0xa001: "INVALID_NAME", 0xa002: "INVALID_ENUM",
                              0xa003: "INVALID_VALUE", 0xa004: "INVALID_OPERATION"}
                print(f"[Audio3D] alSource3i failed: {error_names.get(err, hex(err))}")
            else:
                print(f"[Audio3D] Source {source_id} connected to reverb slot {self._efx_slot}")
        except Exception as e:
            print(f"[Audio3D] Error connecting source to reverb: {e}")
            import traceback
            traceback.print_exc()

    def shutdown(self):
        """Cleanup OpenAL and EFX"""
        self._stop_event.set()
        if self._update_thread:
            self._update_thread.join(timeout=1.0)
        if self.source:
            try:
                self.source.stop()
                self.source.destroy()
            except:
                pass

        # Cleanup EFX
        global _efx
        if _efx and _efx.available:
            try:
                if self._efx_slot:
                    slot = ctypes.c_uint(self._efx_slot)
                    _efx.alDeleteAuxiliaryEffectSlots(1, ctypes.byref(slot))
                    self._efx_slot = None
                if self._efx_effect:
                    effect = ctypes.c_uint(self._efx_effect)
                    _efx.alDeleteEffects(1, ctypes.byref(effect))
                    self._efx_effect = None
            except:
                pass

        if self.initialized:
            try:
                oalQuit()
            except:
                pass
        self.initialized = False

    def _update_positions(self):
        """Update listener and source positions (runs in thread)"""
        listener = oalGetListener()

        while not self._stop_event.is_set():
            try:
                # Fetch latest position from socket (updates prev/curr if new data)
                self.position_reader.update()

                # Interpolate between prev and curr for smooth panning
                self.position_reader.interpolate()

                # Update listener (camera)
                listener.set_position(self.position_reader.get_listener_position())
                listener.set_orientation(self.position_reader.get_listener_orientation())

                # Update source (NPC)
                if self.source:
                    self.source.set_position(self.position_reader.get_source_position())

                time.sleep(0.02)  # 50Hz update
            except:
                pass

    def play_file(self, filename):
        """Play a WAV file with 3D positioning"""
        if not self.initialized:
            if not self.init():
                return False

        filepath = os.path.join(SONORUS_DIR, filename)
        if not os.path.exists(filepath):
            print(f"[Audio3D] File not found: {filepath}")
            return False

        try:
            # Load file and create source
            wav = WaveFile(filepath)
            buffer = Buffer(wav)
            self.source = Source(buffer, destroy_buffer=True)

            # Set initial position
            self.source.set_position(self.position_reader.get_source_position())

            # Configure 3D audio properties
            self.source.set_rolloff_factor(1.0)
            self.source.set_reference_distance(1.0)  # 1 meter
            self.source.set_max_distance(50.0)  # 50 meters

            # Start position update thread
            self._stop_event.clear()
            self._update_thread = threading.Thread(target=self._update_positions, daemon=True)
            self._update_thread.start()

            # Play
            print("[Audio3D] Playing...")
            self.source.play()

            # Wait for completion
            try:
                while self.source and self.source.get_state() == AL_PLAYING:
                    time.sleep(0.05)
            except Exception as e:
                print(f"[Audio3D] State check error: {e}")

            # Cleanup
            self._stop_event.set()
            if self.source is not None:
                try:
                    self.source.destroy()
                except Exception as e:
                    print(f"[Audio3D] Destroy error: {e}")
                self.source = None

            print("[Audio3D] Playback complete")
            return True

        except Exception as e:
            print(f"[Audio3D] Playback error: {e}")
            import traceback
            traceback.print_exc()
            return False

    def play_stream(self, tts_stream, on_chunk_callback=None, on_start=None, use_3d=True,
                    reverb_auxbus=None, reverb_send=1.0, abort_check=None,
                    sentence_boundaries=None, centered=False):
        """
        Play streaming TTS audio with optional 3D positioning and reverb.

        Args:
            on_start: Callback called RIGHT when audio playback begins (for accurate timing)
            use_3d: If False, plays centered stereo audio without 3D spatialization (for player voice)
            reverb_auxbus: Game AuxBus name for reverb preset (e.g., "OutdoorConvolution")
            reverb_send: Wet/dry mix for reverb (0.0-1.0)
            abort_check: Callback that returns True if playback should abort (epoch stale).
            sentence_boundaries: Optional narration boundaries (not implemented for PyOpenAL fallback).
        """
        if not self.initialized:
            if not self.init():
                return False

        # Check abort before starting
        if abort_check and abort_check():
            print("[Audio3D] Abort before start (epoch stale)")
            return False

        self.abort_flag = False  # Reset abort flag at start
        self._paused = False
        self._pause_start = 0
        self._total_pause_duration = 0

        try:
            # CRITICAL: Wait for buffer to have data BEFORE creating SourceStream
            # SourceStream constructor pulls 15+ buffers synchronously, causing underruns
            # if the queue is empty
            print("[Audio3D] Waiting for buffer data before SourceStream creation...")
            wait_start = time.time()
            while tts_stream.buffer_queue.empty():
                if time.time() - wait_start > 10.0:
                    print("[Audio3D] ERROR: Timeout waiting for buffer data")
                    return False
                if self.abort_flag or (abort_check and abort_check()):
                    print("[Audio3D] Aborted while waiting for buffer")
                    return False
                time.sleep(0.01)

            queue_size = tts_stream.buffer_queue.qsize()
            print(f"[Audio3D] Buffer has {queue_size} chunk(s), creating SourceStream...")

            # NOW create streaming source (it will pull from populated queue)
            self.source = SourceStream(tts_stream)

            # Configure audio - 3D or centered stereo
            if use_3d and centered:
                # FPV/VR player voice: source-relative, fixed in front of listener, reverb still applies
                self.source.set_source_relative(True)
                self.source.set_position((0, 0, -0.5))  # 0.5m in front
                self.source.set_gain(1.5)
                self._source_gain = 1.5
                print("[Audio3D] Centered 3D mode: fixed at (0, 0, -0.5) with reverb")
            elif use_3d:
                # NOTE: Initial positions are set via set_initial_positions() BEFORE this call
                # No need to call update() here - positions are passed explicitly through the call chain

                # Load audio settings
                from utils.settings import load_settings
                settings = load_settings()
                audio_cfg = settings.get('audio', {})

                # Volume: user % + 50% boost (100% = 1.0 + 0.5 = 1.5 gain)
                user_volume = audio_cfg.get('volume', 100) / 100.0
                gain = user_volume + 0.5
                rolloff = audio_cfg.get('rolloff', 0.5)

                # Configure 3D audio
                source_pos = self.position_reader.get_source_position()
                self.source.set_position(source_pos)
                self.source.set_gain(gain)
                self._source_gain = gain
                self.source.set_rolloff_factor(rolloff)
                self.source.set_reference_distance(2.0)  # Distance at full volume (meters)
                self.source.set_max_distance(100.0)  # Max distance (meters)

                # DIAGNOSTIC: Log 3D audio setup
                print(f"[Audio3D] Source position: {source_pos}")
                print(f"[Audio3D] Config: Gain={gain}, Rolloff={rolloff}, RefDist=2.0m, MaxDist=100m")

                # Set up listener (camera position)
                listener = oalGetListener()
                listener_pos = self.position_reader.get_listener_position()
                listener.set_position(listener_pos)
                listener.set_orientation(self.position_reader.get_listener_orientation())
                print(f"[Audio3D] Listener position: {listener_pos}")

                # Start position update thread (skip if 3D disabled globally)
                if not DISABLE_3D_POSITIONING:
                    self._stop_event.clear()
                    self._update_thread = threading.Thread(target=self._update_positions, daemon=True)
                    self._update_thread.start()
            else:
                # Non-3D: centered stereo playback (for player voice)
                # CRITICAL: After 3D playback, listener may be at a far position.
                # Set source-relative so position (0,0,0) means "at the listener's head"
                self.source.set_source_relative(True)
                self.source.set_position((0, 0, 0))
                self.source.set_gain(1.5)
                self._source_gain = 1.5
                print("[Audio3D] Non-3D mode: centered stereo playback")

            # Apply reverb if specified
            if reverb_auxbus and EFX_AVAILABLE and self._efx_slot:
                self.set_reverb(reverb_auxbus, reverb_send)
                # Connect source to reverb - need source ID
                try:
                    source_id = int(self.source.id)  # PyOpenAL source ID as int
                    self._connect_source_to_reverb(source_id)
                except Exception as e:
                    print(f"[Audio3D] Failed to get source ID for reverb: {e}")
            elif not reverb_auxbus:
                print("[Audio3D] No reverb (auxbus not specified)")
            elif not EFX_AVAILABLE:
                print("[Audio3D] No reverb (EFX not available)")
            elif not self._efx_slot:
                print("[Audio3D] No reverb (EFX slot not created)")

            # Start playback
            self.source.play()
            tts_stream.playback_started = True  # Now underruns matter
            playback_start = time.time()
            print(f"[Audio3D] {'3D' if use_3d else 'Stereo'} playback started")

            # Call on_start callback - this is the EXACT moment audio begins
            if on_start:
                try:
                    on_start(playback_start)
                except Exception as e:
                    print(f"[Audio3D] on_start callback error: {e}")

            # Update loop - feeds buffers and keeps playing
            update_count = 0
            restart_count = 0
            while True:
                try:
                    if not self.source.update():
                        break  # No more buffers to process
                except Exception as e:
                    # OpenAL errors (AL_INVALID_VALUE etc) can occur during buffer unqueue
                    # This typically means playback is done or in an inconsistent state
                    print(f"[Audio3D] Update error (ending playback): {e}")
                    break

                update_count += 1

                # Check for unexpected stop (buffer starvation) and restart
                # Only restart if: not aborted, stream not done, and source stopped unexpectedly
                try:
                    state = self.source.get_state() if self.source else 0
                    if state == AL_STOPPED and not self.abort_flag and not tts_stream.done:
                        # Source stopped but we have more data - restart it
                        restart_count += 1
                        elapsed = time.time() - playback_start
                        print(f"[Audio3D] *** AL_STOPPED detected at {elapsed:.2f}s - restarting playback (#{restart_count}) ***")
                        self.source.play()
                except Exception:
                    pass

                # DIAGNOSTIC: Log state every 100 updates (~1 second at 10ms sleep)
                if update_count % 100 == 0:
                    elapsed = time.time() - playback_start
                    try:
                        state = self.source.get_state() if self.source else 0
                        print(f"[Audio3D] Update #{update_count}, elapsed={elapsed:.2f}s, AL_state={state}")
                    except Exception:
                        pass  # Skip logging if state check fails

                # Check for abort (interruption or epoch stale)
                if self.abort_flag or (abort_check and abort_check()):
                    print("[Audio3D] Playback aborted")
                    try:
                        if self.source:
                            self.source.stop()
                    except Exception:
                        pass
                    break
                time.sleep(0.01)

            restart_msg = f", {restart_count} restarts" if restart_count > 0 else ""
            print(f"[Audio3D] Update loop ended after {update_count} updates{restart_msg}")

            # Wait for final buffers to finish (unless aborted)
            wait_start = time.time()
            if not self.abort_flag and self.source is not None:
                try:
                    while self.source.get_state() == AL_PLAYING:
                        if self.abort_flag or (abort_check and abort_check()):
                            # Abort requested during wait - stop immediately
                            try:
                                self.source.stop()
                            except Exception:
                                pass
                            break
                        time.sleep(0.02)
                except Exception:
                    pass  # State check failed, assume done

            wait_duration = time.time() - wait_start
            total_duration = time.time() - playback_start
            tts_stream.playback_started = False  # Playback done, no more underruns possible

            # Cleanup
            self._stop_event.set()
            if self.source is not None:
                try:
                    self.source.destroy()
                except Exception:
                    pass
                self.source = None
            aborted = self.abort_flag
            self.abort_flag = False  # Reset for next playback
            print(f"[Audio3D] Playback {'aborted' if aborted else 'complete'}: total={total_duration:.2f}s, final_buffer_wait={wait_duration:.2f}s")
            return not aborted

        except Exception as e:
            tts_stream.playback_started = False
            print(f"[Audio3D] Stream error: {e}")
            import traceback
            traceback.print_exc()
            return False


# ============================================
# Simple API
# ============================================
_player = None

def get_player():
    """Get or create the global Audio3D player"""
    global _player
    if _player is None:
        _player = Audio3DPlayer()
    return _player

def play_file_3d(filename):
    """Play a WAV file with 3D audio"""
    return get_player().play_file(filename)

def create_tts_stream(sample_rate=44100, channels=1):
    """Create a TTS stream for feeding PCM chunks"""
    return TTSStream(sample_rate, channels)

def play_tts_stream(stream):
    """Play a TTS stream with 3D audio"""
    return get_player().play_stream(stream)

def shutdown():
    """Cleanup audio system"""
    global _player
    if _player:
        _player.shutdown()
        _player = None


def test_reverb(auxbus_name: str = None, send_level: float = 1.0,
                wav_file: str = "voice_references/GreyCat_reference_15s.wav"):
    """
    Test reverb by playing a WAV file with the specified reverb preset.

    Args:
        auxbus_name: Reverb preset name (e.g., "OutdoorConvolution"). If None, uses current from Lua.
        send_level: Wet/dry mix (0.0-1.0)
        wav_file: Path to WAV file relative to sonorus folder
    """
    player = get_player()
    if not player.initialized:
        player.init()

    filepath = os.path.join(SONORUS_DIR, wav_file)
    if not os.path.exists(filepath):
        print(f"[TestReverb] File not found: {filepath}")
        return False

    # If no auxbus specified, try to get from lua_socket
    if auxbus_name is None:
        try:
            # Import here to avoid circular imports
            from utils.lua_socket import lua_socket
            reverb = lua_socket.get_current_reverb()
            auxbus_name = reverb.get("auxbus")
            send_level = reverb.get("send", 1.0)
            print(f"[TestReverb] Using current zone reverb: {auxbus_name}")
        except Exception as e:
            print(f"[TestReverb] Could not get current reverb: {e}")
            auxbus_name = "OutdoorOverland"  # Fallback

    print(f"[TestReverb] Playing {wav_file} with reverb: {auxbus_name} (send={send_level:.2f})")

    try:
        # Load and play file
        wav = WaveFile(filepath)
        buffer = Buffer(wav)
        player.source = Source(buffer, destroy_buffer=True)

        # Center audio (source-relative at origin)
        player.source.set_source_relative(True)
        player.source.set_position((0, 0, 0))
        player.source.set_gain(1.5)

        # Apply reverb
        if auxbus_name and _efx and _efx.available and player._efx_slot:
            player.set_reverb(auxbus_name, send_level)
            try:
                source_id = int(player.source.id)  # PyOpenAL source ID as int
                player._connect_source_to_reverb(source_id)
            except Exception as e:
                print(f"[TestReverb] Failed to connect reverb: {e}")
        else:
            print("[TestReverb] Reverb not available")

        # Play
        player.source.play()
        print("[TestReverb] Playing...")

        # Wait for completion
        while player.source and player.source.get_state() == AL_PLAYING:
            time.sleep(0.1)

        # Cleanup
        if player.source:
            player.source.destroy()
            player.source = None

        print("[TestReverb] Done")
        return True

    except Exception as e:
        print(f"[TestReverb] Error: {e}")
        import traceback
        traceback.print_exc()
        return False


# ============================================
# Test
# ============================================
if __name__ == "__main__":
    print("Testing Audio3D...")

    if not OPENAL_AVAILABLE:
        print("PyOpenAL not installed!")
        exit(1)

    # Test file playback
    test_file = "test.wav"
    if os.path.exists(os.path.join(SONORUS_DIR, test_file)):
        print(f"Playing {test_file}...")
        play_file_3d(test_file)
    else:
        print(f"No {test_file} found for testing")

    shutdown()
    print("Done!")
