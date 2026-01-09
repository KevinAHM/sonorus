"""
AI agent utilities for Sonorus.
Handles target selection and interjection decision-making.
"""

from .settings import load_settings
from .dialogue import format_dialogue_entry

# Import llm module from parent directory (handles logging internally)
import llm


def run_target_selection_agent(player_input, looked_at_npc, nearby_characters, recent_dialogue, player_name="Player"):
    """
    Run the target selection agent to determine who the player is addressing.
    Returns: "0" (no target), "NPC>player", or "NPC1>NPC2"
    """
    settings = load_settings()
    conv_settings = settings.get('conversation', {})
    model = conv_settings.get('target_selection_model', 'google/gemini-2.0-flash-001')

    # Format looked-at NPC info
    if looked_at_npc:
        distance_m = round(looked_at_npc.get('distance', 0) / 100)  # UE units (cm) to meters
        crosshair_info = f"The player is looking directly at: {looked_at_npc.get('name', 'Unknown')} ({distance_m}m away)"
    else:
        crosshair_info = "The player is not looking at any specific NPC."

    # Format nearby NPCs
    nearby_formatted = []
    for char in nearby_characters[:10]:
        name = char.get('name', 'Unknown')
        distance_m = round(char.get('distance', 0) / 100)  # UE units (cm) to meters
        nearby_formatted.append(f"- {name} ({distance_m}m away)")
    nearby_str = "\n".join(nearby_formatted) if nearby_formatted else "No NPCs nearby."

    # Format recent dialogue (last 5 lines)
    dialogue_lines = []
    for i, entry in enumerate(recent_dialogue[-5:]):
        if not isinstance(entry, dict):
            print(f"[TargetAgent] ERROR: Entry {i} is {type(entry).__name__}, not dict: {repr(entry)[:200]}")
            continue
        line = format_dialogue_entry(entry, include_time=False, mark_player=True)
        if line:
            dialogue_lines.append(line)
    dialogue_str = "\n".join(dialogue_lines) if dialogue_lines else "No recent dialogue."

    prompt = f"""You are an AI decision-maker determining which NPC {player_name} (the player) is addressing.

## {player_name}'s Input
Text: "{player_input}"

## Looked-at NPC
{crosshair_info}

## Nearby NPCs (ONLY these NPCs can be selected)
{nearby_str}

## Recent Dialogue (for context only - speakers may no longer be nearby)
{dialogue_str}

## CRITICAL RULES
1. You may ONLY select NPCs from the "Nearby NPCs" list above
2. If an NPC appears in dialogue history but is NOT in the nearby list, they are NOT available
3. If ANY NPC is nearby, prefer selecting one over returning "0"

## Instructions
Analyze who {player_name} is addressing:
1. Direct speech to specific NPC (by name, role, or context)
2. {player_name} prompting NPC-to-NPC dialogue ("Ask her about...", "Tell him...")
3. General statement - pick who would most likely respond from NEARBY NPCs
4. Group address ("you two", "which of you") - pick who would have the strongest emotional reaction

IMPORTANT - Conversation Continuity:
- If {player_name} was JUST talking to an NPC (in recent dialogue) and that NPC is still nearby, general statements continue that conversation
- If only ONE NPC is nearby and {player_name} speaks, they are obviously addressing that NPC
- ONLY return "0" if there are truly no NPCs nearby, or the player explicitly asks for someone who isn't present

Consider:
- Active conversation partner (from recent dialogue) is the DEFAULT target for general statements
- Looked-at NPC is a STRONG signal for who {player_name} is addressing
- Distance matters - closer NPCs more likely targets
- Names or roles mentioned in speech

Output format (EXACTLY one of these):
- "0" = No NPCs nearby, or player explicitly requested someone not present
- "NpcId>player" = NPC speaks to {player_name} (the player)
- "NpcId>NpcId" = First NPC speaks to second NPC

Output ONLY the result, nothing else. The NPC ID MUST exactly match one from the Nearby NPCs list (e.g., "SebastianSallow", not "Sebastian Sallow")."""

    messages = [{"role": "user", "content": prompt}]

    # Debug logging
    print(f"[TargetAgent] Nearby NPCs: {[c.get('name') for c in nearby_characters[:5]]}")
    print(f"[TargetAgent] Looked-at: {looked_at_npc.get('name') if looked_at_npc else 'None'}")

    # llm.chat() handles logging internally
    try:
        result = llm.chat(messages, model=model, temperature=0.3, max_tokens=50, context="target_selection")
        if result:
            # Clean up the result
            result = result.strip().strip('"').strip("'")
            print(f"[TargetAgent] Result: {result}")
            return result
    except Exception as e:
        print(f"[TargetAgent] Error: {e}")

    return "0"


