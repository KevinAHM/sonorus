"""
Game mod detection and state management for Sonorus.

Detects installed third-party mods via folder checks and manages live data from Lua.
"""

import os
import threading
import time
from pathlib import Path

# Mod registry - easy to add new mods
MOD_REGISTRY = {
    "house_points": {
        "name": "House Points System",
        "folder": "HousepointSystem",  # Relative to Phoenix/Mods/
        "icon": "trophy",
        "description": "Track house point standings in NPC conversations"
    },
    "floo_companions": {
        "name": "Floo Flame Companions",
        "folder": "FlooFlameCompanions",  # Relative to Phoenix/Mods/
        "icon": "users",
        "description": "Companion system mod - compatible with Sonorus NPC Actions"
    }
}

# Hogwarts professors who can award/deduct house points
PROFESSORS = {
    "PhineasBlack",
    "MatildaWeasley",
    "MudiwaOnai",
    "EleazarFig",
    "SatyavatiShah",
    "AbrahamRonen",
    "AesopSharp",
    "DinahHecat",
    "MirabelGarlick",
    "ChiyoKogawa",
    "BaiHowin",
}

# Valid house point actions
HOUSE_POINT_ACTIONS = {
    "AwardPoints",
    "DeductPoints",
}

VALID_HOUSES = {"Gryffindor", "Slytherin", "Hufflepuff", "Ravenclaw"}


def is_professor(npc_id: str) -> bool:
    """Check if an NPC is a professor who can award house points."""
    return npc_id in PROFESSORS


def parse_house_point_action(action_str: str) -> dict | None:
    """Parse a house point action string like 'AwardPoints Gryffindor 10'.

    Returns dict with {action, house, amount} or None if invalid.
    """
    parts = action_str.split()
    if len(parts) != 3:
        return None

    action_type, house, amount_str = parts

    if action_type not in HOUSE_POINT_ACTIONS:
        return None

    # Normalize house name (case-insensitive match)
    house_normalized = None
    for valid_house in VALID_HOUSES:
        if house.lower() == valid_house.lower():
            house_normalized = valid_house
            break

    if not house_normalized:
        return None

    try:
        amount = int(amount_str)
        if amount <= 0 or amount > 100:  # Reasonable limits
            return None
    except ValueError:
        return None

    return {
        "action": action_type,
        "house": house_normalized,
        "amount": amount
    }

# Runtime state
_installation_status = {}  # mod_id -> bool (folder exists)
_live_data = {}  # mod_id -> dict (from Lua game_context)
_status_lock = threading.Lock()
_check_interval = 30  # seconds
_checker_thread = None
_running = False


def _get_mods_folder() -> Path:
    """Get Phoenix/Mods folder path (relative to sonorus/).

    sonorus/ is in Phoenix/Binaries/Win64/sonorus
    Mods/ is in Phoenix/Mods
    """
    sonorus_dir = Path(__file__).parent.parent  # sonorus/
    phoenix_dir = sonorus_dir.parent.parent.parent  # Phoenix/
    return phoenix_dir / "Mods"


def check_mod_installation():
    """Check which mods are installed (folder exists)."""
    mods_folder = _get_mods_folder()
    with _status_lock:
        for mod_id, info in MOD_REGISTRY.items():
            mod_path = mods_folder / info["folder"]
            exists = mod_path.exists()
            _installation_status[mod_id] = exists


def is_mod_installed(mod_id: str) -> bool:
    """Check if a specific mod is installed."""
    with _status_lock:
        return _installation_status.get(mod_id, False)


def get_all_mod_status() -> dict:
    """Get installation status for all registered mods."""
    with _status_lock:
        return {
            mod_id: {
                "installed": _installation_status.get(mod_id, False),
                "info": MOD_REGISTRY[mod_id],
                "live_data": _live_data.get(mod_id, {})
            }
            for mod_id in MOD_REGISTRY
        }


def update_live_data(mod_id: str, data: dict):
    """Update live data from Lua game_context."""
    with _status_lock:
        _live_data[mod_id] = data


def get_live_data(mod_id: str) -> dict:
    """Get live data for a specific mod."""
    with _status_lock:
        result = _live_data.get(mod_id, {})
        print(f"[Mods] get_live_data({mod_id}): _live_data keys={list(_live_data.keys())}, result keys={list(result.keys()) if result else []}")
        return result


def _checker_loop():
    """Background thread to periodically check mod installation."""
    global _running
    while _running:
        check_mod_installation()
        # Sleep in small increments to allow clean shutdown
        for _ in range(_check_interval):
            if not _running:
                break
            time.sleep(1)


def start_mod_checker():
    """Start background mod detection thread."""
    global _checker_thread, _running
    if _running:
        return  # Already running

    _running = True
    check_mod_installation()  # Check immediately on startup
    _checker_thread = threading.Thread(target=_checker_loop, daemon=True, name="ModChecker")
    _checker_thread.start()
    print("[Mods] Mod checker started")


def stop_mod_checker():
    """Stop the background mod detection thread."""
    global _running
    _running = False
