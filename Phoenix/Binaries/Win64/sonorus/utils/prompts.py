"""
Character prompt utilities for Sonorus.
Handles prompt template substitution and character configuration.
"""

from .settings import load_settings, DEFAULT_SETTINGS, get_setting
from .localization import get_display_name
from .character_bios import build_prompt_bio_sections
from constants import EMOTE_TAGS_PROMPT

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

NARRATION_FORMAT_WITH_VOCAL_TAGS = (
    '## Output Format\n'
    'You can mix spoken dialogue with brief narration. Not every response requires narration. Examples:\n'
    '"Hello there." *He paused, considering his words carefully.* "What brings you here?"\n'
    '"So, how about it?" *He leaned in, waiting for a response.*\n'
    '*He scrunched his nose thoughtfully before answering* "I suppose I can help you with that."\n'
    'Rules:\n'
    '- Spoken words go in double quotes\n'
    '- Actions, thoughts, or scene descriptions go in *asterisks* — always use third person ("He smirked" not "I smirked")\n'
    "- A single *word* or short emphasis phrase can be emphasis: \"I *really* don't think so.\"\n"
    '- Narration/stage direction in *asterisks* should usually be at least 3 words or include punctuation.\n'
    "- Do NOT use double quotes for emphasis or irony around individual words — use *asterisks* instead\n"
    '- Always include at least one line of dialogue. Narration is optional and *should be used sparingly* for character depth.\n'
    '- The sentence limit in your instructions applies to the ENTIRE response — dialogue and narration combined. A narration block counts as a sentence.\n'
    '- For brief audible vocal bursts that fit the allowed sound tags, prefer the tag (for example [laugh]) instead of only narrating it in prose.\n'
    '- It is fine to use both when natural: keep the audible sound in a tag and use narration for the visible action or tone around it.\n'
    '- `[Action: X]` tags (if applicable) go at the very end, after all dialogue and narration.'
)

NARRATION_FORMAT_NO_VOCAL_TAGS = (
    '## Output Format\n'
    'You can mix spoken dialogue with brief narration. Not every response requires narration. Examples:\n'
    '"Hello there." *He paused, considering his words carefully.* "What brings you here?"\n'
    '"So, how about it?" *He leaned in, waiting for a response.*\n'
    '*He scrunched his nose thoughtfully before answering* "I suppose I can help you with that."\n'
    'Rules:\n'
    '- Spoken words go in double quotes\n'
    '- Actions, thoughts, or scene descriptions go in *asterisks* — always use third person ("He smirked" not "I smirked")\n'
    "- A single *word* or short emphasis phrase can be emphasis: \"I *really* don't think so.\"\n"
    '- Narration/stage direction in *asterisks* should usually be at least 3 words or include punctuation.\n'
    "- Do NOT use double quotes for emphasis or irony around individual words — use *asterisks* instead\n"
    '- Always include at least one line of dialogue. Narration is optional and *should be used sparingly* for character depth.\n'
    '- The sentence limit in your instructions applies to the ENTIRE response — dialogue and narration combined. A narration block counts as a sentence.\n'
    '- `[Action: X]` tags (if applicable) go at the very end, after all dialogue and narration.'
)


