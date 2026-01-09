"""
Character prompt utilities for Sonorus.
Handles prompt template substitution and character configuration.
"""

from .settings import load_settings, DEFAULT_SETTINGS
from .localization import get_display_name


def substitute_placeholders(prompt, context):
    """
    Substitute placeholders in prompt template.
    Supported: {name}, {house}, {role}, {backstory}, {location}, {time}, {player}, {player_house}
    Unknown placeholders are left as-is.
    """
    for key, value in context.items():
        if value:
            prompt = prompt.replace(f'{{{key}}}', str(value))
    return prompt


def get_character(npc_id, game_context=None):
    """
    Get character display name and prompt from settings, including bios for context.

    Args:
        npc_id: Internal NPC ID (e.g., "SebastianSallow", "NellieOggspire")
        game_context: Optional game context dict for placeholder substitution

    Returns:
        Tuple of (display_name, prompt) where display_name is like "Sebastian Sallow"
    """
    settings = load_settings()
    prompts = settings.get('prompts', {})
    bios = prompts.get('bios', {})
    default_prompt = prompts.get('default', DEFAULT_SETTINGS['prompts']['default'])

    # Get display name from ID using localization
    display_name = get_display_name(npc_id) if npc_id else "Hogwarts Resident"

    # Build context for placeholder substitution
    placeholder_context = {
        'name': display_name,
        'house': '',
        'role': '',
        'backstory': '',
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
        placeholder_context['player_house'] = game_context.get('playerHouse', '')

    # Substitute placeholders in base prompt
    prompt = substitute_placeholders(default_prompt, placeholder_context)

    # Append actions instructions if actions are enabled
    conv_settings = settings.get('conversation', {})
    if conv_settings.get('actions_enabled', False):
        prompt += "\n\nActions: Optionally include ONE action at the END using [Action: X] where X is: Follow, Leave, or Stop. Most responses need no action."

    # Build bio context section
    bio_sections = []

    # Get NPC bio (try ID first, then display name)
    npc_bio = bios.get(npc_id) if npc_id else None
    if not npc_bio:
        npc_bio = bios.get(display_name)
    if npc_bio:
        bio_sections.append(f"About you ({display_name}): {npc_bio}")

    # Get player bio - clarify this is who the USER is
    player_bio = bios.get('Player') or bios.get(player_name)
    if player_bio:
        bio_sections.append(f"About the user (who is {player_name}): {player_bio}")

    # Append bios to prompt if any exist
    if bio_sections:
        prompt = prompt + "\n\n" + "\n\n".join(bio_sections)

    return (display_name, prompt)
