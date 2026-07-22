"""
In-Game Text Input Capture using pynput with win32_event_filter

All key handling happens in win32_event_filter using Windows APIs.
This ensures we can both capture AND suppress keys reliably.
"""

import threading
import ctypes
import time
import pyperclip
from pynput import keyboard
from .voice import can_activate_hotkey, is_game_window_active

user32 = ctypes.windll.user32

# Windows message types
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105

# VK codes
VK_RETURN = 0x0D
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
VK_F11 = 0x7A
VK_F12 = 0x7B
VK_ESCAPE = 0x1B
VK_BACK = 0x08
VK_SPACE = 0x20
VK_TAB = 0x09
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12  # Alt
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1
VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3
VK_LMENU = 0xA4  # Left Alt
VK_RMENU = 0xA5  # Right Alt

VK_MODIFIERS = {VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN,
                VK_LSHIFT, VK_RSHIFT, VK_LCONTROL, VK_RCONTROL, VK_LMENU, VK_RMENU}
VK_SHIFT_KEYS = {VK_SHIFT, VK_LSHIFT, VK_RSHIFT}
VK_CTRL_KEYS = {VK_CONTROL, VK_LCONTROL, VK_RCONTROL}
VK_ALT_KEYS = {VK_MENU, VK_LMENU, VK_RMENU}
VK_WIN_KEYS = {VK_LWIN, VK_RWIN}


def is_key_pressed(vk):
    """Check if a key is currently pressed using GetAsyncKeyState."""
    return (user32.GetAsyncKeyState(vk) & 0x8000) != 0


def vk_to_char(vk, shift_pressed=None):
    """Convert virtual key code to character using current keyboard state."""
    # Get current keyboard state
    keyboard_state = (ctypes.c_ubyte * 256)()
    if not user32.GetKeyboardState(keyboard_state):
        return None

    # In low-level hooks, GetKeyboardState may be stale.
    # Manually set/clear shift state from the hook-tracked modifier state.
    if shift_pressed is None:
        shift_pressed = (
            is_key_pressed(VK_SHIFT)
            or is_key_pressed(VK_LSHIFT)
            or is_key_pressed(VK_RSHIFT)
        )
    if shift_pressed:
        keyboard_state[VK_SHIFT] = 0x80
        keyboard_state[VK_LSHIFT] = 0x80
        keyboard_state[VK_RSHIFT] = 0x80
    else:
        # Clear stale shift state
        keyboard_state[VK_SHIFT] = 0x00
        keyboard_state[VK_LSHIFT] = 0x00
        keyboard_state[VK_RSHIFT] = 0x00

    # Check caps lock toggle state (GetKeyState returns toggle in low bit)
    if user32.GetKeyState(0x14) & 1:  # VK_CAPITAL
        keyboard_state[0x14] = 0x01

    # Get scan code for the virtual key
    scan_code = user32.MapVirtualKeyW(vk, 0)

    # Convert to unicode character
    buffer = (ctypes.c_wchar * 5)()
    result = user32.ToUnicode(vk, scan_code, keyboard_state, buffer, 5, 0)

    if result == 1:
        char = buffer[0]
        # Only return printable characters
        if char.isprintable():
            return char
    return None


