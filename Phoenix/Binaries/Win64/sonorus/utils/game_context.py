"""
Game context utilities for Sonorus.
Handles formatting of game context for LLM prompts.
"""

import time

from .settings import load_settings
from .landmarks import get_landmark_beacons, format_beacons_for_llm
from .localization import find_npc_id_by_name, get_display_name


def format_game_context(context, current_speaker=None, participants=None, observer_mode=False):
    """Format game context for LLM prompt

    Args:
        context: Game context dict from Lua
        current_speaker: NPC ID of the character being prompted (to exclude from nearby list)
        participants: List of participant names in the conversation (for interjections).
                      If None, defaults to just the player.
        observer_mode: If True, the player is not involved in the conversation (director mode).
                       Skips player-centric info like visibility, attire, and player header.
    """
    if not context:
        return ""

    player_name = context.get('playerName', 'Unknown')
    player_house = context.get('playerHouse', 'Unknown')
    in_stealth = context.get('inStealth', False)

    settings = load_settings()
    conv_settings = settings.get('conversation', {})

    # === RESOLVE LOCATION (needed for header + scene) ===
    zone = context.get('zoneLocation', '')
    region = context.get('location', '')
    location_name = zone if zone else (region.replace('_', ' ') if region and region not in ("Hogwarts", "Unknown", "") else '')
    location_clause = f" in {location_name}" if location_name else ""

    # === HEADER: Speaking with ===
    header_parts = []
    if observer_mode:
        # In observer mode (director prompt), NPCs are speaking to each other
        # Participants list contains the other conversation participants
        if participants:
            if len(participants) == 1:
                speaking_with = participants[0]
            elif len(participants) == 2:
                speaking_with = f"{participants[0]} and {participants[1]}"
            else:
                speaking_with = ", ".join(participants[:-1]) + f", and {participants[-1]}"
            header_parts.append(f"You are currently{location_clause}, in a conversation with {speaking_with}.")
        # No player visibility/status info in observer mode
    elif participants:
        if len(participants) == 1:
            speaking_with = participants[0]
        elif len(participants) == 2:
            speaking_with = f"{participants[0]} and {participants[1]}"
        else:
            speaking_with = ", ".join(participants[:-1]) + f", and {participants[-1]}"
        header_parts.append(f"You are currently{location_clause}, speaking with {speaking_with}.")
    elif player_name and player_name != "Unknown":
        player_desc = f"You are currently{location_clause}, speaking with {player_name}, a {player_house} student"
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
        header_parts.append(player_desc + ".")

    # Visibility status (skip in observer mode)
    if not observer_mode:
        if in_stealth:
            header_parts.append(f"{player_name} has the Disillusionment charm active (invisible/hard to see).")
        else:
            header_parts.append(f"{player_name} is visible (no Disillusionment charm).")

    # Companion status (skip in observer mode unless companion is a participant)
    if context.get('hasCompanion') and not observer_mode:
        companion_id = context.get('companionId', '')
        companion_name = get_display_name(companion_id) if companion_id else 'companion'
        companion_status = "invisible (Disillusionment charm)" if in_stealth else "visible"
        if context.get('companionIsSwimming'):
            companion_status += " and swimming"
        if context.get('companionIsOnBroom'):
            companion_status += " and flying on a broom"
        # Rephrase to second person if prompting the companion themselves
        if current_speaker and companion_id and current_speaker == companion_id:
            header_parts.append(f"You are {companion_status}, accompanying {player_name}.")
        else:
            header_parts.append(f"{companion_name} is accompanying {player_name} and is {companion_status}.")

    # === PLAYER ATTIRE (if enabled, skip in observer mode) ===
    attire_section = ""
    gear_context_enabled = conv_settings.get('gear_context', True)
    player_gear = context.get('playerGear', '')
    if player_gear and gear_context_enabled and not observer_mode:
        attire_section = f"\n\n**{player_name}'s attire:**\n{player_gear}"
        attire_section += f"\n**Note:** Don't comment on {player_name}'s attire unless directly relevant to the conversation."

    # === PLAYER FOCUS (for companions, skip in observer mode) ===
    focus_section = ""
    mission_context_enabled = conv_settings.get('mission_context', True)
    companion_id = context.get('companionId', '')
    if mission_context_enabled and current_speaker and companion_id and not observer_mode:
        if current_speaker == companion_id:
            current_quest = context.get('currentQuest', '')
            quest_objective = context.get('questObjective', '')
            if current_quest or quest_objective:
                focus_parts = []
                if current_quest:
                    focus_parts.append(f"Quest: {current_quest}")
                if quest_objective:
                    focus_parts.append(f"Their goal: {quest_objective}")
                focus_section = f"\n\n**{player_name}'s current focus:**\n" + "\n".join(focus_parts)
                focus_section += f"\n(This is just for your awareness as {player_name}'s companion. Don't push them to pursue it - they'll get to it when they're ready. You may reference it naturally if it comes up.)"

    # === DATE/TIME/LOCATION ===
    scene_parts = []
    date_formatted = context.get('dateFormatted', '')
    time_formatted = context.get('timeFormatted', '')
    time_period = context.get('timePeriod', 'Day')

    if date_formatted:
        scene_parts.append(f"**Date:** {date_formatted}")

    if time_formatted:
        time_desc = {
            'Night': 'nighttime', 'Dawn': 'early morning', 'Morning': 'morning',
            'Noon': 'midday', 'Afternoon': 'afternoon', 'Evening': 'evening'
        }.get(time_period, '')
        scene_parts.append(f"**Time:** {time_formatted}" + (f" ({time_desc})" if time_desc else ""))

    if location_name:
        scene_parts.append(f"**Your current location:** {location_name}")

    # === NEARBY CHARACTERS ===
    nearby = context.get('nearbyNpcs', [])
    editor_guidance = settings.get('prompts', {}).get('editor_guidance', {})
    nearby_parts = []

    # In observer mode, don't list player as "speaking with you"
    if player_name and player_name != "Unknown" and not observer_mode:
        nearby_parts.append(f"- {player_name} (speaking with you)")

    companion_id = context.get('companionId', '')
    for char in nearby:
        npc_id = char.get('name', 'Unknown')
        if current_speaker and npc_id.lower() == current_speaker.lower():
            continue
        distance_m = round(char.get('distance', 0) / 100)
        npc_name = get_display_name(npc_id)
        guidance = editor_guidance.get(npc_id) or editor_guidance.get(npc_name)
        is_companion = companion_id and npc_id.lower() == companion_id.lower() and player_name
        if is_companion:
            tag = f"{player_name}'s companion"
        else:
            tag = f"~{distance_m}m away"
        if guidance:
            nearby_parts.append(f"- {npc_name} ({tag}): {guidance}")
        else:
            nearby_parts.append(f"- {npc_name} ({tag})")

    # === VISION CONTEXT ===
    vision_section = ""
    try:
        from vision_agent import get_agent
        agent = get_agent()
        vision_ctx = agent.get_current_context() if agent else None
        if vision_ctx:
            age = time.time() - vision_ctx.get('timestamp', 0)
            if age > 300:
                vision_ctx = None
            elif zone or region:
                ctx_zone = vision_ctx.get('zoneLocation', '')
                ctx_region = vision_ctx.get('location', '')
                if zone and ctx_zone and zone.lower() != ctx_zone.lower():
                    vision_ctx = None
                elif not zone and region and ctx_region and region.lower() != ctx_region.lower():
                    vision_ctx = None
        if vision_ctx:
            vision_parts = []
            if vision_ctx.get('scene'):
                vision_parts.append(f"**Scene:** {vision_ctx['scene']}")
            if vision_ctx.get('notable'):
                vision_parts.append(f"**Notable details:** {vision_ctx['notable']}")
            if vision_ctx.get('player'):
                vision_parts.append(f"**{player_name}:** {vision_ctx['player']}")
            if vision_ctx.get('atmosphere'):
                vision_parts.append(f"**Atmosphere:** {vision_ctx['atmosphere']}")
            if vision_ctx.get('characters'):
                vision_parts.append(f"**Visible:** {vision_ctx['characters']}")
            if vision_parts:
                vision_section = "\n".join(vision_parts)
            elif vision_ctx.get('description'):
                vision_section = vision_ctx['description']
    except Exception:
        pass

    # === LANDMARKS ===
    landmark_section = ""
    try:
        beacons = get_landmark_beacons()
        if zone and beacons:
            zone_lower = zone.lower()
            beacons = [b for b in beacons if zone_lower not in b['name'].lower()
                      and b['name'].lower() not in zone_lower]
        beacon_str = format_beacons_for_llm(beacons)
        if beacon_str:
            landmark_section = beacon_str
    except Exception as e:
        print(f"[Context] Error getting beacons: {e}")

    # === HOUSE POINTS (if mod enabled) ===
    house_points_section = ""
    try:
        from . import mods
        hp_settings = settings.get('game_mods', {}).get('house_points', {})
        context_enabled = hp_settings.get('context_enabled', True)
        mod_installed = mods.is_mod_installed('house_points')
        print(f"[Context] House points check: context_enabled={context_enabled}, mod_installed={mod_installed}")
        if context_enabled and mod_installed:
            hp_live = mods.get_live_data('house_points')
            print(f"[Context] House points raw: {hp_live}")
            hp_points = hp_live.get('points', {})
            print(f"[Context] House points data: {bool(hp_points)} keys={list(hp_points.keys()) if hp_points else []}")
            if hp_points:
                # Determine current season from game date
                season_name = ""
                month = int(context.get('month', 0) or 0)
                if month:
                    # Spring: Feb-Apr (2-4), Summer: May-Jul (5-7)
                    # Autumn: Aug-Oct (8-10), Winter: Nov-Jan (11,12,1)
                    if month in (2, 3, 4):
                        season_name = "Spring"
                    elif month in (5, 6, 7):
                        season_name = "Summer"
                    elif month in (8, 9, 10):
                        season_name = "Autumn"
                    elif month in (11, 12, 1):
                        season_name = "Winter"

                # Build markdown table with all time periods
                table_lines = ["**House Point Standings:**"]
                if season_name:
                    table_lines.append(f"Current season: {season_name}")
                table_lines.extend([
                    "| House      | Season | Month | Week | Day |",
                    "|------------|--------|-------|------|-----|"
                ])
                for house in ["Gryffindor", "Slytherin", "Hufflepuff", "Ravenclaw"]:
                    if house in hp_points:
                        p = hp_points[house]
                        table_lines.append(
                            f"| {house:10} | {p.get('season', 0):6} | {p.get('month', 0):5} | {p.get('week', 0):4} | {p.get('day', 0):3} |"
                        )
                house_points_section = "\n".join(table_lines)
    except Exception as e:
        print(f"[Context] Error getting house points: {e}")

    # === BUILD FINAL OUTPUT ===
    output = []

    # Header (speaking with, visibility)
    output.append(" ".join(header_parts))

    # Attire and focus (inline with header area)
    if attire_section:
        output.append(attire_section)
    if focus_section:
        output.append(focus_section)

    # Scene info
    if scene_parts:
        output.append("\n\n" + "\n".join(scene_parts))

    # Nearby characters
    if nearby_parts:
        output.append("\n\n**Nearby characters:**\n" + "\n".join(nearby_parts))

    # Vision
    if vision_section:
        output.append("\n\n**What you can see:**\n" + vision_section)

    # Landmarks
    if landmark_section:
        output.append("\n\n" + landmark_section)

    # House Points (game mod)
    if house_points_section:
        output.append("\n\n" + house_points_section)

    # Commitments (only when enabled)
    if current_speaker:
        try:
            if load_settings().get('commitments', {}).get('enabled', False):
                from .commitments import build_commitment_context
                commitment_section = build_commitment_context(current_speaker, player_name=context.get('playerName'))
                if commitment_section:
                    output.append("\n\n" + commitment_section)
        except Exception as e:
            print(f"[GameContext] Error building commitment context: {e}")

    if not output:
        return ""

    return "## Current Situation\n" + "".join(output)
