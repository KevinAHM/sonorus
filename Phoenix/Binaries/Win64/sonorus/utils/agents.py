"""
AI agent utilities for Sonorus.
Handles target selection, interjection decision-making, and input correction.
"""

import re
from .settings import load_settings, is_dev_mode, DEFAULT_SETTINGS
from .dialogue import format_dialogue_entry
from .localization import get_display_name, find_npc_id_by_name
from .mods import is_professor

# Role annotations for target selection (helps less capable models match "professor", "shopkeeper", etc.)
NPC_ROLES = {
    # School staff
    "PhineasBlack": "Headmaster",
    "MatildaWeasley": "Deputy Headmistress, Professor",
    "CuthbertBinns": "Professor, Ghost",
    # Hogsmeade shopkeepers
    "Sirona": "Bartender, Innkeeper",
    "GerboldOllivander": "Wandmaker, Shopkeeper",
    "AlbieWeekes": "Shopkeeper",
    "AugustusHill": "Shopkeeper",
    "ParryPippin": "Shopkeeper, Potioneer",
    "TimothyTeasdale": "Shopkeeper",
    "ThomasBrown": "Shopkeeper",
    "CalliopSnelling": "Shopkeeper",
    # Three Broomsticks
    "SironaRyan": "Bartender, Innkeeper",
}


from .profiler import Profiler

# Import llm module from parent directory (handles logging internally)
import llm

# Get shared profiler instance
_profiler = Profiler.get("chat_flow")


def _parse_scene_continuation(result, nearby_characters, player_name, last_speaker_name=None, label="SceneCont"):
    """
    Parse scene continuation result. Supports two formats:
    New: 3-line (Speaker: dialogue / Replying to: Name / How I know: ...)
    Old: Single line (Speaker (to Target): dialogue)
    Returns "0" or "SpeakerId>TargetId"
    """
    result = result.strip()
    lines = [l.strip() for l in result.split('\n') if l.strip()]

    if not lines:
        print(f"[{label}] Empty result — returning 0")
        return "0"

    first_line = lines[0]
    print(f"[{label}] Raw: {first_line}")

    # Narrator = no one speaks
    if first_line.lower().startswith("narrator"):
        print(f"[{label}] Narrator — scene pauses")
        return "0"

    # Try old format: "Speaker (to Target): dialogue"
    old_match = re.match(r'^(.+?)\s*\(to\s+(.+?)\)\s*:', first_line)
    if old_match:
        speaker_display = old_match.group(1).strip()
        target_display = old_match.group(2).strip()
    else:
        # New format: "Speaker: dialogue" with target on line 2
        new_match = re.match(r'^(.+?):\s', first_line)
        if not new_match:
            print(f"[{label}] Could not parse speaker — returning 0")
            return "0"
        speaker_display = new_match.group(1).strip()
        target_display = None

        # Look for "Replying to: Name" in subsequent lines
        for line in lines[1:]:
            target_match = re.match(r'^Replying to:\s*(.+)', line, re.IGNORECASE)
            if target_match:
                target_display = target_match.group(1).strip().rstrip('.')
                break

    # Player speaking = their turn
    if speaker_display.lower() == player_name.lower():
        print(f"[{label}] Player ({player_name}) — returning 0")
        return "0"

    # Same speaker as last = no interjection
    if last_speaker_name and speaker_display.lower() == last_speaker_name.lower():
        print(f"[{label}] Same speaker ({last_speaker_name}) — returning 0")
        return "0"

    # Resolve speaker
    speaker_id = find_npc_id_by_name(speaker_display, nearby_characters)

    # Resolve target
    target_id = "player"  # default
    if target_display:
        if target_display.lower() == "nobody":
            target_id = "player"
        elif target_display.lower() == player_name.lower():
            target_id = "player"
        else:
            target_id = find_npc_id_by_name(target_display, nearby_characters)

    decision = f"{speaker_id}>{target_id}"
    print(f"[{label}] {decision}")
    return decision


