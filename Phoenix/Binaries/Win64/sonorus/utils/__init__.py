"""
Sonorus utility modules.

Re-exports all public functions from submodules for convenient imports.
"""

from .settings import (
    SONORUS_DIR,
    DATA_DIR,
    SETTINGS_FILE,
    CONFIG_HTML,
    DEFAULT_SETTINGS,
    load_settings,
    save_settings,
    deep_merge,
    get_setting,
    read_file,
    write_file,
)

from .text_utils import (
    split_into_sentences,
    sanitize_name,
    parse_target_result,
    filter_npcs_by_earshot,
    validate_speaker_in_nearby,
)

from .localization import (
    MAIN_LOCALIZATION_FILE,
    load_localization,
    get_reverse_localization,
    id_from_name,
    find_npc_id_by_name,
)

from .landmarks import (
    LANDMARK_LOCATIONS_FILE,
    set_lua_socket as set_landmarks_lua_socket,
    load_landmarks,
    load_player_position,
    calculate_distance,
    get_cardinal_direction,
    format_distance,
    get_landmark_beacons,
    format_beacons_for_llm,
    format_beacons_for_vision,
)

from .dialogue import (
    load_dialogue_history,
    save_dialogue_history,
    collapse_consecutive_duplicate,
    collapse_consecutive_spells,
    filter_dialogue_history,
    prettify_voice_name,
    format_dialogue_history,
)

from .game_context import (
    format_game_context,
)

from .prompts import (
    substitute_placeholders,
    get_character,
)

from .llm_utils import (
    LOGS_DIR,
    log_llm,
    call_llm,
    parse_action,
    strip_action_tag,
)

from .agents import (
    run_target_selection_agent,
    run_interjection_agent,
)

from .conversation import (
    ConversationState,
    PreBuffer,
)

from .lua_socket import (
    LuaSocketServer,
)

from .game_monitor import (
    GAME_PROCESS_NAME,
    is_game_running,
    start_game_monitor,
)
