"""
Game context utilities for Sonorus.
Handles formatting of game context for LLM prompts.
"""

import re
import time

from .settings import load_settings
from .landmarks import get_landmark_beacons, format_beacons_for_llm


def format_game_context(context, current_speaker=None):
    """Format game context for LLM prompt

    Args:
        context: Game context dict from Lua
        current_speaker: NPC ID of the character being prompted (to exclude from nearby list)
    """
    if not context:
        return ""

    parts = []
    player_name = context.get('playerName', 'Unknown')
    player_house = context.get('playerHouse', 'Unknown')

    if player_name and player_name != "Unknown":
        # Build player description with optional status modifiers
        player_desc = f"You are speaking with {player_name}, a {player_house} student"

        # Add status modifiers (only when true)
        status_parts = []
        if context.get('inCombat'):
            status_parts.append("currently in combat")
        if context.get('isOnBroom'):
            status_parts.append("flying on a broom")

        if status_parts:
            player_desc += f" who is {' and '.join(status_parts)}"

        parts.append(player_desc + ".")

    date_formatted = context.get('dateFormatted', '')
    time_formatted = context.get('timeFormatted', '')
    time_period = context.get('timePeriod', 'Day')

    if date_formatted:
        parts.append(f"The date is {date_formatted}.")

    if time_formatted:
        time_descriptions = {
            'Night': f"It is {time_formatted}, nighttime.",
            'Dawn': f"It is {time_formatted}, early morning.",
            'Morning': f"It is {time_formatted} in the morning.",
            'Noon': f"It is {time_formatted}, midday.",
            'Afternoon': f"It is {time_formatted} in the afternoon.",
            'Evening': f"It is {time_formatted} in the evening.",
        }
        parts.append(time_descriptions.get(time_period, f"It is {time_formatted}."))

    # Use specific zone location from HUD if available, fallback to broad location
    zone = context.get('zoneLocation', '')
    region = context.get('location', '')
    if zone:
        parts.append(f"Current location: {zone}.")
    elif region and region not in ("Hogwarts", "Unknown", ""):
        region = region.replace('_', ' ')
        parts.append(f"Current location: {region}.")

    # Format nearby NPCs with bios
    nearby = context.get('nearbyNpcs', [])
    if nearby:
        settings = load_settings()
        bios = settings.get('prompts', {}).get('bios', {})

        nearby_parts = []
        for char in nearby:
            name = char.get('name', 'Unknown')

            # Skip the current speaker (NPC shouldn't see themselves in nearby list)
            if current_speaker and name.lower().replace(' ', '') == current_speaker.lower().replace(' ', ''):
                continue

            # Distance is in Unreal units (centimeters), convert to meters
            distance_cm = char.get('distance', 0)
            distance_m = round(distance_cm / 100)

            # Prettify name (SebastianSallow -> Sebastian Sallow)
            display_name = re.sub(r'([a-z])([A-Z])', r'\1 \2', name)

            # Look up bio
            bio = bios.get(name) or bios.get(display_name)
            if bio:
                nearby_parts.append(f"- {display_name} (~{distance_m}m away): {bio}")
            else:
                nearby_parts.append(f"- {display_name} (~{distance_m}m away)")

        if nearby_parts:
            parts.append("\n\nNearby characters:\n" + "\n".join(nearby_parts))

    # Add vision context (scene description from vision agent - read directly from memory)
    try:
        from vision_agent import get_agent
        agent = get_agent()
        vision_ctx = agent.get_current_context() if agent else None
        if vision_ctx:
            # Check age (skip if older than 5 minutes)
            age = time.time() - vision_ctx.get('timestamp', 0)
            if age > 300:
                vision_ctx = None
            # Check location match (skip if from different location)
            elif zone or region:
                ctx_zone = vision_ctx.get('zoneLocation', '')
                ctx_region = vision_ctx.get('location', '')
                # Must match either zone or region
                if zone and ctx_zone and zone.lower() != ctx_zone.lower():
                    vision_ctx = None
                elif not zone and region and ctx_region and region.lower() != ctx_region.lower():
                    vision_ctx = None
        if vision_ctx:
            # Build text from structured fields
            vision_parts = []
            if vision_ctx.get('scene'):
                vision_parts.append(f"Scene: {vision_ctx['scene']}")
            if vision_ctx.get('atmosphere'):
                vision_parts.append(f"Atmosphere: {vision_ctx['atmosphere']}")
            if vision_ctx.get('characters'):
                vision_parts.append(f"Visible: {vision_ctx['characters']}")
            if vision_parts:
                parts.append(f"\n\nWhat you can see:\n" + "\n".join(vision_parts))
            elif vision_ctx.get('description'):
                parts.append(f"\n\nWhat you can see:\n{vision_ctx['description']}")
    except Exception:
        pass  # Vision context is optional

    # Add landmark beacons (spatial context)
    # Filter out the current zone location to avoid redundancy
    try:
        beacons = get_landmark_beacons()

        # Filter out exact or close matches to zone location
        if zone and beacons:
            zone_lower = zone.lower()
            beacons = [b for b in beacons if zone_lower not in b['name'].lower()
                      and b['name'].lower() not in zone_lower]

        beacon_str = format_beacons_for_llm(beacons)
        if beacon_str:
            parts.append(f"\n\n{beacon_str}")
    except Exception as e:
        print(f"[Context] Error getting beacons: {e}")

    if not parts:
        return ""

    return "Current situation:\n" + " ".join(parts[:4]) + ("".join(parts[4:]) if len(parts) > 4 else "")
