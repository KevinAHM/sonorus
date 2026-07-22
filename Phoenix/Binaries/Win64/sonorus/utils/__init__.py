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
    is_llm_provider_feature_disabled,
    read_file,
    write_file,
    is_dev_mode,
    set_dev_mode,
    dev_print,
)

from .text_utils import (
    split_into_sentences,
    remove_unpaired_double_quotes,
    sanitize_name,
    parse_target_result,
    filter_npcs_by_earshot,
    validate_speaker_in_nearby,
    detect_spell_in_text,
    correct_spell_names_in_text,
    is_significant_npc,
    strip_parentheses,
    extract_director_prefix,
)

from .localization import (
    get_localization_path,
    get_subtitles_path,
    invalidate_cache as invalidate_localization_cache,
    load_localization,
    get_display_name,
    get_reverse_localization,
    get_lowercase_map,
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
    replace_dialogue_history,  # Bulk operations only - see docstring
    save_dialogue_history,     # Alias for backwards compatibility
    collapse_consecutive_duplicate,
    collapse_consecutive_spells,
    filter_dialogue_history,
    prettify_voice_name,
    format_dialogue_entry,
    format_dialogue_history,
    get_time_since_last_interaction,
    format_time_gap,
    is_named_npc,
)

from .game_context import (
    format_game_context,
)

from .prompts import (
    substitute_placeholders,
    get_speech_rules,
    build_character_guidance_sections,
    get_world_lore_block,
    get_character,
)

from .llm_utils import (
    LOGS_DIR,
    log_llm,
    LLM_ERROR_FALLBACK,
    call_llm,
    call_llm_stream,
    stream_sentences,
    parse_action,
    parse_actions,
    strip_action_tag,
)

from .agents import (
    run_target_selection_agent,
    run_interjection_agent,
    run_event_commentary_agent,
    run_move_classifier,
    run_rhetorical_question_classifier,
    run_input_correction_agent,
    run_prompt_parser_agent,
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

from .profiler import (
    Profiler,
    profiler,
)

from .memory_queue import (
    queue_npcs_for_processing,
    get_queue_status,
    ensure_worker_running,
    stop_worker,
    is_processing,
    graceful_shutdown,
    reset_npc_state,
    retry_failed_chapters,
)
