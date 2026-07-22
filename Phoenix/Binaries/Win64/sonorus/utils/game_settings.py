"""
Helpers for reading Hogwarts Legacy game settings from GameUserSettings.ini.
"""

import os


SUBTITLES_DISABLED_WARNING = (
    "Subtitles are currently DISABLED in Hogwarts Legacy. "
    "Sonorus requires subtitles to be ON to function correctly. "
    "Please enable subtitles in the game's Audio settings."
)


def get_game_user_settings_path():
    """Return the Hogwarts Legacy GameUserSettings.ini path."""
    return os.path.join(
        os.environ.get("LOCALAPPDATA", ""),
        "Hogwarts Legacy", "Saved", "Config",
        "WindowsNoEditor", "GameUserSettings.ini"
    )


def get_game_subtitles_enabled():
    """Return True/False if known, or None if the setting could not be read."""
    ini_path = get_game_user_settings_path()
    if not os.path.isfile(ini_path):
        return None

    with open(ini_path, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if stripped.lower().startswith("subtitlesenabled="):
                value = stripped.split("=", 1)[1].strip().lower()
                if value in ("true", "1"):
                    return True
                if value in ("false", "0"):
                    return False
                return None

    return None


def get_game_settings_warnings():
    """Return warning strings for problematic game settings."""
    warnings = []
    try:
        subtitles_enabled = get_game_subtitles_enabled()
        if subtitles_enabled is False:
            warnings.append(SUBTITLES_DISABLED_WARNING)
    except Exception:
        pass
    return warnings
