"""
Game context utilities for Sonorus.
Handles formatting of game context for LLM prompts.
"""

import time

from .settings import load_settings
from .landmarks import get_landmark_beacons, format_beacons_for_llm
from .localization import find_npc_id_by_name, get_display_name


def format_game_context(context, current_speaker=None, participants=None):
    """Format game context for LLM prompt

    Args:
        context: Game context dict from Lua
        current_speaker: NPC ID of the character being prompted (to exclude from nearby list)
        participants: List of participant names in the conversation (for interjections).
                      If None, defaults to just the player.
    """
    if not context:
        return ""

    parts = []
    player_name = context.get('playerName', 'Unknown')
    player_house = context.get('playerHouse', 'Unknown')

    # Build "You are speaking with X, Y, and Z" based on participants
    if participants:
        # Format participant list with "and" before last item
        if len(participants) == 1:
            speaking_with = participants[0]
        elif len(participants) == 2:
            speaking_with = f"{participants[0]} and {participants[1]}"
        else:
            speaking_with = ", ".join(participants[:-1]) + f", and {participants[-1]}"
        parts.append(f"You are speaking with {speaking_with}.")
    elif player_name and player_name != "Unknown":
        # Default: just the player (original behavior)
        # Build player description with optional status modifiers
        player_desc = f"You are speaking with {player_name}, a {player_house} student"

        # Add status modifiers (only when true)
        status_parts = []
        if context.get('inCombat'):
            status_parts.append("currently in combat")
        if context.get('isOnBroom'):
            status_parts.append("flying on a broom")
        if context.get('isSwimming'):
            status_parts.append("swimming")
        if context.get('hoodUp'):
            status_parts.append("with their hood up")

        if status_parts:
            player_desc += f" who is {' and '.join(status_parts)}"

        parts.append(player_desc + ".")

    # Disillusionment charm status (always show)
    in_stealth = context.get('inStealth', False)
    if in_stealth:
        parts.append(f"{player_name} has the Disillusionment charm active (invisible/hard to see).")
    else:
        parts.append(f"{player_name} is visible (no Disillusionment charm).")

    # Companion visibility and status
    if context.get('hasCompanion'):
        companion_id = context.get('companionId', '')
        companion_name = get_display_name(companion_id) if companion_id else 'companion'
        companion_status = []
        if in_stealth:
            companion_status.append("invisible (Disillusionment charm)")
        else:
            companion_status.append("visible")
        if context.get('companionIsSwimming'):
            companion_status.append("swimming")
        parts.append(f"{companion_name} is accompanying {player_name} and is {' and '.join(companion_status)}.")

    # Player equipment/gear (what they're wearing) - if enabled in settings
    settings = load_settings()
    conv_settings = settings.get('conversation', {})
    gear_context_enabled = conv_settings.get('gear_context', True)
    player_gear = context.get('playerGear', '')
    if player_gear and gear_context_enabled:
        parts.append(f"\n\n**{player_name}'s attire:**\n{player_gear}")
        parts.append(f"\n**Note:** Don't comment on {player_name}'s attire unless directly relevant to the conversation.")

    # Mission info for companions - if enabled and speaker is the companion
    mission_context_enabled = conv_settings.get('mission_context', True)
    companion_id = context.get('companionId', '')
    if mission_context_enabled and current_speaker and companion_id:
        # Compare speaker ID with companion ID directly
        if current_speaker == companion_id:
            current_quest = context.get('currentQuest', '')
            quest_objective = context.get('questObjective', '')
            if current_quest or quest_objective:
                mission_parts = []
                if current_quest:
                    mission_parts.append(f"**Quest:** {current_quest}")
                if quest_objective:
                    mission_parts.append(f"**Their goal:** {quest_objective}")
                parts.append(f"\n\n**{player_name}'s current focus:**\n" + "\n".join(mission_parts))
                parts.append(f"\n(This is just for your awareness as {player_name}'s companion. Don't push them to pursue it - they'll get to it when they're ready. You may reference it naturally if it comes up.)")

    date_formatted = context.get('dateFormatted', '')
    time_formatted = context.get('timeFormatted', '')
    time_period = context.get('timePeriod', 'Day')

    if date_formatted:
        parts.append(f"\n\n**Date:** {date_formatted}.")

    if time_formatted:
        time_descriptions = {
            'Night': f"\n**Time:** {time_formatted} (nighttime).",
            'Dawn': f"\n**Time:** {time_formatted} (early morning).",
            'Morning': f"\n**Time:** {time_formatted} (morning).",
            'Noon': f"\n**Time:** {time_formatted} (midday).",
            'Afternoon': f"\n**Time:** {time_formatted} (afternoon).",
            'Evening': f"\n**Time:** {time_formatted} (evening).",
        }
        parts.append(time_descriptions.get(time_period, f"\n**Time:** {time_formatted}."))

    # Use specific zone location from HUD if available, fallback to broad location
    zone = context.get('zoneLocation', '')
    region = context.get('location', '')
    if zone:
        parts.append(f"\n**Location:** {zone}.")
    elif region and region not in ("Hogwarts", "Unknown", ""):
        region = region.replace('_', ' ')
        parts.append(f"\n**Location:** {region}.")

    # Format nearby NPCs with bios
    nearby = context.get('nearbyNpcs', [])

    # Build nearby list - always include player first
    settings = load_settings()
    bios = settings.get('prompts', {}).get('bios', {})
    nearby_parts = []

    # Add player to nearby (they're the one you're talking to)
    if player_name and player_name != "Unknown":
        nearby_parts.append(f"- {player_name} (speaking with you)")

    if nearby:
        for char in nearby:
            npc_id = char.get('name', 'Unknown')

            # Skip the current speaker (NPC shouldn't see themselves in nearby list)
            if current_speaker and npc_id.lower() == current_speaker.lower():
                continue

            # Distance is in Unreal units (centimeters), convert to meters
            distance_cm = char.get('distance', 0)
            distance_m = round(distance_cm / 100)

            # Get display name from ID using localization
            npc_name = get_display_name(npc_id)

            # Look up bio (try ID first, then display name)
            bio = bios.get(npc_id) or bios.get(npc_name)
            if bio:
                nearby_parts.append(f"- {npc_name} (~{distance_m}m away): {bio}")
            else:
                nearby_parts.append(f"- {npc_name} (~{distance_m}m away)")

    # Add nearby characters section (includes player + any nearby NPCs)
    if nearby_parts:
        parts.append("\n\n**Nearby characters:**\n" + "\n".join(nearby_parts))

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
                vision_parts.append(f"**Scene:** {vision_ctx['scene']}")
            if vision_ctx.get('player'):
                vision_parts.append(f"**{player_name}:** {vision_ctx['player']}")
            if vision_ctx.get('atmosphere'):
                vision_parts.append(f"**Atmosphere:** {vision_ctx['atmosphere']}")
            if vision_ctx.get('characters'):
                vision_parts.append(f"**Visible:** {vision_ctx['characters']}")
            if vision_parts:
                parts.append(f"\n\n**What you can see:**\n" + "\n".join(vision_parts))
            elif vision_ctx.get('description'):
                parts.append(f"\n\n**What you can see:**\n{vision_ctx['description']}")
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

    # Join all parts with space - sections that need separation already have \n\n prefix
    return "**Current situation:**\n" + " ".join(parts)
