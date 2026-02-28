"""
Voice input capture for Speech-to-Text.

Supports two modes:
1. Push-to-Talk (PTT): Hold hotkey to record, release to transcribe
2. Open Mic: Toggle continuous listening with VAD-based turn detection

Uses sounddevice for microphone recording.

Protection mechanisms:
- Only activates when game window is active
- Blocks when game is paused (via check_pause callback)
- Lets Alt/Win system combos through
- Anti-repeat protection after recording stops
- Cancels recording if game loses focus mid-recording
"""
import threading
import time
import ctypes
import os
import queue
from typing import Optional, Literal, Callable
from pynput import keyboard, mouse
import sounddevice as sd
import numpy as np

# Lua socket instance (set by server.py)
_lua_socket = None

def set_lua_socket(socket):
    """Set the lua socket instance for sending notifications."""
    global _lua_socket
    _lua_socket = socket


def _send_stt_state(active: bool, source: str = "ptt"):
    """Send STT input state to Lua for preview lock management.

    Args:
        active: True when speech input starts, False when cancelled/failed
        source: "ptt" for push-to-talk, "open_mic" for VAD-based
    """
    if _lua_socket:
        try:
            _lua_socket.send({
                "type": "stt_input",
                "active": active,
                "source": source
            })
        except Exception as e:
            print(f"[STT] Failed to send stt_input: {e}")

def _apply_mic_gain(audio_bytes: bytes) -> bytes:
    """Apply mic gain boost from settings to raw PCM int16 audio bytes."""
    from utils.settings import get_setting
    gain_db = get_setting('stt.mic_gain_db', 0)
    if not gain_db:
        return audio_bytes
    gain_linear = 10 ** (gain_db / 20.0)
    audio = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32)
    audio = np.clip(audio * gain_linear, -32768, 32767).astype(np.int16)
    return audio.tobytes()


# Sound file paths (wav for winsound compatibility)
# sounds/ is at sonorus root, not in input/
_SONORUS_DIR = os.path.dirname(os.path.dirname(__file__))
_SOUNDS_DIR = os.path.join(_SONORUS_DIR, 'sounds')
_SOUND_ON = os.path.join(_SOUNDS_DIR, 'stt-on.wav')
_SOUND_OFF = os.path.join(_SOUNDS_DIR, 'stt-off.wav')
_SOUND_ERR = os.path.join(_SOUNDS_DIR, 'stt-err.wav')
_SOUND_TOGGLE_ON = os.path.join(_SOUNDS_DIR, 'stt-toggle-on.wav')
_SOUND_TOGGLE_OFF = os.path.join(_SOUNDS_DIR, 'stt-toggle-off.wav')


def _play_sound(path, delay=0):
    """Play a wav file in background thread (non-blocking)."""
    import winsound
    def _play():
        try:
            if delay > 0:
                time.sleep(delay)
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
        except Exception as e:
            print(f"[STT] Sound playback error: {e}")
    threading.Thread(target=_play, daemon=True).start()


def play_error_sound():
    """Play error sound for blocked actions (public API)."""
    _play_sound(_SOUND_ERR)


user32 = ctypes.windll.user32

# XInput for controller aim detection (LT = aim on gamepad)
# xinput1_4.dll ships with Windows — zero dependency
class _XINPUT_GAMEPAD(ctypes.Structure):
    _fields_ = [
        ("wButtons", ctypes.c_ushort),
        ("bLeftTrigger", ctypes.c_ubyte),
        ("bRightTrigger", ctypes.c_ubyte),
        ("sThumbLX", ctypes.c_short), ("sThumbLY", ctypes.c_short),
        ("sThumbRX", ctypes.c_short), ("sThumbRY", ctypes.c_short),
    ]

class _XINPUT_STATE(ctypes.Structure):
    _fields_ = [
        ("dwPacketNumber", ctypes.c_ulong),
        ("Gamepad", _XINPUT_GAMEPAD),
    ]

try:
    _xinput = ctypes.windll.xinput1_4
except OSError:
    try:
        _xinput = ctypes.windll.xinput9_1_0
    except OSError:
        _xinput = None

VK_RBUTTON = 0x02  # Right mouse button

def is_aiming() -> bool:
    """Check if player is aiming (right-click or controller LT). Instant, no polling."""
    # Mouse right-click
    if (user32.GetAsyncKeyState(VK_RBUTTON) & 0x8000) != 0:
        return True
    # Controller left trigger (analog 0-255, threshold ~50 to avoid accidental)
    if _xinput is not None:
        state = _XINPUT_STATE()
        if _xinput.XInputGetState(0, ctypes.byref(state)) == 0:
            if state.Gamepad.bLeftTrigger > 50:
                return True
    return False

# Windows message types
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105

# VK codes for modifiers (let these through)
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12  # Alt
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1
VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3
VK_LMENU = 0xA4
VK_RMENU = 0xA5

VK_MODIFIERS = {VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN,
                VK_LSHIFT, VK_RSHIFT, VK_LCONTROL, VK_RCONTROL, VK_LMENU, VK_RMENU}

# VK codes for function keys
VK_F1 = 0x70
VK_F2 = 0x71
VK_F3 = 0x72
VK_F4 = 0x73
VK_F5 = 0x74
VK_F6 = 0x75
VK_F7 = 0x76
VK_F8 = 0x77
VK_F9 = 0x78
VK_F10 = 0x79
VK_RETURN = 0x0D

# Hotkey name to VK code mapping
HOTKEY_VK_MAP = {
    'f1': VK_F1, 'f2': VK_F2, 'f3': VK_F3, 'f4': VK_F4, 'f5': VK_F5,
    'f6': VK_F6, 'f7': VK_F7, 'f8': VK_F8, 'f9': VK_F9, 'f10': VK_F10,
    'enter': VK_RETURN,
}


def is_key_pressed(vk):
    """Check if a key is currently pressed."""
    return (user32.GetAsyncKeyState(vk) & 0x8000) != 0


