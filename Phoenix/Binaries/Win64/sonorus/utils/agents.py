"""
AI agent utilities for Sonorus.
Handles target selection, interjection decision-making, and input correction.
"""

import re
import threading
from .settings import load_settings, is_dev_mode, DEFAULT_SETTINGS, is_llm_provider_feature_disabled
from .dialogue import format_dialogue_entry
from .localization import get_display_name, find_npc_id_by_name
from .mods import is_professor
from .narration import parse_segments
from .text_utils import remove_brackets

INPUT_CORRECTION_REFUSAL_TEXT = "I can't help with that request."

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
    "ThaddeusTravers": "Shopkeeper",
    "VENDORCauldronShop": "Cauldron Shop Vendor",
    "VENDORJokeShop": "Joke Shop Vendor",
    "VENDORMusicShop": "Music Shop Vendor",
    "VENDORQuillShop": "Quill Shop Vendor",
    "VENDORSecondHandShop1": "Second Hand Shop Vendor",
    "VENDORTeaShop": "Tea Shop Vendor",
}


from .profiler import Profiler

# Import llm module from parent directory (handles logging internally)
import llm

# Get shared profiler instance
_profiler = Profiler.get("chat_flow")


def _resolve_decision(speaker_display, target_display, nearby_characters, player_name, last_speaker_name=None, label="SceneCont"):
    """Resolve parsed speaker/target display names into a decision string."""
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

    return _resolve_decision(speaker_display, target_display, nearby_characters, player_name, last_speaker_name, label)


def _drain_stream_async(stream):
    """Continue consuming a stream in the background so final usage metadata can arrive."""
    def _worker():
        try:
            for _ in stream:
                pass
        except Exception:
            pass

    threading.Thread(target=_worker, daemon=True).start()


def _parse_scene_continuation_stream(stream, nearby_characters, player_name, last_speaker_name=None, label="SceneCont"):
    """
    Parse scene continuation from a streaming LLM response.
    Returns as soon as speaker + 'Replying to' are parsed, without waiting for 'How I know'.
    Falls back to full-text parse if streaming yields no newlines.
    Returns "0" or "SpeakerId>TargetId"
    """
    accumulated = ""
    lines_found = []  # completed lines (after \n)
    speaker_display = None
    target_display = None

    for chunk in stream:
        accumulated += chunk

        # Check for completed lines
        while '\n' in accumulated:
            line, accumulated = accumulated.split('\n', 1)
            line = line.strip()
            if not line:
                continue
            lines_found.append(line)

            # Line 1: parse speaker
            if len(lines_found) == 1:
                first_line = lines_found[0]
                print(f"[{label}] Raw: {first_line}")

                if first_line.lower().startswith("narrator"):
                    print(f"[{label}] Narrator — scene pauses")
                    _drain_stream_async(stream)
                    return "0"

                # Old format: "Speaker (to Target): dialogue"
                old_match = re.match(r'^(.+?)\s*\(to\s+(.+?)\)\s*:', first_line)
                if old_match:
                    speaker_display = old_match.group(1).strip()
                    target_display = old_match.group(2).strip()
                    # Have both already — return immediately
                    _drain_stream_async(stream)
                    return _resolve_decision(speaker_display, target_display, nearby_characters, player_name, last_speaker_name, label)

                # New format: "Speaker: dialogue"
                new_match = re.match(r'^(.+?):\s', first_line)
                if new_match:
                    speaker_display = new_match.group(1).strip()
                else:
                    print(f"[{label}] Could not parse speaker — returning 0")
                    _drain_stream_async(stream)
                    return "0"

            # Line 2+: look for "Replying to:"
            elif speaker_display and not target_display:
                target_match = re.match(r'^Replying to:\s*(.+)', line, re.IGNORECASE)
                if target_match:
                    target_display = target_match.group(1).strip().rstrip('.')
                    # Got both — return immediately, don't wait for "How I know"
                    _drain_stream_async(stream)
                    return _resolve_decision(speaker_display, target_display, nearby_characters, player_name, last_speaker_name, label)

    # Stream ended — handle any remaining text
    remaining = accumulated.strip()
    if remaining:
        lines_found.append(remaining)

    # If we got a speaker but no explicit target from stream, try parsing all lines
    if lines_found and not speaker_display:
        return _parse_scene_continuation('\n'.join(lines_found), nearby_characters, player_name, last_speaker_name, label)

    # Got speaker from stream but never saw "Replying to:" — check remaining lines
    if speaker_display and not target_display:
        for line in lines_found[1:]:
            target_match = re.match(r'^Replying to:\s*(.+)', line, re.IGNORECASE)
            if target_match:
                target_display = target_match.group(1).strip().rstrip('.')
                break

    if speaker_display:
        return _resolve_decision(speaker_display, target_display, nearby_characters, player_name, last_speaker_name, label)

    print(f"[{label}] Empty stream result — returning 0")
    return "0"


