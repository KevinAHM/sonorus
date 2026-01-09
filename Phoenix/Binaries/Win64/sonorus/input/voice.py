"""
Push-to-talk audio capture for Speech-to-Text.

Supports keyboard hotkeys (F1-F10, Enter) and mouse buttons (middle mouse).
Uses sounddevice for microphone recording.

Protection mechanisms (same as input_capture.py):
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
from pynput import keyboard, mouse
import sounddevice as sd
import numpy as np

# Sound file paths (wav for winsound compatibility)
# sounds/ is at sonorus root, not in input/
_SONORUS_DIR = os.path.dirname(os.path.dirname(__file__))
_SOUNDS_DIR = os.path.join(_SONORUS_DIR, 'sounds')
_SOUND_ON = os.path.join(_SOUNDS_DIR, 'stt-on.wav')
_SOUND_OFF = os.path.join(_SOUNDS_DIR, 'stt-off.wav')
_SOUND_ERR = os.path.join(_SOUNDS_DIR, 'stt-err.wav')


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


class STTCapture:
    """Push-to-talk audio capture with STT transcription."""

    def __init__(self, on_transcribe_callback, hotkey='middle_mouse', check_pause=None, on_error=None):
        """
        Args:
            on_transcribe_callback: Called with transcribed text
            hotkey: Push-to-talk key (default: 'middle_mouse')
            check_pause: Optional callable that returns True if capture should be blocked
            on_error: Optional callback for error messages (for user notification)
        """
        self.on_transcribe = on_transcribe_callback
        self.hotkey_name = hotkey.lower()
        self.check_pause = check_pause
        self.on_error = on_error

        # Recording state
        self.recording = False
        self.audio_buffer = []
        self._lock = threading.Lock()
        self._stop_time = 0  # Timestamp of last recording stop (anti-repeat)
        self._current_sample_rate = 16000  # Set when recording starts

        # Listeners
        self.keyboard_listener = None
        self.mouse_listener = None

        # Audio stream
        self._stream = None

        # Determine input type
        self._is_mouse_hotkey = self.hotkey_name == 'middle_mouse'
        self._hotkey_vk = HOTKEY_VK_MAP.get(self.hotkey_name, None)

    def start(self):
        """Start listening for hotkey."""
        if self._is_mouse_hotkey:
            if self.mouse_listener is not None:
                return
            self.mouse_listener = mouse.Listener(
                on_click=self._on_mouse_click
            )
            self.mouse_listener.start()
        else:
            if self.keyboard_listener is not None:
                return
            self.keyboard_listener = keyboard.Listener(
                win32_event_filter=self._win32_filter,
                suppress=False
            )
            self.keyboard_listener.start()

        print(f"[STT] Push-to-talk started (hotkey: {self.hotkey_name})")

    def _on_mouse_click(self, x, y, button, pressed):
        """Handle mouse button events for middle mouse."""
        if button != mouse.Button.middle:
            return

        # === NOT RECORDING ===
        if not self.recording:
            # Anti-repeat: prevent immediate reactivation
            if time.time() - self._stop_time < 0.15:
                return

            # Check if game window is active
            if not is_game_window_active():
                print(f"[STT] Hotkey blocked: game window not active")
                return

            # Check pause callback (game paused, not loaded, etc.)
            if self.check_pause:
                is_paused = self.check_pause()
                if is_paused:
                    print("[STT] Hotkey blocked: check_pause() returned True")
                    return

            if pressed:
                self._start_recording()

        # === RECORDING ===
        else:
            # If game window lost focus while recording, cancel
            if not is_game_window_active():
                print("[STT] Recording cancelled (game lost focus)")
                self._cancel_recording()
                return

            if not pressed:
                self._stop_recording()

    def _win32_filter(self, msg, data):
        """Handle keyboard hotkey press/release."""
        vk = data.vkCode

        # Always let modifier keys through
        if vk in VK_MODIFIERS:
            return

        # Only handle our hotkey
        if vk != self._hotkey_vk:
            return

        # Check modifier states - let Alt/Win combos through (system shortcuts)
        alt_pressed = is_key_pressed(VK_MENU) or is_key_pressed(VK_LMENU) or is_key_pressed(VK_RMENU)
        win_pressed = is_key_pressed(VK_LWIN) or is_key_pressed(VK_RWIN)

        if alt_pressed or win_pressed:
            return

        # === NOT RECORDING (key down to start) ===
        if msg in (WM_KEYDOWN, WM_SYSKEYDOWN) and not self.recording:
            # Anti-repeat: prevent immediate reactivation
            if time.time() - self._stop_time < 0.15:
                return

            # Check if game window is active
            if not is_game_window_active():
                print(f"[STT] Hotkey blocked: game window not active")
                return

            # Check pause callback (game paused, not loaded, etc.)
            if self.check_pause:
                is_paused = self.check_pause()
                if is_paused:
                    print("[STT] Hotkey blocked: check_pause() returned True")
                    return

            self._start_recording()
            self.keyboard_listener.suppress_event()

        # === RECORDING (key up to stop) ===
        elif msg in (WM_KEYUP, WM_SYSKEYUP) and self.recording:
            # If game window lost focus while recording, cancel
            if not is_game_window_active():
                print("[STT] Recording cancelled (game lost focus)")
                self._cancel_recording()
                self.keyboard_listener.suppress_event()
                return

            self._stop_recording()
            self.keyboard_listener.suppress_event()

    def _start_recording(self):
        """Begin audio capture."""
        t0 = time.time()
        with self._lock:
            t1 = time.time()
            if self.recording:
                return

            self.recording = True
            self.audio_buffer = []

            # Load settings fresh for hot-reload support
            from utils.settings import load_settings
            settings = load_settings()
            t2 = time.time()
            stt_settings = settings.get('stt', {})
            sample_rate = stt_settings.get('sample_rate', 16000)
            channels = stt_settings.get('channels', 1)
            self._current_sample_rate = sample_rate  # Store for _stop_recording

            # Audio callback for sounddevice stream
            def audio_callback(indata, frames, time_info, status):
                if status:
                    print(f"[STT] Audio status: {status}")
                if self.recording:
                    self.audio_buffer.append(indata.copy())

            try:
                self._stream = sd.InputStream(
                    samplerate=sample_rate,
                    channels=channels,
                    dtype='int16',
                    callback=audio_callback,
                    blocksize=512,
                    latency='low'
                )
                t3 = time.time()
                self._stream.start()
                t4 = time.time()

                # Trigger vision capture for fresh context while user is speaking
                try:
                    from vision_agent import get_agent
                    agent = get_agent()
                    if agent:
                        agent.capture_now()
                except Exception:
                    pass  # Vision capture is optional

                _play_sound(_SOUND_ON)
                t5 = time.time()
                print(f"[STT] Recording started (lock:{(t1-t0)*1000:.0f}ms settings:{(t2-t1)*1000:.0f}ms stream_create:{(t3-t2)*1000:.0f}ms stream_start:{(t4-t3)*1000:.0f}ms sound:{(t5-t4)*1000:.0f}ms)")
            except Exception as e:
                print(f"[STT] Failed to start recording: {e}")
                _play_sound(_SOUND_ERR)
                self.recording = False

    def _cancel_recording(self):
        """Cancel recording without transcribing."""
        with self._lock:
            if not self.recording:
                return

            self.recording = False
            self._stop_time = time.time()

            # Stop stream
            if self._stream:
                try:
                    self._stream.stop()
                    self._stream.close()
                except:
                    pass
                self._stream = None

            self.audio_buffer = []
            print("[STT] Recording cancelled")

    def _stop_recording(self):
        """Stop recording and transcribe."""
        with self._lock:
            if not self.recording:
                return

            self.recording = False
            self._stop_time = time.time()

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
                return

            audio_data = np.concatenate(self.audio_buffer)
            audio_bytes = audio_data.tobytes()
            duration = len(audio_data) / self._current_sample_rate

            print(f"[STT] Recording stopped ({duration:.1f}s)")

            # Minimum duration check
            if duration < 0.3:
                print("[STT] Recording too short, ignoring")
                _play_sound(_SOUND_ERR, delay=0.5)
                return

            # Clear buffer (memory cleanup)
            self.audio_buffer = []

            # Transcribe in background thread
            threading.Thread(
                target=self._transcribe_async,
                args=(audio_bytes,),
                daemon=True
            ).start()

    def _transcribe_async(self, audio_bytes):
        """Transcribe audio in background."""
        try:
            from services import stt

            result = stt.transcribe(audio_bytes, self._current_sample_rate)

            if result["success"] and result["text"]:
                print(f"[STT] Result: \"{result['text']}\"")
                if self.on_transcribe:
                    self.on_transcribe(result["text"])
            elif result["error"]:
                error_msg = result['error']
                print(f"[STT] Transcription failed: {error_msg}")
                _play_sound(_SOUND_ERR, delay=0.5)
                if self.on_error:
                    self.on_error(f"Speech recognition failed: {error_msg}")
            else:
                print("[STT] No speech detected")
                _play_sound(_SOUND_ERR, delay=0.5)
                if self.on_error:
                    self.on_error("No speech detected")

        except Exception as e:
            print(f"[STT] Transcription error: {e}")
            _play_sound(_SOUND_ERR, delay=0.5)
            if self.on_error:
                self.on_error(f"Speech recognition error: {e}")

    def stop(self):
        """Stop the capture system."""
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


def register_callbacks(on_transcribe_callback, check_pause=None, on_error=None):
    """Register callbacks for STT without starting capture.

    Call this at startup to enable hot-reload even when STT starts disabled.
    """
    global _stored_callbacks
    _stored_callbacks = {
        'on_transcribe': on_transcribe_callback,
        'check_pause': check_pause,
        'on_error': on_error
    }


def start_capture(on_transcribe_callback, hotkey='middle_mouse', check_pause=None, on_error=None):
    """Start STT capture with callback."""
    global _capture_instance, _stored_callbacks

    # Store callbacks for potential hot-reload later
    _stored_callbacks = {
        'on_transcribe': on_transcribe_callback,
        'check_pause': check_pause,
        'on_error': on_error
    }

    if _capture_instance:
        _capture_instance.stop()
    _capture_instance = STTCapture(on_transcribe_callback, hotkey, check_pause, on_error)
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


def restart_capture():
    """Restart STT capture with fresh settings, reusing stored callbacks.

    Works whether STT is currently running or not - can enable from disabled state
    if callbacks were previously registered via start_capture().

    Returns True if capture was (re)started, False if STT not available or no callbacks.
    """
    global _capture_instance

    # Get callbacks - from running instance or stored
    if _capture_instance:
        on_transcribe = _capture_instance.on_transcribe
        check_pause = _capture_instance.check_pause
        on_error = _capture_instance.on_error
        _capture_instance.stop()
    elif _stored_callbacks:
        on_transcribe = _stored_callbacks.get('on_transcribe')
        check_pause = _stored_callbacks.get('check_pause')
        on_error = _stored_callbacks.get('on_error')
    else:
        print("[STT] Restart failed: no callbacks registered")
        return False

    # Load fresh settings
    from utils.settings import load_settings
    from services import stt as stt_service

    settings = load_settings()
    stt_settings = settings.get('stt', {})

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
    _capture_instance = STTCapture(on_transcribe, hotkey, check_pause, on_error)
    _capture_instance.start()
    print(f"[STT] Capture started (hotkey: {hotkey})")
    return True