def get_active_window_title():
    """Get the title of the active window."""
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return ""
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        return buffer.value
    except:
        return ""


def is_game_window_active():
    """Check if Hogwarts Legacy window is active."""
    title = get_active_window_title().lower().strip()
    return title == "hogwarts legacy"


def can_activate_hotkey(check_pause_callback=None) -> bool:
    """
    Shared guard for all hotkeys - checks if hotkey should be allowed.

    Returns True if hotkey can activate, False if blocked.
    Checks:
    - Mod is enabled (server.enabled setting)
    - Game window is active (foreground)
    - Game is not paused/in cinematic (via callback)
    """
    # Check if mod is enabled
    try:
        from utils.settings import load_settings
        settings = load_settings()
        if not settings.get('server', {}).get('enabled', True):
            return False
    except Exception:
        pass  # If settings fail to load, allow activation
    if not is_game_window_active():
        return False
    if check_pause_callback and check_pause_callback():
        return False
    return True


class RingBuffer:
    """Fixed-size ring buffer for audio samples."""

    def __init__(self, max_samples: int):
        self._buffer = np.zeros(max_samples, dtype=np.float32)
        self._write_pos = 0
        self._samples_written = 0
        self._max_samples = max_samples

    def write(self, samples: np.ndarray):
        """Write samples to buffer."""
        if samples.dtype == np.int16:
            samples = samples.astype(np.float32) / 32768.0

        n = len(samples)
        if n >= self._max_samples:
            self._buffer[:] = samples[-self._max_samples:]
            self._write_pos = 0
            self._samples_written = self._max_samples
        else:
            end_pos = self._write_pos + n
            if end_pos <= self._max_samples:
                self._buffer[self._write_pos:end_pos] = samples
            else:
                first_part = self._max_samples - self._write_pos
                self._buffer[self._write_pos:] = samples[:first_part]
                self._buffer[:n - first_part] = samples[first_part:]
            self._write_pos = end_pos % self._max_samples
            self._samples_written = min(self._samples_written + n, self._max_samples)

    def get_last_n_samples(self, n: int) -> np.ndarray:
        """Get the last n samples from the buffer."""
        n = min(n, self._samples_written)
        if n == 0:
            return np.array([], dtype=np.float32)

        if self._write_pos >= n:
            return self._buffer[self._write_pos - n:self._write_pos].copy()
        else:
            first_part = n - self._write_pos
            return np.concatenate([
                self._buffer[self._max_samples - first_part:],
                self._buffer[:self._write_pos]
            ])

    def get_all(self) -> np.ndarray:
        """Get all samples in chronological order."""
        return self.get_last_n_samples(self._samples_written)

    def clear(self):
        """Clear the buffer."""
        self._write_pos = 0
        self._samples_written = 0

    @property
    def samples_written(self) -> int:
        return self._samples_written