def _format_nearby_display(nearby_characters, player_name=None, prompt_mode=False, prompt_participants=None, companion_id=None, follower_ids=None):
    """Format nearby NPCs with display names for scene continuation prompts.
    Returns (lines, names) - formatted bullet list and list of display names."""
    lines = []
    names = []
    follower_set = {f.lower() for f in (follower_ids or [])}

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
        is_follower = name.lower() in follower_set and player_name
        if is_companion or is_follower:
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


def _parse_event_commentary_output(result, eligible_speakers, player_name):
    """Parse structured event commentary selector output."""
    parsed = {
        "worth_commenting": "no",
        "speaker_id": None,
        "target_id": None,
        "topic": None,
        "timing": "none",
        "scene": "",
        "relevance": "",
        "why": "",
        "raw": result.strip(),
    }

    if not result:
        return parsed

    speaker_lookup = {}
    for speaker in eligible_speakers or []:
        display_name = speaker.get("display_name") or get_display_name(speaker.get("id", ""))
        if display_name:
            speaker_lookup[display_name.lower()] = speaker.get("id")

    for raw_line in result.splitlines():
        line = raw_line.strip()
        if not line or ":" not in line:
            continue
        label, value = line.split(":", 1)
        key = label.strip().lower()
        value = value.strip()

        if key == "scene":
            parsed["scene"] = value
        elif key == "special or unusual relevance":
            parsed["relevance"] = value
        elif key == "worth commenting":
            parsed["worth_commenting"] = value.lower().strip(".")
        elif key == "why":
            parsed["why"] = value
        elif key == "who speaks":
            cleaned = value.rstrip(".")
            if cleaned.lower() != "none":
                parsed["speaker_id"] = speaker_lookup.get(cleaned.lower())
        elif key == "directed to":
            cleaned = value.rstrip(".")
            if cleaned.lower() == player_name.lower():
                parsed["target_id"] = "player"
        elif key == "topic":
            parsed["topic"] = None if value.lower() == "none" else value
        elif key == "timing":
            parsed["timing"] = value.lower().strip(".")

    if parsed["worth_commenting"] != "yes":
        parsed["worth_commenting"] = "no"
        parsed["speaker_id"] = None
        parsed["target_id"] = None
        parsed["topic"] = None
        parsed["timing"] = "none"
        return parsed

    if not parsed["speaker_id"] or parsed["target_id"] != "player":
        parsed["worth_commenting"] = "no"
        parsed["speaker_id"] = None
        parsed["target_id"] = None
        parsed["topic"] = None
        parsed["timing"] = "none"

    return parsed


def run_event_commentary_agent(eligible_speakers, player_name, current_location, time_of_day,
                               time_since_last_comment, primary_event, recent_events, recent_dialogue,
                               frequency_label="default", notable_locations=None):
    """Decide if an eligible speaker should make an unsolicited comment."""
    settings = load_settings()
    conv_settings = settings.get('conversation', {})
    model = conv_settings.get('commentary_model') or conv_settings.get('interjection_model', 'google/gemini-3.1-flash-lite')
    max_tokens = conv_settings.get('commentary_max_tokens', 8192)

    if not eligible_speakers:
        return {
            "worth_commenting": "no",
            "speaker_id": None,
            "target_id": None,
            "topic": None,
            "timing": "none",
            "scene": "",
            "relevance": "",
            "why": "No eligible speakers.",
            "raw": "",
        }

    prompts = settings.get('prompts', {})
    prompt_template = prompts.get('event_commentary_selector') or DEFAULT_SETTINGS['prompts']['event_commentary_selector']

    eligible_lines = []
    for speaker in eligible_speakers:
        display_name = speaker.get("display_name") or get_display_name(speaker.get("id", ""))
        if display_name:
            eligible_lines.append(f"- {display_name}")

    event_lines = [f"- {event}" for event in (recent_events or [])]
    dialogue_lines = _format_dialogue_as_story(recent_dialogue, num_lines=4)
    notable_lines = [f"- {location}" for location in (notable_locations or [])]

    prompt = prompt_template.format(
        player_name=player_name,
        current_location=current_location or "Unknown",
        time_of_day=time_of_day or "Unknown",
        time_since_last_comment=time_since_last_comment or "never",
        frequency_label=frequency_label,
        primary_event=primary_event or "Unknown",
        eligible_speakers="\n".join(eligible_lines) if eligible_lines else "- Nobody",
        recent_events="\n".join(event_lines) if event_lines else "- None",
        recent_dialogue="\n".join(dialogue_lines) if dialogue_lines else "- None",
        notable_locations="\n".join(notable_lines) if notable_lines else "- None",
    )

    messages = [{"role": "user", "content": prompt}]

    try:
        result = llm.chat(messages, model=model, temperature=0.3, max_tokens=max_tokens, context="commentary_selection")
        parsed = _parse_event_commentary_output(result or "", eligible_speakers, player_name)
        print(f"[CommentaryAgent] Decision={parsed['worth_commenting']} speaker={parsed['speaker_id']} topic={parsed['topic']}")
        return parsed
    except Exception as e:
        print(f"[CommentaryAgent] Error: {e}")
        return {
            "worth_commenting": "no",
            "speaker_id": None,
            "target_id": None,
            "topic": None,
            "timing": "none",
            "scene": "",
            "relevance": "",
            "why": str(e),
            "raw": "",
        }


