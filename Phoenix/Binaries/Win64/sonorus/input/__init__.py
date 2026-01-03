"""
Sonorus input capture modules.

Submodules:
- text: In-game text input capture using pynput
- voice: Push-to-talk audio capture for STT
- hotkeys: Stop/reset conversation hotkey handling
"""
from . import text
from . import voice
from . import hotkeys
