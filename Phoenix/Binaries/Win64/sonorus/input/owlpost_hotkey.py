"""
Owl Post Hotkey — toggles the Owl Post browser overlay.

Follows the same pattern as mode_hotkey.py and fpv_hotkey.py.
"""
import threading
import ctypes
from pynput import keyboard
from .voice import can_activate_hotkey, get_active_window_title

user32 = ctypes.windll.user32

# VK codes
VK_OEM_3 = 0xC0  # ` ~ (backtick/tilde)
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

# Modifiers to ignore
VK_MENU = 0x12
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_CONTROL = 0x11

HOTKEY_VK_MAP = {
    'backquote': VK_OEM_3, 'tilde': VK_OEM_3, '`': VK_OEM_3, '~': VK_OEM_3,
    'home': VK_HOME, 'end': VK_END, 'insert': VK_INSERT,
    'f1': VK_F1, 'f2': VK_F2, 'f3': VK_F3, 'f4': VK_F4, 'f5': VK_F5,
    'f6': VK_F6, 'f7': VK_F7, 'f8': VK_F8, 'f9': VK_F9, 'f10': VK_F10,
    'f11': VK_F11, 'f12': VK_F12,
}

# Module state
_listener = None
_hotkey_vk = VK_OEM_3
_check_pause = None
_overlay = None
_overlay_title = None


def is_key_pressed(vk):
    return (user32.GetAsyncKeyState(vk) & 0x8000) != 0


def _win32_event_filter(msg, data):
    global _hotkey_vk, _check_pause, _overlay

    if msg not in (0x0100, 0x0104):  # WM_KEYDOWN, WM_SYSKEYDOWN
        return True

    vk = data.vkCode

    if is_key_pressed(VK_MENU) or is_key_pressed(VK_LWIN) or is_key_pressed(VK_RWIN) or is_key_pressed(VK_CONTROL):
        return True

    if vk != _hotkey_vk:
        return True

    # Game or overlay must be active (so user can close overlay when it has focus)
    overlay_focused = _overlay_title and _overlay_title.lower() in get_active_window_title().lower()
    if not can_activate_hotkey(_check_pause) and not overlay_focused:
        return True

    # Toggle overlay in background thread
    if _overlay:
        threading.Thread(target=_overlay.toggle, daemon=True).start()

    return False  # Suppress key


def set_overlay(overlay, title_match=None):
    """Set the BrowserOverlay instance to toggle."""
    global _overlay, _overlay_title
    _overlay = overlay
    _overlay_title = title_match


def start_capture(hotkey='backquote', check_pause=None):
    """Start listening for owl post hotkey."""
    global _listener, _hotkey_vk, _check_pause

    stop_capture()

    _check_pause = check_pause
    _hotkey_vk = HOTKEY_VK_MAP.get(hotkey.lower(), VK_OEM_3)

    _listener = keyboard.Listener(
        win32_event_filter=_win32_event_filter,
        suppress=False
    )
    _listener.start()
    print(f"[OwlPostHotkey] Listening for hotkey: {hotkey} (VK={hex(_hotkey_vk)})")


def stop_capture():
    """Stop listening for hotkey."""
    global _listener
    if _listener:
        try:
            _listener.stop()
        except Exception:
            pass
        _listener = None


def set_hotkey(hotkey):
    """Update hotkey without restarting listener."""
    global _hotkey_vk
    new_vk = HOTKEY_VK_MAP.get(hotkey.lower(), VK_OEM_3)
    if new_vk != _hotkey_vk:
        _hotkey_vk = new_vk
        print(f"[OwlPostHotkey] Hotkey updated to: {hotkey} (VK={hex(_hotkey_vk)})")
