"""
Sonorus data directory.

Contains configuration and runtime data files:
- settings.json: User configuration
- config.html: Web UI for configuration
- main_localization.json: NPC/location display names
- subtitles.json: Dialogue text by line ID
- dialogue_history.json: Conversation history
- landmark_locations.json: World location data
- voice_manifest.json: Voice sample metadata
- system_events.json: Event log for dashboard
"""
from pathlib import Path

DATA_DIR = Path(__file__).parent
