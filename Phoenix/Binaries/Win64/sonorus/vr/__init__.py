"""
VR headset tracking package.

Public API - drop-in replacement for the VR functions previously in audio/spatial_lav.py.
Consumers import from here: ``from vr import is_vr_active, init_vr_tracker``
"""
import threading

from utils.settings import load_settings, register_vr_callback
from vr.manager import VRManager

# Module-level singleton
_vr_active = False
_vr_tracker: VRManager | None = None
_retry_thread: threading.Thread | None = None
_retry_stop = threading.Event()
_lua_socket_ref = None  # Stashed so retry thread can pass it to manager
_stop_conv_callback = None  # Stashed so late-init managers get the callback


def is_vr_active() -> bool:
    """Check if VR headset tracking is active (cached, no runtime ping)."""
    return _vr_active


def is_vr_spell_mode() -> bool:
    """Check if VR wand is in spell-casting pose (arm extended, wand at gaze level)."""
    return _vr_tracker.vr_spell_mode if (_vr_tracker and _vr_tracker.initialized) else False


def get_vr_tracker() -> VRManager | None:
    """Get the module-level VRManager singleton (may be None if not initialized)."""
    return _vr_tracker


def _try_init(lua_socket=None) -> bool:
    """Attempt to initialize VRManager. Returns True on success."""
    global _vr_tracker, _vr_active
    manager = VRManager()
    if manager.init(lua_socket=lua_socket):
        if _stop_conv_callback:
            manager._stop_conversation_cb = _stop_conv_callback
        _vr_tracker = manager
        _vr_active = True
        register_vr_callback(is_vr_active)
        return True
    return False


def _retry_loop():
    """Background retry: check for UEVR shared memory every 10s until found."""
    while not _retry_stop.is_set():
        _retry_stop.wait(10.0)
        if _retry_stop.is_set():
            break
        if _vr_tracker and _vr_tracker.initialized:
            break
        if _try_init(lua_socket=_lua_socket_ref):
            break


def init_vr_tracker(lua_socket=None) -> VRManager | None:
    """Initialize the module-level VRManager at server startup.

    Called once from server.py. If no backend is available immediately,
    starts a background retry thread that polls silently until UEVR appears.
    """
    global _retry_thread, _lua_socket_ref
    _lua_socket_ref = lua_socket

    if _vr_tracker and _vr_tracker.initialized:
        return _vr_tracker

    settings = load_settings(raw=True)
    audio_cfg = settings.get("audio", {})
    if not audio_cfg.get("vr_tracking", True):
        return None

    # Try immediately
    if _try_init(lua_socket=lua_socket):
        return _vr_tracker

    # Not available yet — start silent background retry
    _retry_stop.clear()
    _retry_thread = threading.Thread(target=_retry_loop, daemon=True)
    _retry_thread.start()
    return None


def set_vr_lua_socket(lua_socket) -> None:
    """Set lua_socket on the VR tracker (called when Lua connects).

    If VR wasn't initialized yet (e.g. UEVR injected after server started),
    retry initialization now — the shared memory should exist by this point.
    """
    global _vr_tracker, _vr_active, _lua_socket_ref
    _lua_socket_ref = lua_socket

    if _vr_tracker:
        _vr_tracker._lua_socket = lua_socket
        return

    # VR not initialized yet — retry (UEVR may have been injected since startup)
    settings = load_settings(raw=True)
    audio_cfg = settings.get("audio", {})
    if not audio_cfg.get("vr_tracking", True):
        return

    _try_init(lua_socket=lua_socket)


def set_stop_conversation_callback(callback) -> None:
    """Register the stop-conversation callback for VR hold gesture.

    Called from server.py after stop_conversation is defined.  Stashed at
    module level so late-initializing managers also receive it.
    """
    global _stop_conv_callback
    _stop_conv_callback = callback
    if _vr_tracker:
        _vr_tracker._stop_conversation_cb = callback


def shutdown() -> None:
    """Shut down VR tracking."""
    global _vr_tracker, _vr_active, _retry_thread
    _retry_stop.set()
    if _retry_thread:
        _retry_thread.join(timeout=1.0)
        _retry_thread = None
    if _vr_tracker:
        _vr_tracker.shutdown()
        _vr_tracker = None
    _vr_active = False
