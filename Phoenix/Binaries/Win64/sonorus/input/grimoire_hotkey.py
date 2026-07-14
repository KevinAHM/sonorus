"""
Grimoire Hotkey — hold Escape to toggle the Wizard's Grimoire overlay.

Tap Escape (< 500ms): re-injected to game (pause menu, etc.)
Hold Escape (>= 500ms): toggles the grimoire overlay.
"""
import threading
import ctypes
import time
from pynput import keyboard
from .voice import can_activate_hotkey, get_active_window_title

user32 = ctypes.windll.user32

# VK codes
VK_ESCAPE = 0x1B
VK_MENU = 0x12
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_CONTROL = 0x11

# Low-level keyboard hook flag for injected keys (keybd_event / SendInput)
LLKHF_INJECTED = 0x10
KEYEVENTF_KEYUP = 0x0002


def is_key_pressed(vk):
    return (user32.GetAsyncKeyState(vk) & 0x8000) != 0


# Module state
_listener = None
_check_pause = None
_overlay = None
_overlay_title = None  # title_match from the overlay, for focus detection

_lock = threading.Lock()
_escape_down_time = None
_escape_held = False
_hold_timer = None
_hold_triggered = False


def _is_overlay_focused():
    """Check if the grimoire overlay browser window currently has focus."""
    if not _overlay_title:
        return False
    title = get_active_window_title().lower()
    return _overlay_title.lower() in title


def _on_hold():
    """Called after 500ms if Escape is still held — toggle the overlay."""
    global _hold_triggered
    with _lock:
        if _escape_down_time is None:
            return  # Keyup already happened, abort
        _hold_triggered = True
    if _overlay:
        threading.Thread(target=_overlay.toggle, daemon=True).start()


def _reinject_escape():
    """Re-inject an Escape tap so the game receives it."""
    user32.keybd_event(VK_ESCAPE, 0, 0, 0)
    time.sleep(0.03)
    user32.keybd_event(VK_ESCAPE, 0, KEYEVENTF_KEYUP, 0)


def _win32_event_filter(msg, data):
    global _escape_down_time, _escape_held, _hold_timer, _hold_triggered

    vk = data.vkCode

    if vk != VK_ESCAPE:
        return True

    # Let our own re-injected keys pass through
    if data.flags & LLKHF_INJECTED:
        return True

    # Let modifier combos through (Alt+Esc, Ctrl+Esc, Win+Esc)
    if is_key_pressed(VK_MENU) or is_key_pressed(VK_LWIN) or is_key_pressed(VK_RWIN) or is_key_pressed(VK_CONTROL):
        return True

    # === KEYUP ===
    if msg in (0x0101, 0x0105):  # WM_KEYUP, WM_SYSKEYUP
        with _lock:
            if not _escape_held:
                return True

            _escape_held = False
            _escape_down_time = None

            if _hold_timer is not None:
                _hold_timer.cancel()
                _hold_timer = None

            triggered = _hold_triggered
            _hold_triggered = False

        if triggered:
            # Hold opened the overlay — swallow the keyup
            return False

        # Quick tap — re-inject Escape so game gets it
        threading.Thread(target=_reinject_escape, daemon=True).start()
        return False  # Suppress original keyup (re-injected pair will come through)

    # === KEYDOWN ===
    if msg not in (0x0100, 0x0104):  # WM_KEYDOWN, WM_SYSKEYDOWN
        return True

    with _lock:
        # Ignore key repeat while held
        if _escape_down_time is not None:
            return False

    # Allow when game is focused OR overlay is focused (so user can close it)
    overlay_focused = _is_overlay_focused()
    if not can_activate_hotkey(_check_pause) and not overlay_focused:
        return True

    # If overlay is open/focused, close immediately on tap — no hold needed
    if overlay_focused:
        if _overlay:
            threading.Thread(target=_overlay.toggle, daemon=True).start()
        return False

    with _lock:
        # Start tracking the hold (for opening)
        _escape_down_time = time.time()
        _escape_held = True
        _hold_triggered = False

        if _hold_timer is not None:
            _hold_timer.cancel()
        _hold_timer = threading.Timer(0.5, _on_hold)
        _hold_timer.daemon = True
        _hold_timer.start()

    return False  # Suppress — will re-inject on tap


def set_overlay(overlay, title_match=None):
    """Set the overlay toggler (anything with a .toggle() method).

    Args:
        overlay: Object with .toggle() method (e.g. OverlayManager.toggler())
        title_match: Window title substring for detecting overlay focus
    """
    global _overlay, _overlay_title
    _overlay = overlay
    _overlay_title = title_match


def start_capture(check_pause=None):
    """Start listening for Escape hold."""
    global _listener, _check_pause

    stop_capture()

    _check_pause = check_pause

    _listener = keyboard.Listener(
        win32_event_filter=_win32_event_filter,
        suppress=False
    )
    _listener.start()
    print("[GrimoireHotkey] Listening for Escape (hold to toggle)")


def stop_capture():
    """Stop listening."""
    global _listener, _hold_timer, _escape_down_time, _escape_held, _hold_triggered

    with _lock:
        if _hold_timer is not None:
            _hold_timer.cancel()
            _hold_timer = None
        _escape_down_time = None
        _escape_held = False
        _hold_triggered = False

    if _listener:
        try:
            _listener.stop()
        except Exception:
            pass
        _listener = None