def get_speech_rules(*, narration_enabled_override=None):
    """
    Build TTS speech rules based on the active TTS provider and model settings.

    Returns provider-appropriate instructions for how the LLM should format
    its text output for optimal TTS synthesis.
    """
    provider = get_setting('tts.provider', 'inworld')
    stt_enabled = get_setting('stt.provider', 'none') != 'none'

    if narration_enabled_override is None:
        narration_enabled = get_setting('conversation.narration_enabled', False)
    else:
        narration_enabled = bool(narration_enabled_override)

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
        lines.append('**Non-verbal sounds** (use sparingly where natural): [laugh], [sigh], [breathe], [cough], [clear_throat], [yawn]')
        lines.append('Use these for short audible sounds the listener would hear. If narration is also present, keep the sound as a tag and let narration cover only the visible action, expression, or scene detail.')
        if narration_enabled:
            lines.append('Keep sound tags inside the quoted spoken line they belong to, for example "[laugh] That\'s absurd." or "That\'s absurd [laugh]." Never place a sound tag outside quotes or between dialogue and narration.')
        else:
            lines.append('Keep sound tags inside the spoken line they belong to, for example [laugh] That\'s absurd. or That\'s absurd [laugh]. Do not use quotation marks around dialogue.')

        if narration_enabled:
            lines.append('')
            lines.append(NARRATION_FORMAT_WITH_VOCAL_TAGS)
        else:
            lines.append('')
            lines.append('**Output ONLY spoken dialogue** (plus allowed vocal tags above, and `[Action: X]` tags if applicable — see Actions section). Do not use quotation marks around dialogue. No narration, no action descriptions, no stage directions, no internal thoughts — in ANY format (asterisks, parentheses, brackets, or prose). Action tags never replace dialogue; always speak first.')

        rules = '\n'.join(lines)

    # OmniVoice — supports only [laugh] as an audio tag
    elif provider == 'omnivoice':
        lines = [
            '## Voice Performance',
            'Your text is converted to speech. Follow these rules:',
            "- Use contractions naturally (don't, can't, I'm, we're)",
            '- Write numbers and dates in spoken form ("twenty-three" not "23", "march fifteenth" not "3/15")',
            '- Vary sentence openings — do not repeat the same starter across sentences',
            '- Vary sentence length',
        ]
        if not narration_enabled:
            lines.append('- You can wrap a word in *asterisks* to stress it')
        lines.append('')
        lines.append('**Non-verbal sounds** (use sparingly where natural): [laugh]')
        lines.append('Use [laugh] for short audible laughs the listener would hear. This is the only sound tag available.')
        if narration_enabled:
            lines.append('Keep [laugh] inside the quoted spoken line it belongs to, for example "[laugh] That\'s absurd." Never place it outside quotes or in narration.')
            lines.append('')
            lines.append(NARRATION_FORMAT_WITH_VOCAL_TAGS)
        else:
            lines.append('Keep [laugh] inside the spoken line it belongs to, for example [laugh] That\'s absurd. Do not use quotation marks around dialogue.')
            lines.append('')
            lines.append('**Output ONLY spoken dialogue** (plus [laugh] where natural, and `[Action: X]` tags if applicable — see Actions section). Do not use quotation marks around dialogue. No narration, no action descriptions, no stage directions, no internal thoughts — in ANY format (asterisks, parentheses, brackets, or prose). Action tags never replace dialogue; always speak first.')
        rules = '\n'.join(lines)

    # Pocket TTS / none / unknown providers - plain text only
    elif narration_enabled:
        rules = (
            '## Voice Performance\n'
            'Your text is converted to speech. Write plain spoken text.\n'
            "- Use contractions naturally (don't, can't, I'm, we're)\n"
            '- Vary sentence length\n'
            '\n'
            + NARRATION_FORMAT_NO_VOCAL_TAGS
        )

    else:
        rules = (
            '## Voice Performance\n'
            'Your text is converted to speech. Write plain spoken text.\n'
            "- Use contractions naturally (don't, can't, I'm, we're)\n"
            '- Vary sentence length\n'
            '\n'
            '**Output ONLY spoken dialogue.** Do not use quotation marks around dialogue. No narration, no action descriptions, no stage directions — in ANY format (*asterisks*, parentheses, brackets, or prose). Write plain spoken text only.'
        )

    # Facial emote tags: global setting, all providers
    # Tags are stripped before TTS by each provider's filter; they drive facial animation only
    if get_setting('conversation.emotes_enabled', False):
        if narration_enabled:
            rules += (
                f'\n**Emotion** (optional, at start of a quoted spoken line, skip if does not match the emotion of the sentence): {EMOTE_TAGS_PROMPT}'
                '\nPlace the emotion tag inside the opening quote, before the first word. Can combine with a sound tag. Examples:'
                '\n"[happy] That\'s wonderful news!" *She clapped her hands.*'
                '\n"[content] This is nice. I could stay here awhile."'
                '\n"[tired] I need a moment. It has been a very long day."'
                '\n"[fond] You always did have a kind heart."'
                '\n"[shy] Oh. I didn\'t expect you to say that."'
                '\n"[beam] There you are. I was so hoping to see you."'
                '\n"[proud] Well done. I knew you had it in you."'
                '\n"[smug] Oh, I knew I was right."'
                '\n*He hesitated.* "[concerned] Are you sure about this?"'
                '\n"[sympathy] I\'m sorry. That must have been difficult."'
                '\n"[annoyed] Must you do that here and now?"'
                '\n"[confused] Wait. What are you talking about?"'
                '\n"[cringe] Oh no. Please do not say that again."'
                '\n"[curious] And what, exactly, were you doing in the Restricted Section?"'
                '\n"[happy] [laugh] I can\'t believe it worked!"'
                '\n"[angry] You had no right to do that." *His jaw clenched.*'
                '\nNever place an emotion tag outside quotes or in narration.'
            )
        else:
            rules += f'\n**Emotion** (optional, at sentence start, skip if does not match the emotion of the sentence): {EMOTE_TAGS_PROMPT}'

    if stt_enabled:
        rules += '\n\n{player} may be using voice input, so interpret the intent behind their words rather than reacting to odd phrasing or apparent misspellings. e.g. "Hogs meet" likely means "Hogsmeade".'

    return rules