class ChatInputCapture:
    def __init__(self, send_callback, hotkey='enter', check_pause=None):
        self.send = send_callback
        self.hotkey_name = hotkey.lower()
        self.hotkey_vk = self._parse_hotkey_vk(hotkey)
        self.check_pause = check_pause

        self.active = False
        self.text_buffer = ""
        self._lock = threading.Lock()
        self.listener = None
        self._deactivate_time = 0  # Timestamp of last deactivation (to prevent key repeat issues)

        # Director mode: Hold hotkey 500ms+ for "Prompt:" mode vs tap for "You:" mode
        self._hotkey_down_time = None  # When hotkey was pressed down
        self._hotkey_held = False  # True while hotkey is physically held (KEYDOWN to KEYUP)
        self._prompt_mode = False  # True = director prompt mode, False = normal chat mode
        self._hold_timer = None  # Timer for hold detection (cancelled on keyup)
        self._modifiers_down = set()

    # Hotkey name to VK code mapping (must match config.html dropdown options)
    HOTKEY_VK_MAP = {
        'enter': VK_RETURN,
        'f1': VK_F1, 'f2': VK_F2, 'f3': VK_F3, 'f4': VK_F4, 'f5': VK_F5,
        'f6': VK_F6, 'f7': VK_F7, 'f8': VK_F8, 'f9': VK_F9, 'f10': VK_F10,
        'f11': VK_F11, 'f12': VK_F12,
    }

    def _parse_hotkey_vk(self, hotkey):
        """Convert hotkey name to VK code."""
        hotkey = hotkey.lower()
        if hotkey in self.HOTKEY_VK_MAP:
            return self.HOTKEY_VK_MAP[hotkey]
        elif len(hotkey) == 1:
            # For single characters, get VK code
            return user32.VkKeyScanW(ord(hotkey)) & 0xFF
        print(f"[InputCapture] Unknown hotkey '{hotkey}', defaulting to Enter")
        return VK_RETURN

    def start(self):
        if self.listener is not None:
            return
        self.listener = keyboard.Listener(
            win32_event_filter=self._win32_filter,
            suppress=False
        )
        self.listener.start()
        print(f"[InputCapture] Started - hotkey: {self.hotkey_name}")

    def _modifier_pressed(self, keys):
        return any(vk in self._modifiers_down for vk in keys)

    def _update_modifier_state(self, vk, is_down):
        if vk not in VK_MODIFIERS:
            return
        if is_down:
            self._modifiers_down.add(vk)
        else:
            self._modifiers_down.discard(vk)

    def _reset_modifier_state(self):
        self._modifiers_down.clear()

    def _open_chat_with_mode(self, mode):
        """Open chat input with specified mode. Called from timer or keyup.

        Returns True if chat was opened, False if already open.
        Thread-safe: all checks and modifications under lock.
        """
        with self._lock:
            if self.active:
                return False  # Already open

            self.active = True
            self.text_buffer = ""
            self._prompt_mode = (mode == "prompt")
            # Clear timer state since we're opening chat
            self._hold_timer = None

        # Trigger vision capture for fresh context (outside lock)
        try:
            from vision_agent import get_agent
            agent = get_agent()
            if agent:
                agent.capture_now()
        except Exception:
            pass  # Vision capture is optional

        print(f"[InputCapture] Chat ACTIVE ({mode} mode)")
        self._send_update()
        return True

    def _hold_timer_callback(self):
        """Called after 500ms if hotkey is still held - open in prompt mode."""
        # Check if hotkey is still being held (down_time not cleared by keyup)
        # Note: _open_chat_with_mode handles the lock and active check
        if self._hotkey_down_time is not None:
            if self._open_chat_with_mode("prompt"):
                # Successfully opened - clear down_time so keyup doesn't log confusion
                self._hotkey_down_time = None

    def _win32_filter(self, msg, data):
        """
        Handle ALL key processing here.
        - Capture keys for chat buffer
        - Call suppress_event() to block from game
        - Return value controls whether on_press/on_release is called (we don't use those)

        Director Mode (hold-to-prompt):
        - Tap (< 500ms): Opens chat with "You: " on release
        - Hold (>= 500ms): Opens chat with "Prompt: " after 500ms (no need to release)
        """
        vk = data.vkCode
        if msg in (WM_KEYDOWN, WM_SYSKEYDOWN):
            self._update_modifier_state(vk, True)
        elif msg in (WM_KEYUP, WM_SYSKEYUP):
            self._update_modifier_state(vk, False)

        # Check modifier states directly from Windows
        alt_pressed = self._modifier_pressed(VK_ALT_KEYS) or is_key_pressed(VK_MENU) or is_key_pressed(VK_LMENU) or is_key_pressed(VK_RMENU)
        win_pressed = self._modifier_pressed(VK_WIN_KEYS) or is_key_pressed(VK_LWIN) or is_key_pressed(VK_RWIN)
        ctrl_pressed = self._modifier_pressed(VK_CTRL_KEYS) or is_key_pressed(VK_CONTROL) or is_key_pressed(VK_LCONTROL) or is_key_pressed(VK_RCONTROL)
        shift_pressed = self._modifier_pressed(VK_SHIFT_KEYS) or is_key_pressed(VK_SHIFT) or is_key_pressed(VK_LSHIFT) or is_key_pressed(VK_RSHIFT)

        # Always let Alt/Win combos through (system shortcuts)
        if alt_pressed or win_pressed:
            return

        # While chat is active, swallow modifier presses/releases too so they do
        # not leak into the game as sprint/aim/etc. binds.
        if vk in VK_MODIFIERS:
            if self.active:
                if not is_game_window_active():
                    with self._lock:
                        if self.active:
                            self.active = False
                            self._deactivate_time = time.time()
                            self._reset_modifier_state()
                            self.text_buffer = ""
                            print("[InputCapture] Closed (game lost focus)")
                            self._send_update()
                    return
                self.listener.suppress_event()
            return

        # === HANDLE KEYUP ===
        if msg in (WM_KEYUP, WM_SYSKEYUP):
            if vk == self.hotkey_vk and self._hotkey_held:
                self._hotkey_held = False
                held_duration = time.time() - self._hotkey_down_time if self._hotkey_down_time else 0
                self._hotkey_down_time = None

                # Cancel the hold timer (prevents stale timer from firing later)
                if self._hold_timer is not None:
                    self._hold_timer.cancel()
                    self._hold_timer = None

                # If chat not open yet (quick tap), open in chat mode
                if not self.active:
                    if self._open_chat_with_mode("chat"):
                        print(f"[InputCapture] Quick tap ({held_duration*1000:.0f}ms) -> chat mode")

                self.listener.suppress_event()
                return

            if self.active:
                if not is_game_window_active():
                    with self._lock:
                        if self.active:
                            self.active = False
                            self._deactivate_time = time.time()
                            self._reset_modifier_state()
                            self.text_buffer = ""
                            print("[InputCapture] Closed (game lost focus)")
                            self._send_update()
                    return
                self.listener.suppress_event()
            return

        # Only handle key down events from here
        if msg not in (WM_KEYDOWN, WM_SYSKEYDOWN):
            return

        # === NOT IN CHAT MODE ===
        if not self.active:
            # Prevent immediate reactivation due to key repeat
            if time.time() - self._deactivate_time < 0.15:
                return

            # Check for hotkey press
            if vk == self.hotkey_vk:
                # Ignore key repeat (hotkey already down)
                if self._hotkey_down_time is not None:
                    self.listener.suppress_event()
                    return

                # Shared guard - game must be active and not paused
                if not can_activate_hotkey(self.check_pause):
                    return

                # Cancel any existing timer (debounce)
                if self._hold_timer is not None:
                    self._hold_timer.cancel()
                    self._hold_timer = None

                # Record press time and start timer for hold detection
                self._hotkey_down_time = time.time()
                self._hotkey_held = True
                print("[InputCapture] Hotkey pressed - tap for chat, hold for prompt")

                # Start timer - if still held after 500ms, open in prompt mode
                self._hold_timer = threading.Timer(0.5, self._hold_timer_callback)
                self._hold_timer.daemon = True
                self._hold_timer.start()

                self.listener.suppress_event()
            return

        # === CHAT IS ACTIVE ===
        # If game window lost focus, close chat and let keys through
        if not is_game_window_active():
            with self._lock:
                if self.active:
                    self.active = False
                    self._deactivate_time = time.time()
                    self._reset_modifier_state()
                    self.text_buffer = ""
                    print("[InputCapture] Closed (game lost focus)")
                    self._send_update()
            return  # Let key through to other applications

        with self._lock:
            # Ignore Enter while hotkey is physically held (prevents key repeat from submitting)
            if vk == VK_RETURN and self._hotkey_held:
                self.listener.suppress_event()
                return
            if vk == VK_RETURN:
                self._submit()
            elif vk == VK_ESCAPE:
                self._cancel()
            elif vk == VK_BACK:
                if self.text_buffer:
                    self.text_buffer = self.text_buffer[:-1]
                    self._send_update()
            elif vk == VK_SPACE:
                self.text_buffer += ' '
                self._send_update()
            elif vk == VK_TAB:
                self.text_buffer += '    '
                self._send_update()
            elif ctrl_pressed and vk == 0x56:  # Ctrl+V
                self._handle_paste()
            else:
                # Convert VK to character (handles shift for !@# etc.)
                char = vk_to_char(vk, shift_pressed=shift_pressed)
                if char:
                    self.text_buffer += char
                    self._send_update()

        # Suppress key from reaching game
        self.listener.suppress_event()

    def _handle_paste(self):
        try:
            text = pyperclip.paste()
            if text:
                text = text.replace('\r\n', ' ').replace('\n', ' ')
                text = ''.join(c for c in text if c.isprintable())
                # Truncate to prevent lag from large pastes
                max_len = 500
                remaining = max_len - len(self.text_buffer)
                if remaining > 0:
                    text = text[:remaining]
                    self.text_buffer += text
                    self._send_update()
        except Exception as e:
            print(f"[InputCapture] Paste error: {e}")

    def _submit(self):
        text = self.text_buffer.strip()
        self.active = False
        self._deactivate_time = time.time()
        self._reset_modifier_state()
        if text:
            mode_name = "prompt" if self._prompt_mode else "chat"
            print(f"[InputCapture] Submit ({mode_name}): {text}")
            self._send_message("chat_submit", text)
        # Always send chat_input with active=False to clear the Lua subtitle display
        self._send_message("chat_input", "", active=False)
        self.text_buffer = ""
        # Reset mode after submit (will be set again on next hotkey release)
        self._prompt_mode = False
        self._hotkey_down_time = None

    def _cancel(self):
        self.active = False
        self._deactivate_time = time.time()
        self._reset_modifier_state()
        self.text_buffer = ""
        self._prompt_mode = False
        self._hotkey_down_time = None
        self._hotkey_held = False
        if self._hold_timer is not None:
            self._hold_timer.cancel()
            self._hold_timer = None
        print("[InputCapture] Cancelled")
        self._send_update()

    def force_close(self, reason=""):
        if self.active:
            with self._lock:
                self.active = False
                self._deactivate_time = time.time()
                self._reset_modifier_state()
                self.text_buffer = ""
                self._prompt_mode = False
                self._hotkey_down_time = None
                self._hotkey_held = False
                if self._hold_timer is not None:
                    self._hold_timer.cancel()
                    self._hold_timer = None
            print(f"[InputCapture] Force closed: {reason}")
            self._send_update()

    def _send_update(self):
        self._send_message("chat_input", self.text_buffer, active=self.active)

    def _send_message(self, msg_type, text, active=None):
        if active is None:
            active = self.active
        # Include mode: "prompt" for director mode, "chat" for normal mode
        mode = "prompt" if self._prompt_mode else "chat"
        msg = {"type": msg_type, "text": text, "active": active, "mode": mode}
        try:
            self.send(msg)
        except Exception as e:
            print(f"[InputCapture] Send error: {e}")

    def stop(self):
        if self.listener:
            self.listener.stop()
            self.listener = None
            print("[InputCapture] Stopped")

    def set_hotkey(self, hotkey):
        self.hotkey_name = hotkey.lower()
        self.hotkey_vk = self._parse_hotkey_vk(hotkey)


_capture_instance = None


def get_capture():
    return _capture_instance


def start_capture(send_callback, hotkey='enter', check_pause=None):
    global _capture_instance
    if _capture_instance:
        _capture_instance.stop()
    _capture_instance = ChatInputCapture(send_callback, hotkey, check_pause)
    _capture_instance.start()
    return _capture_instance


def stop_capture():
    global _capture_instance
    if _capture_instance:
        _capture_instance.stop()
        _capture_instance = None


def set_capture_hotkey(hotkey):
    if _capture_instance:
        _capture_instance.set_hotkey(hotkey)