class STTCapture:
    """
    Voice input capture with PTT and Open Mic modes.

    PTT Mode: Hold hotkey to record, release to transcribe
    Open Mic Mode: Toggle continuous listening with VAD turn detection
    """

    def __init__(self, on_transcribe_callback, hotkey='middle_mouse', check_pause=None, on_error=None, on_interrupt=None):
        """
        Args:
            on_transcribe_callback: Called with transcribed text
            hotkey: Push-to-talk key (default: 'middle_mouse')
            check_pause: Optional callable that returns True if capture should be blocked
            on_error: Optional callback for error messages (for user notification)
            on_interrupt: Optional callback called when speech starts (for interrupting audio)
        """
        self.on_transcribe = on_transcribe_callback
        self.hotkey_name = hotkey.lower()
        self.check_pause = check_pause
        self.on_error = on_error
        self.on_interrupt = on_interrupt  # Called when speech starts (to interrupt playback)
        self.on_soft_interrupt: Optional[Callable[[], None]] = None
        self.on_soft_interrupt_cancel: Optional[Callable[[], None]] = None

        # Mode: 'ptt' or 'open_mic'
        self._mode: Literal['ptt', 'open_mic'] = 'ptt'

        # Recording state (PTT mode)
        self.recording = False
        self.audio_buffer = []
        self._lock = threading.Lock()
        self._stop_time = 0  # Timestamp of last recording stop (anti-repeat)
        self._current_sample_rate = 16000  # Set when recording starts

        # Open mic state
        self._open_mic_active = False
        self._open_mic_thread: Optional[threading.Thread] = None
        self._open_mic_stop_event = threading.Event()
        self._open_mic_lock = threading.Lock()  # Protects utterance state
        self._vad_processor = None
        self._turn_detector = None
        self._ring_buffer: Optional[RingBuffer] = None
        self._utterance_buffer: list = []
        self._speech_in_progress = False
        self._last_speech_end_time = 0
        self._silence_start_time = 0
        self._turn_timeout = 3.0
        self._min_silence_for_turn = 1.0  # Wait for silence before checking turn detection (loaded from settings)
        self._turn_check_pending = False  # True when silence detected, reset on speech or turn complete
        self._last_turn_check_time = 0  # Timestamp of last turn detection check
        self._pre_speech_samples = 16000  # 1 second at 16kHz
        # Audio queue for VAD processing (separate from real-time callback)
        self._audio_queue: Optional[list] = None
        self._audio_queue_lock = threading.Lock()

        # Listeners
        self.keyboard_listener = None
        self.mouse_listener = None

        # Audio stream
        self._stream = None

        # Determine input type
        self._is_mouse_hotkey = self.hotkey_name == 'middle_mouse'
        self._hotkey_vk = HOTKEY_VK_MAP.get(self.hotkey_name, None)

        # Callback for speech start (for interruption handling)
        self.on_speech_start: Optional[Callable[[], None]] = None

        # Spell detection state (shared by PTT and open mic)
        self._spell_queue: Optional[queue.Queue] = None
        self._spell_stop: Optional[threading.Event] = None
        self._spell_thread: Optional[threading.Thread] = None
        self._spell_detected = False
        self._spell_detect_time = 0.0

    @property
    def mode(self) -> Literal['ptt', 'open_mic']:
        """Current capture mode."""
        return self._mode

    @mode.setter
    def mode(self, value: Literal['ptt', 'open_mic']):
        """Set capture mode. Stops current capture if changing modes."""
        if value == self._mode:
            return

        # Stop current mode
        if self._mode == 'open_mic' and self._open_mic_active:
            self._stop_open_mic()
        elif self._mode == 'ptt' and self.recording:
            self._cancel_recording()

        self._mode = value
        print(f"[STT] Mode changed to: {value}")

    def start(self):
        """Start listening for hotkey."""
        if self._is_mouse_hotkey:
            if self.mouse_listener is not None:
                return
            self.mouse_listener = mouse.Listener(on_click=self._on_mouse_click)
            self.mouse_listener.start()
        else:
            if self.keyboard_listener is not None:
                return
            self.keyboard_listener = keyboard.Listener(
                win32_event_filter=self._win32_filter,
                suppress=False
            )
            self.keyboard_listener.start()

        print(f"[STT] Capture started (hotkey: {self.hotkey_name}, mode: {self._mode})")

    def _on_mouse_click(self, x, y, button, pressed):
        """Handle mouse button events."""
        if button != mouse.Button.middle:
            return

        if self._mode == 'open_mic':
            # Toggle mode: only respond to press, not release
            if pressed:
                self._handle_open_mic_toggle()
        else:
            # PTT mode: hold to record
            self._handle_ptt_event(pressed)

    def _win32_filter(self, msg, data):
        """Handle keyboard hotkey press/release."""
        vk = data.vkCode

        if vk in VK_MODIFIERS:
            return
        if vk != self._hotkey_vk:
            return

        # Let Alt/Win combos through
        alt_pressed = is_key_pressed(VK_MENU) or is_key_pressed(VK_LMENU) or is_key_pressed(VK_RMENU)
        win_pressed = is_key_pressed(VK_LWIN) or is_key_pressed(VK_RWIN)
        if alt_pressed or win_pressed:
            return

        if self._mode == 'open_mic':
            # Toggle mode: only respond to key down
            if msg in (WM_KEYDOWN, WM_SYSKEYDOWN):
                self._handle_open_mic_toggle()
                self.keyboard_listener.suppress_event()
        else:
            # PTT mode
            if msg in (WM_KEYDOWN, WM_SYSKEYDOWN):
                self._handle_ptt_event(pressed=True)
                self.keyboard_listener.suppress_event()
            elif msg in (WM_KEYUP, WM_SYSKEYUP):
                self._handle_ptt_event(pressed=False)
                self.keyboard_listener.suppress_event()

    def _handle_ptt_event(self, pressed: bool):
        """Handle PTT press/release events."""
        if not self.recording:
            # Starting recording - check all guards
            if time.time() - self._stop_time < 0.15:
                return
            if not can_activate_hotkey(self.check_pause):
                return
            if pressed:
                self._start_recording()
        else:
            # Recording in progress - only check focus (pause handled in callback)
            if not is_game_window_active():
                print("[STT] Recording cancelled (game lost focus)")
                self._cancel_recording()
                return
            if not pressed:
                self._stop_recording()

    def _handle_open_mic_toggle(self):
        """Handle open mic toggle."""
        # Shared guard - game must be active and not paused
        if not can_activate_hotkey(self.check_pause):
            return

        # Anti-repeat
        if time.time() - self._stop_time < 0.15:
            return

        if not self._open_mic_active:
            self._start_open_mic()
        else:
            self._stop_open_mic()

    # ==================== Spell Detection ====================

    def _start_spell_detection(self, on_detected):
        """Start spell detection worker thread. on_detected(game_spell, model_name) called on hit."""
        from utils.settings import get_setting
        if not get_setting('stt.voice_spells', True):
            return

        from services.spell_detector import get_detector
        detector = get_detector()
        if detector is None:
            # Not loaded yet (preload still running or failed) — skip for this session.
            # Don't call warm_up() here: it blocks and we may be inside self._lock.
            return

        self._spell_queue = queue.Queue()
        self._spell_stop = threading.Event()
        self._spell_detected = False
        self._spell_detect_time = 0.0

        self._spell_thread = threading.Thread(
            target=self._spell_detection_worker,
            args=(self._spell_queue, self._spell_stop, on_detected),
            daemon=True
        )
        self._spell_thread.start()

    def _stop_spell_detection(self):
        """Stop spell detection worker thread and clean up."""
        if self._spell_stop:
            self._spell_stop.set()
        if self._spell_thread and self._spell_thread.is_alive():
            self._spell_thread.join(timeout=1.0)
        self._spell_thread = None
        self._spell_queue = None
        self._spell_stop = None

    def _spell_detection_worker(self, spell_queue, stop_event, on_detected):
        """Always-on spell detection: pull int16 chunks, run predict(), call on_detected.

        Runs continuously with no detection window. Uses a simple 1s cooldown after
        each detection to prevent double-triggers. Aim gating happens at decision
        points (VAD speech start, _send_cast_spell) rather than here.
        """
        from utils.settings import get_setting
        from services.spell_detector import get_detector, get_best_detection

        detector = get_detector()
        if detector is None:
            print("[SpellDetect] Worker: detector is None, exiting")
            return

        threshold = get_setting('stt.spell_detection_threshold', 0.8)
        BATCH_SIZE = 1280
        audio_accumulator = np.array([], dtype=np.int16)
        last_detection_time = 0.0
        COOLDOWN = 1.0

        while not stop_event.is_set():
            try:
                chunk = spell_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            # Simple cooldown after detection (prevents double-triggers)
            if time.time() - last_detection_time < COOLDOWN:
                continue

            audio_accumulator = np.concatenate([audio_accumulator, chunk])
            if len(audio_accumulator) < BATCH_SIZE:
                continue

            while len(audio_accumulator) >= BATCH_SIZE:
                batch = audio_accumulator[:BATCH_SIZE]
                audio_accumulator = audio_accumulator[BATCH_SIZE:]
                try:
                    scores = detector.predict(batch)
                    result = get_best_detection(scores, threshold)
                    if result:
                        game_spell, model_name, score = result
                        print(f"[SpellDetect] Detected: {model_name} -> {game_spell} (score={score:.3f})")
                        on_detected(game_spell, model_name)
                        detector.reset()
                        audio_accumulator = np.array([], dtype=np.int16)
                        last_detection_time = time.time()
                        while not spell_queue.empty():
                            try:
                                spell_queue.get_nowait()
                            except queue.Empty:
                                break
                        break
                except Exception as e:
                    print(f"[SpellDetect] Error: {e}")

    def _send_cast_spell(self, game_spell) -> bool:
        """Send cast_spell command to Lua (wakeword source, no text field).

        Returns True if the spell was sent, False if suppressed (VR gate, no socket).
        """
        # Gate on aiming/spell pose before sending
        try:
            from vr import is_vr_active, is_vr_spell_mode
            if is_vr_active():
                if not is_vr_spell_mode():
                    print(f"[SpellDetect] Suppressed {game_spell} (VR wand not in pose)")
                    return False
            elif not is_aiming():
                print(f"[SpellDetect] Suppressed {game_spell} (not aiming)")
                return False
        except Exception:
            if not is_aiming():
                print(f"[SpellDetect] Suppressed {game_spell} (not aiming)")
                return False
        # Aiming confirmed or VR wand ready: send to Lua
        if _lua_socket:
            try:
                _lua_socket.send({
                    "type": "cast_spell",
                    "spell": game_spell,
                })
                return True
            except Exception as e:
                print(f"[SpellDetect] Failed to send cast_spell: {e}")
        return False

    # ==================== PTT Mode Methods ====================

    def _start_recording(self):
        """Begin audio capture (PTT mode)."""
        t0 = time.time()
        with self._lock:
            if self.recording:
                return

            self.recording = True
            self.audio_buffer = []

            from utils.settings import load_settings
            settings = load_settings()
            stt_settings = settings.get('stt', {})
            sample_rate = stt_settings.get('sample_rate', 16000)
            channels = stt_settings.get('channels', 1)
            self._current_sample_rate = sample_rate

            # Reference for spell queue (captured in closure)
            spell_q = None

            def audio_callback(indata, frames, time_info, status):
                if status:
                    print(f"[STT] Audio status: {status}")
                if self.recording:
                    self.audio_buffer.append(indata.copy())
                    # Feed spell detection queue (non-blocking)
                    if spell_q is not None:
                        try:
                            spell_q.put_nowait(indata.flatten().copy())
                        except queue.Full:
                            pass

            try:
                self._stream = sd.InputStream(
                    samplerate=sample_rate,
                    channels=channels,
                    dtype='int16',
                    callback=audio_callback,
                    blocksize=512,
                    latency='low'
                )
                self._stream.start()

                # Start spell detection thread
                def _on_ptt_spell_detected(game_spell, model_name):
                    if not self._send_cast_spell(game_spell):
                        return  # Suppressed (VR gate) — let PTT continue as normal speech
                    self._spell_detected = True
                    self._spell_detect_time = time.time()
                    # Release STT preview lock (spell fires immediately)
                    _send_stt_state(False, "ptt")

                self._start_spell_detection(_on_ptt_spell_detected)
                # Set closure reference so audio callback feeds the queue
                if self._spell_queue is not None:
                    spell_q = self._spell_queue

                # Notify Lua to lock NPC (preview lock for STT)
                _send_stt_state(True, "ptt")

                # Trigger vision capture
                try:
                    from vision_agent import get_agent
                    agent = get_agent()
                    if agent:
                        agent.capture_now()
                except Exception:
                    pass

                # Fire speech start callback (for interruption)
                if self.on_speech_start:
                    try:
                        self.on_speech_start()
                    except Exception as e:
                        print(f"[STT] on_speech_start error: {e}")

                _play_sound(_SOUND_ON)
                print(f"[STT] Recording started ({(time.time()-t0)*1000:.0f}ms)")
            except Exception as e:
                print(f"[STT] Failed to start recording: {e}")
                _play_sound(_SOUND_ERR)
                self.recording = False
                self._stop_spell_detection()

    def _cancel_recording(self):
        """Cancel recording without transcribing."""
        with self._lock:
            if not self.recording:
                return

            self.recording = False
            self._stop_time = time.time()

            # Stop spell detection
            self._stop_spell_detection()

            # Stop stream
            if self._stream:
                try:
                    self._stream.stop()
                    self._stream.close()
                except:
                    pass
                self._stream = None

            self.audio_buffer = []

            # Notify Lua to release preview lock
            _send_stt_state(False, "ptt")
            print("[STT] Recording cancelled")

    def _stop_recording(self):
        """Stop recording and transcribe."""
        with self._lock:
            if not self.recording:
                return

            self.recording = False
            self._stop_time = time.time()

            # Stop spell detection thread, then read result
            self._stop_spell_detection()
            spell_was_detected = self._spell_detected
            spell_detect_time = self._spell_detect_time

            # Stop stream
            if self._stream:
                try:
                    self._stream.stop()
                    self._stream.close()
                except:
                    pass
                self._stream = None

            _play_sound(_SOUND_OFF)

            # Combine audio chunks
            if not self.audio_buffer:
                print("[STT] No audio recorded")
                # Release preview lock (unless spell already released it)
                if not spell_was_detected:
                    _send_stt_state(False, "ptt")
                return

            audio_data = np.concatenate(self.audio_buffer)

            # If spell was detected during PTT, trim buffer to post-detection audio only
            if spell_was_detected:
                # Calculate how many samples came after spell detection
                elapsed_since_spell = self._stop_time - spell_detect_time
                post_spell_samples = int(elapsed_since_spell * self._current_sample_rate)
                if post_spell_samples > 0 and post_spell_samples < len(audio_data):
                    audio_data = audio_data[-post_spell_samples:]
                    duration = len(audio_data) / self._current_sample_rate
                    print(f"[STT] Spell detected, trimmed to post-spell audio ({duration:.1f}s)")
                    if duration < 0.3:
                        print("[STT] Post-spell audio too short, skipping STT")
                        self.audio_buffer = []
                        return
                else:
                    # No meaningful post-spell audio
                    print("[STT] Spell detected, no post-spell audio to transcribe")
                    self.audio_buffer = []
                    return

            duration = len(audio_data) / self._current_sample_rate
            audio_bytes = audio_data.tobytes()

            print(f"[STT] Recording stopped ({duration:.1f}s)")

            # Minimum duration check
            if duration < 0.3:
                print("[STT] Recording too short, ignoring")
                _play_sound(_SOUND_ERR, delay=0.5)
                # Release preview lock
                if not spell_was_detected:
                    _send_stt_state(False, "ptt")
                return

            # Clear buffer (memory cleanup)
            self.audio_buffer = []

            # For post-spell transcription, re-acquire preview lock for STT phase
            if spell_was_detected:
                _send_stt_state(True, "ptt")

            # Transcribe in background thread
            threading.Thread(
                target=self._transcribe_async,
                args=(audio_bytes,),
                daemon=True
            ).start()

    def _transcribe_async(self, audio_bytes, source: str = "ptt"):
        """Transcribe audio in background.

        Args:
            audio_bytes: Raw audio data to transcribe
            source: "ptt" or "open_mic" - used for preview lock release on failure
        """
        try:
            from services import stt

            # Apply mic gain boost if configured
            audio_bytes = _apply_mic_gain(audio_bytes)

            result = stt.transcribe(audio_bytes, self._current_sample_rate)

            if result["success"] and result["text"]:
                print(f"[STT] Result: \"{result['text']}\"")
                if self.on_transcribe:
                    self.on_transcribe(result["text"])
                # Success: conversation will start, lock transfers - no release needed
            elif result["error"]:
                error_msg = result['error']
                print(f"[STT] Transcription failed: {error_msg}")
                _play_sound(_SOUND_ERR, delay=0.5)
                if source == "open_mic" and self.on_soft_interrupt_cancel:
                    try:
                        self.on_soft_interrupt_cancel()
                    except Exception as e:
                        print(f"[STT] Soft interrupt cancel error: {e}")
                # Release preview lock on failure
                _send_stt_state(False, source)
                if self.on_error:
                    self.on_error(f"Speech recognition failed: {error_msg}")
            else:
                print("[STT] No speech detected")
                _play_sound(_SOUND_ERR, delay=0.5)
                if source == "open_mic" and self.on_soft_interrupt_cancel:
                    try:
                        self.on_soft_interrupt_cancel()
                    except Exception as e:
                        print(f"[STT] Soft interrupt cancel error: {e}")
                # Release preview lock on failure
                _send_stt_state(False, source)
                if self.on_error:
                    self.on_error("No speech detected")

        except Exception as e:
            print(f"[STT] Transcription error: {e}")
            _play_sound(_SOUND_ERR, delay=0.5)
            if source == "open_mic" and self.on_soft_interrupt_cancel:
                try:
                    self.on_soft_interrupt_cancel()
                except Exception as e2:
                    print(f"[STT] Soft interrupt cancel error: {e2}")
            # Release preview lock on failure
            _send_stt_state(False, source)
            if self.on_error:
                self.on_error(f"Speech recognition error: {e}")

    # ==================== Open Mic Mode Methods ====================

    def _start_open_mic(self):
        """Start open mic continuous listening."""
        if self._open_mic_active:
            return

        # Load settings
        from utils.settings import load_settings
        settings = load_settings()
        stt_settings = settings.get('stt', {})
        open_mic_settings = settings.get('open_mic', {})

        sample_rate = stt_settings.get('sample_rate', 16000)
        self._current_sample_rate = sample_rate

        vad_threshold = open_mic_settings.get('vad_threshold', 0.5)
        self._turn_timeout = open_mic_settings.get('turn_timeout_secs', 3.0)
        utterance_end_ms = open_mic_settings.get('utterance_end_ms', 500)
        self._min_silence_for_turn = utterance_end_ms / 1000.0  # Convert to seconds
        pre_speech_ms = open_mic_settings.get('pre_speech_ms', 200)
        self._pre_speech_samples = int(pre_speech_ms * sample_rate / 1000)

        # Initialize VAD (uses cached model if preloaded)
        try:
            from services.vad import VADProcessor, is_loaded as vad_is_loaded
            # Quick check if model needs loading (will show notification)
            if not vad_is_loaded() and _lua_socket:
                _lua_socket.send_notification("Loading speech models...")
            self._vad_processor = VADProcessor(
                threshold=vad_threshold,
                sample_rate=sample_rate,
                min_silence_ms=200  # 200ms silence before triggering speech end
            )
        except Exception as e:
            print(f"[STT] Failed to initialize VAD: {e}")
            _play_sound(_SOUND_ERR)
            return

        # Initialize turn detector (uses cached model if preloaded)
        try:
            from services.turn_detection import TurnDetector
            self._turn_detector = TurnDetector()
        except Exception as e:
            print(f"[STT] Failed to initialize turn detector: {e}")
            _play_sound(_SOUND_ERR)
            return

        # Initialize ring buffer (10 seconds of audio)
        self._ring_buffer = RingBuffer(max_samples=sample_rate * 10)

        # Reset state
        self._utterance_buffer = []
        self._speech_in_progress = False
        self._silence_start_time = 0
        self._turn_check_pending = False
        self._last_turn_check_time = 0

        # Start spell detection for open mic
        def _on_open_mic_spell_detected(game_spell, model_name):
            if not self._send_cast_spell(game_spell):
                return  # Suppressed (VR gate) — let conversation continue
            # Spell cast is orthogonal to the interrupt decision.
            # Don't touch speech state — let the normal STT pipeline continue.
            # If user spoke real words, STT transcribes → full interrupt.
            # If noise only, STT returns nothing → resume.

        self._start_spell_detection(_on_open_mic_spell_detected)

        # Start open mic
        self._open_mic_active = True
        self._open_mic_stop_event.clear()

        self._open_mic_thread = threading.Thread(
            target=self._open_mic_loop,
            daemon=True
        )
        self._open_mic_thread.start()

        _play_sound(_SOUND_TOGGLE_ON)
        if _lua_socket:
            _lua_socket.send_notification("Open Mic enabled")
        print(f"[STT] Open mic started (threshold: {vad_threshold}, endpointing: {utterance_end_ms}ms)")

    def _stop_open_mic(self):
        """Stop open mic continuous listening."""
        if not self._open_mic_active:
            return

        self._open_mic_active = False
        self._open_mic_stop_event.set()
        self._stop_time = time.time()

        # Stop spell detection
        self._stop_spell_detection()

        # Wait for thread to stop - give enough time for turn detection model inference
        if self._open_mic_thread and self._open_mic_thread.is_alive():
            self._open_mic_thread.join(timeout=5.0)
        thread_stopped = self._open_mic_thread is None or not self._open_mic_thread.is_alive()
        self._open_mic_thread = None

        # Only close stream here if the loop thread didn't clean up (its finally block handles it)
        if not thread_stopped and self._stream:
            try:
                self._stream.abort()  # abort is safer than close when callback may be active
                self._stream.close()
            except:
                pass
            self._stream = None

        # Discard any in-progress utterance
        was_speaking = self._speech_in_progress
        self._utterance_buffer = []
        self._speech_in_progress = False
        self._silence_start_time = 0
        self._turn_check_pending = False

        # If speech was in progress, cancel soft interrupt and release preview lock
        if was_speaking:
            if self.on_soft_interrupt_cancel:
                try:
                    self.on_soft_interrupt_cancel()
                except Exception as e:
                    print(f"[STT] Soft interrupt cancel error: {e}")
            _send_stt_state(False, "open_mic")

        _play_sound(_SOUND_TOGGLE_OFF)
        if _lua_socket:
            _lua_socket.send_notification("Open Mic disabled")
        print("[STT] Open mic stopped")

    def _open_mic_loop(self):
        """Main loop for open mic continuous listening."""
        sample_rate = self._current_sample_rate
        chunk_size = self._vad_processor.chunk_size

        # Initialize audio queue
        self._audio_queue = []

        # Capture spell queue reference for closure
        spell_q = self._spell_queue

        def audio_callback(indata, frames, time_info, status):
            if status:
                print(f"[STT] Audio status: {status}")

            # Fast path: just copy audio to queue, process VAD in main loop
            audio_copy = indata.flatten().copy()
            with self._audio_queue_lock:
                self._audio_queue.append(audio_copy)

            # Feed spell detection queue (non-blocking)
            if spell_q is not None:
                try:
                    spell_q.put_nowait(audio_copy)
                except queue.Full:
                    pass

        try:
            self._stream = sd.InputStream(
                samplerate=sample_rate,
                channels=1,
                dtype='int16',
                callback=audio_callback,
                blocksize=chunk_size,
                latency='low'
            )
            self._stream.start()

            # Main loop: process audio queue and run VAD
            while self._open_mic_active and not self._open_mic_stop_event.is_set():
                # Get pending audio chunks
                with self._audio_queue_lock:
                    chunks_to_process = self._audio_queue
                    self._audio_queue = []

                # Process each chunk through ring buffer and VAD
                for chunk in chunks_to_process:
                    # Write to ring buffer
                    self._ring_buffer.write(chunk)
                    # Process VAD
                    if self._open_mic_active:
                        self._process_vad_chunk(chunk)

                # Check guards periodically
                with self._open_mic_lock:
                    if self._speech_in_progress:
                        # Check if we should discard in-progress speech
                        should_discard = False
                        discard_reason = ""

                        if not is_game_window_active():
                            should_discard = True
                            discard_reason = "game lost focus"
                        elif self.check_pause:
                            pause_reason = self.check_pause()
                            if pause_reason:
                                should_discard = True
                                discard_reason = pause_reason if isinstance(pause_reason, str) else "game paused/cinematic"

                        if should_discard:
                            print(f"[STT] Open mic: {discard_reason}, discarding utterance")
                            self._utterance_buffer = []
                            self._speech_in_progress = False
                            self._silence_start_time = 0
                            self._turn_check_pending = False
                            self._last_turn_check_time = 0
                            self._vad_processor.reset()
                            if self.on_soft_interrupt_cancel:
                                try:
                                    self.on_soft_interrupt_cancel()
                                except Exception as e:
                                    print(f"[STT] Soft interrupt cancel error: {e}")
                            _send_stt_state(False, "open_mic")

                    # Check for silence and run turn detection
                    if self._speech_in_progress and self._silence_start_time > 0:
                        silence_duration = time.time() - self._silence_start_time

                        # Hard timeout - force complete after turn_timeout seconds of silence
                        if silence_duration >= self._turn_timeout:
                            print(f"[STT] Silence timeout ({silence_duration:.1f}s), forcing turn complete")
                            self._on_turn_complete_locked()
                        # After minimum silence gap, check turn detection (re-check every 1s)
                        elif self._turn_check_pending and silence_duration >= self._min_silence_for_turn and self._utterance_buffer:
                            now = time.time()
                            time_since_last_check = now - self._last_turn_check_time if self._last_turn_check_time > 0 else float('inf')

                            if time_since_last_check >= 1.0:
                                self._last_turn_check_time = now
                                audio_array = np.concatenate(self._utterance_buffer)
                                duration = len(audio_array) / self._current_sample_rate

                                if duration >= 0.3:
                                    try:
                                        result = self._turn_detector.predict(audio_array)
                                        print(f"[STT] Turn detection: complete={result['complete']}, prob={result['probability']:.2f}")

                                        if result['complete']:
                                            self._on_turn_complete_locked()
                                        else:
                                            print("[STT] Turn incomplete, re-checking in 1s")
                                    except Exception as e:
                                        print(f"[STT] Turn detection error: {e}")

                # Small sleep to avoid busy-waiting, but fast enough for responsive VAD
                time.sleep(0.01)  # 10ms

        except Exception as e:
            print(f"[STT] Open mic loop error: {e}")
        finally:
            if self._stream:
                try:
                    self._stream.stop()
                    self._stream.close()
                except:
                    pass
                self._stream = None

    def _process_vad_chunk(self, audio_chunk: np.ndarray):
        """Process audio chunk through VAD."""
        if not self._open_mic_active:
            return

        # Convert to float32 for consistent storage
        if audio_chunk.dtype == np.int16:
            audio_float = audio_chunk.astype(np.float32) / 32768.0
        else:
            audio_float = audio_chunk.copy()

        # Skip if game not focused
        if not is_game_window_active():
            return

        try:
            event = self._vad_processor.process_chunk(audio_chunk)
        except Exception as e:
            print(f"[STT] VAD processing error: {e}")
            return

        with self._open_mic_lock:
            if event == 'start':
                self._on_vad_speech_start_locked(audio_float)
            elif event == 'end':
                self._on_vad_speech_pause_locked()
            elif self._speech_in_progress:
                # Accumulate audio during speech (not on start - handled in start callback)
                self._utterance_buffer.append(audio_float)
                # Reset silence timer while speech is detected
                if self._vad_processor.is_speaking:
                    self._silence_start_time = 0

    def _on_vad_speech_start_locked(self, trigger_chunk: np.ndarray):
        """Called when VAD detects speech start. Must be called with _open_mic_lock held."""
        if self._speech_in_progress:
            return

        # Aiming = spell mode. Suppress conversation, let spell detection handle audio.
        if is_aiming():
            return
        try:
            from vr import is_vr_active, is_vr_spell_mode
            if is_vr_active() and is_vr_spell_mode():
                return
        except Exception:
            pass

        # Check guards
        if self.check_pause:
            pause_reason = self.check_pause()
            if pause_reason:
                reason_str = pause_reason if isinstance(pause_reason, str) else "unknown"
                print(f"[STT] Speech start blocked: {reason_str}")
                self._vad_processor.reset()
                return

        # Trigger soft interrupt (pause audio) or fall back to hard interrupt
        if self.on_soft_interrupt:
            try:
                self.on_soft_interrupt()
            except Exception as e:
                print(f"[STT] Soft interrupt error: {e}")
        elif self.on_interrupt:
            try:
                self.on_interrupt()
            except Exception as e:
                print(f"[STT] Interrupt callback error: {e}")

        self._speech_in_progress = True
        self._silence_start_time = 0

        # Notify Lua to lock NPC (preview lock for STT)
        _send_stt_state(True, "open_mic")

        # Get pre-speech audio from ring buffer
        # Note: Ring buffer was written BEFORE VAD processing, so it includes trigger_chunk.
        # We want pre_speech_samples BEFORE the trigger chunk, so subtract chunk size.
        chunk_size = self._vad_processor.chunk_size
        pre_speech_with_trigger = self._pre_speech_samples + chunk_size
        pre_speech = self._ring_buffer.get_last_n_samples(pre_speech_with_trigger)
        if len(pre_speech) > chunk_size:
            # Remove the trigger chunk (it's at the end of ring buffer)
            pre_speech = pre_speech[:-chunk_size]

        # Build utterance: pre-speech + trigger chunk
        self._utterance_buffer = []
        if len(pre_speech) > 0:
            self._utterance_buffer.append(pre_speech)
        self._utterance_buffer.append(trigger_chunk)

        # Trigger vision capture (outside lock would be better but this is quick)
        try:
            from vision_agent import get_agent
            agent = get_agent()
            if agent:
                agent.capture_now()
            else:
                print("[STT] Vision capture skipped: no agent instance")
        except Exception as e:
            print(f"[STT] Vision capture error: {e}")

        # Fire speech start callback (for interruption)
        if self.on_speech_start:
            try:
                self.on_speech_start()
            except Exception as e:
                print(f"[STT] on_speech_start error: {e}")

        _play_sound(_SOUND_ON)
        print("[STT] Speech detected")

    def _on_vad_speech_pause_locked(self):
        """Called when VAD detects speech pause (silence). Must be called with _open_mic_lock held."""
        if not self._speech_in_progress:
            return

        # Start silence timer - turn detection will be checked in main loop
        # after sufficient silence has accumulated
        if self._silence_start_time == 0:
            self._silence_start_time = time.time()
            self._turn_check_pending = True  # Need to check turn detection after min silence

    def _on_turn_complete_locked(self):
        """Called when turn is determined complete. Must be called with _open_mic_lock held."""
        if not self._speech_in_progress:
            return

        self._speech_in_progress = False
        self._silence_start_time = 0
        self._turn_check_pending = False
        self._last_turn_check_time = 0

        _play_sound(_SOUND_OFF)

        if not self._utterance_buffer:
            print("[STT] No audio in utterance buffer")
            return

        # Combine utterance audio
        audio_array = np.concatenate(self._utterance_buffer)
        duration = len(audio_array) / self._current_sample_rate

        print(f"[STT] Turn complete ({duration:.1f}s)")

        if duration < 0.3:
            print("[STT] Utterance too short, ignoring")
            self._utterance_buffer = []
            if self.on_soft_interrupt_cancel:
                try:
                    self.on_soft_interrupt_cancel()
                except Exception as e:
                    print(f"[STT] Soft interrupt cancel error: {e}")
            _send_stt_state(False, "open_mic")
            return

        # Convert to int16 bytes for STT
        audio_int16 = (audio_array * 32768.0).astype(np.int16)
        audio_bytes = audio_int16.tobytes()

        # Clear buffer
        self._utterance_buffer = []

        # Transcribe in background (spawn outside lock context)
        threading.Thread(
            target=self._transcribe_async,
            args=(audio_bytes, "open_mic"),
            daemon=True
        ).start()

    # ==================== Common Methods ====================

    @property
    def is_open_mic_active(self) -> bool:
        """Whether open mic is currently active."""
        return self._open_mic_active

    def stop(self):
        """Stop the capture system."""
        if self._mode == 'open_mic' and self._open_mic_active:
            self._stop_open_mic()

        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except:
                pass
            self._stream = None
        if self.keyboard_listener:
            self.keyboard_listener.stop()
            self.keyboard_listener = None
        if self.mouse_listener:
            self.mouse_listener.stop()
            self.mouse_listener = None
        print("[STT] Capture stopped")

    def set_hotkey(self, hotkey):
        """Update hotkey (requires restart to take effect)."""
        old_is_mouse = self._is_mouse_hotkey
        self.hotkey_name = hotkey.lower()
        self._is_mouse_hotkey = self.hotkey_name == 'middle_mouse'
        self._hotkey_vk = HOTKEY_VK_MAP.get(self.hotkey_name, None)

        # If input type changed, restart listeners
        if old_is_mouse != self._is_mouse_hotkey:
            self.stop()
            self.start()

        print(f"[STT] Hotkey changed to: {self.hotkey_name}")


