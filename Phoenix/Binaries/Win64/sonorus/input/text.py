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

user32 = ctypes.windll.user32

# Windows message types
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105

# VK codes
VK_RETURN = 0x0D
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


def get_active_window_title():
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
    title = get_active_window_title().lower().strip()
    return title == "hogwarts legacy"

def is_key_pressed(vk):
    """Check if a key is currently pressed using GetAsyncKeyState."""
    return (user32.GetAsyncKeyState(vk) & 0x8000) != 0


def vk_to_char(vk):
    """Convert virtual key code to character using current keyboard state."""
    # Get current keyboard state
    keyboard_state = (ctypes.c_ubyte * 256)()
    if not user32.GetKeyboardState(keyboard_state):
        return None

    # In low-level hooks, GetKeyboardState may be stale.
    # Manually set/clear shift state from GetAsyncKeyState (physical key state)
    if is_key_pressed(VK_SHIFT) or is_key_pressed(VK_LSHIFT) or is_key_pressed(VK_RSHIFT):
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

    def _parse_hotkey_vk(self, hotkey):
        """Convert hotkey name to VK code."""
        hotkey = hotkey.lower()
        if hotkey == 'enter':
            return VK_RETURN
        elif hotkey == 'tab':
            return VK_TAB
        elif len(hotkey) == 1:
            # For single characters, get VK code
            return user32.VkKeyScanW(ord(hotkey)) & 0xFF
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

    def _win32_filter(self, msg, data):
        """
        Handle ALL key processing here.
        - Capture keys for chat buffer
        - Call suppress_event() to block from game
        - Return value controls whether on_press/on_release is called (we don't use those)
        """
        # Only handle key down events
        if msg not in (WM_KEYDOWN, WM_SYSKEYDOWN):
            return

        vk = data.vkCode

        # Always let modifier keys through untouched
        if vk in VK_MODIFIERS:
            return

        # Check modifier states directly from Windows
        alt_pressed = is_key_pressed(VK_MENU) or is_key_pressed(VK_LMENU) or is_key_pressed(VK_RMENU)
        win_pressed = is_key_pressed(VK_LWIN) or is_key_pressed(VK_RWIN)
        ctrl_pressed = is_key_pressed(VK_CONTROL) or is_key_pressed(VK_LCONTROL) or is_key_pressed(VK_RCONTROL)

        # Always let Alt/Win combos through (system shortcuts)
        if alt_pressed or win_pressed:
            return

        # === NOT IN CHAT MODE ===
        if not self.active:
            # Prevent immediate reactivation due to key repeat
            if time.time() - self._deactivate_time < 0.15:
                return

            # Check for hotkey to open chat (only when game window is active)
            if vk == self.hotkey_vk:
                game_active = is_game_window_active()
                if not game_active:
                    print(f"[InputCapture] Hotkey blocked: game window not active (window: '{get_active_window_title()}')")
                    return

                # Check if game is paused
                if self.check_pause:
                    is_paused = self.check_pause()
                    if is_paused:
                        print("[InputCapture] Hotkey blocked: check_pause() returned True")
                        return

                with self._lock:
                    self.active = True
                    self.text_buffer = ""

                # Trigger vision capture for fresh context
                try:
                    from vision_agent import get_agent
                    agent = get_agent()
                    if agent:
                        agent.capture_now()
                except Exception:
                    pass  # Vision capture is optional

                print("[InputCapture] Chat ACTIVE")
                self._send_update()
                # Suppress the hotkey so game doesn't see it
                self.listener.suppress_event()
            return

        # === CHAT IS ACTIVE ===
        # If game window lost focus, close chat and let keys through
        if not is_game_window_active():
            with self._lock:
                if self.active:
                    self.active = False
                    self._deactivate_time = time.time()
                    self.text_buffer = ""
                    print("[InputCapture] Closed (game lost focus)")
                    self._send_update()
            return  # Let key through to other applications

        with self._lock:
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
                char = vk_to_char(vk)
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
                self.text_buffer += text
                self._send_update()
        except Exception as e:
            print(f"[InputCapture] Paste error: {e}")

    def _submit(self):
        text = self.text_buffer.strip()
        self.active = False
        self._deactivate_time = time.time()
        if text:
            print(f"[InputCapture] Submit: {text}")
            self._send_message("chat_submit", text)
        else:
            self._send_message("chat_input", "", active=False)
        self.text_buffer = ""

    def _cancel(self):
        self.active = False
        self._deactivate_time = time.time()
        self.text_buffer = ""
        print("[InputCapture] Cancelled")
        self._send_update()

    def force_close(self, reason=""):
        if self.active:
            with self._lock:
                self.active = False
                self._deactivate_time = time.time()
                self.text_buffer = ""
            print(f"[InputCapture] Force closed: {reason}")
            self._send_update()

    def _send_update(self):
        self._send_message("chat_input", self.text_buffer, active=self.active)

    def _send_message(self, msg_type, text, active=None):
        if active is None:
            active = self.active
        msg = {"type": msg_type, "text": text, "active": active}
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
