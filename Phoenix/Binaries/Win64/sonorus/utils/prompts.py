"""
Character prompt utilities for Sonorus.
Handles prompt template substitution and character configuration.
"""

from .settings import load_settings, DEFAULT_SETTINGS, get_setting
from .localization import get_display_name

# Language code to display name mapping for AI response instruction
LANGUAGE_NAMES = {
    "DE_DE": "German",
    "FR_FR": "French",
    "ES_ES": "Spanish (Spain)",
    "ES_MX": "Spanish (Latin America)",
    "IT_IT": "Italian",
    "PT_BR": "Portuguese",
    "JA_JP": "Japanese",
    "KO_KR": "Korean",
    "ZH_CN": "Chinese",
    "ZH_TW": "Chinese",
    "PL_PL": "Polish",
    "RU_RU": "Russian",
    "AR_AE": "Arabic",
}

NARRATION_FORMAT = (
    '## Output Format\n'
    'You can mix spoken dialogue with brief narration. Example:\n'
    '"Hello there." *He paused, considering his words carefully.* "What brings you here?"\n'
    'Rules:\n'
    '- Spoken words go in double quotes\n'
    '- Actions, thoughts, or scene descriptions go in *asterisks* — always use third person ("He smirked" not "I smirked")\n'
    "- A single *word* inside quotes is emphasis: \"I *really* don't think so.\"\n"
    "- Do NOT use \"scare quotes\" — use single quotes or *emphasis* instead\n"
    '- Always include at least one line of dialogue. Narration is optional and should be used sparingly for character depth.\n'
    '- `[Action: X]` tags (if applicable) go at the very end, after all dialogue and narration.'
)


def get_speech_rules():
    """
    Build TTS speech rules based on the active TTS provider and model settings.

    Returns provider-appropriate instructions for how the LLM should format
    its text output for optimal TTS synthesis.
    """
    provider = get_setting('tts.provider', 'inworld')
    stt_enabled = get_setting('stt.provider', 'none') != 'none'

    narration_enabled = get_setting('conversation.narration_enabled', False)

    if provider in ('inworld', 'elevenlabs'):
        lines = [
            '## Voice Performance',
            'Your text is converted to speech. Follow these rules:',
            "- Use contractions naturally (don't, can't, I'm, we're)",
            '- Write numbers and dates in spoken form ("twenty-three" not "23", "march fifteenth" not "3/15")',
            '- Vary sentence openings — do not repeat the same starter across sentences',
            '- Vary sentence length',
        ]
        # Asterisk emphasis instruction — only when narration is off
        # (when narration is on, the Output Format section defines asterisk semantics)
        if not narration_enabled:
            lines.append('- You can wrap a word in *asterisks* to stress it')
        lines.append('')
        lines.append('**Non-verbal sounds** (use sparingly where natural): `[laugh]`, `[sigh]`, `[breathe]`, `[cough]`, `[clear_throat]`, `[yawn]`')

        # Emotion & delivery tags: only for Inworld with emotion_delivery enabled and 1.5+ model
        if provider == 'inworld':
            emotion_delivery = get_setting('tts.inworld.emotion_delivery', False)
            model = get_setting('tts.inworld.model', '')
            is_1_5_plus = model and '1.5' in model

            if emotion_delivery and is_1_5_plus:
                lines.append('**Emotion** (optional, at sentence start): `[happy]`, `[sad]`, `[angry]`, `[surprised]`, `[fearful]`, `[disgusted]`')
                lines.append('**Delivery** (optional, at sentence start): `[laughing]`, `[whispering]`')

        if narration_enabled:
            lines.append('')
            lines.append(NARRATION_FORMAT)
        else:
            lines.append('')
            lines.append('**Output ONLY spoken dialogue** (plus allowed vocal tags above, and `[Action: X]` tags if applicable — see Actions section). No narration, no action descriptions, no stage directions, no internal thoughts — in ANY format (asterisks, parentheses, brackets, or prose). Action tags never replace dialogue; always speak first.')

        rules = '\n'.join(lines)

    # Pocket TTS / none / unknown providers - plain text only
    elif narration_enabled:
        rules = (
            '## Voice Performance\n'
            'Your text is converted to speech. Write plain spoken text.\n'
            "- Use contractions naturally (don't, can't, I'm, we're)\n"
            '- Vary sentence length\n'
            '\n'
            + NARRATION_FORMAT
        )

    else:
        rules = (
            '## Voice Performance\n'
            'Your text is converted to speech. Write plain spoken text.\n'
            "- Use contractions naturally (don't, can't, I'm, we're)\n"
            '- Vary sentence length\n'
            '\n'
            '**Output ONLY spoken dialogue.** No narration, no action descriptions, no stage directions — in ANY format (*asterisks*, parentheses, brackets, or prose). Write plain spoken text only.'
        )

    if stt_enabled:
        rules += '\n\n{player} may be using voice input, so interpret the intent behind their words rather than reacting to odd phrasing or apparent misspellings. e.g. "Hogs meet" likely means "Hogsmeade".'

    return rules