# Module-level instance management (follows input_capture.py pattern)
_capture_instance = None
_stored_callbacks = {}  # Persist callbacks for hot-reload from disabled state


def get_capture():
    """Get the current capture instance."""
    return _capture_instance


def register_callbacks(on_transcribe_callback, check_pause=None, on_error=None, on_interrupt=None):
    """Register callbacks for STT without starting capture.

    Call this at startup to enable hot-reload even when STT starts disabled.
    """
    global _stored_callbacks
    _stored_callbacks = {
        'on_transcribe': on_transcribe_callback,
        'check_pause': check_pause,
        'on_error': on_error,
        'on_interrupt': on_interrupt
    }


def start_capture(on_transcribe_callback, hotkey='middle_mouse', check_pause=None, on_error=None, on_interrupt=None):
    """Start STT capture with callback."""
    global _capture_instance, _stored_callbacks

    # Store callbacks for potential hot-reload later
    _stored_callbacks = {
        'on_transcribe': on_transcribe_callback,
        'check_pause': check_pause,
        'on_error': on_error,
        'on_interrupt': on_interrupt
    }

    if _capture_instance:
        _capture_instance.stop()

    _capture_instance = STTCapture(on_transcribe_callback, hotkey, check_pause, on_error, on_interrupt)

    # Check if open mic mode should be enabled
    from utils.settings import load_settings
    settings = load_settings()
    open_mic_settings = settings.get('open_mic', {})
    if open_mic_settings.get('enabled', False):
        _capture_instance.mode = 'open_mic'

    _capture_instance.start()
    return _capture_instance