def _format_nearby_display(nearby_characters, player_name=None, prompt_mode=False, prompt_participants=None, companion_id=None):
    """Format nearby NPCs with display names for scene continuation prompts.
    Returns (lines, names) - formatted bullet list and list of display names."""
    lines = []
    names = []

    # Add player first if provided
    if player_name:
        lines.append(f"- {player_name}")
        names.append(player_name)

    for char in nearby_characters[:10]:
        name = char.get('name', 'Unknown')
        distance_m = round(char.get('distance', 0) / 100)
        if prompt_mode and prompt_participants:
            if name.lower() not in [p.lower() for p in prompt_participants]:
                continue
        display = get_display_name(name)
        role_label = NPC_ROLES.get(name) or ("Professor" if is_professor(name) else "")
        role = f" ({role_label})" if role_label else ""
        is_companion = companion_id and name.lower() == companion_id.lower() and player_name
        if is_companion:
            lines.append(f"- {display}{role} ({player_name}'s companion)")
        else:
            lines.append(f"- {display}{role} ({distance_m}m away)")
        if display not in names:
            names.append(display)

    return lines, names


def _format_dialogue_as_story(recent_dialogue, num_lines=15):
    """Format recent dialogue as a clean script (no targets, no timestamps, no [PLAYER] markers)."""
    dialogue_lines = []
    for i, entry in enumerate(recent_dialogue[-num_lines:]):
        if not isinstance(entry, dict):
            continue
        line = format_dialogue_entry(entry, include_time=False, mark_player=False)
        if line:
            # Strip "(to Target)" between speaker name and colon
            line = re.sub(r'^(.+?)\s*\(to [^)]+\)(:\s)', r'\1\2', line)
            dialogue_lines.append(line)
    return dialogue_lines


def run_target_selection_agent(player_input, looked_at_npc, nearby_characters, recent_dialogue, player_name="Player", current_location="Unknown Location", companion_id=None):
    """
    Run the target selection agent to determine who the player is addressing.
    Uses scene continuation: appends the player's line and sees who the model thinks responds.
    Returns: "0" (no target), "NPC>player", or "NPC1>NPC2"
    """
    settings = load_settings()
    conv_settings = settings.get('conversation', {})
    model = conv_settings.get('target_selection_model', 'meta-llama/llama-4-scout:nitro')
    max_tokens = conv_settings.get('speaker_selection_max_tokens', 512)

    # Format nearby NPCs with display names (includes player)
    nearby_lines, char_names = _format_nearby_display(nearby_characters, player_name=player_name, companion_id=companion_id)
    nearby_str = "\n".join(nearby_lines) if nearby_lines else "No characters present."
    character_names_pipe = "|".join(char_names)

    # Format recent dialogue as story + append player's new line
    dialogue_lines = _format_dialogue_as_story(recent_dialogue, num_lines=10)
    dialogue_lines.append(f"{player_name}: {player_input}")
    dialogue_str = "\n".join(dialogue_lines) if dialogue_lines else "No recent dialogue."

    # Extra context: gaze direction
    extra_context = ""
    if looked_at_npc:
        looked_at_display = get_display_name(looked_at_npc.get('name', 'Unknown'))
        extra_context = f"\n{player_name} is looking at {looked_at_display}.\n"

    # Debug logging
    print(f"[TargetAgent] Nearby NPCs: {[c.get('name') for c in nearby_characters[:5]]}")
    print(f"[TargetAgent] Looked-at: {looked_at_npc.get('name') if looked_at_npc else 'None'}")

    # Load prompt template
    prompts = settings.get('prompts', {})
    prompt_template = prompts.get('scene_continuation') or DEFAULT_SETTINGS['prompts']['scene_continuation']
    prompt = prompt_template.format(
        nearby_str=nearby_str,
        dialogue_str=dialogue_str,
        extra_context=extra_context,
        character_names_pipe=character_names_pipe,
        player=player_name,
        player_name=player_name
    )

    messages = [{"role": "user", "content": prompt}]

    try:
        result = llm.chat(messages, model=model, temperature=0.3, max_tokens=max_tokens, context="target_selection")
        if result:
            decision = _parse_scene_continuation(result, nearby_characters, player_name, label="TargetAgent")

            # For target selection, if we got "0" but there are NPCs, fall back to closest
            if decision == "0" and nearby_characters:
                closest = nearby_characters[0].get('name', 'Unknown')
                fallback = f"{closest}>player"
                print(f"[TargetAgent] Fallback to closest: {fallback}")
                return fallback

            return decision
    except Exception as e:
        print(f"[TargetAgent] Error: {e}")

    # Ultimate fallback: closest NPC
    if nearby_characters:
        closest = nearby_characters[0].get('name', 'Unknown')
        return f"{closest}>player"
    return "0"


