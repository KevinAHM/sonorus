"""
Sonorus input capture modules.

Submodules:
- text: In-game text input capture using pynput
- voice: Push-to-talk audio capture for STT
- hotkeys: Stop/reset conversation hotkey handling
- mode_hotkey: Conversation mode cycling hotkey
"""
from . import text
from . import voice
from . import hotkeys
from . import mode_hotkey

# Export shared guard for all hotkeys
from .voice import can_activate_hotkey