def stop_capture():
    """Stop STT capture."""
    global _capture_instance
    if _capture_instance:
        _capture_instance.stop()
        _capture_instance = None


def set_capture_hotkey(hotkey):
    """Update hotkey on running capture."""
    if _capture_instance:
        _capture_instance.set_hotkey(hotkey)


def set_capture_mode(mode: Literal['ptt', 'open_mic']):
    """Set capture mode on running capture."""
    if _capture_instance:
        _capture_instance.mode = mode


def restart_capture():
    """Restart STT capture with fresh settings, reusing stored callbacks.

    Works whether STT is currently running or not - can enable from disabled state
    if callbacks were previously registered via start_capture().

    Returns True if capture was (re)started, False if STT not available or no callbacks.
    """
    global _capture_instance

    # Get callbacks - from running instance or stored
    on_speech_start = None
    on_interrupt = None
    on_soft_interrupt = None
    on_soft_interrupt_cancel = None
    if _capture_instance:
        on_transcribe = _capture_instance.on_transcribe
        check_pause = _capture_instance.check_pause
        on_error = _capture_instance.on_error
        on_interrupt = _capture_instance.on_interrupt
        on_speech_start = _capture_instance.on_speech_start
        on_soft_interrupt = _capture_instance.on_soft_interrupt
        on_soft_interrupt_cancel = _capture_instance.on_soft_interrupt_cancel
        _capture_instance.stop()
    elif _stored_callbacks:
        on_transcribe = _stored_callbacks.get('on_transcribe')
        check_pause = _stored_callbacks.get('check_pause')
        on_error = _stored_callbacks.get('on_error')
        on_interrupt = _stored_callbacks.get('on_interrupt')
    else:
        print("[STT] Restart failed: no callbacks registered")
        return False

    # Load fresh settings
    from utils.settings import load_settings
    from services import stt as stt_service

    settings = load_settings()
    stt_settings = settings.get('stt', {})
    open_mic_settings = settings.get('open_mic', {})

    # Check if STT is available with current settings
    if not stt_service.is_available():
        provider = stt_settings.get('provider', 'none')
        if provider == 'none':
            print("[STT] Disabled (provider: none)")
        else:
            print(f"[STT] Provider '{provider}' not configured (missing API key)")
        _capture_instance = None
        return False

    # Start with fresh settings
    hotkey = stt_settings.get('hotkey', 'middle_mouse')
    _capture_instance = STTCapture(on_transcribe, hotkey, check_pause, on_error, on_interrupt)

    # Restore callbacks
    if on_speech_start:
        _capture_instance.on_speech_start = on_speech_start
    if on_soft_interrupt:
        _capture_instance.on_soft_interrupt = on_soft_interrupt
    if on_soft_interrupt_cancel:
        _capture_instance.on_soft_interrupt_cancel = on_soft_interrupt_cancel

    # Set mode based on settings
    if open_mic_settings.get('enabled', False):
        _capture_instance.mode = 'open_mic'

    _capture_instance.start()
    print(f"[STT] Capture started (hotkey: {hotkey}, mode: {_capture_instance.mode})")
    return True
