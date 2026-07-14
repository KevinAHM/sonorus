"""
First-Person View Toggle Hotkey

Configurable hotkey that toggles first-person camera mode.
Sends toggle_fpv message to Lua via socket.

Follows mode_hotkey.py pattern with pynput Windows hooks.
"""

import threading
import ctypes
from pynput import keyboard
from .voice import can_activate_hotkey

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
VK_HOME = 0x24
VK_END = 0x23
VK_INSERT = 0x2D
VK_DELETE = 0x2E

# Modifiers to ignore (let Alt+Tab, Win key, etc through)
VK_MENU = 0x12
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_CONTROL = 0x11

HOTKEY_VK_MAP = {
    'home': VK_HOME, 'end': VK_END, 'insert': VK_INSERT, 'delete': VK_DELETE,
    'f1': VK_F1, 'f2': VK_F2, 'f3': VK_F3, 'f4': VK_F4, 'f5': VK_F5,
    'f6': VK_F6, 'f7': VK_F7, 'f8': VK_F8, 'f9': VK_F9, 'f10': VK_F10,
    'f11': VK_F11, 'f12': VK_F12,
}

# Module state
_listener = None
_check_pause = None
_lua_socket = None
_hotkey_vk = VK_INSERT


def is_key_pressed(vk):
    """Check if a key is currently pressed."""
    return (user32.GetAsyncKeyState(vk) & 0x8000) != 0


def set_lua_socket(socket):
    """Set the Lua socket instance for sending FPV toggle."""
    global _lua_socket
    _lua_socket = socket


def _toggle_fpv():
    """Send FPV toggle to Lua."""
    print("[FPVHotkey] Toggle first-person view")
    if _lua_socket:
        _lua_socket.send_toggle_fpv()


def _win32_event_filter(msg, data):
    """Low-level keyboard hook for reliable key capture."""
    global _check_pause, _hotkey_vk

    # Only process key down
    if msg not in (0x0100, 0x0104):  # WM_KEYDOWN, WM_SYSKEYDOWN
        return True  # Let it through

    vk = data.vkCode

    # Always let modifier combos through (Alt+Tab, Win key, etc)
    if is_key_pressed(VK_MENU) or is_key_pressed(VK_LWIN) or is_key_pressed(VK_RWIN) or is_key_pressed(VK_CONTROL):
        return True

    # Only handle our configured hotkey
    if vk != _hotkey_vk:
        return True

    # Shared guard - game must be active and not paused
    if not can_activate_hotkey(_check_pause):
        return True

    # Trigger FPV toggle
    threading.Thread(target=_toggle_fpv, daemon=True).start()

    # Suppress the key (don't let game see it)
    return False


def start_capture(hotkey='insert', check_pause=None):
    """
    Start listening for FPV hotkey.

    Args:
        hotkey: Hotkey name ('insert', 'end', 'f1'-'f12', etc.)
        check_pause: Optional callback that returns True if game is paused
    """
    global _listener, _check_pause, _hotkey_vk

    stop_capture()  # Stop any existing listener

    _check_pause = check_pause
    _hotkey_vk = HOTKEY_VK_MAP.get(hotkey.lower(), VK_INSERT)

    _listener = keyboard.Listener(
        win32_event_filter=_win32_event_filter,
        suppress=False
    )
    _listener.start()
    print(f"[FPVHotkey] Listening for hotkey: {hotkey} (VK={hex(_hotkey_vk)})")


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
    new_vk = HOTKEY_VK_MAP.get(hotkey.lower(), VK_INSERT)
    if new_vk != _hotkey_vk:
        _hotkey_vk = new_vk
        print(f"[FPVHotkey] Hotkey updated to: {hotkey} (VK={hex(_hotkey_vk)})")