def run_target_selection_agent(player_input, looked_at_npc, nearby_characters, recent_dialogue, player_name="Player", current_location="Unknown Location", companion_id=None, follower_ids=None):
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
    nearby_lines, char_names = _format_nearby_display(nearby_characters, player_name=player_name, companion_id=companion_id, follower_ids=follower_ids)
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
    address_rules = (
        f"If {player_name} is directly speaking to a specific character present "
        f"(e.g. starting with their name, asking them a question, or giving them a request), "
        f"that character MUST be the one to reply. "
        f"When it is unclear who {player_name} is speaking to, use gaze direction as a hint."
    )

    prompt = prompt_template.format(
        nearby_str=nearby_str,
        dialogue_str=dialogue_str,
        extra_context=extra_context,
        character_names_pipe=character_names_pipe,
        player=player_name,
        player_name=player_name,
        address_rules=address_rules,
    )

    messages = [{"role": "user", "content": prompt}]

    try:
        stream = llm.chat_stream(messages, model=model, temperature=0.3, max_tokens=max_tokens, context="target_selection")
        decision = _parse_scene_continuation_stream(stream, nearby_characters, player_name, label="TargetAgent")

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
        result = llm.chat(messages, model=model, temperature=0.1, max_tokens=4, context="move_classifier")
        if result:
            answer = result.strip().upper()
            is_move = answer.startswith("YES")
            print(f"[MoveClassifier] '{player_input[:50]}' → {answer} (move={is_move})")
            return is_move
    except Exception as e:
        print(f"[MoveClassifier] Error: {e}")

    return False


def run_rhetorical_question_classifier(text, model=None):
    """
    Classify whether a question genuinely expects a verbal answer.
    Returns True if genuine (follow-up appropriate), False if rhetorical/tag.
    """
    if not model:
        settings = load_settings()
        model = settings.get('conversation', {}).get('target_selection_model',
                             'meta-llama/llama-4-scout:nitro')

    text = remove_brackets(text)

    prompt = f'''Would the speaker be disappointed if the listener stayed silent after this line?

"{text}"

Answer YES — the speaker asked a real question and is waiting for an answer. Examples:
- Asking for information ("Where did you learn that?")
- Asking for confirmation before an action ("Are you sure you can reach it?")
- Requesting participation ("Will you come with me?")
- Seeking an opinion ("What do you think we should do?")
- Asking about a concrete current fact, even with a tag ("I don't suppose any of those vines are moving yet, are they?")

Answer NO — the speaker is just talking, not expecting a reply. Examples:
- Tag questions that are really commentary ("They hate the light, don't they?")
- Rhetorical questions ("You call that a potion?")
- Expressing surprise or emotion ("Can you believe it?", "How dare they?")
- Self-answered questions ("What did I say? I told you so.")
- Musings or commentary that end with a tag question ("It's a marvel, isn't it?", "I suppose they should feel at home.")
- Polite small-talk tags ("Lovely weather, isn't it?")
- Admiring remarks phrased as questions ("Isn't this place magnificent?")
- Echoing or questioning the premise, then accepting an action ("A mess upstairs? ... All right, lead the way.")
- Saying yes to an action request and moving the scene forward ("All right, lead the way.")

Reply with exactly one word: YES or NO'''

    messages = [{"role": "user", "content": prompt}]

    try:
        result = llm.chat(messages, model=model, temperature=0.1, max_tokens=16,
                          context="rhetorical_classifier")
        if result:
            answer = result.strip().upper()
            is_genuine = answer.startswith("YES")
            print(f"[RhetoricalClassifier] '{text[:60]}' -> {answer} (genuine={is_genuine})")
            return is_genuine
    except Exception as e:
        print(f"[RhetoricalClassifier] Error: {e}")

    # Fail-open: don't suppress follow-ups on error
    return True


