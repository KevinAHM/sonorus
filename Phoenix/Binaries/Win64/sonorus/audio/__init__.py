"""
Sonorus audio modules.

Submodules:
- spatial: 3D audio playback using PyOpenAL
- lipsync: Phoneme-based lip sync viseme generation
- playback: TTS playback coordination with lipsync
"""
from . import spatial
from . import lipsync
from . import playback

# Re-export commonly used items for convenience
from .spatial import (
    OPENAL_AVAILABLE,
    shutdown,
    get_player,
    create_tts_stream,
    play_tts_stream,
    TTSStream,
    Audio3DPlayer,
    PositionReader,
)

from .playback import (
    PlaybackCoordinator,
    TurnState,
    get_coordinator,
    init_coordinator,
)