def run_move_classifier(player_input, speaker_name, target_name, model=None):
    """
    Classify whether the player is commanding a companion to physically move nearby.
    Runs AFTER target selection, only when keywords triggered and target is companion.
    Returns True if it's a local move command, False otherwise.
    """
    if not model:
        settings = load_settings()
        model = settings.get('conversation', {}).get('target_selection_model', 'meta-llama/llama-4-scout:nitro')

    prompt = f"""Is "{speaker_name}" giving "{target_name}" a direct command to physically reposition to a specific nearby spot (within a few meters)?

"{player_input}"

Answer YES only if ALL of these are true:
- It references a specific nearby physical destination (over there, right here, next to me, behind that pillar, out of the way, etc.)
- It's a direct repositioning command, NOT a conversational phrase like "let's go", "come on", "let's get going", "shall we", "let's head out"
- It is NOT about traveling to a named location (Hogsmeade, class, the library, home, etc.)
- It is NOT a suggestion to go somewhere, leave, or depart together

Answer NO for general movement phrases, departing together, or anything without a specific nearby destination.

Reply with exactly one word: YES or NO"""

    messages = [{"role": "user", "content": prompt}]

    try:
        result = llm.chat(messages, model=model, temperature=0, max_tokens=4, context="move_classifier")
        if result:
            answer = result.strip().upper()
            is_move = answer.startswith("YES")
            print(f"[MoveClassifier] '{player_input[:50]}' → {answer} (move={is_move})")
            return is_move
    except Exception as e:
        print(f"[MoveClassifier] Error: {e}")

    return False