def substitute_placeholders(prompt, context):
    """
    Substitute placeholders in prompt template.
    Supported: {name}, {house}, {role}, {backstory}, {location}, {time}, {player}, {player_house}, {speaking_to}
    Unknown placeholders are left as-is.
    """
    for key, value in context.items():
        if value:
            prompt = prompt.replace(f'{{{key}}}', str(value))
    return prompt


def get_character(npc_id, game_context=None, speaking_to=None, prompt_mode=False, scenario=None):
    """
    Get character display name and prompt from settings, including editor's guidance for context.

    Args:
        npc_id: Internal NPC ID (e.g., "SebastianSallow", "NellieOggspire")
        game_context: Optional game context dict for placeholder substitution
        speaking_to: Display name of who this NPC is responding to (for {speaking_to} placeholder)
        prompt_mode: If True, this is a director-prompted NPC-to-NPC conversation
        scenario: The conversation scenario/topic from the director prompt

    Returns:
        Tuple of (display_name, prompt) where display_name is like "Sebastian Sallow"
    """
    settings = load_settings()
    prompts = settings.get('prompts', {})
    editor_guidance = prompts.get('editor_guidance', {})
    default_prompt = prompts.get('default', DEFAULT_SETTINGS['prompts']['default'])

    # Get display name from ID using localization
    display_name = get_display_name(npc_id) if npc_id else "Hogwarts Resident"

    # Build context for placeholder substitution
    speech_rules = get_speech_rules()
    # If the user's prompt already has the voice input line baked in, don't duplicate it
    if 'voice input' in default_prompt:
        speech_rules = speech_rules.split('\n\n{player} may be using voice input')[0]
    placeholder_context = {
        'name': display_name,
        'house': '',
        'role': '',
        'backstory': '',
        'speech_rules': speech_rules,
    }

    # Add game context if available
    player_name = 'the student'
    if game_context:
        # Use specific zone location if available, fallback to broad location
        zone = game_context.get('zoneLocation', '')
        placeholder_context['location'] = zone if zone else game_context.get('location', '')
        placeholder_context['time'] = game_context.get('timeFormatted', '')
        player_name = game_context.get('playerName', 'the student')
        placeholder_context['player'] = player_name
        placeholder_context['player_name'] = player_name  # Alias for {player}
        placeholder_context['player_house'] = game_context.get('playerHouse', '')

    # Add speaking_to (who this NPC is responding to)
    # In prompt mode, don't default to player - only use explicit speaking_to
    if prompt_mode:
        placeholder_context['speaking_to'] = speaking_to if speaking_to else "another character"
    else:
        # Normal mode: default to player_name if not specified
        placeholder_context['speaking_to'] = speaking_to if speaking_to else player_name

    # Substitute placeholders in base prompt
    prompt = substitute_placeholders(default_prompt, placeholder_context)

    # In prompt mode (NPC-to-NPC), remove player-specific voice input instruction
    if prompt_mode:
        import re
        prompt = re.sub(r"\n*\S+ may be using voice input[^\n]*", "", prompt)
        prompt = re.sub(r"\n{3,}", "\n\n", prompt).strip()  # Clean up extra blank lines

    # In prompt mode, add the scenario/topic
    if prompt_mode and scenario:
        prompt += f"\n\n**Scene Direction:** {scenario}. Have a natural conversation on this topic, responding authentically to what others say."

    # NOTE: Action instructions (JoinAsCompanion/LeaveCompanion, house points) are added
    # dynamically in server.py based on NPC role and enabled settings, not here in the base prompt.

    # Build bio context section
    # When memory is OFF: editor_guidance acts as direct bio (injected into prompts)
    # When memory is ON: editor_guidance influences bio generation, so don't inject here
    #                    (the generated bio is injected via get_contextual_memory instead)
    memory_enabled = settings.get('memory', {}).get('enabled', False)

    if not memory_enabled:
        bio_sections = []

        # Get NPC bio (try ID first, then display name)
        npc_bio = editor_guidance.get(npc_id) if npc_id else None
        if not npc_bio:
            npc_bio = editor_guidance.get(display_name)
        if npc_bio:
            bio_sections.append(f"About you ({display_name}): {npc_bio}")

        # Get player bio (skip in prompt mode - player not directly involved)
        if not prompt_mode:
            player_bio = editor_guidance.get('Player') or editor_guidance.get(player_name)
            if player_bio:
                bio_sections.append(f"About {player_name}: {player_bio}")

        # Append bios to prompt if any exist
        if bio_sections:
            prompt = prompt + "\n\n" + "\n\n".join(bio_sections)

    # Append world lore if set
    world_lore = prompts.get('world_lore', '').strip()
    if world_lore:
        world_lore = substitute_placeholders(world_lore, placeholder_context)
        prompt += f"\n\n## World Lore\n{world_lore}"

    # Add language instruction for non-English games
    language = settings.get('setup', {}).get('language', 'EN_US')
    if language != 'EN_US':
        lang_name = LANGUAGE_NAMES.get(language, 'the game language')
        prompt += f"\n\n**Response Language:** You MUST respond ONLY in {lang_name}. Do not use English."

    return (display_name, prompt)