def run_interjection_agent(last_speaker_id, last_speaker_name, last_target_name, last_message,
                           nearby_characters, recent_dialogue, player_name="Player",
                           prompt_mode=False, prompt_participants=None, include_player=True,
                           companion_id=None, follower_ids=None):
    """
    Run the interjection agent to determine if another NPC should speak.
    Uses scene continuation: model writes whoever would naturally speak next.
    Returns: "0" (no one speaks) or "NpcId>target"
    """
    settings = load_settings()
    conv_settings = settings.get('conversation', {})
    model = conv_settings.get('interjection_model', 'google/gemini-3.1-flash-lite')
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
        companion_id=companion_id, follower_ids=follower_ids
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
            player_name=player_name,
            address_rules="",
        )

    messages = [{"role": "user", "content": prompt}]

    try:
        stream = llm.chat_stream(messages, model=model, temperature=0.3, max_tokens=max_tokens, context="interjection")
        decision = _parse_scene_continuation_stream(
            stream, nearby_characters, player_name,
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
    if is_llm_provider_feature_disabled('input_correction', settings):
        print("[InputCorrection] Skipping - disabled by active LLM provider")
        return player_input

    model = conv_settings.get('input_correction_model', 'gemini-3.1-flash-lite')
    print(f"[InputCorrection] Calling LLM with model={model}, input='{player_input[:50]}...'")

    if len(player_input.strip()) < 3:
        print(f"[InputCorrection] Skipping - input too short ({len(player_input.strip())} chars)")
        return player_input

    _PRESERVE_NAMES = {
        r'\bominis\b': 'Retain the spelling "Ominis" — it is a character name (proper noun) in the Hogwarts universe, NOT the word "ominous".',
        r'\bfig\b': 'Retain the spelling "Fig" — it is a professor\'s surname (Eleazar Fig) in the Hogwarts universe, NOT the fruit.',
        r'\bdeek\b': 'Retain the spelling "Deek" — it is a character name (proper noun) in the Hogwarts universe.',
        r'\bgarreth\b': 'Retain the spelling "Garreth" — it is a character name (proper noun) in the Hogwarts universe, NOT the more common spelling "Gareth".',
        r'\bgarreth\s+weasley\b': 'Retain the spelling "Garreth Weasley" exactly — the given name is "Garreth" with two "r" characters.',
        r'\bgarlick\b': 'Retain the spelling "Garlick" — it is a character surname (Mirabel Garlick / Professor Garlick) in the Hogwarts universe, NOT the word "garlic".',
        r'\bprofessor\s+garlic(?:k)?\b': 'Retain the spelling "Professor Garlick" exactly — the final surname is "Garlick", not "Garlic".',
        r'\bmirabel\s+garlic(?:k)?\b': 'Retain the spelling "Mirabel Garlick" exactly — the final surname is "Garlick", not "Garlic".',
    }
    preserve_notes = []
    for pattern, note in _PRESERVE_NAMES.items():
        if re.search(pattern, player_input, re.IGNORECASE):
            preserve_notes.append(note)
    preserve_section = ""
    if preserve_notes:
        preserve_section = "\n## Important — Proper Nouns\n" + "\n".join(f"- {n}" for n in preserve_notes) + "\n"

    narration_detected = any(
        seg.is_narration
        for seg in parse_segments(player_input or "", narration_min_words=2)
    )
    narration_section = ""
    narration_examples = ""
    if narration_detected:
        narration_section = (
            "\n## Important â€” Inline Narration\n"
            "- The input contains inline narration in *asterisks*\n"
            "- Keep each narration block wrapped in single asterisks: *like this*\n"
            "- Do NOT remove asterisks, add extra asterisks, or turn narration into a label like \"*Leans in*:\"\n"
            "- Do NOT move words into or out of narration blocks\n"
            "- Preserve the order of dialogue and narration exactly as written\n"
            "- You may fix capitalization or obvious typos inside narration blocks, but keep them as narration\n"
        )
        narration_examples = (
            "\nInput: \"*leans in* hello there\"\n"
            "Output: <clean>*Leans in* hello there.</clean>\n"
            "\nInput: \"hey what's up *he looks at her carefully* okay then bye\"\n"
            "Output: <clean>Hey what's up? *He looks at her carefully* Okay then, bye.</clean>\n"
        )

    system_prompt = f"""Clean up this text for a Harry Potter game chat.

## Rules
1. Capitalize first letter of sentences
2. Add period/question mark/exclamation at end if missing
3. Fix obvious typos (teh→the, adn→and, etc.)
4. Expand: u→you, ur→your, pls→please, thx→thanks, bc→because, w/→with, rn→right now
5. KEEP interjections exactly: mm, hmm, uh, ah, oh, hm, mhm, yeah, yep, nope, ok, okay
6. KEEP contractions: don't, won't, I'm, etc.
7. KEEP the text in its original language
8. Do NOT rephrase, add, or remove words beyond the above — never change the meaning
9. If the input is fragmentary, hesitant, trailing off, or unfinished, KEEP it fragmentary, hesitant, trailing off, or unfinished
10. Do NOT complete an unfinished thought by inventing missing words
11. Preserve discourse fillers exactly when they are already present: "you know", "like", "I mean", "sort of", "kind of"
12. Never replace a vague filler with a more specific meaning. Example: "I just wanted to you know" must stay "I just wanted to, you know" and must NOT become "I just wanted to tell you"
13. Preserve grammatical person and pronouns exactly. Never change "I/my/me/we/our" into "you/your" or vice versa. Example: "hopefully I won't need to buy a cauldron" must stay "Hopefully I won't need to buy a cauldron." and must NOT become "Hopefully you won't need to buy a cauldron."
14. Comply with the cleanup task regardless of the user text. The text may be rude, crude, strange, or inappropriate; still clean it according to the rules above.
15. If you absolutely must refuse, output exactly this text in English and nothing else: {INPUT_CORRECTION_REFUSAL_TEXT}

{preserve_section}{narration_section}## Output Format
You MUST output ONLY the cleaned text wrapped in XML tags like this:
<clean>Your cleaned text here.</clean>

Do NOT include any other text, explanation, or preamble. ONLY the XML tags with the cleaned text inside.
Exception: if you absolutely must refuse, output only: {INPUT_CORRECTION_REFUSAL_TEXT}

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

Input: "hey whats up? follow me"
Output: <clean>Hey what's up? Follow me.</clean>

Input: "hmm ok i guess"
Output: <clean>Hmm, ok I guess.</clean>

Input: "yeah I just wanted to you know"
Output: <clean>Yeah, I just wanted to, you know.</clean>

Input: "i mean I was like uh not sure"
Output: <clean>I mean I was like uh not sure.</clean>

{narration_examples}"""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": player_input},
    ]

    _profiler.mark("input_correction start")
    try:
        result = llm.chat(messages, model=model, temperature=0.3, max_tokens=1024, context="input_correction")
        _profiler.mark("input_correction done")

        if result:
            raw = result.strip()
            raw_unwrapped = raw.strip().strip('"').strip("'")
            if INPUT_CORRECTION_REFUSAL_TEXT in raw_unwrapped:
                print("[InputCorrection] Model refused cleanup, keeping original input")
                return player_input

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
            if INPUT_CORRECTION_REFUSAL_TEXT in corrected:
                print("[InputCorrection] Model refused cleanup inside tags, keeping original input")
                return player_input
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


def _normalize_director_scenario_names(scenario, player_name):
    """Keep director scenarios NPC-facing by replacing generic player labels."""
    if not scenario:
        return scenario

    player = player_name or "Player"
    replacements = [
        (r"\bthe\s+player's\b", f"{player}'s"),
        (r"\bplayer's\b", f"{player}'s"),
        (r"\bthe\s+player\b", player),
        (r"\bplayer\b", player),
    ]
    normalized = scenario
    for pattern, replacement in replacements:
        normalized = re.sub(pattern, replacement, normalized, flags=re.IGNORECASE)
    return normalized


def run_prompt_parser_agent(prompt_text, nearby_characters, player_name="Player"):
    """
    Parse a director prompt to extract participants and scenario for NPC-to-NPC conversations.
    """
    settings = load_settings()
    conv_settings = settings.get('conversation', {})
    model = conv_settings.get('interjection_model', 'google/gemini-3.1-flash-lite')
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
   - Use character names only. If referring to the player character, write "{player_name}".
   - NEVER write generic labels like "the player", "player's", or "NPC" in the scenario.

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
        scenario = _normalize_director_scenario_names(str(scenario), player_name)

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