def run_interjection_agent(last_speaker_id, last_speaker_name, last_target_name, last_message,
                           nearby_characters, recent_dialogue, player_name="Player",
                           prompt_mode=False, prompt_participants=None, include_player=True,
                           companion_id=None):
    """
    Run the interjection agent to determine if another NPC should speak.
    Uses scene continuation: model writes whoever would naturally speak next.
    Returns: "0" (no one speaks) or "NpcId>target"
    """
    settings = load_settings()
    conv_settings = settings.get('conversation', {})
    model = conv_settings.get('interjection_model', 'x-ai/grok-4.1-fast')
    max_tokens = conv_settings.get('speaker_selection_max_tokens', 512)

    # In prompt mode with exactly 2 NPC participants (no player), just alternate between them
    if prompt_mode and prompt_participants and len(prompt_participants) == 2 and not include_player:
        other = prompt_participants[0] if prompt_participants[1].lower() == last_speaker_id.lower() else prompt_participants[1]
        print(f"[InterjectionAgent] Prompt mode 2-person: {other}>{last_speaker_id}")
        return f"{other}>{last_speaker_id}"

    # Format nearby NPCs with display names (includes player)
    nearby_lines, char_names = _format_nearby_display(
        nearby_characters, player_name=player_name,
        prompt_mode=prompt_mode, prompt_participants=prompt_participants,
        companion_id=companion_id
    )

    if not nearby_lines:
        print("[InterjectionAgent] No characters present - returning 0")
        return "0"

    nearby_str = "\n".join(nearby_lines)
    character_names_pipe = "|".join(char_names)

    # Format recent dialogue as story
    dialogue_lines = _format_dialogue_as_story(recent_dialogue, num_lines=15)
    dialogue_str = "\n".join(dialogue_lines) if dialogue_lines else "No recent dialogue."

    # Load prompt template
    prompts = settings.get('prompts', {})

    # Build prompt - different wording for prompt mode
    if prompt_mode:
        player_option = f'\n- "NpcId>player" = NPC speaks to {player_name}' if include_player else ""
        prompt_template = prompts.get('interjection_prompt_mode') or DEFAULT_SETTINGS['prompts']['interjection_prompt_mode']
        prompt = prompt_template.format(
            last_speaker_name=last_speaker_name,
            last_target_name=last_target_name,
            last_message=last_message,
            nearby_str=nearby_str,
            dialogue_str=dialogue_str,
            player_option=player_option,
            player_name=player_name
        )
    else:
        prompt_template = prompts.get('scene_continuation') or DEFAULT_SETTINGS['prompts']['scene_continuation']
        prompt = prompt_template.format(
            nearby_str=nearby_str,
            dialogue_str=dialogue_str,
            extra_context="",
            character_names_pipe=character_names_pipe,
            player=player_name,
            player_name=player_name
        )

    messages = [{"role": "user", "content": prompt}]

    try:
        result = llm.chat(messages, model=model, temperature=0.3, max_tokens=max_tokens, context="interjection")
        if result:
            decision = _parse_scene_continuation(
                result, nearby_characters, player_name,
                last_speaker_name=last_speaker_name, label="InterjectionAgent"
            )

            # In prompt mode without player, treat "player" target as end of conversation
            if prompt_mode and not include_player:
                if ">player" in decision.lower():
                    print("[InterjectionAgent] Player selected in prompt mode without player - ending")
                    return "0"

            return decision
    except Exception as e:
        print(f"[InterjectionAgent] Error: {e}")

    return "0"