def build_character_guidance_sections(
    npc_id,
    display_name,
    *,
    player_name=None,
    prompt_mode=False,
    include_player_bio=True,
):
    """Build static-bio grounding sections for a character prompt."""
    settings = load_settings()
    return build_prompt_bio_sections(
        npc_id,
        display_name,
        player_name=player_name,
        prompt_mode=prompt_mode,
        include_player_bio=include_player_bio,
        settings=settings,
    )


def get_world_lore_block(game_context=None, *, placeholder_context=None, include_heading=True):
    """Build the shared world-lore block, including auto world facts."""
    settings = load_settings()
    prompts = settings.get('prompts', {})
    world_lore = prompts.get('world_lore', '').strip()
    try:
        from .world_facts import get_global_facts
        mission_statuses = game_context.get('missionStatuses') if game_context else None
        player = game_context.get('playerName') if game_context else None
        auto_facts = get_global_facts(mission_statuses, player_name=player)
        if auto_facts:
            world_lore = f"{world_lore}\n{auto_facts}".strip() if world_lore else auto_facts
    except Exception as e:
        print(f"[WorldFacts] Error getting global facts: {e}")

    if not world_lore:
        return ""

    if placeholder_context:
        world_lore = substitute_placeholders(world_lore, placeholder_context)
    if include_heading:
        return f"## World Lore\n{world_lore}"
    return world_lore


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
        prompt = re.sub(r"\n*.+ may be using voice input[^\n]*", "", prompt)
        prompt = re.sub(r"\n{3,}", "\n\n", prompt).strip()  # Clean up extra blank lines

    # In prompt mode, add the scenario/topic
    if prompt_mode and scenario:
        prompt += f"\n\n**Scene Direction:** {scenario}. Have a natural conversation on this topic, responding authentically to what others say."

    # NOTE: Action instructions (JoinAsCompanion/LeaveCompanion, house points) are added
    # dynamically in server.py based on NPC role and enabled settings, not here in the base prompt.

    # Build bio context section
    # When memory is effectively OFF for this NPC: inject static bios directly.
    # When memory is effectively ON: static bio is added through contextual memory instead.
    bio_sections = build_character_guidance_sections(
        npc_id,
        display_name,
        player_name=player_name,
        prompt_mode=prompt_mode,
        include_player_bio=not prompt_mode,
    )
    if bio_sections:
        prompt = prompt + "\n\n" + "\n\n".join(bio_sections)

    world_lore_block = get_world_lore_block(game_context, placeholder_context=placeholder_context, include_heading=True)
    if world_lore_block:
        prompt += f"\n\n{world_lore_block}"

    # Add language instruction for non-English games
    language = settings.get('setup', {}).get('language', 'EN_US')
    if language != 'EN_US':
        lang_name = LANGUAGE_NAMES.get(language, 'the game language')
        prompt += f"\n\n**Response Language:** You MUST respond ONLY in {lang_name}. Do not use English."

    return (display_name, prompt)