def run_interjection_agent(last_speaker_id, last_speaker_name, last_target_name, last_message, nearby_characters, recent_dialogue, player_name="Player"):
    """
    Run the interjection agent to determine if another NPC should speak.

    Args:
        last_speaker_id: Internal ID of who just spoke (e.g., "SebastianSallow")
        last_speaker_name: Display name of who just spoke (e.g., "Sebastian Sallow")
        last_target_name: Display name of who they spoke to (e.g., "Adri Valter")
        last_message: What they said
        nearby_characters: List of nearby NPCs with 'name' field (ID format)
        recent_dialogue: Recent dialogue history
        player_name: Player's display name

    Returns:
        "0" (no one speaks) or "NpcId>target" (e.g., "NellieOggspire>player")
    """
    settings = load_settings()
    conv_settings = settings.get('conversation', {})
    model = conv_settings.get('interjection_model', 'google/gemini-2.0-flash-001')

    # Format nearby NPCs (excluding last speaker)
    other_npcs = []
    for char in nearby_characters[:10]:
        name = char.get('name', 'Unknown')
        distance_m = round(char.get('distance', 0) / 100)  # UE units (cm) to meters
        if name.lower() != last_speaker_id.lower():
            other_npcs.append(f"- {name} ({distance_m}m away)")

    # Short-circuit: if no other NPCs can speak, don't bother calling LLM
    if not other_npcs:
        print("[InterjectionAgent] No other NPCs nearby - returning 0")
        return "0"

    nearby_str = "\n".join(other_npcs)

    # Format recent dialogue (last 15 lines for better context)
    dialogue_lines = []
    for i, entry in enumerate(recent_dialogue[-15:]):
        if not isinstance(entry, dict):
            print(f"[InterjectionAgent] ERROR: Entry {i} is {type(entry).__name__}, not dict: {repr(entry)[:200]}")
            print(f"[InterjectionAgent] Full recent_dialogue types: {[type(e).__name__ for e in recent_dialogue[-15:]]}")
            continue
        line = format_dialogue_entry(entry, include_time=False, mark_player=True)
        if line:
            dialogue_lines.append(line)
    dialogue_str = "\n".join(dialogue_lines) if dialogue_lines else "No recent dialogue."

    prompt = f"""Select which nearby NPC should speak next, if anyone.

## What Just Happened
{last_speaker_name} said to {last_target_name}: "{last_message}"

## Nearby NPCs Who Can Speak
{nearby_str}

## Recent Dialogue (for context)
{dialogue_str}

## Rules
- ONLY select from the NPCs listed above
- Return "0" if no one has a reason to speak

## When Should Someone Speak?
- They were directly addressed or mentioned
- They have a strong emotional reaction to what was said
- Their personality would naturally lead them to interject

Output EXACTLY one of:
- "0" = No one speaks
- "NpcId>player" = NPC speaks to the player ({player_name})
- "NpcId>NpcId" = NPC speaks to another NPC

Example: "SebastianSallow>player" or "DuncanHobhouse>NellieOggspire"

Output ONLY the result, nothing else. The NPC ID MUST exactly match one from the Nearby NPCs list (e.g., "SebastianSallow", not "Sebastian Sallow")."""

    messages = [{"role": "user", "content": prompt}]

    # llm.chat() handles logging internally
    try:
        result = llm.chat(messages, model=model, temperature=0.3, max_tokens=50, context="interjection")
        if result:
            # Clean up the result
            result = result.strip().strip('"').strip("'")
            print(f"[InterjectionAgent] Result: {result}")
            return result
    except Exception as e:
        print(f"[InterjectionAgent] Error: {e}")

    return "0"