def run_input_correction_agent(player_input):
    """
    Run the input correction agent to fix grammar and spelling in player input.
    Uses a small, fast model to clean up typos without changing meaning.
    """
    settings = load_settings()
    conv_settings = settings.get('conversation', {})

    enabled = conv_settings.get('input_correction_enabled', False)
    if not enabled:
        return player_input

    model = conv_settings.get('input_correction_model', 'gemini-2.5-flash-lite')
    print(f"[InputCorrection] Calling LLM with model={model}, input='{player_input[:50]}...'")

    if len(player_input.strip()) < 3:
        print(f"[InputCorrection] Skipping - input too short ({len(player_input.strip())} chars)")
        return player_input

    _PRESERVE_NAMES = {
        r'\bominis\b': 'Retain the spelling "Ominis" — it is a character name (proper noun) in the Hogwarts universe, NOT the word "ominous".',
        r'\bfig\b': 'Retain the spelling "Fig" — it is a professor\'s surname (Eleazar Fig) in the Hogwarts universe, NOT the fruit.',
    }
    preserve_notes = []
    for pattern, note in _PRESERVE_NAMES.items():
        if re.search(pattern, player_input, re.IGNORECASE):
            preserve_notes.append(note)
    preserve_section = ""
    if preserve_notes:
        preserve_section = "\n## Important — Proper Nouns\n" + "\n".join(f"- {n}" for n in preserve_notes) + "\n"

    prompt = f"""Clean up this text for a Harry Potter game chat.

## Rules
1. Capitalize first letter of sentences
2. Add period/question mark/exclamation at end if missing
3. Fix obvious typos (teh→the, adn→and, etc.)
4. Expand: u→you, ur→your, pls→please, thx→thanks, bc→because, w/→with, rn→right now
5. KEEP interjections exactly: mm, hmm, uh, ah, oh, hm, mhm, yeah, yep, nope, ok, okay
6. KEEP contractions: don't, won't, I'm, etc.
7. KEEP the text in its original language
8. Do NOT rephrase, add, or remove words beyond the above — never change the meaning

{preserve_section}## Output Format
You MUST output ONLY the cleaned text wrapped in XML tags like this:
<clean>Your cleaned text here.</clean>

Do NOT include any other text, explanation, or preamble. ONLY the XML tags with the cleaned text inside.

## Examples
Input: "mm i am hungry"
Output: <clean>Mm, I am hungry.</clean>

Input: "u mean ur wand isnt a broomstick"
Output: <clean>You mean your wand isn't a broomstick?</clean>

Input: "yeah thats what i thougth"
Output: <clean>Yeah, that's what I thought.</clean>

Input: "can u teach me stupefy pls"
Output: <clean>Can you teach me Stupefy please?</clean>

Input: "why always me"
Output: <clean>Why always me?</clean>

Input: "hmm ok i guess"
Output: <clean>Hmm, ok I guess.</clean>

## Input
"{player_input}"

## Output"""

    messages = [{"role": "user", "content": prompt}]

    _profiler.mark("input_correction start")
    try:
        result = llm.chat(messages, model=model, temperature=0, max_tokens=1024, context="input_correction")
        _profiler.mark("input_correction done")

        if result:
            raw = result.strip()

            clean_match = re.search(r'<clean>(.*?)</clean>', raw, re.DOTALL | re.IGNORECASE)
            if clean_match:
                corrected = clean_match.group(1)
            else:
                any_tag_match = re.search(r'<(\w+)>(.*?)</\1>', raw, re.DOTALL | re.IGNORECASE)
                if any_tag_match:
                    corrected = any_tag_match.group(2)
                elif '<' in raw and '>' in raw:
                    start = raw.find('>') + 1
                    end = raw.rfind('<')
                    if start < end:
                        corrected = raw[start:end]
                    else:
                        corrected = raw
                else:
                    corrected = raw

            corrected = corrected.strip().strip('"').strip("'")
            print(f"[InputCorrection] '{player_input}' -> '{corrected}'")

            orig_len = len(player_input)
            new_len = len(corrected)
            if new_len > orig_len * 1.5 + 10:
                print(f"[InputCorrection] Too long ({new_len} vs {orig_len}), keeping original")
                return player_input
            if new_len < orig_len * 0.5 and orig_len > 10:
                print(f"[InputCorrection] Too short ({new_len} vs {orig_len}), keeping original")
                return player_input

            return corrected
    except Exception as e:
        _profiler.mark("input_correction done")
        print(f"[InputCorrection] Error: {e}")

    return player_input


