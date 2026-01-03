"""
AI agent utilities for Sonorus.
Handles target selection and interjection decision-making.
"""

from .settings import load_settings

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
    for entry in recent_dialogue[-5:]:
        speaker = entry.get('speaker', 'Unknown')
        target = entry.get('target', '')
        text = entry.get('text', '')
        if target:
            dialogue_lines.append(f"{speaker} (to {target}): {text}")
        else:
            dialogue_lines.append(f"{speaker}: {text}")
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
- "NPC Name>player" = NPC speaks to {player_name} (the player)
- "NPC1 Name>NPC2 Name" = NPC1 speaks to NPC2

Output ONLY the result, nothing else. The NPC name MUST match one from the Nearby NPCs list."""

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


def run_interjection_agent(last_speaker, last_target, last_message, nearby_characters, recent_dialogue, player_name="Player"):
    """
    Run the interjection agent to determine if another NPC should speak.
    Returns: "0" (silence preferred) or "NPC Name>target"
    """
    settings = load_settings()
    conv_settings = settings.get('conversation', {})
    model = conv_settings.get('interjection_model', 'google/gemini-2.0-flash-001')

    # Format nearby NPCs (excluding last speaker)
    nearby_formatted = []
    for char in nearby_characters[:10]:
        name = char.get('name', 'Unknown')
        distance_m = round(char.get('distance', 0) / 100)  # UE units (cm) to meters
        if name.lower() != last_speaker.lower():
            nearby_formatted.append(f"- {name} ({distance_m}m away)")
    nearby_str = "\n".join(nearby_formatted) if nearby_formatted else "No other NPCs nearby."

    # Format recent dialogue (last 15 lines for better context)
    dialogue_lines = []
    for entry in recent_dialogue[-15:]:
        speaker = entry.get('speaker', 'Unknown')
        target = entry.get('target', '')
        text = entry.get('text', '')
        is_player = entry.get('isPlayer', False)
        # Mark player messages clearly
        speaker_label = f"[PLAYER] {speaker}" if is_player else speaker
        if target:
            dialogue_lines.append(f"{speaker_label} (to {target}): {text}")
        else:
            dialogue_lines.append(f"{speaker_label}: {text}")
    dialogue_str = "\n".join(dialogue_lines) if dialogue_lines else "No recent dialogue."

    prompt = f"""Select which NPC should speak next, if anyone.

## What Just Happened
{last_speaker} said to {last_target}: "{last_message}"

## Other Nearby NPCs (ONLY these NPCs can be selected)
{nearby_str}

## Recent Dialogue (for context only - speakers may no longer be nearby)
{dialogue_str}

## CRITICAL RULES
1. You may ONLY select NPCs from the "Other Nearby NPCs" list above
2. If an NPC appears in dialogue history but is NOT in the nearby list, they are NOT available
3. NEVER select {last_speaker} (they just spoke)
4. NEVER select {player_name} - they are the player character, not an NPC
5. Return "0" if no suitable NPC is in the nearby list

## Selection Criteria
Consider:
1. If the player addressed multiple NPCs (e.g., "you two", "what do you all think"), those who haven't responded should speak
2. Was this NPC directly mentioned or addressed?
3. Does their role require a response?
4. Are they emotionally affected by what was said?
5. Would they naturally interject based on personality?

If the conversation has naturally concluded or no one has a reason to speak, return "0".

Output format (EXACTLY one of these):
- "0" = No one speaks (or no valid NPC in range)
- "NPC Name>player" = NPC speaks to {player_name} (the player)
- "NPC Name>{last_speaker}" = NPC responds to the last speaker

Output ONLY the result, nothing else. The NPC name MUST match one from the Nearby NPCs list."""

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
