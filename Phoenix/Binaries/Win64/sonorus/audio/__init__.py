"""
Sonorus audio modules.

Submodules:
- spatial_lav: 3D audio playback using libaudioverse (preferred)
- spatial: 3D audio playback using PyOpenAL (fallback)
- lipsync: Phoneme-based lip sync viseme generation
- playback: TTS playback coordination with lipsync
"""
from . import lipsync
from . import playback

# Pick audio backend: libaudioverse preferred, PyOpenAL fallback
# Note: spatial_lav.py catches its own ImportError internally (LAV_AVAILABLE),
# so we check the flag rather than relying on ImportError from the module.
from . import spatial_lav
if spatial_lav.LAV_AVAILABLE:
    spatial = spatial_lav
    from .spatial_lav import (
        LAV_AVAILABLE as OPENAL_AVAILABLE,
        shutdown as _audio_shutdown,
        get_player,
        Audio3DPlayer,
        PositionReader,
        TTSStream,
        create_tts_stream,
    )
    _backend = "libaudioverse"
else:
    from . import spatial
    from .spatial import (
        OPENAL_AVAILABLE,
        shutdown as _audio_shutdown,
        get_player,
        Audio3DPlayer,
        PositionReader,
        TTSStream,
        create_tts_stream,
    )
    _backend = "pyopenal"

print(f"[Audio] Backend: {_backend}")

# Backward-compat re-exports: VR functions now live in the vr package.
# Existing consumers that import from audio will still work.
from vr import init_vr_tracker, set_vr_lua_socket


def shutdown():
    """Shut down both audio and VR subsystems."""
    _audio_shutdown()
    try:
        from vr import shutdown as vr_shutdown
        vr_shutdown()
    except Exception:
        pass


from .playback import (
    PlaybackCoordinator,
    TurnState,
    get_coordinator,
    init_coordinator,
)