def run_prompt_parser_agent(prompt_text, nearby_characters, player_name="Player"):
    """
    Parse a director prompt to extract participants and scenario for NPC-to-NPC conversations.
    """
    settings = load_settings()
    conv_settings = settings.get('conversation', {})
    model = conv_settings.get('interjection_model', 'x-ai/grok-4.1-fast')
    max_tokens = 8192  # Prompt parser needs full JSON response

    nearby_formatted = []
    for char in nearby_characters[:15]:
        name = char.get('name', 'Unknown')
        distance_m = round(char.get('distance', 0) / 100)
        nearby_formatted.append(f"- {name} ({distance_m}m)")
    nearby_str = "\n".join(nearby_formatted) if nearby_formatted else "No NPCs nearby."

    prompt = f"""Parse this director prompt for an NPC conversation scene.

## Director's Prompt
"{prompt_text}"

## Available NPCs (ONLY these can be selected)
{nearby_str}

## Player Name
{player_name}

## Task
Extract:
1. **Participants**: Which NPCs should be in this conversation? Match names from the nearby list ONLY.
   - Handle nicknames/variants (e.g., "Sebastian" -> "SebastianSallow", "Ominis" -> "OminisSGaunt")
   - If a name isn't in the nearby list, skip it — do NOT substitute a different NPC
   - If no names match, return an empty participants list: []
   - NEVER add NPCs that weren't mentioned or implied in the prompt

2. **Include {player_name}**: Is {player_name} mentioned or implied as a participant?
   - Look for: "{player_name}", "me", "I", "us", "we", or similar first-person references
   - If {player_name} is included, they participate in the conversation

3. **Scenario**: What should they discuss/do? Extract the topic or situation.
   - Keep it brief (1-2 sentences max)
   - If no specific topic, use "have a casual conversation"

## Output Format (JSON only)
{{"participants": ["NpcId1", "NpcId2"], "includes_player_character": false, "scenario": "discuss topic"}}

IMPORTANT: NPC IDs must EXACTLY match names from the Available NPCs list. Output ONLY valid JSON, nothing else."""

    messages = [{"role": "user", "content": prompt}]

    print(f"[PromptParser] Parsing: '{prompt_text[:50]}...'")
    print(f"[PromptParser] Nearby NPCs: {[c.get('name') for c in nearby_characters[:5]]}")

    try:
        result = llm.chat(messages, model=model, temperature=0.1, max_tokens=max_tokens, context="prompt_parser")
        if not result:
            print(f"[PromptParser] LLM returned empty/None (model={model})")
            return None

        print(f"[PromptParser] Raw LLM response ({len(result)} chars): {result[:300]}")
        result = result.strip()
        if result.startswith("```"):
            result = result.split("```")[1]
            if result.startswith("json"):
                result = result[4:]
        result = result.strip()

        import json
        try:
            parsed = json.loads(result)
        except json.JSONDecodeError as e:
            # Try to extract JSON object from within prose text
            import re
            match = re.search(r'\{[^{}]*"participants"[^{}]*\}', result)
            if match:
                print(f"[PromptParser] JSON not at top level, extracted from prose: {match.group()[:200]}")
                try:
                    parsed = json.loads(match.group())
                except json.JSONDecodeError:
                    print(f"[PromptParser] Extracted JSON also invalid")
                    print(f"[PromptParser] Cleaned text: {result[:300]}")
                    return None
            else:
                print(f"[PromptParser] JSON parse error: {e}")
                print(f"[PromptParser] Cleaned text: {result[:300]}")
                return None

        participants = parsed.get('participants', [])
        include_player = parsed.get('includes_player_character', parsed.get('include_player', False))
        scenario = parsed.get('scenario', 'have a conversation')

        print(f"[PromptParser] Parsed JSON - participants: {participants}, include_player: {include_player}, scenario: {scenario!r}")

        nearby_names = {c.get('name', '').lower(): c.get('name') for c in nearby_characters}
        print(f"[PromptParser] Nearby name lookup: {nearby_names}")

        validated_participants = []
        for p in participants:
            p_lower = p.lower()
            if p_lower in nearby_names:
                validated_participants.append(nearby_names[p_lower])
                print(f"[PromptParser]   '{p}' -> exact match: {nearby_names[p_lower]}")
            else:
                matched = False
                for nearby_lower, nearby_name in nearby_names.items():
                    if p_lower in nearby_lower or nearby_lower in p_lower:
                        validated_participants.append(nearby_name)
                        print(f"[PromptParser]   '{p}' -> fuzzy match: {nearby_name}")
                        matched = True
                        break
                if not matched:
                    print(f"[PromptParser]   '{p}' -> NO MATCH in nearby list")

        if not validated_participants:
            print(f"[PromptParser] No valid participants after validation (raw: {participants})")
            return None

        print(f"[PromptParser] Final: {validated_participants}, include_player={include_player}")
        return {
            'participants': validated_participants,
            'include_player': include_player,
            'scenario': scenario
        }

    except Exception as e:
        print(f"[PromptParser] Error: {e}")

    return None
