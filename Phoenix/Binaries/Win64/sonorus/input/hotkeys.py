"""
Stop Conversation Hotkey Capture

Simple key press handler for stopping/resetting conversations.
Uses pynput with game window check.
"""

import threading
import ctypes
from pynput import keyboard

user32 = ctypes.windll.user32

# VK codes
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
VK_DELETE = 0x2E

# Modifiers to ignore (let Alt+Tab, Win key, etc through)
VK_MENU = 0x12
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_CONTROL = 0x11

HOTKEY_VK_MAP = {
    'f1': VK_F1, 'f2': VK_F2, 'f3': VK_F3, 'f4': VK_F4, 'f5': VK_F5,
    'f6': VK_F6, 'f7': VK_F7, 'f8': VK_F8, 'f9': VK_F9, 'f10': VK_F10,
    'f11': VK_F11, 'f12': VK_F12, 'escape': VK_ESCAPE, 'delete': VK_DELETE,
}

# Module state
_listener = None
_callback = None
_hotkey_vk = VK_DELETE
_check_pause = None


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
    """Check if game window is in foreground."""
    title = get_active_window_title().lower().strip()
    return title == "hogwarts legacy"


def _win32_event_filter(msg, data):
    """Low-level keyboard hook for reliable key capture."""
    global _callback, _hotkey_vk, _check_pause

    # Only process key down
    if msg not in (0x0100, 0x0104):  # WM_KEYDOWN, WM_SYSKEYDOWN
        return True  # Let it through

    vk = data.vkCode

    # Always let modifier combos through (Alt+Tab, Win key, etc)
    if is_key_pressed(VK_MENU) or is_key_pressed(VK_LWIN) or is_key_pressed(VK_RWIN) or is_key_pressed(VK_CONTROL):
        return True

    # Only handle our hotkey
    if vk != _hotkey_vk:
        return True

    # Only when game is active
    if not is_game_window_active():
        return True

    # Check if game is paused
    if _check_pause and _check_pause():
        return True

    # Trigger callback
    if _callback:
        threading.Thread(target=_callback, daemon=True).start()

    # Suppress the key (don't let game see it)
    return False


def start_capture(callback, hotkey='f8', check_pause=None):
    """
    Start listening for stop hotkey.

    Args:
        callback: Function to call when hotkey is pressed
        hotkey: Hotkey name ('f1'-'f10', 'escape')
        check_pause: Optional callback that returns True if game is paused
    """
    global _listener, _callback, _hotkey_vk, _check_pause

    stop_capture()  # Stop any existing listener

    _callback = callback
    _check_pause = check_pause
    _hotkey_vk = HOTKEY_VK_MAP.get(hotkey.lower(), VK_DELETE)

    _listener = keyboard.Listener(
        win32_event_filter=_win32_event_filter,
        suppress=False
    )
    _listener.start()
    print(f"[StopCapture] Listening for hotkey: {hotkey} (VK={hex(_hotkey_vk)})")


def stop_capture():
    """Stop listening for hotkey."""
    global _listener
    if _listener:
        try:
            _listener.stop()
        except:
            pass
        _listener = None


def set_hotkey(hotkey):
    """Update hotkey without restarting listener."""
    global _hotkey_vk
    new_vk = HOTKEY_VK_MAP.get(hotkey.lower(), VK_DELETE)
    if new_vk != _hotkey_vk:
        _hotkey_vk = new_vk
        print(f"[StopCapture] Hotkey updated to: {hotkey} (VK={hex(_hotkey_vk)})")
