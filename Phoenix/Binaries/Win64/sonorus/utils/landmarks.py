"""
Landmark and spatial utilities for Sonorus.
Handles landmark beacons, distance calculations, and cardinal directions.
"""

import os
import json
import math

from .settings import DATA_DIR
from constants import (
    LANDMARK_MAX_DISTANCE,
    LANDMARK_VERTICAL_THRESHOLD,
    LANDMARK_BEACON_COUNT,
)

LANDMARK_LOCATIONS_FILE = os.path.join(DATA_DIR, "landmark_locations.json")

# Reference to lua socket for getting game context (set by server.py)
_lua_socket = None

def set_lua_socket(socket):
    """Set the lua socket reference for reading game context."""
    global _lua_socket
    _lua_socket = socket

# Module-level cache
_landmark_cache = None
_landmark_cache_mtime = 0


def load_landmarks():
    """Load landmark database with caching"""
    global _landmark_cache, _landmark_cache_mtime
    try:
        if os.path.exists(LANDMARK_LOCATIONS_FILE):
            mtime = os.path.getmtime(LANDMARK_LOCATIONS_FILE)
            if _landmark_cache is None or mtime > _landmark_cache_mtime:
                with open(LANDMARK_LOCATIONS_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                _landmark_cache = data.get('landmarks', [])
                _landmark_cache_mtime = mtime
            return _landmark_cache
    except Exception as e:
        print(f"[Landmarks] Error loading: {e}")
    return []


def load_player_position():
    """Load player position from game_context (via socket cache)"""
    try:
        if _lua_socket:
            game_context = _lua_socket.get_game_context()
            if game_context:
                x = game_context.get('x')
                y = game_context.get('y')
                z = game_context.get('z')
                if x is not None and y is not None and z is not None:
                    return {
                        'x': x,
                        'y': y,
                        'z': z,
                        'location': game_context.get('location', 'Unknown')
                    }
    except Exception as e:
        print(f"[Landmarks] Error loading player position: {e}")
    return None


def calculate_distance(pos1, pos2):
    """Calculate 3D Euclidean distance"""
    dx = pos2['x'] - pos1['x']
    dy = pos2['y'] - pos1['y']
    dz = pos2['z'] - pos1['z']
    return math.sqrt(dx*dx + dy*dy + dz*dz)


def get_cardinal_direction(from_pos, to_pos):
    """Get cardinal direction from one point to another (N/S/E/W/NE/etc)"""
    dx = to_pos['x'] - from_pos['x']
    dy = to_pos['y'] - from_pos['y']

    # In UE4: +X is typically East, +Y is typically North (but often inverted)
    # Hogwarts Legacy uses: +X = East, -Y = South (Y increases going north)
    # We'll use the angle to determine direction
    if dx == 0 and dy == 0:
        return ""

    angle = math.degrees(math.atan2(dy, dx))

    # Normalize to 0-360
    if angle < 0:
        angle += 360

    # Map angle to cardinal direction (E=0, N=90, W=180, S=270)
    # But UE4 Y-axis is often flipped, so adjust
    # After testing, -Y seems to be South in this game
    # So: angle 0 = East, 90 = South, 180 = West, 270 = North
    directions = [
        (337.5, 22.5, "east"),
        (22.5, 67.5, "southeast"),
        (67.5, 112.5, "south"),
        (112.5, 157.5, "southwest"),
        (157.5, 202.5, "west"),
        (202.5, 247.5, "northwest"),
        (247.5, 292.5, "north"),
        (292.5, 337.5, "northeast"),
    ]

    for start, end, direction in directions:
        if start <= angle < end:
            return direction
        # Handle wrap-around for east (337.5-360 and 0-22.5)
        if direction == "east" and (angle >= 337.5 or angle < 22.5):
            return direction

    return "east"  # Default


def format_distance(distance_units):
    """Format distance in human-readable form (meters or km)"""
    # UE4 units: approximately 100 units = 1 meter
    meters = distance_units / 100

    if meters < 1000:
        return f"{int(meters)}m"
    else:
        return f"{meters/1000:.1f}km"


def get_landmark_beacons(player_pos=None, world_name=None, max_distance=None, count=None):
    """
    Get nearby landmarks as beacons with distance and direction.
    Returns list of dicts: [{name, distance, direction, raw_distance}, ...]
    """
    if max_distance is None:
        max_distance = LANDMARK_MAX_DISTANCE
    if count is None:
        count = LANDMARK_BEACON_COUNT

    # Load player position if not provided
    if player_pos is None:
        pos_data = load_player_position()
        if not pos_data:
            return []
        player_pos = {'x': pos_data.get('x', 0), 'y': pos_data.get('y', 0), 'z': pos_data.get('z', 0)}
        if world_name is None:
            world_name = pos_data.get('location', '')

    landmarks = load_landmarks()
    if not landmarks:
        return []

    beacons = []
    for lm in landmarks:
        lm_world = lm.get('world', '')

        # Filter by world (Hogwarts landmarks only visible in Hogwarts, etc.)
        # But Overland landmarks are visible from Hogwarts since they're connected
        if world_name:
            # Normalize world names
            player_world_lower = world_name.lower()
            lm_world_lower = lm_world.lower()

            # Same world or connected worlds
            is_same_world = lm_world_lower in player_world_lower or player_world_lower in lm_world_lower
            is_overland_connected = ('hogwarts' in player_world_lower and lm_world_lower == 'overland') or \
                                   ('overland' in player_world_lower and 'hogwarts' in lm_world_lower) or \
                                   ('hogsmeade' in player_world_lower and lm_world_lower == 'overland') or \
                                   ('overland' in player_world_lower and 'hogsmeade' in lm_world_lower)

            if not (is_same_world or is_overland_connected):
                # Skip landmarks in completely different worlds (e.g., Sanctuary)
                continue

        lm_pos = {'x': lm.get('x', 0), 'y': lm.get('y', 0), 'z': lm.get('z', 0)}

        # Calculate distance
        dist = calculate_distance(player_pos, lm_pos)
        if dist > max_distance:
            continue

        # Get horizontal direction
        direction = get_cardinal_direction(player_pos, lm_pos)

        # Get vertical component
        z_diff = lm_pos['z'] - player_pos['z']
        vertical = ""
        if abs(z_diff) > LANDMARK_VERTICAL_THRESHOLD:
            vertical = "above" if z_diff > 0 else "below"

        # Combine direction
        if vertical and direction:
            full_direction = f"{vertical}, {direction}"
        elif vertical:
            full_direction = vertical
        else:
            full_direction = direction

        beacons.append({
            'name': lm.get('name', 'Unknown'),
            'distance': format_distance(dist),
            'direction': full_direction,
            'raw_distance': dist,
            'world': lm_world
        })

    # Sort by distance and return top N
    beacons.sort(key=lambda b: b['raw_distance'])
    return beacons[:count]


def format_beacons_for_llm(beacons):
    """Format beacon list as a concise string for LLM context"""
    if not beacons:
        return ""

    parts = []
    for b in beacons:
        if b['direction']:
            parts.append(f"{b['name']} ({b['distance']} {b['direction']})")
        else:
            parts.append(f"{b['name']} ({b['distance']})")

    return "Nearby landmarks: " + ", ".join(parts)


def format_beacons_for_vision(beacons):
    """Format beacon list for vision AI (more detailed, helps identify location)"""
    if not beacons:
        return ""

    lines = ["Known locations near player (use screenshot to determine if inside any):"]
    for b in beacons[:5]:  # Top 5 for vision
        if b['direction']:
            lines.append(f"- {b['name']}: {b['distance']} {b['direction']}")
        else:
            lines.append(f"- {b['name']}: {b['distance']}")

    return "\n".join(lines)
