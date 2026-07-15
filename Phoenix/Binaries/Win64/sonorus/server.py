"""
Sonorus Server - Persistent HTTP server for LLM + TTS

Runs in background, communicates with UE4SS Lua via HTTP and TCP socket.
"""
import os
import queue
import re
import sys
import json
import time
import subprocess
import threading
import webbrowser
import faulthandler
from concurrent.futures import ThreadPoolExecutor, as_completed

# Ensure script directory is in sys.path for embedded Python
_script_dir = os.path.dirname(os.path.abspath(__file__))
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)

# Enable faulthandler to catch hard crashes (segfaults, etc.)
# This dumps a traceback to the crash log file when Python crashes
_crash_log_path = os.path.join(_script_dir, "logs", "crash.log")
os.makedirs(os.path.dirname(_crash_log_path), exist_ok=True)
_crash_log_file = open(_crash_log_path, "a")
_crash_log_file.write(f"\n{'='*60}\nServer started at {time.strftime('%Y-%m-%d %H:%M:%S')}\n{'='*60}\n")
_crash_log_file.flush()
faulthandler.enable(file=_crash_log_file, all_threads=True)

# Write immediate heartbeat to prevent duplicate server spawns during import
# (Lua checks this file before spawning new server)
# Use atomic write (temp + rename) to prevent race condition with Lua reading
_heartbeat_path = os.path.join(_script_dir, "server.heartbeat")
_heartbeat_tmp = _heartbeat_path + ".tmp"
with open(_heartbeat_tmp, "w") as f:
    f.write(str(int(time.time())))
os.replace(_heartbeat_tmp, _heartbeat_path)

from flask import Flask, request, jsonify, send_file, Response

# Import utility modules
from utils import (
    # Settings
    SONORUS_DIR,
    DATA_DIR,
    SETTINGS_FILE,
    CONFIG_HTML,
    DEFAULT_SETTINGS,
    load_settings,
    save_settings,
    deep_merge,
    is_llm_provider_feature_disabled,
    read_file,
    write_file,
    # Text utils
    split_into_sentences,
    remove_unpaired_double_quotes,
    parse_target_result,
    filter_npcs_by_earshot,
    validate_speaker_in_nearby,
    detect_spell_in_text,
    correct_spell_names_in_text,
    is_significant_npc,
    strip_parentheses,
    extract_director_prefix,
    # Localization
    load_localization,
    get_display_name,
    find_npc_id_by_name,
    # Dialogue
    load_dialogue_history,
    save_dialogue_history,
    filter_dialogue_history,
    format_dialogue_history,
    get_time_since_last_interaction,
    format_time_gap,
    # Game context
    format_game_context,
    # Prompts
    get_character,
    # LLM utils
    call_llm,
    call_llm_stream,
    stream_sentences,
    LLM_ERROR_FALLBACK,
    parse_action,
    parse_actions,
    strip_action_tag,
    # Agents
    run_target_selection_agent,
    run_interjection_agent,
    run_move_classifier,
    run_rhetorical_question_classifier,
    run_input_correction_agent,
    run_prompt_parser_agent,
    # Conversation
    ConversationState,
    PreBuffer,
    # Socket
    LuaSocketServer,
    # Landmarks
    set_landmarks_lua_socket,
    # Game monitor
    start_game_monitor,
)

# Import shared constants
from constants import (
    CONVERSATION_EARSHOT_DISTANCE,
    NPC_QUESTION_FOLLOW_UP_TIMEOUT_SECONDS,
    RECENT_DIALOGUE_WINDOW_SECONDS,
    VERSION,
)
from utils.event_commentary import EventCommentaryOrchestrator
from utils.game_context import format_static_context, format_dynamic_context
from utils.dialogue import format_dialogue_as_messages
from utils.conversation_topics import conversation_topics, load_persisted_topic
from utils.emote_embeddings import ensure_emote_index_async
from utils.memory import (
    filter_memory_enabled_npc_ids,
    get_contextual_memory,
    search_relevant_facts,
)
from utils.llm_utils import strip_response_metadata, call_llm_messages
from runtime.streaming_playback import (
    build_live_sentence_stream,
    iter_completed_response_sentences,
    play_completed_response_streaming,
    start_streaming_playback_session,
)
from runtime.commentary_runtime import (
    generate_commentary_response,
    generate_follow_up_response,
    stream_linger_goodbye_lines,
    play_commentary_response,
    run_commentary_turn,
    run_follow_up_turn,
    run_attention_turn,
)

# Import our modules
try:
    from services import tts
    TTS_AVAILABLE = True
except ImportError as e:
    print(f"[WARN] TTS service not available: {e}")
    tts = None
    TTS_AVAILABLE = False

try:
    from audio import shutdown as audio_shutdown, get_player as audio_get_player
    AUDIO3D_AVAILABLE = True
except ImportError as e:
    print(f"[WARN] audio module not available: {e}")
    AUDIO3D_AVAILABLE = False
    audio_get_player = None

try:
    from audio import lipsync
    LIPSYNC_AVAILABLE = True
except ImportError as e:
    print(f"[WARN] audio.lipsync module not available: {e}")
    import traceback
    print(traceback.format_exc().rstrip())
    LIPSYNC_AVAILABLE = False

try:
    import vision_agent
    VISION_AGENT_AVAILABLE = True
except ImportError as e:
    print(f"[WARN] vision_agent module not available: {e}")
    VISION_AGENT_AVAILABLE = False

import llm
import event_logger

# Import route blueprints
from routes import register_blueprints, setup_bp, config_bp, dialogue_bp, commitments_bp, voice_manager_bp
from routes.setup import is_setup_complete, set_lua_socket as set_setup_lua_socket
from routes.config import set_lua_socket as set_config_lua_socket
from routes.dialogue import set_load_game_context
from routes.commitments import set_lua_socket as set_commitments_lua_socket, set_load_game_context as set_commitments_game_context
from routes.owlpost import set_load_game_context as set_owlpost_game_context, set_board_orchestrator as owlpost_set_board_orchestrator, set_conv_state as set_owlpost_conv_state
from runtime.owl_orchestrator import OwlMailOrchestrator, OwlBoardOrchestrator, set_load_game_context as set_owl_orchestrator_game_context, set_lua_socket as set_owl_lua_socket, send_owl_post_summary
# owl_post_db init deferred to first player handshake via PlayerContext

# Profiler for timing analysis (set DEV_MODE in utils/profiler.py)
from utils.profiler import Profiler
from utils.gpu_info import detect_gpu
_profiler = Profiler.get("chat_flow")

try:
    from input import text as input_capture
    INPUT_CAPTURE_AVAILABLE = True
except ImportError as e:
    print(f"[WARN] input.text module not available: {e}")
    INPUT_CAPTURE_AVAILABLE = False

try:
    from input import voice as stt_capture
    from input.voice import set_lua_socket as set_stt_lua_socket
    from services import stt as stt_service
    STT_AVAILABLE = True
except ImportError as e:
    print(f"[WARN] input.voice module not available: {e}")
    STT_AVAILABLE = False
    set_stt_lua_socket = None

try:
    from input import hotkeys as stop_capture
    STOP_CAPTURE_AVAILABLE = True
except ImportError as e:
    print(f"[WARN] input.hotkeys module not available: {e}")
    STOP_CAPTURE_AVAILABLE = False

try:
    from input import mode_hotkey
    MODE_HOTKEY_AVAILABLE = True
except ImportError as e:
    print(f"[WARN] input.mode_hotkey module not available: {e}")
    MODE_HOTKEY_AVAILABLE = False

try:
    from input import fpv_hotkey
    FPV_HOTKEY_AVAILABLE = True
except ImportError as e:
    print(f"[WARN] input.fpv_hotkey module not available: {e}")
    FPV_HOTKEY_AVAILABLE = False

try:
    from input import owlpost_hotkey, grimoire_hotkey
    from utils.overlay import BrowserOverlay, OverlayManager
    OWLPOST_HOTKEY_AVAILABLE = True
except ImportError as e:
    print(f"[WARN] input.owlpost_hotkey module not available: {e}")
    OWLPOST_HOTKEY_AVAILABLE = False

# ============================================
# Simple Cancellation System
# ============================================
# Separate from conv_state - just a timestamp-based flag
# Conversation epoch system - each conversation gets unique ID
# Workers capture their epoch at start and check if still current
_conversation_epoch = 0
_conversation_epoch_lock = threading.Lock()

def start_new_conversation():
    """Start a new conversation, invalidating all previous ones. Returns epoch ID."""
    global _conversation_epoch
    with _conversation_epoch_lock:
        _conversation_epoch += 1
        epoch = _conversation_epoch
    print(f"[Epoch] New conversation epoch: {epoch}")
    return epoch

def is_conversation_valid(epoch, log=False):
    """Check if the given epoch is still the current conversation.

    Args:
        epoch: The epoch to check
        log: If True, log when invalidated (use sparingly to avoid spam)
    """
    with _conversation_epoch_lock:
        current = _conversation_epoch
    if epoch != current:
        if log:
            print(f"[Epoch] Conversation {epoch} invalidated (current: {current})")
        return False
    return True

def get_current_epoch():
    """Get the current conversation epoch."""
    with _conversation_epoch_lock:
        return _conversation_epoch

# Legacy compatibility - these now use epoch system
_cancel_timestamp = 0

def request_cancel():
    """Signal cancellation - starts new epoch, invalidating current conversation."""
    global _cancel_timestamp
    _cancel_timestamp = time.time()
    start_new_conversation()  # This invalidates all workers checking their epoch

def is_cancelled(max_age=10):
    """Legacy check - prefer is_conversation_valid(epoch) in workers."""
    if _cancel_timestamp == 0:
        return False
    age = time.time() - _cancel_timestamp
    cancelled = age < max_age
    if cancelled:
        print(f"[Cancel] Check: cancelled (age={age:.1f}s)")
    return cancelled

def clear_cancel():
    """Clear cancellation timestamp (epoch is NOT cleared - workers stay invalid)."""
    global _cancel_timestamp
    if _cancel_timestamp > 0:
        print(f"[Cancel] Timestamp cleared (epoch unchanged)")
    _cancel_timestamp = 0

# ============================================
# Flask App
# ============================================
app = Flask(__name__)

# Server state
state = {
    "tts_active": False,
    "current_character": None,
    "last_response": None,
    "last_action": None,
}

_tts_activity_lock = threading.Lock()
_tts_activity_next_token = 0
_tts_activity_tokens = set()


def _begin_tts_activity():
    """Mark a TTS worker as active without clobbering overlapping work."""
    global _tts_activity_next_token
    with _tts_activity_lock:
        _tts_activity_next_token += 1
        token = _tts_activity_next_token
        _tts_activity_tokens.add(token)
        state["tts_active"] = True
        return token


def _end_tts_activity(token):
    """Release a TTS worker token and update aggregate activity state."""
    with _tts_activity_lock:
        _tts_activity_tokens.discard(token)
        state["tts_active"] = bool(_tts_activity_tokens)


def _is_tts_activity_active():
    with _tts_activity_lock:
        return bool(_tts_activity_tokens)

# ============================================
# Global Instances
# ============================================

# Global conversation state
conv_state = ConversationState()

# Temporary issue #3 workaround: keep Python and the in-game Lua client on the
# same known-safe port. Pre-release 5 publishes an auto-selected port, but its
# Lua client still hardcodes 8173 and never reads the published port file.
lua_socket = LuaSocketServer(port=int(os.getenv("SONORUS_SOCKET_PORT", "8420")))

# Wire up socket with external modules
if INPUT_CAPTURE_AVAILABLE:
    lua_socket.set_input_capture(input_capture)
lua_socket.set_conv_state(conv_state)

# Initialize playback coordinator
from audio.playback import init_coordinator
playback_coordinator = init_coordinator(lua_socket)

_FULL_GUARD_CONTEXT_GROUPS = ["position", "state", "player", "time", "zone", "npcs", "gear", "companion", "mission"]
_PLAYBACK_GUARD_CONTEXT_GROUPS = ["npcs", "player", "state", "companion"]
_unsolicited_generation_lock = threading.Lock()


def _request_guard_context(groups=None, timeout: float = 1.0):
    """Refresh context for guard checks with a small shared wrapper."""
    return lua_socket.request_context_refresh(groups=groups or _FULL_GUARD_CONTEXT_GROUPS, timeout=timeout)


def _is_follow_up_nudge_enabled() -> bool:
    return load_settings().get("conversation", {}).get("followup_nudge", True)


def _needs_narration_format_nudge(history_messages, conv_settings) -> bool:
    """Detect stale non-narration history after narration mode is enabled."""
    if not conv_settings.get('narration_enabled', False):
        return False
    recent_messages = [m for m in (history_messages or []) if (m.get('content') or '').strip()][-5:]
    if not recent_messages:
        return False
    return any('"' not in (m.get('content') or '') for m in recent_messages)


def _is_farewell_line_enabled() -> bool:
    return load_settings().get("conversation", {}).get("farewell_line", True)


def _filter_context_npcs(context):
    return filter_npcs_by_earshot(
        context.get("nearbyNpcs", []),
        player_in_stealth=context.get("inStealth", False),
        player_on_broom=context.get("isOnMount", False),
        companion_on_broom=context.get("companionIsOnBroom", False),
        companion_id=context.get("companionId"),
        companion_following=context.get("hasCompanion", False),
    )


def _validate_live_conversation_context(
    context,
    speaker_id=None,
    target_id=None,
    require_idle=False,
    require_no_playback=False,
):
    """Return (ok, reason, filtered_npcs) for current conversational guards."""
    if require_idle and conv_state.state != "idle":
        return False, "conversation_active", []
    if require_no_playback and lua_socket.playback_active:
        return False, "playback_active", []
    if context.get("isGamePaused"):
        return False, "game_paused", []
    if context.get("inCinematic"):
        return False, "in_cinematic", []
    if context.get("inCombat"):
        return False, "in_combat", []

    fresh_npcs = _filter_context_npcs(context)
    if speaker_id and not validate_speaker_in_nearby(speaker_id, fresh_npcs, load_localization):
        return False, "speaker_left", fresh_npcs
    if target_id and str(target_id).lower() != "player":
        if not validate_speaker_in_nearby(target_id, fresh_npcs, load_localization):
            return False, "target_left", fresh_npcs
    return True, "", fresh_npcs


def _abort_due_to_guard_failure(reason, speaker_id=None, target_id=None, speaker_name=None):
    """Reset to idle and return a standard response payload for guard failures."""
    if reason == "speaker_left":
        display_name = speaker_name or get_display_name(speaker_id)
        lua_socket.send_notification(f"{display_name} walked away")
        message = "Speaker left the area"
        status = "aborted"
    elif reason == "target_left":
        display_name = get_display_name(target_id)
        lua_socket.send_notification(f"{display_name} walked away")
        message = "Target left the area"
        status = "aborted"
    elif reason == "in_combat":
        lua_socket.send_notification("Cannot talk during combat")
        message = "In combat"
        status = "error"
    elif reason == "in_cinematic":
        message = "In cinematic"
        status = "error"
    elif reason == "game_paused":
        message = "Game paused"
        status = "error"
    elif reason == "playback_active":
        message = "Playback already active"
        status = "aborted"
    elif reason == "conversation_active":
        message = "Conversation already active"
        status = "aborted"
    else:
        message = reason or "Guard failed"
        status = "aborted"

    conv_state.state = "idle"
    conv_state.queue = []
    conv_state.turn_count = 0
    conv_state.interrupted = False
    lua_socket.send_conversation_state("idle")
    return {"success": False, "status": status, "message": message, "reason": reason}


def _validate_unsolicited_guard_context(
    context,
    *,
    speaker_id,
    target_id,
    require_companion_speaker=False,
    require_no_playback=True,
):
    """Shared fresh-context guard path for commentary/follow-up unsolicited turns."""
    ok, reason, fresh_npcs = _validate_live_conversation_context(
        context,
        speaker_id=speaker_id,
        target_id=target_id,
        require_idle=True,
        require_no_playback=require_no_playback,
    )
    if not ok:
        return False, reason, fresh_npcs

    if require_companion_speaker:
        if context.get("companionForcedWaiting"):
            return False, "companion_forced_waiting", fresh_npcs
        companion_id = context.get("companionId")
        if not context.get("hasCompanion") or not companion_id:
            return False, "no_companion", fresh_npcs
        if str(companion_id).lower() != str(speaker_id).lower():
            return False, "speaker_not_companion", fresh_npcs

    return True, "", fresh_npcs


def _try_begin_unsolicited_generation(kind: str) -> bool:
    if not _unsolicited_generation_lock.acquire(blocking=False):
        print(f"[{kind}] Aborted - another unsolicited turn is already generating")
        return False
    return True


def _end_unsolicited_generation():
    if _unsolicited_generation_lock.locked():
        _unsolicited_generation_lock.release()


class QuestionFollowUpCoordinator:
    """Schedules a delayed single-line follow-up after a completed final question."""

    def __init__(self):
        self._lock = threading.Lock()
        self._timer = None
        self._generation = 0
        self._pending_speaker_id = None
        self._pending_speakers = None  # Full speaker set from conversation that spawned the follow-up

    def cancel_pending(self, reason: str = "unknown"):
        """Cancel any pending follow-up timer. Sends conversation_finished if a follow-up was pending."""
        speakers_to_finish = None
        had_timer = False
        with self._lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None
                speakers_to_finish = self._pending_speakers
                self._pending_speaker_id = None
                self._pending_speakers = None
                had_timer = True
            self._generation += 1
        if had_timer:
            print(f"[FollowUp] Cancelled pending timer ({reason})")
        if speakers_to_finish:
            lua_socket.send_conversation_finished(speakers_to_finish)

    def is_generation_current(self, generation: int) -> bool:
        with self._lock:
            return generation == self._generation

    def get_pending_speaker_id(self):
        with self._lock:
            return self._pending_speaker_id

    def schedule_if_eligible(self, entry: dict):
        if not entry:
            return False
        if conv_state.prompt_mode:
            return False
        if not _is_follow_up_nudge_enabled():
            return False

        speaker_id = entry.get("voiceName") or entry.get("speakerId")
        target_id = str(entry.get("targetId") or "")
        text = (entry.get("text") or "").strip()
        if not speaker_id or not text or "?" not in text:
            return False
        if target_id.lower() != "player":
            return False
        if entry.get("followUp"):
            return False
        if entry.get("goodbye"):
            return False
        if entry.get("interrupted"):
            return False
        if not entry.get("_playback_completed", True):
            return False

        if not run_rhetorical_question_classifier(text):
            print(f"[FollowUp] Skipped rhetorical question from {speaker_id}")
            return False

        # Re-check live state after classifier latency
        if conv_state.prompt_mode:
            return False
        if conv_state.state != "idle":
            return False

        candidate = {
            "speaker_id": speaker_id,
            "target_id": "player",
            "last_question_text": text,
        }

        with self._lock:
            if self._timer:
                self._timer.cancel()
            self._generation += 1
            generation = self._generation
            self._pending_speaker_id = speaker_id
            self._pending_speakers = set(conv_state.conversation_speakers)
            self._timer = threading.Timer(
                NPC_QUESTION_FOLLOW_UP_TIMEOUT_SECONDS,
                self._fire,
                args=(generation, candidate),
            )
            self._timer.daemon = True
            self._timer.start()
        print(f"[FollowUp] Scheduled for {speaker_id} in {NPC_QUESTION_FOLLOW_UP_TIMEOUT_SECONDS:.1f}s")
        return True

    def _fire(self, generation: int, candidate: dict):
        with self._lock:
            if generation != self._generation:
                return
            self._timer = None
            self._pending_speaker_id = None
            finished_speakers = self._pending_speakers or {candidate["speaker_id"]}
            self._pending_speakers = None

        if not _try_begin_unsolicited_generation("FollowUp"):
            # Could not acquire lock — conversation is still finished
            lua_socket.send_conversation_finished(finished_speakers)
            return

        try:
            speaker_id = candidate["speaker_id"]
            if not _is_follow_up_nudge_enabled():
                print(f"[FollowUp] Skipped for {speaker_id} (disabled in settings)")
                return
            context = _request_guard_context(groups=_FULL_GUARD_CONTEXT_GROUPS, timeout=1.0)
            ok, reason, _ = _validate_unsolicited_guard_context(
                context,
                speaker_id=speaker_id,
                target_id="player",
            )
            if not ok:
                print(f"[FollowUp] Aborted before generation ({reason})")
                return
            if not self.is_generation_current(generation):
                print("[FollowUp] Aborted before generation (superseded)")
                return

            def before_play_guard():
                if not self.is_generation_current(generation):
                    print("[FollowUp] Aborted before playback (superseded)")
                    return False
                playback_context = _request_guard_context(groups=_PLAYBACK_GUARD_CONTEXT_GROUPS, timeout=1.0)
                playback_ok, playback_reason, _ = _validate_unsolicited_guard_context(
                    playback_context,
                    speaker_id=speaker_id,
                    target_id="player",
                    require_no_playback=False,
                )
                if not playback_ok:
                    print(f"[FollowUp] Aborted before playback ({playback_reason})")
                    return False
                return True

            played = run_follow_up_turn(
                speaker_id,
                "player",
                context,
                candidate["last_question_text"],
                lua_socket=lua_socket,
                conv_state=conv_state,
                start_new_conversation=start_new_conversation,
                clear_cancel=clear_cancel,
                is_conversation_valid=is_conversation_valid,
                can_use_streaming_tts=_can_use_streaming_tts,
                run_streaming_tts_async=run_streaming_tts_async,
                tts_service=tts,
                load_settings_func=load_settings,
                stream_sentences_func=stream_sentences,
                play_completed_response_streaming_func=start_completed_response_streaming_playback,
                run_tts_async=run_tts_async,
                tts_available=TTS_AVAILABLE,
                before_play_guard=before_play_guard,
            )
            print(f"[FollowUp] Played={played} speaker={speaker_id}")
        finally:
            _end_unsolicited_generation()
            # Follow-up done (played or aborted) — conversation is truly finished
            lua_socket.send_conversation_finished(finished_speakers)


question_follow_up = QuestionFollowUpCoordinator()

MAX_LINGER_GOODBYE_SPEAKERS = 3
_LINGER_GOODBYE_STREAM_END = object()


def _filter_pending_history_entries(log_prefix: str, discard_predicate, discard_label: str):
    if not conv_state.pending_history_entries:
        return

    retained = []
    discarded = 0
    for entry in conv_state.pending_history_entries:
        if discard_predicate(entry):
            topic_update_id = entry.get("_topic_update_id")
            if topic_update_id:
                conversation_topics.discard(topic_update_id)
            discarded += 1
            continue
        retained.append(entry)
    if discarded:
        print(f"{log_prefix} Discarded {discarded} pending {discard_label} entries")
    conv_state.pending_history_entries = retained


def _retain_full_playback_pending_entries(log_prefix: str):
    _filter_pending_history_entries(
        log_prefix,
        lambda entry: entry.get("_require_full_playback") and not entry.get("_playback_completed"),
        "full-playback",
    )


def _discard_pending_goodbye_entries(log_prefix: str):
    _filter_pending_history_entries(
        log_prefix,
        lambda entry: entry.get("goodbye"),
        "goodbye",
    )


def _commit_pending_history_if_any(log_prefix: str):
    if not conv_state.pending_history_entries:
        return 0, []
    count, committed_entries = conv_state.commit_pending_history()
    for entry in committed_entries:
        topic_update_id = entry.get("_topic_update_id")
        if not topic_update_id:
            continue
        if entry.get("_playback_completed", True) and not entry.get("interrupted"):
            source_ids = entry.get("sourceEntryIds") or []
            source_entry_id = source_ids[-1] if source_ids else None
            conversation_topics.mark_committed(topic_update_id, source_entry_id)
        else:
            conversation_topics.discard(topic_update_id)
    print(f"{log_prefix} Committed {count} history entries")
    return count, committed_entries


def _discard_pending_history_if_any(log_prefix: str, reason: str) -> int:
    if not conv_state.pending_history_entries:
        return 0
    count = len(conv_state.pending_history_entries)
    for entry in conv_state.pending_history_entries:
        topic_update_id = entry.get("_topic_update_id")
        if topic_update_id:
            conversation_topics.discard(topic_update_id)
    print(f"{log_prefix} Discarded {count} pending entries ({reason})")
    conv_state.pending_history_entries = []
    return count


def _create_unsolicited_history_entry(
    speaker_id: str,
    target_id: str,
    response: str,
    game_context: dict,
    fresh_npcs,
    *,
    require_full_playback: bool = False,
    extra_fields: dict | None = None,
):
    player_name = game_context.get("playerName", "Unknown")
    speaker_name = get_display_name(speaker_id)
    target_name = get_display_name(target_id) if target_id.lower() != "player" else player_name
    y, m, d = game_context.get("year"), game_context.get("month"), game_context.get("day")
    game_date = f"{y}/{int(m):02d}/{int(d):02d}" if y and m and d else game_context.get("dateFormatted", "")
    entry = {
        "timestamp": int(time.time()),
        "gameTime": game_context.get("timeFormatted", "") or game_context.get("time", ""),
        "gameDate": game_date,
        "speaker": speaker_name,
        "voiceName": speaker_id,
        "target": target_name,
        "targetId": "Player" if target_id and target_id.lower() == "player" else target_id,
        "text": response,
        "isAIResponse": True,
        "isPlayer": False,
        "type": "dialogue",
        "earshot": get_earshot_witnesses(fresh_npcs or [], speaker_id),
        "_playback_completed": False,
    }
    if require_full_playback:
        entry["_require_full_playback"] = True
    if extra_fields:
        entry.update(extra_fields)
    return entry


def _create_linger_goodbye_history_entry(
    speaker_id: str,
    target_id: str,
    response: str,
    game_context: dict,
    fresh_npcs,
):
    return _create_unsolicited_history_entry(
        speaker_id,
        target_id,
        response,
        game_context,
        fresh_npcs,
        require_full_playback=True,
        extra_fields={
            "eventTrigger": "linger_goodbye",
            "eventTopic": "linger_expiry",
            "goodbye": True,
            "goodbyeReason": "linger_expiry",
        },
    )


class LingerGoodbyeCoordinator:
    """Coordinates terminal group goodbye lines for expired linger batches."""

    def __init__(self):
        self._lock = threading.Lock()
        self._generation = 0
        self._linger_generation = None
        self._claimed = False
        self._playback_started = False

    def is_generation_current(self, generation: int) -> bool:
        with self._lock:
            return generation == self._generation

    def _begin_generation(self, linger_generation: int):
        with self._lock:
            self._generation += 1
            generation = self._generation
            self._linger_generation = linger_generation
            self._claimed = False
            self._playback_started = False
            return generation

    def _claim_if_current(self, generation: int) -> bool:
        with self._lock:
            if generation != self._generation:
                return False
            self._claimed = True
            return True

    def _mark_playback_started(self, generation: int) -> bool:
        with self._lock:
            if generation != self._generation:
                return False
            self._playback_started = True
            return True

    def _clear_if_current(self, generation: int):
        with self._lock:
            if generation != self._generation:
                return
            self._linger_generation = None
            self._claimed = False
            self._playback_started = False

    def _abort_if_current(self, generation: int, reason: str):
        linger_generation = None
        with self._lock:
            if generation != self._generation:
                return
            if self._linger_generation is not None and not self._playback_started:
                linger_generation = self._linger_generation
            self._linger_generation = None
            self._claimed = False
            self._playback_started = False
        if linger_generation is not None:
            lua_socket.send_linger_goodbye_abort(linger_generation, reason)

    def cancel_pending(self, reason: str = "unknown"):
        linger_generation = None
        with self._lock:
            if self._linger_generation is not None and not self._playback_started:
                linger_generation = self._linger_generation
            self._generation += 1
            self._linger_generation = None
            self._claimed = False
            self._playback_started = False
        if linger_generation is not None:
            print(f"[LingerGoodbye] Cancelled pending batch ({reason})")
            lua_socket.send_linger_goodbye_abort(linger_generation, reason)

    def _select_speakers(self, batch_speaker_ids, context, fresh_npcs):
        batch_ids = []
        seen = set()
        for speaker_id in batch_speaker_ids or []:
            if not isinstance(speaker_id, str):
                continue
            norm = speaker_id.lower()
            if norm in seen:
                continue
            seen.add(norm)
            batch_ids.append(speaker_id)

        if not batch_ids:
            return []

        companion_id = str(context.get("companionId") or "")
        follower_ids = {str(fid).lower() for fid in (context.get("followers") or []) if isinstance(fid, str)}
        ordered = []
        batch_lookup = {sid.lower(): sid for sid in batch_ids}

        for speaker_id in reversed(conv_state.conversation_turn_speakers or []):
            resolved = batch_lookup.get(str(speaker_id).lower())
            if resolved and resolved not in ordered:
                ordered.append(resolved)
        for speaker_id in batch_ids:
            if speaker_id not in ordered:
                ordered.append(speaker_id)

        selected = []
        for speaker_id in ordered:
            speaker_lower = speaker_id.lower()
            if companion_id and speaker_lower == companion_id.lower():
                continue
            if speaker_lower in follower_ids:
                continue
            if not validate_speaker_in_nearby(speaker_id, fresh_npcs, load_localization):
                continue
            selected.append(speaker_id)
            if len(selected) >= MAX_LINGER_GOODBYE_SPEAKERS:
                break
        return selected

    def _build_allowed_target_ids(self, selected_speaker_ids, context, fresh_npcs):
        allowed = ["player"]
        seen = {"player"}

        def add_target(candidate_id):
            if not candidate_id or not isinstance(candidate_id, str):
                return
            lowered = candidate_id.lower()
            if lowered in seen:
                return
            if validate_speaker_in_nearby(candidate_id, fresh_npcs, load_localization):
                allowed.append(candidate_id)
                seen.add(lowered)

        for speaker_id in selected_speaker_ids:
            add_target(speaker_id)
        for speaker_id in conv_state.conversation_turn_speakers or []:
            add_target(speaker_id)
        add_target(str(context.get("companionId") or ""))
        for follower_id in context.get("followers") or []:
            add_target(follower_id)
        return allowed

    def _build_default_target_ids(self, selected_speaker_ids, context, fresh_npcs):
        default_targets = {}
        player_name = str(context.get("playerName") or "")
        companion_id = str(context.get("companionId") or "")
        follower_ids = {str(fid).lower() for fid in (context.get("followers") or []) if isinstance(fid, str)}

        fallback_target = "player" if player_name else None
        if not fallback_target:
            for speaker_id in reversed(conv_state.conversation_turn_speakers or []):
                lowered = str(speaker_id).lower()
                if lowered in follower_ids:
                    continue
                if companion_id and lowered == companion_id.lower():
                    continue
                if lowered in {sid.lower() for sid in selected_speaker_ids}:
                    continue
                if validate_speaker_in_nearby(speaker_id, fresh_npcs, load_localization):
                    fallback_target = speaker_id
                    break
        if not fallback_target:
            fallback_target = "player"

        for speaker_id in selected_speaker_ids:
            default_targets[speaker_id] = "player" if player_name else fallback_target
        return default_targets

    def _normalize_target_id(self, raw_target_id, allowed_target_ids, fresh_npcs, player_name: str):
        target_id = str(raw_target_id or "player").strip()
        if not target_id:
            return "player"
        if target_id.lower() == "player":
            return "player"
        if player_name and target_id.lower().replace(" ", "") == player_name.lower().replace(" ", ""):
            return "player"
        if " " in target_id:
            resolved = find_npc_id_by_name(target_id, fresh_npcs)
            if resolved:
                target_id = resolved
        if target_id.lower() not in {tid.lower() for tid in allowed_target_ids}:
            return "player"
        if not validate_speaker_in_nearby(target_id, fresh_npcs, load_localization):
            return "player"
        return target_id

    def _abort_prepared_turn(self, prepared: dict | None):
        if not prepared:
            return
        pre_buffer = prepared.get("pre_buffer")
        if pre_buffer:
            pre_buffer.abort()

    def _wait_for_prepare_completion(self, generation: int, epoch: int, prepared: dict | None, timeout: float = 60.0) -> bool:
        if not prepared:
            return True
        event = prepared.get("download_complete_event")
        if not event:
            return True

        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._is_sequence_stale(generation, epoch):
                return False
            if event.wait(timeout=0.1):
                return True

        print(f"[LingerGoodbye] Prepare completion timeout for {prepared.get('speaker_id')}")
        return False

    def _enqueue_streamed_turns(
        self,
        generation: int,
        turn_queue: queue.Queue,
        stop_event: threading.Event,
        selected_speaker_ids,
        context: dict,
        conversation_order,
        departing_ids,
        default_target_ids,
        allowed_target_ids,
    ):
        try:
            for parsed in stream_linger_goodbye_lines(
                selected_speaker_ids,
                context,
                conversation_speaker_order=conversation_order,
                departing_ids=departing_ids,
                allowed_target_ids=allowed_target_ids,
            ):
                if stop_event.is_set() or not self.is_generation_current(generation):
                    break
                speaker_id = parsed.get("speaker_id")
                text = str(parsed.get("text") or "").strip()
                if not speaker_id or not text:
                    continue
                turn_queue.put({
                    "speaker_id": speaker_id,
                    "target_id": parsed.get("target_id") or default_target_ids.get(speaker_id, "player"),
                    "text": text,
                })
        except Exception as e:
            print(f"[LingerGoodbye] Stream worker error: {e}")
        finally:
            turn_queue.put(_LINGER_GOODBYE_STREAM_END)

    def _next_streamed_turn(self, generation: int, epoch: int, turn_queue: queue.Queue, stop_event: threading.Event):
        while True:
            if stop_event.is_set() or self._is_sequence_stale(generation, epoch):
                return _LINGER_GOODBYE_STREAM_END
            try:
                return turn_queue.get(timeout=0.25)
            except queue.Empty:
                continue

    def _is_sequence_stale(self, generation: int, epoch: int | None = None) -> bool:
        if not self.is_generation_current(generation):
            return True
        if epoch is not None and not is_conversation_valid(epoch):
            return True
        return False

    def _play_turn_legacy(self, generation: int, epoch: int, turn: dict, allowed_target_ids, played_speakers: set) -> bool:
        speaker_id = turn["speaker_id"]
        response = turn["text"].strip()
        if not response:
            return False

        player_name = (lua_socket.get_game_context() or {}).get("playerName", "Player")
        pre_context = _request_guard_context(groups=_PLAYBACK_GUARD_CONTEXT_GROUPS, timeout=1.0)
        if not self.is_generation_current(generation):
            print("[LingerGoodbye] Turn aborted before validation (superseded)")
            return False

        ok, reason, fresh_npcs = _validate_live_conversation_context(
            pre_context,
            speaker_id=speaker_id,
            target_id=None,
            require_no_playback=True,
        )
        if not ok:
            print(f"[LingerGoodbye] Skipping {speaker_id} before play_turn ({reason})")
            return False

        target_id = self._normalize_target_id(turn.get("target_id"), allowed_target_ids, fresh_npcs, player_name)
        speaker_name = get_display_name(speaker_id)
        target_name = get_display_name(target_id) if target_id.lower() != "player" else player_name
        pending_entry = _create_linger_goodbye_history_entry(speaker_id, target_id, response, pre_context, fresh_npcs)

        lua_socket.send_lock_npc(speaker_id, target_id)
        if target_id.lower() != "player":
            lua_socket.send_lock_npc(target_id, speaker_id)

        conv_state.add_to_queue(speaker_name, target_name, response, speaker_id=speaker_id)
        conv_state.turn_count += 1

        def before_play():
            if not self.is_generation_current(generation):
                return {"success": False, "status": "cancelled", "message": "Goodbye superseded"}
            playback_context = _request_guard_context(groups=_PLAYBACK_GUARD_CONTEXT_GROUPS, timeout=1.0)
            playback_ok, playback_reason, _ = _validate_live_conversation_context(
                playback_context,
                speaker_id=speaker_id,
                target_id=target_id,
                require_no_playback=True,
            )
            if not playback_ok:
                return {"success": False, "status": "aborted", "message": playback_reason}
            return None

        playback_result = start_completed_response_streaming_playback(
            response,
            speaker_id,
            speaker_name,
            target_id,
            conv_state.turn_count,
            epoch,
            raw_response=response,
            pending_history_entry=pending_entry,
            before_play_callback=before_play,
        )
        turn_started = False

        if playback_result.get("success"):
            turn_started = True
            self._mark_playback_started(generation)
        elif playback_result.get("status"):
            print(f"[LingerGoodbye] Skipping {speaker_id} ({playback_result.get('message', playback_result.get('status'))})")
            return False
        elif playback_result.get("fallback_required"):
            abort_result = before_play()
            if abort_result:
                print(f"[LingerGoodbye] Fallback aborted for {speaker_id} ({abort_result.get('message')})")
                return False
            subtitle_seed_text = remove_unpaired_double_quotes(response)
            turn_result = lua_socket.send_play_turn(
                speaker_id=speaker_id,
                display_name=speaker_name,
                text=subtitle_seed_text,
                turn_index=conv_state.turn_count,
                target_id=target_id,
                streaming_subtitles=False,
            )
            if not turn_result.get("success"):
                print(f"[LingerGoodbye] play_turn failed for {speaker_id}")
                return False
            conv_state.add_pending_history(pending_entry)
            turn_started = True
            self._mark_playback_started(generation)
            if TTS_AVAILABLE and response.strip():
                tts_thread = threading.Thread(
                    target=run_tts_async,
                    args=(response, speaker_id, turn_result.get("positions"), turn_result.get("turn_id"), epoch, pending_entry),
                    daemon=True,
                )
                tts_thread.start()
            else:
                pending_entry["_playback_completed"] = True
        else:
            print(f"[LingerGoodbye] Playback failed for {speaker_id}: {playback_result.get('error')}")
            return False

        if lua_socket.pipeline_active:
            lua_socket.wait_for_pipeline_stop(timeout=60.0)

        if not is_conversation_valid(epoch):
            _discard_pending_goodbye_entries("[LingerGoodbye]")
            return False

        _retain_full_playback_pending_entries("[LingerGoodbye]")
        if is_conversation_valid(epoch) and conv_state.pending_history_entries:
            _, committed_entries = _commit_pending_history_if_any("[LingerGoodbye]")
            if pending_entry.get("_playback_completed"):
                played_speakers.update(
                    entry.get("voiceName")
                    for entry in committed_entries
                    if entry.get("goodbye") and entry.get("voiceName")
                )
        elif conv_state.pending_history_entries:
            _discard_pending_goodbye_entries("[LingerGoodbye]")

        if pending_entry.get("_playback_completed") or (turn_started and not TTS_AVAILABLE):
            return True
        return False

    def _prepare_buffered_turn(self, generation: int, epoch: int, turn: dict, allowed_target_ids, *, allow_overlap: bool) -> dict | None:
        speaker_id = turn["speaker_id"]
        response = turn["text"].strip()
        if not response:
            return None

        player_name = (lua_socket.get_game_context() or {}).get("playerName", "Player")
        pre_context = _request_guard_context(groups=_PLAYBACK_GUARD_CONTEXT_GROUPS, timeout=1.0)
        if self._is_sequence_stale(generation, epoch):
            print("[LingerGoodbye] Turn preparation aborted before validation (stale)")
            return None

        ok, reason, fresh_npcs = _validate_live_conversation_context(
            pre_context,
            speaker_id=speaker_id,
            target_id=None,
            require_no_playback=not allow_overlap,
        )
        if not ok:
            print(f"[LingerGoodbye] Skipping {speaker_id} before buffering ({reason})")
            return None

        target_id = self._normalize_target_id(turn.get("target_id"), allowed_target_ids, fresh_npcs, player_name)
        speaker_name = get_display_name(speaker_id)
        target_name = get_display_name(target_id) if target_id.lower() != "player" else player_name
        pending_entry = _create_linger_goodbye_history_entry(speaker_id, target_id, response, pre_context, fresh_npcs)

        lua_socket.send_lock_npc(speaker_id, target_id)
        if target_id.lower() != "player":
            lua_socket.send_lock_npc(target_id, speaker_id)

        conv_state.add_to_queue(speaker_name, target_name, response, speaker_id=speaker_id)
        conv_state.turn_count += 1

        sentence_subtitles = load_settings().get('conversation', {}).get('sentence_subtitles', True)
        subtitle_seed_text = remove_unpaired_double_quotes(response)
        turn_result = lua_socket.send_play_turn(
            speaker_id=speaker_id,
            display_name=speaker_name,
            text=subtitle_seed_text,
            turn_index=conv_state.turn_count,
            target_id=target_id,
            streaming_subtitles=sentence_subtitles,
        )
        if not turn_result.get("success"):
            print(f"[LingerGoodbye] play_turn failed for {speaker_id}")
            return None

        pre_buffer = PreBuffer()
        download_complete_event = threading.Event()
        pre_buffer.start_buffering(
            speaker_name,
            speaker_id,
            target_id,
            response,
            positions=turn_result.get("positions"),
            turn_id=turn_result.get("turn_id"),
            reverb_auxbus=turn_result.get("reverb_auxbus"),
            reverb_send=turn_result.get("reverb_send", 1.0),
        )

        def buffer_tts():
            try:
                if self._is_sequence_stale(generation, epoch) or pre_buffer.abort_flag:
                    return

                ready_signaled = [False]

                def on_buffer_ready(tts_stream, word_timings, visemes, sentence_boundaries=None):
                    if not ready_signaled[0]:
                        ready_signaled[0] = True
                        pre_buffer.mark_ready(
                            tts_stream,
                            word_timings,
                            visemes,
                            sentence_boundaries=sentence_boundaries,
                        )

                def tts_abort_check():
                    return self._is_sequence_stale(generation, epoch) or pre_buffer.abort_flag

                result = tts.prepare_tts(
                    response,
                    speaker_id,
                    abort_check=tts_abort_check,
                    on_ready=on_buffer_ready,
                    lua_socket=lua_socket,
                    history_entry=pending_entry,
                )

                if result and not ready_signaled[0]:
                    tts_stream, word_timings, visemes = result[:3]
                    sentence_boundaries = result[3] if len(result) > 3 else []
                    pre_buffer.mark_ready(
                        tts_stream,
                        word_timings,
                        visemes,
                        sentence_boundaries=sentence_boundaries,
                    )
                elif not result:
                    if tts_abort_check():
                        print(f"[LingerGoodbye] Buffer preparation aborted for {speaker_id}")
                    else:
                        print(f"[LingerGoodbye] Buffer preparation failed for {speaker_id}")
            finally:
                download_complete_event.set()

        threading.Thread(target=buffer_tts, daemon=True).start()

        return {
            "speaker_id": speaker_id,
            "speaker_name": speaker_name,
            "target_id": target_id,
            "target_name": target_name,
            "pending_entry": pending_entry,
            "pre_buffer": pre_buffer,
            "download_complete_event": download_complete_event,
        }

    def _play_prepared_turn(self, generation: int, epoch: int, prepared: dict, played_speakers: set) -> bool:
        speaker_id = prepared["speaker_id"]
        target_id = prepared["target_id"]
        pre_buffer = prepared["pre_buffer"]

        if not pre_buffer.ready_event.wait(timeout=15.0):
            print(f"[LingerGoodbye] Buffer timeout for {speaker_id}")
            pre_buffer.abort()
            return False

        if self._is_sequence_stale(generation, epoch):
            pre_buffer.abort()
            return False

        playback_context = _request_guard_context(groups=_PLAYBACK_GUARD_CONTEXT_GROUPS, timeout=1.0)
        playback_ok, playback_reason, _ = _validate_live_conversation_context(
            playback_context,
            speaker_id=speaker_id,
            target_id=target_id,
            require_no_playback=True,
        )
        if not playback_ok:
            print(f"[LingerGoodbye] Skipping {speaker_id} before buffered playback ({playback_reason})")
            pre_buffer.abort()
            return False

        buffered = pre_buffer.consume()
        if not buffered:
            print(f"[LingerGoodbye] Buffer empty for {speaker_id}")
            return False
        buffered["history_entry"] = prepared["pending_entry"]

        if self._is_sequence_stale(generation, epoch):
            if buffered.get("tts_stream"):
                buffered["tts_stream"].clean_up()
            return False

        self._mark_playback_started(generation)
        play_prebuffered_response(buffered, blocking=False, epoch=epoch)

        if self._is_sequence_stale(generation, epoch):
            return False

        conv_state.add_pending_history(prepared["pending_entry"])
        print(f"[LingerGoodbye] Turn {conv_state.turn_count}: {prepared['speaker_name']}")
        return True

    def _run_prebuffered_sequence(self, generation: int, epoch: int, turn_queue: queue.Queue, stop_event: threading.Event, allowed_target_ids, played_speakers: set):
        current_prepared = None
        stream_exhausted = False

        while True:
            if current_prepared is None:
                while not stream_exhausted and current_prepared is None:
                    next_turn = self._next_streamed_turn(generation, epoch, turn_queue, stop_event)
                    if next_turn is _LINGER_GOODBYE_STREAM_END:
                        stream_exhausted = True
                        break
                    current_prepared = self._prepare_buffered_turn(
                        generation,
                        epoch,
                        next_turn,
                        allowed_target_ids,
                        allow_overlap=False,
                    )
                if current_prepared is None:
                    break
                if not self._play_prepared_turn(generation, epoch, current_prepared, played_speakers):
                    self._abort_prepared_turn(current_prepared)
                    current_prepared = None
                    continue

            next_prepared = None
            can_prepare_next = self._wait_for_prepare_completion(generation, epoch, current_prepared)
            if not can_prepare_next:
                stream_exhausted = True
            while not stream_exhausted and next_prepared is None and self.is_generation_current(generation):
                next_turn = self._next_streamed_turn(generation, epoch, turn_queue, stop_event)
                if next_turn is _LINGER_GOODBYE_STREAM_END:
                    stream_exhausted = True
                    break
                next_prepared = self._prepare_buffered_turn(
                    generation,
                    epoch,
                    next_turn,
                    allowed_target_ids,
                    allow_overlap=True,
                )

            if lua_socket.pipeline_active:
                lua_socket.wait_for_pipeline_stop(timeout=60.0)

            if not is_conversation_valid(epoch):
                self._abort_prepared_turn(next_prepared)
                _discard_pending_goodbye_entries("[LingerGoodbye]")
                print("[LingerGoodbye] Sequence cleanup skipped (stale epoch)")
                return

            _retain_full_playback_pending_entries("[LingerGoodbye]")
            if conv_state.pending_history_entries:
                _, committed_entries = _commit_pending_history_if_any("[LingerGoodbye]")
                played_speakers.update(
                    entry.get("voiceName")
                    for entry in committed_entries
                    if entry.get("goodbye") and entry.get("voiceName")
                )

            if not self.is_generation_current(generation):
                self._abort_prepared_turn(next_prepared)
                current_prepared = None
                stream_exhausted = True
                continue

            current_prepared = next_prepared
            if current_prepared and not self._play_prepared_turn(generation, epoch, current_prepared, played_speakers):
                self._abort_prepared_turn(current_prepared)
                current_prepared = None

    def _run_sequence(self, generation: int, turn_queue: queue.Queue, stop_event: threading.Event, allowed_target_ids):
        if not self.is_generation_current(generation):
            return set()

        epoch = start_new_conversation()
        clear_cancel()
        conv_state.reset()
        conv_state.state = "playing"
        conv_state.turn_count = 0
        lua_socket.send_conversation_state("playing")

        played_speakers = set()
        use_prebuffer = bool(TTS_AVAILABLE and getattr(tts, "prepare_tts", None))
        if use_prebuffer:
            self._run_prebuffered_sequence(generation, epoch, turn_queue, stop_event, allowed_target_ids, played_speakers)
        else:
            while True:
                turn = self._next_streamed_turn(generation, epoch, turn_queue, stop_event)
                if turn is _LINGER_GOODBYE_STREAM_END:
                    break
                if not self.is_generation_current(generation):
                    print("[LingerGoodbye] Sequence aborted mid-run (superseded)")
                    break
                self._play_turn_legacy(generation, epoch, turn, allowed_target_ids, played_speakers)

        if lua_socket.pipeline_active:
            lua_socket.wait_for_pipeline_stop(timeout=60.0)

        if not is_conversation_valid(epoch):
            _discard_pending_goodbye_entries("[LingerGoodbye]")
            print("[LingerGoodbye] Sequence cleanup skipped (stale epoch)")
            return played_speakers

        conv_state.state = "idle"
        conv_state.interrupted = False
        lua_socket.send_conversation_state("idle", end_behavior="release_all")
        clear_cancel()
        if played_speakers:
            lua_socket.send_conversation_finished(played_speakers)
        conv_state.reset()
        return played_speakers

    def handle_due_event(self, data: dict):
        linger_generation = int(data.get("generation") or -1)
        batch_speaker_ids = data.get("speaker_ids") or []
        if linger_generation < 0 or not batch_speaker_ids:
            return
        if not _is_farewell_line_enabled():
            print("[LingerGoodbye] Skipped expired linger batch (disabled in settings)")
            lua_socket.send_linger_goodbye_abort(linger_generation, "disabled")
            return

        generation = self._begin_generation(linger_generation)
        if not _try_begin_unsolicited_generation("LingerGoodbye"):
            self._abort_if_current(generation, "lock_busy")
            return

        stop_event = None
        try:
            context = _request_guard_context(groups=_FULL_GUARD_CONTEXT_GROUPS, timeout=1.0)
            ok, reason, fresh_npcs = _validate_live_conversation_context(
                context,
                require_idle=True,
                require_no_playback=True,
            )
            if not ok:
                print(f"[LingerGoodbye] Aborted before claim ({reason})")
                self._abort_if_current(generation, reason)
                return
            if not self.is_generation_current(generation):
                self._abort_if_current(generation, "superseded")
                return

            selected_speakers = self._select_speakers(batch_speaker_ids, context, fresh_npcs)
            if not selected_speakers:
                print("[LingerGoodbye] No eligible speakers in expired linger batch")
                self._abort_if_current(generation, "no_eligible_speakers")
                return

            if not lua_socket.send_linger_goodbye_claim(linger_generation, selected_speakers):
                print("[LingerGoodbye] Failed to claim linger goodbye batch")
                self._abort_if_current(generation, "claim_send_failed")
                return
            if not self._claim_if_current(generation):
                self._abort_if_current(generation, "superseded")
                return

            allowed_target_ids = self._build_allowed_target_ids(selected_speakers, context, fresh_npcs)
            default_target_ids = self._build_default_target_ids(selected_speakers, context, fresh_npcs)
            conversation_order = list(conv_state.conversation_turn_speakers or [])
            stop_event = threading.Event()
            turn_queue = queue.Queue()
            stream_thread = threading.Thread(
                target=self._enqueue_streamed_turns,
                args=(
                    generation,
                    turn_queue,
                    stop_event,
                    selected_speakers,
                    context,
                    conversation_order,
                    list(batch_speaker_ids),
                    default_target_ids,
                    allowed_target_ids,
                ),
                daemon=True,
            )
            stream_thread.start()

            played_speakers = self._run_sequence(generation, turn_queue, stop_event, allowed_target_ids)
            stop_event.set()
            if not played_speakers:
                self._abort_if_current(generation, "no_turns_played")
                return
            self._clear_if_current(generation)
        finally:
            if stop_event is not None:
                stop_event.set()
            _end_unsolicited_generation()


linger_goodbye = LingerGoodbyeCoordinator()

# Attention meter stare lock: prevents NPCs from endlessly reacting to staring
# Key: voice_id, Value: True (locked until player speaks to them)
_attention_stare_locks = {}


def _should_suppress_attention_for_follow_up(voice_id: str, dialogue_history: list) -> bool:
    """Return True when an unanswered follow-up should block attention continuation."""
    if not voice_id or not dialogue_history:
        return False

    voice_lower = voice_id.lower()
    for entry in reversed(dialogue_history):
        entry_voice = str(entry.get("voiceName") or "").lower()
        target_id = str(entry.get("targetId") or "").lower()
        is_player = bool(entry.get("isPlayer"))

        involves_npc = entry_voice == voice_lower or (is_player and target_id == voice_lower)
        if not involves_npc:
            continue

        # Once the player has actually engaged this NPC again, attention can resume normally.
        if is_player and target_id == voice_lower:
            return False

        # If the NPC's latest relevant line was a follow-up, eye contact alone should not count.
        if entry_voice == voice_lower and entry.get("followUp"):
            return True

        return False

    return False


def handle_attention_threshold(event_data):
    """Handle attention:threshold event from Lua gaze accumulator."""
    voice_id = event_data.get("voiceId", "?")
    print(f"[Attention] Received threshold for {voice_id}")

    settings = load_settings()
    if not settings.get("conversation", {}).get("attention_meter_enabled", True):
        print(f"[Attention] Skipped {voice_id} - attention_meter_enabled is off in settings")
        return

    voice_id = event_data.get("voiceId")
    if not voice_id:
        print("[Attention] Skipped - no voiceId in event data")
        return

    def pending_follow_up_blocks_attention(log_prefix: str) -> bool:
        pending_follow_up_speaker_id = question_follow_up.get_pending_speaker_id()
        if not pending_follow_up_speaker_id:
            return False
        print(
            f"[Attention] Suppressed for {voice_id} "
            f"(pending follow-up from {pending_follow_up_speaker_id}, {log_prefix})"
        )
        return True

    if pending_follow_up_blocks_attention("before_generation"):
        return

    if not _try_begin_unsolicited_generation("Attention"):
        return

    try:
        context = _request_guard_context(groups=_FULL_GUARD_CONTEXT_GROUPS, timeout=1.0)
        ok, reason, _ = _validate_unsolicited_guard_context(
            context,
            speaker_id=voice_id,
            target_id="player",
        )
        if not ok:
            print(f"[Attention] Aborted ({reason})")
            return

        if pending_follow_up_blocks_attention("after_guard_context"):
            return

        # Determine mode: check for recent dialogue with this NPC
        recent_cutoff = time.time() - RECENT_DIALOGUE_WINDOW_SECONDS
        dialogue_history = load_dialogue_history(context)
        if _should_suppress_attention_for_follow_up(voice_id, dialogue_history):
            print(f"[Attention] Suppressed for {voice_id} (awaiting response to follow-up)")
            return

        has_recent = any(
            entry.get("timestamp", 0) >= recent_cutoff
            and (entry.get("voiceName") == voice_id
                 or (entry.get("isPlayer") and str(entry.get("targetId", "")).lower() == voice_id.lower()))
            for entry in reversed(dialogue_history)
        )

        if has_recent:
            mode = "continuation"
        else:
            if not settings.get("conversation", {}).get("attention_cold_approach_enabled", True):
                print(f"[Attention] Skipped cold approach for {voice_id} - cold_approach_enabled is off in settings")
                return
            mode = "cold_approach"
            if _attention_stare_locks.get(voice_id):
                print(f"[Attention] Skipped cold approach for {voice_id} (stare locked)")
                return

        def before_play_guard():
            if pending_follow_up_blocks_attention("before_playback"):
                return False
            playback_context = _request_guard_context(groups=_PLAYBACK_GUARD_CONTEXT_GROUPS, timeout=1.0)
            playback_ok, playback_reason, _ = _validate_unsolicited_guard_context(
                playback_context,
                speaker_id=voice_id,
                target_id="player",
                require_no_playback=False,
            )
            if not playback_ok:
                print(f"[Attention] Aborted before playback ({playback_reason})")
                return False
            if pending_follow_up_blocks_attention("after_playback_guard_context"):
                return False
            return True

        print(f"[Attention] Generating {mode} for {voice_id}")

        played = run_attention_turn(
            voice_id,
            "player",
            context,
            mode,
            lua_socket=lua_socket,
            conv_state=conv_state,
            start_new_conversation=start_new_conversation,
            clear_cancel=clear_cancel,
            is_conversation_valid=is_conversation_valid,
            can_use_streaming_tts=_can_use_streaming_tts,
            run_streaming_tts_async=run_streaming_tts_async,
            tts_service=tts,
            load_settings_func=load_settings,
            stream_sentences_func=stream_sentences,
            play_completed_response_streaming_func=start_completed_response_streaming_playback,
            run_tts_async=run_tts_async,
            tts_available=TTS_AVAILABLE,
            before_play_guard=before_play_guard,
        )

        if played and mode == "cold_approach":
            _attention_stare_locks[voice_id] = True
            print(f"[Attention] Stare lock set for {voice_id}")

        print(f"[Attention] Played={played} speaker={voice_id} mode={mode}")
    finally:
        _end_unsolicited_generation()


# Connect lipsync module to socket for viseme streaming
if LIPSYNC_AVAILABLE:
    lipsync.set_lua_socket(lua_socket)

# Connect vision agent to socket for game context
if VISION_AGENT_AVAILABLE:
    vision_agent.set_lua_socket(lua_socket)

# Connect landmarks module to socket for player position
set_landmarks_lua_socket(lua_socket)

# Register route blueprints and wire up dependencies
register_blueprints(app)

# Owl Post database init deferred to first player handshake via PlayerContext

set_config_lua_socket(lua_socket)
set_setup_lua_socket(lua_socket)
set_commitments_lua_socket(lua_socket)
if set_stt_lua_socket:
    set_stt_lua_socket(lua_socket)
# Note: set_load_game_context is called after load_game_context is defined below

# ============================================
# Download Complete Signaling (for pre-buffering)
# ============================================
_download_complete_event = threading.Event()


def signal_download_complete():
    """Called when TTS download finishes (audio may still be playing)."""
    _download_complete_event.set()
    print("[Signal] Download complete - can buffer next response")


def wait_for_download_complete(timeout=60.0):
    """Wait for TTS download to complete. Returns True if signaled, False on timeout."""
    result = _download_complete_event.wait(timeout=timeout)
    _download_complete_event.clear()
    return result


# ============================================
# Game Context Helper
# ============================================
def load_game_context():
    """Get game context from socket cache (sent by Lua)"""
    return lua_socket.get_game_context()


# Wire up dialogue and commitments routes with game context loader
set_load_game_context(load_game_context)
set_commitments_game_context(load_game_context)
set_owlpost_game_context(load_game_context)
set_owlpost_conv_state(conv_state)
set_owl_orchestrator_game_context(load_game_context)
set_owl_lua_socket(lua_socket)

# Owl Post orchestrators
_owl_mail_orchestrator = OwlMailOrchestrator()
_owl_board_orchestrator = OwlBoardOrchestrator()
_overlay_manager = None  # Set in main() when hotkeys are initialized
owlpost_set_board_orchestrator(_owl_board_orchestrator)


# ============================================
# Earshot Witness Tracking
# ============================================
def get_earshot_witnesses(nearby_npcs, speaker_id):
    """Get list of significant NPC IDs within earshot, excluding speaker and player.

    Only tracks NPCs that have voice references (significant characters).
    This filters out random students, enemy mobs, generic townspeople, etc.

    Args:
        nearby_npcs: List of nearby NPC dicts with 'name' and 'distance'
        speaker_id: The speaker's internal ID (to exclude from witnesses)

    Returns:
        List of NPC IDs (strings) who were within earshot
    """
    witnesses = []
    for npc in nearby_npcs:
        npc_id = npc.get('name', '')
        if not npc_id:
            continue
        # Skip speaker
        if npc_id == speaker_id:
            continue
        # Skip player
        if npc_id.lower() in ('player', 'playermale', 'playerfemale'):
            continue
        # Skip insignificant NPCs (only track those with voice references)
        if not is_significant_npc(npc_id):
            continue
        witnesses.append(npc_id)
    return witnesses


# ============================================
# Chapter Evaluation at Conversation End
# ============================================
def evaluate_conversation_chapters(conv_state, load_history_func, get_name_func):
    """Queue NPCs for memory indexing when a conversation ends.

    Called when a conversation truly ends (no pending player input).
    Uses the memory queue system for robust, failure-tolerant processing.

    Args:
        conv_state: ConversationState with tracked speakers/location
        load_history_func: Function to load dialogue history (unused, kept for API compat)
        get_name_func: Function to get display name from NPC ID (unused, kept for API compat)
    """
    speakers = conv_state.conversation_speakers
    if not speakers:
        return

    try:
        from utils.memory import is_memory_available
        if not is_memory_available():
            return
    except ImportError:
        return

    # Queue all speakers for processing (significant NPCs only)
    # The queue system handles:
    # - Deduplication (won't re-queue if already queued)
    # - Single-threaded processing (no race conditions)
    # - Per-entry checkpointing (failure recovery)
    # - Idempotent Graphiti sync (no duplicate episodes)
    try:
        from utils.memory_queue import queue_npcs_for_processing
        speaker_list = [s for s in speakers if is_significant_npc(s)]
        if not speaker_list:
            return
        print(f"[Memory] Queueing {len(speaker_list)} NPCs for memory indexing: {speaker_list}")
        queue_npcs_for_processing(speaker_list)
    except ImportError as e:
        print(f"[Memory] Memory queue not available: {e}")
    except Exception as e:
        print(f"[Memory] Error queueing NPCs: {e}")


# ============================================
# TTS Thread
# ============================================
def _can_use_streaming_tts():
    """Check if streaming LLM→TTS pipeline is available."""
    if not TTS_AVAILABLE:
        return False
    try:
        provider = tts.get_provider()
        # Inworld WS
        if hasattr(provider, 'ws_connected') and provider.ws_connected:
            return True
        # Any provider with sentence streaming (e.g., Pocket ONNX)
        if hasattr(provider, 'synthesize_stream_sentences'):
            return True
        return False
    except Exception:
        return False


def run_streaming_tts_async(sentence_gen, character_name, setup_event, setup_data,
                             full_text_holder, epoch=None):
    """Run streaming LLM→TTS pipeline in background thread.

    Args:
        sentence_gen: Generator yielding clean sentences (action tags stripped)
        character_name: Speaker ID
        setup_event: Event signaled by caller after play_turn completes
        setup_data: Dict populated by caller with 'positions', 'turn_id'
        full_text_holder: Dict with 'text' key set when LLM finishes
        epoch: Conversation epoch for staleness checks
    """
    global state
    tts_activity_token = _begin_tts_activity()
    my_epoch = epoch

    lua_socket.pipeline_active = True
    lua_socket.pipeline_event.clear()

    def should_abort():
        if setup_data and setup_data.get("_abort"):
            return True
        if my_epoch is not None and not is_conversation_valid(my_epoch):
            return True
        return False

    def on_stop(completed: bool):
        history_entry = setup_data.get("_history_entry") if setup_data else None
        if isinstance(history_entry, dict):
            history_entry["_playback_completed"] = bool(completed)
        if setup_data is not None:
            setup_data["_playback_completed"] = bool(completed)
        if my_epoch is None or is_conversation_valid(my_epoch):
            lua_socket.send_lipsync_stop()
            print(f"[StreamTTS] Playback ended (completed={completed}) - sent via socket")
        else:
            print(f"[StreamTTS] Epoch {my_epoch} stale - skipping lipsync_stop")

    def on_download_complete():
        signal_download_complete()

    try:
        if TTS_AVAILABLE:
            print(f"[StreamTTS] Starting speak_streaming for {character_name} (epoch={my_epoch})")
            result = tts.speak_streaming(
                sentence_gen, character_name,
                full_text_holder=full_text_holder,
                setup_event=setup_event,
                setup_data=setup_data,
                on_stop=on_stop,
                on_download_complete=on_download_complete,
                lua_socket=lua_socket,
                abort_check=should_abort,
                profiler=_profiler
            )
            if result["success"]:
                print(f"[StreamTTS] Complete (epoch={my_epoch})")
            else:
                print(f"[StreamTTS] Failed: {result.get('error')} (epoch={my_epoch})")
                if my_epoch is None or is_conversation_valid(my_epoch):
                    lua_socket.send_lipsync_stop()
        else:
            print(f"[StreamTTS] TTS not available")
            if my_epoch is None or is_conversation_valid(my_epoch):
                lua_socket.send_lipsync_stop()
    except Exception as e:
        print(f"[StreamTTS] Error: {e}")
        import traceback
        traceback.print_exc()
        if my_epoch is None or is_conversation_valid(my_epoch):
            lua_socket.send_lipsync_stop()
    finally:
        _end_tts_activity(tts_activity_token)
        print(f"[StreamTTS] Thread exiting (epoch={my_epoch})")


def _iter_completed_response_sentences(response_text, narration_enabled=False, log_prefix="[RespStream]"):
    """Compatibility wrapper for the extracted completed-response iterator."""
    yield from iter_completed_response_sentences(
        response_text,
        narration_enabled=narration_enabled,
        log_prefix=log_prefix,
    )


def start_completed_response_streaming_playback(
    response,
    speaker_id,
    speaker_name,
    target_id,
    turn_index,
    epoch,
    *,
    raw_response=None,
    action="None",
    house_point_actions=None,
    pending_history_entry=None,
    wait_for_prior_tts=None,
    before_play_callback=None,
):
    """Play a completed response through the sentence-streaming subtitle/TTS path."""
    return play_completed_response_streaming(
        response,
        speaker_id,
        speaker_name,
        target_id,
        turn_index,
        epoch,
        raw_response=raw_response,
        action=action,
        house_point_actions=house_point_actions,
        pending_history_entry=pending_history_entry,
        wait_for_prior_tts=wait_for_prior_tts,
        before_play_callback=before_play_callback,
        remove_unpaired_double_quotes=remove_unpaired_double_quotes,
        add_pending_history=conv_state.add_pending_history,
        run_streaming_tts_async=run_streaming_tts_async,
        tts_service=tts,
        lua_socket=lua_socket,
        load_settings_func=load_settings,
        can_use_streaming_tts_func=_can_use_streaming_tts,
        is_conversation_valid_func=is_conversation_valid,
    )


def run_tts_async(text, character_name, positions=None, turn_id=None, epoch=None, history_entry=None):
    """Run TTS in background thread with download complete signaling for pre-buffering.

    Args:
        epoch: Conversation epoch - if stale, don't signal completion events
    """
    global state
    tts_activity_token = _begin_tts_activity()
    my_epoch = epoch  # Capture for closures

    # CRITICAL: Mark pipeline as active BEFORE starting
    # This ensures wait_for_pipeline_stop() will block until audio finishes
    lua_socket.pipeline_active = True
    lua_socket.pipeline_event.clear()

    def should_abort():
        """Epoch-based abort check for NPC TTS playback."""
        if my_epoch is not None and not is_conversation_valid(my_epoch):
            return True
        return False

    def on_stop(completed: bool):
        if isinstance(history_entry, dict):
            history_entry["_playback_completed"] = bool(completed)
        # Only signal if still current conversation
        if my_epoch is None or is_conversation_valid(my_epoch):
            lua_socket.send_lipsync_stop()
            print(f"[TTS] Playback ended (completed={completed}) - sent via socket")
        else:
            print(f"[TTS] Epoch {my_epoch} stale - skipping lipsync_stop signal")

    def on_download_complete():
        # Signal that we can start buffering the next response
        signal_download_complete()

    try:
        if TTS_AVAILABLE:
            result = tts.speak(
                text, character_name,
                on_stop=on_stop,
                on_download_complete=on_download_complete,
                lua_socket=lua_socket,
                initial_positions=positions,
                turn_id=turn_id,
                abort_check=should_abort,
                history_entry=history_entry,
                profiler=_profiler
            )
            if result["success"]:
                print(f"[TTS] Complete")
            else:
                print(f"[TTS] Failed: {result.get('error')}")
                if my_epoch is None or is_conversation_valid(my_epoch):
                    lua_socket.send_lipsync_stop()
        else:
            print("[TTS] Inworld not available")
            if my_epoch is None or is_conversation_valid(my_epoch):
                lua_socket.send_lipsync_stop()
    except Exception as e:
        print(f"[TTS] Error: {e}")
        if my_epoch is None or is_conversation_valid(my_epoch):
            lua_socket.send_lipsync_stop()
    finally:
        _end_tts_activity(tts_activity_token)


def run_player_tts(text, turn_id, game_context=None, abort_check=None, epoch=None, history_entry=None):
    """
    Run TTS for player's spoken line (blocking).
    Called when player_voice_enabled is True.
    Uses non-3D audio since the player is the listener.

    Args:
        abort_check: Callable that returns True if we should abort
        epoch: Conversation epoch - if stale, don't signal completion events

    Returns True on success, False on failure.
    """
    global state
    my_epoch = epoch  # Capture for closures
    _profiler.mark("run_player_tts entered")

    # Check for abort before starting
    if abort_check and abort_check():
        print("[PlayerTTS] Aborted before starting")
        return False

    _profiler.mark("load_settings start")
    settings = load_settings()
    conv_settings = settings.get('conversation', {})
    _profiler.mark("load_settings done")

    if isinstance(history_entry, dict):
        history_entry.setdefault("_tts_archive_speaker_id", "Player")

    # Get player voice name - priority: settings override > game context > fallback
    player_voice_override = conv_settings.get('player_voice_name', '')

    if player_voice_override:
        # Settings override takes priority
        player_voice_name = player_voice_override
        print(f"[PlayerTTS] Using override voice: {player_voice_name}")
    elif game_context and game_context.get('playerVoiceId'):
        # Use detected voice from game (PlayerMale or PlayerFemale)
        player_voice_name = game_context.get('playerVoiceId')
        print(f"[PlayerTTS] Using detected voice: {player_voice_name}")
    else:
        # Fallback
        player_voice_name = "PlayerMale"
        print(f"[PlayerTTS] Using fallback voice: {player_voice_name}")

    _profiler.mark("get_or_create_voice start")
    # Verify voice exists (will auto-clone if reference file exists)
    voice = tts.get_or_create_voice(player_voice_name, lua_socket=lua_socket)
    _profiler.mark("get_or_create_voice done")
    if not voice:
        print(f"[PlayerTTS] No voice available for '{player_voice_name}' - skipping player TTS")
        return False

    tts_text = strip_parentheses(text)
    if not tts_text:
        print("[PlayerTTS] Text empty after stripping parentheses - skipping TTS")
        return False

    print(f"[PlayerTTS] Speaking as player ({player_voice_name}): \"{tts_text[:50]}...\"")
    tts_activity_token = _begin_tts_activity()

    def on_stop(completed: bool):
        if isinstance(history_entry, dict):
            history_entry["_playback_completed"] = bool(completed)
        # Only signal if still current conversation
        if my_epoch is None or is_conversation_valid(my_epoch):
            lua_socket.send_lipsync_stop()
        else:
            print(f"[PlayerTTS] Epoch {my_epoch} stale - skipping lipsync_stop signal")

    try:
        if TTS_AVAILABLE:
            # Note: We don't signal download_complete for player TTS
            # That signal is for NPC pre-buffering, which shouldn't start until NPC speaks
            # No 3D positioning - player voice plays centered (non-spatial)
            _profiler.mark("tts.speak start")
            result = tts.speak(
                tts_text,
                player_voice_name,
                on_stop=on_stop,
                on_download_complete=None,  # Don't signal - this is player turn
                lua_socket=lua_socket,
                initial_positions=None,  # No 3D audio for player voice
                turn_id=turn_id,
                abort_check=abort_check,
                history_entry=history_entry,
                profiler=_profiler  # Pass profiler for internal timing
            )
            _profiler.mark("tts.speak returned")
            if result["success"]:
                print(f"[PlayerTTS] Complete")
                return True
            else:
                print(f"[PlayerTTS] Failed: {result.get('error')}")
                if my_epoch is None or is_conversation_valid(my_epoch):
                    lua_socket.send_lipsync_stop()
                return False
        else:
            print("[PlayerTTS] Inworld not available")
            if my_epoch is None or is_conversation_valid(my_epoch):
                lua_socket.send_lipsync_stop()
            return False
    except Exception as e:
        print(f"[PlayerTTS] Error: {e}")
        if my_epoch is None or is_conversation_valid(my_epoch):
            lua_socket.send_lipsync_stop()
        return False
    finally:
        _end_tts_activity(tts_activity_token)


def play_buffered_player_tts(tts_stream, visemes, turn_id, player_name, abort_check=None, epoch=None,
                             streaming_visemes=False, positions=None, reverb_auxbus=None, reverb_send=1.0,
                             centered=False, sentence_boundaries=None, sentence_subtitles=False):
    """
    Play pre-buffered player TTS with lip sync.

    This is the fast path for player voice - the audio was buffered in parallel
    with target_selection/memory ops, so we just need to play it.

    Uses PlaybackCoordinator for synchronized lipsync with optional 3D audio.

    Args:
        epoch: Conversation epoch - if stale, don't signal completion events
        streaming_visemes: If True, visemes list is still being appended to by
            the synthesis thread. Uses set_viseme_source() for live updates.
        positions: Initial 3D positions dict (camX/Y/Z, camYaw, npcX/Y/Z)
        reverb_auxbus: Reverb aux bus name for spatial audio
        reverb_send: Reverb send level (0.0-1.0)
    """
    global state
    my_epoch = epoch  # Capture for closure

    if abort_check and abort_check():
        print("[PlayerTTS] Aborted before playback")
        return False

    tts_activity_token = _begin_tts_activity()

    try:
        from audio import get_player
        from audio.playback import get_coordinator

        player = get_player()
        coordinator = get_coordinator()

        if not coordinator:
            print("[PlayerTTS] No coordinator available")
            return False

        # Determine if 3D audio is available (player position from Lua)
        use_3d = bool(positions) and positions.get("npcX") is not None
        print(f"[PlayerTTS] play_buffered: use_3d={use_3d}, centered={centered}, has_positions={bool(positions)}")

        # FPV/VR centered: force use_3d for reverb routing, no position tracking needed
        if centered and not use_3d:
            use_3d = True  # Route through EnvironmentNode for reverb

        # Create turn with 3D audio when positions are available
        turn = coordinator.create_turn(
            turn_id,
            speaker_id="player",
            use_3d=use_3d,
            reverb_auxbus=reverb_auxbus if use_3d else None,
            reverb_send=reverb_send,
            centered=centered
        )
        turn.audio_stream = tts_stream
        turn.sentence_boundaries = sentence_boundaries or []
        turn._sentence_subtitles = bool(sentence_subtitles)

        # Set up 3D audio positioning (same pattern as NPC path)
        if use_3d and not centered:
            player.position_reader.set_socket(lua_socket)
            cam = (positions.get("camX", 0), positions.get("camY", 0), positions.get("camZ", 0))
            npc = (positions.get("npcX", 0), positions.get("npcY", 0), positions.get("npcZ", 0))
            yaw = positions.get("camYaw", 0)
            player.position_reader.set_initial_positions(cam, yaw, npc)
            print(f"[PlayerTTS] 3D audio enabled - source_pos=({npc[0]:.0f},{npc[1]:.0f},{npc[2]:.0f})")
        elif centered:
            print("[PlayerTTS] Centered 3D mode (FPV/VR) - reverb enabled, no tracking")
        else:
            print("[PlayerTTS] No 3D positions - using centered stereo")

        # Add visemes for lip sync
        if visemes:
            if streaming_visemes:
                turn.set_viseme_source(visemes)
                print(f"[PlayerTTS] Streaming visemes from live source ({len(visemes)} so far)")
            else:
                turn.add_visemes(visemes)
                print(f"[PlayerTTS] Using {len(turn.viseme_buffer)} pre-computed visemes")

        # Play with synchronized lipsync (blocking), with epoch-aware abort
        success = coordinator.play_turn(turn_id, player, blocking=True, abort_check=abort_check)

        if success:
            print(f"[PlayerTTS] Playback complete")
        return success

    except Exception as e:
        print(f"[PlayerTTS] Playback error: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        _end_tts_activity(tts_activity_token)
        # Only signal completion if we're still the current conversation
        if my_epoch is None or is_conversation_valid(my_epoch):
            lua_socket.send_lipsync_stop()
        else:
            print(f"[PlayerTTS] Epoch {my_epoch} stale - skipping lipsync_stop signal")


def play_prebuffered_response(buffered, blocking=True, epoch=None):
    """
    Play a pre-buffered TTS stream with lipsync.

    Uses PlaybackCoordinator for synchronized handshake:
    1. Send lipsync_start with accumulated visemes
    2. Wait for lipsync_ready from Lua
    3. Start audio playback
    4. Send audio_sync during playback for drift correction

    Args:
        buffered: Pre-buffered TTS data
        blocking: If True, block until playback complete
        epoch: Conversation epoch - if stale, don't signal completion events
    """
    speaker = buffered["speaker"]
    speaker_id = buffered["speaker_id"]
    tts_stream = buffered["tts_stream"]
    visemes = buffered.get("visemes", [])
    positions = buffered.get("positions", {})
    turn_id = buffered.get("turn_id")
    reverb_auxbus = buffered.get("reverb_auxbus")
    reverb_send = buffered.get("reverb_send", 1.0)
    text = buffered.get("text", "")  # For interrupt trimming
    word_timings = buffered.get("word_timings", [])  # For interrupt trimming
    sentence_boundaries = buffered.get("sentence_boundaries", [])
    history_entry = buffered.get("history_entry")
    my_epoch = epoch  # Capture for closure

    print(f"[PlayBuffer] Playing: {speaker} (turn={turn_id}, epoch={my_epoch}, "
          f"{len(visemes)} visemes, {len(sentence_boundaries)} boundaries)")

    # Mark pipeline as active BEFORE signaling download complete
    lua_socket.pipeline_active = True
    lua_socket.pipeline_event.clear()

    # NOTE: Don't signal_download_complete() here!
    # The buffer_thread may still be running (receiving remaining chunks after threshold).
    # The buffer_tts() function signals download complete when prepare_tts() returns.

    def do_playback():
        completed = False
        try:
            from audio import get_player
            from audio.playback import get_coordinator

            player = get_player()
            coordinator = get_coordinator()

            # Connect position reader to socket for real-time position updates
            player.position_reader.set_socket(lua_socket)

            # Set up reverb callback for live updates during playback
            def on_reverb_update(auxbus, send):
                player.set_reverb(auxbus, send)
            lua_socket.set_reverb_callback(on_reverb_update)

            # Set initial 3D positions DIRECTLY (eliminates race condition)
            # use_3d based on whether positions are provided (check key exists, not value truthiness)
            use_3d = bool(positions) and positions.get("npcX") is not None
            if use_3d:
                cam = (positions.get("camX", 0), positions.get("camY", 0), positions.get("camZ", 0))
                npc = (positions.get("npcX", 0), positions.get("npcY", 0), positions.get("npcZ", 0))
                yaw = positions.get("camYaw", 0)
                player.position_reader.set_initial_positions(cam, yaw, npc)

            # Create turn with pre-computed visemes and reverb
            turn = coordinator.create_turn(
                turn_id, speaker_id=speaker_id, use_3d=use_3d,
                reverb_auxbus=reverb_auxbus, reverb_send=reverb_send
            )
            turn.audio_stream = tts_stream
            turn.sentence_boundaries = sentence_boundaries or []
            turn._sentence_subtitles = load_settings().get('conversation', {}).get('sentence_subtitles', True)

            # Set text and word timings for interrupt trimming
            # Use set_word_timings_source to keep synced with the still-growing list
            # (TTS download continues in background after prebuffer starts playing)
            turn.original_text = text
            if word_timings:
                turn.set_word_timings_source(word_timings)

            # Connect viseme source for streaming - the visemes list reference
            # keeps growing as buffer_thread continues processing chunks
            if visemes is not None:
                turn.set_viseme_source(visemes)
                print(f"[PlayBuffer] Connected viseme source ({len(visemes)} initial, streaming enabled)")

            # Create abort check callback that uses epoch
            def should_abort():
                if my_epoch is not None and not is_conversation_valid(my_epoch):
                    return True
                return False

            # Use coordinator for synchronized playback with epoch-aware abort
            completed = bool(coordinator.play_turn(turn_id, player, blocking=True, abort_check=should_abort))

            print(f"[PlayBuffer] Complete: {speaker}")
        except Exception as e:
            print(f"[PlayBuffer] Error: {e}")
            import traceback
            traceback.print_exc()
        finally:
            if isinstance(history_entry, dict):
                history_entry["_playback_completed"] = completed
            lua_socket.set_reverb_callback(None)  # Clear reverb callback
            # Only signal completion if we're still the current conversation
            # This prevents old playback threads from waking up new conversation's waits
            if my_epoch is None or is_conversation_valid(my_epoch):
                lua_socket.send_lipsync_stop()
            else:
                print(f"[PlayBuffer] Epoch {my_epoch} stale - skipping lipsync_stop signal")

    if blocking:
        do_playback()
    else:
        playback_thread = threading.Thread(target=do_playback, daemon=True)
        playback_thread.start()


# ============================================
# Conversation Control
# ============================================
def stop_conversation(source: str = "unknown", notify: bool = True):
    """Stop the current conversation - shared helper for all input methods.

    Args:
        source: Who triggered the stop (for logging)
        notify: Whether to show "Conversation stopped" notification
    """
    print(f"[Server] Stopping conversation (source: {source})")
    question_follow_up.cancel_pending(f"stop_conversation:{source}")
    linger_goodbye.cancel_pending(f"stop_conversation:{source}")

    # 0. Trim pending history to what was actually spoken (before aborting audio)
    # Only trim if TTS is enabled - when TTS is None, "interrupting" is just skipping the wait time
    try:
        settings = load_settings()
        tts_provider = settings.get('tts', {}).get('provider', '')

        if tts_provider and tts_provider.lower() != 'none':
            from audio.playback import get_coordinator
            coordinator = get_coordinator()
            if coordinator:
                trimmed_text = coordinator.get_interrupted_text()
                if trimmed_text is not None and conv_state.pending_history_entries:
                    if not trimmed_text:
                        # Nothing was spoken at all - discard the entry entirely
                        discarded = conv_state.pending_history_entries.pop()
                        topic_update_id = discarded.get("_topic_update_id")
                        if topic_update_id:
                            conversation_topics.discard(topic_update_id)
                        print(f"[Server] Discarded unspoken entry: '{discarded.get('text', '')[:60]}...'")
                    else:
                        # Strip any partial action tags (e.g. stray "[" from incomplete "[Action: ...]")
                        trimmed_text = strip_action_tag(trimmed_text)
                        last_entry = conv_state.pending_history_entries[-1]
                        original_text = last_entry.get('text', '')
                        if trimmed_text != original_text:
                            last_entry['text'] = trimmed_text
                            last_entry['interrupted'] = True
                            print(f"[Server] Trimmed history: '{original_text[:50]}...' -> '{trimmed_text[:50]}...'")
    except Exception as e:
        print(f"[Server] History trim error: {e}")

    # 0b. Commit pending history entries NOW (before interjection finally block can discard them)
    # This ensures interrupted speech is saved to dialogue history
    if conv_state.pending_history_entries:
        try:
            full_playback_entries = []
            skipped_entries = []
            for entry in conv_state.pending_history_entries:
                if entry.get("_require_full_playback") and not entry.get("_playback_completed"):
                    skipped_entries.append(entry)
                else:
                    full_playback_entries.append(entry)
            if skipped_entries:
                for entry in skipped_entries:
                    topic_update_id = entry.get("_topic_update_id")
                    if topic_update_id:
                        conversation_topics.discard(topic_update_id)
                print(f"[Server] Discarded {len(skipped_entries)} pending full-playback entries on interrupt")
            conv_state.pending_history_entries = full_playback_entries
            count, committed_entries = conv_state.commit_pending_history()
            for entry in committed_entries:
                topic_update_id = entry.get("_topic_update_id")
                if not topic_update_id:
                    continue
                if entry.get("_playback_completed", True) and not entry.get("interrupted"):
                    source_ids = entry.get("sourceEntryIds") or []
                    conversation_topics.mark_committed(
                        topic_update_id,
                        source_ids[-1] if source_ids else None,
                    )
                else:
                    conversation_topics.discard(topic_update_id)
            print(f"[Server] Committed {count} pending history entries on interrupt")
        except Exception as e:
            print(f"[Server] History commit error: {e}")

    # 1. Request cancellation (timestamp-based, doesn't touch conv_state)
    request_cancel()

    # 2. Abort audio playback immediately
    if AUDIO3D_AVAILABLE and audio_get_player:
        try:
            player = audio_get_player()
            if player:
                player.abort()
        except Exception as e:
            print(f"[Server] Audio abort error: {e}")

    # 2b. Stop coordinator sync loop (prevents old turn from sending visemes)
    try:
        from audio.playback import get_coordinator
        coordinator = get_coordinator()
        if coordinator:
            coordinator.stop_current()
    except Exception as e:
        print(f"[Server] Coordinator stop error: {e}")

    # 3. Clear playback tracking
    lua_socket.playback_active = False
    lua_socket.playback_event.set()
    lua_socket.pipeline_active = False
    lua_socket.pipeline_event.set()

    # 4. Send reset to Lua (triggers ResetState + releases NPCs)
    lua_socket.send_reset()

    # 5. Show notification (optional)
    if notify:
        lua_socket.send_notification("Conversation stopped")


# Register interrupt callback with socket (for Lua-initiated interrupts like cinematics)
lua_socket.set_interrupt_callback(stop_conversation)


# ============================================
# Chat Processing
# ============================================
def process_chat_request(data, is_continuation=False):
    """Process a chat request - called by HTTP endpoint or file queue

    Args:
        data: Request data with 'user_input'
        is_continuation: If True, this continues an existing conversation (don't reset speakers/turns)
    """
    global state

    # Start profiling from the very beginning
    _profiler.start("chat_to_audio")
    question_follow_up.cancel_pending("process_chat_request")
    linger_goodbye.cancel_pending("process_chat_request")

    # If conversation is ongoing (processing or playing), interrupt it
    # This handles text input, PTT, and any other input method
    if conv_state.state == "processing" or lua_socket.pipeline_active:
        stop_conversation(source="new_input", notify=False)
    else:
        # Even if no previous conversation, start new epoch for this one
        # This ensures each conversation has a unique epoch ID
        start_new_conversation()

    # Capture epoch for this conversation - used by TTS and interjection loop
    current_epoch = get_current_epoch()

    # Clear any stale cancellation timestamp (epoch is NOT cleared)
    clear_cancel()

    # Mark as processing
    conv_state.state = "processing"

    user_input = data.get('user_input', '').strip()
    character_name = data.get('character_name', '')
    character_id = data.get('character_id', 'unknown')

    # Check for voice spell command FIRST (only from voice input, if enabled)
    # Acts as fallback for spells that wakeword detection missed
    settings = load_settings()
    if data.get("from_stt") and settings.get('stt', {}).get('voice_spells', True):
        spell_name, matched_text = detect_spell_in_text(user_input)
        if spell_name:
            print(f"[Chat] Spell detected (text): '{matched_text}' -> {spell_name}")
            lua_socket.send({
                "type": "cast_spell",
                "spell": spell_name,
                "text": user_input
            })
            return {"status": "spell_cast", "spell": spell_name}

    # Request fresh game context from Lua (selective groups for efficiency)
    # position needed for landmark beacons in format_game_context
    _profiler.mark("context_refresh start")
    game_context = _request_guard_context(groups=_FULL_GUARD_CONTEXT_GROUPS, timeout=1.0)
    _profiler.mark("context_refresh done")

    # Block if in cinematic or combat
    ok, reason, nearby_npcs = _validate_live_conversation_context(game_context)
    if not ok:
        print(f"[Chat] Blocked at initial context refresh ({reason})")
        guard_result = _abort_due_to_guard_failure(reason)
        return {"error": guard_result["message"]}

    # Apply input correction (grammar/spelling fixes) if enabled
    # Skip for speech input (already formatted) unless using Moonshine (raw unpunctuated text)
    # Skip for prompt/director mode - it's an instruction, not player dialogue
    if data.get('mode', 'chat') != "prompt" and (not data.get("from_stt") or load_settings().get('stt', {}).get('provider') == 'moonshine'):
        user_input = run_input_correction_agent(user_input)

    # Fix spell name mistranscriptions from STT (e.g. "lumis" -> "Lumos")
    if data.get("from_stt"):
        user_input = correct_spell_names_in_text(user_input)

    print(f"[Chat] User: \"{user_input}\"")

    if not user_input:
        print("[Chat] ERROR: No user input!")
        return {"error": "No user_input provided"}

    # Load conversation state and settings
    settings = load_settings()
    conv_settings = settings.get('conversation', {})
    conv_state.max_turns = conv_settings.get('max_turns', 6)

    # ============================================
    # EARLY PLAYER TTS BUFFERING
    # Start buffering immediately - only needs user_input + playerVoiceId
    # This runs in parallel with target_selection, memory ops, etc.
    # ============================================
    player_tts_buffer = None  # Will hold (tts_stream, word_timings, visemes[, sentence_boundaries])
    player_tts_ready = threading.Event()
    player_tts_error = [None]  # Mutable container for error
    player_tts_archive_holder = {"_tts_archive_speaker_id": "Player"}
    # NOTE: No abort flag - player TTS always completes (player always speaks what they typed)
    # Only is_cancelled() can abort (F8 reset). "No valid target" doesn't stop player speech.

    # Check player voice settings early (before target_selection)
    is_from_stt = data.get('from_stt', False)
    mode = data.get('mode', 'chat')  # "chat" or "prompt" (director mode)
    # Player voice is disabled in prompt mode - player isn't speaking, they're directing
    player_voice_enabled = conv_settings.get('player_voice_enabled', True) and mode != 'prompt'
    player_display_text = user_input
    player_name = game_context.get('playerName', 'Player')

    # Handle interruption BEFORE starting any background work
    if conv_state.state == "playing":
        print("[Chat] Interrupting current playback")
        conv_state.pending_player_input = user_input
        conv_state.interrupted = True
        # Only show player message for normal chat, not director prompts
        if mode != 'prompt':
            lua_socket.send_player_message(player_name, player_display_text)
        lua_socket.send_conversation_state("playing", interrupted=True)
        return {"status": "queued_interrupt", "message": "Input queued, interrupting current conversation"}

    # Player TTS thread and completion event (used later to wait before NPC speaks)
    player_tts_thread = None
    player_tts_done = threading.Event()

    if player_voice_enabled and TTS_AVAILABLE:
        # IMPORTANT: Send conversation state BEFORE player turn starts
        # This clears the Lua queue first, so player turn won't be accidentally cleared
        # when we send it again after LLM response (for the !player_voice_enabled case)
        conv_state.state = "playing"
        lua_socket.send_conversation_state("playing")

        _profiler.mark("player_tts_buffer start")
        print(f"[Chat] Starting early player TTS buffering + playback...")

        def player_tts_full_flow():
            """
            Complete player TTS flow in one thread:
            1. Buffer the audio (prepare_tts)
            2. As soon as ready, do Lua handshake
            3. Start playback immediately

            This runs independently of main thread (target_selection, memory, etc.)
            """
            nonlocal player_tts_buffer
            try:
                # Get player voice name
                player_voice_override = conv_settings.get('player_voice_name', '')
                if player_voice_override:
                    player_voice_name = player_voice_override
                elif game_context.get('playerVoiceId'):
                    player_voice_name = game_context.get('playerVoiceId')
                else:
                    player_voice_name = "PlayerMale"

                print(f"[PlayerTTS] Buffering with voice: {player_voice_name}")

                tts_text = strip_parentheses(user_input)
                if not tts_text:
                    print("[PlayerTTS] Text empty after stripping parentheses - skipping TTS")
                    return

                # Step 1: Start synthesis with early buffer callback
                # on_ready fires after TTS_BUFFER_SECONDS of audio is buffered,
                # allowing playback to start while synthesis continues.
                buffer_ready = threading.Event()
                early_result = [None]
                synth_error = [None]

                def on_buffer_ready(stream, timings, visemes, sentence_boundaries=None):
                    early_result[0] = (stream, timings, visemes, sentence_boundaries or [])
                    buffer_ready.set()

                def run_synthesis():
                    try:
                        tts.prepare_tts(
                            tts_text,
                            player_voice_name,
                            abort_check=is_cancelled,
                            lua_socket=lua_socket,
                            on_ready=on_buffer_ready,
                            history_entry=player_tts_archive_holder,
                            narration_min_words=2,
                        )
                        # prepare_tts calls tts_stream.finish() before returning,
                        # which signals end-of-stream to the audio player.
                    except Exception as e:
                        synth_error[0] = str(e)
                    finally:
                        if not buffer_ready.is_set():
                            buffer_ready.set()

                synth_thread = threading.Thread(target=run_synthesis, daemon=True)
                synth_thread.start()

                # Wait for buffer threshold (or full completion for short utterances)
                buffer_ready.wait()

                if synth_error[0]:
                    player_tts_error[0] = synth_error[0]
                    print(f"[PlayerTTS] Buffer failed - {synth_error[0]}")
                    return

                if not early_result[0]:
                    player_tts_error[0] = "prepare_tts returned None"
                    print("[PlayerTTS] Buffer failed - no result")
                    return

                if is_cancelled():
                    print("[PlayerTTS] Cancelled after buffering")
                    return

                tts_stream, word_timings, visemes = early_result[0][:3]
                sentence_boundaries = early_result[0][3] if len(early_result[0]) > 3 else []
                player_tts_buffer = early_result[0]
                _profiler.mark("player_tts_buffer ready")
                print(f"[PlayerTTS] Buffer ready: {len(visemes)} visemes")

                # NOTE: We do NOT set conversation state here. Player speaking is independent
                # of whether there's a valid conversation target. The main thread manages
                # conversation state. Player TTS just does lip sync and plays audio.

                # Step 2: Do Lua handshake for lip sync setup (synthesis may still be running)
                _profiler.mark("send_player_turn_start")
                player_turn_result = lua_socket.send_player_turn_start(
                    player_name=player_name,
                    text=player_display_text,
                    timeout=1.0
                )
                _profiler.mark("send_player_turn_start done")

                if is_cancelled():
                    print("[PlayerTTS] Cancelled after handshake")
                    return

                # Step 3: Extract positions/reverb and play with streaming visemes.
                # The visemes list is still being appended to by the synthesis thread
                # if it hasn't finished. set_viseme_source() connects the coordinator's
                # sync loop to pick up new visemes as they arrive.
                turn_id = player_turn_result.get("turn_id")
                player_voice_spatial = conv_settings.get('player_voice_spatial', True)
                positions = player_turn_result.get("positions", {}) if player_voice_spatial else {}
                reverb_auxbus = player_turn_result.get("reverb_auxbus") if player_voice_spatial else None
                reverb_send = player_turn_result.get("reverb_send", 1.0)

                # FPV/VR: use centered 3D (reverb but no side-spatialization)
                first_person_active = player_turn_result.get("first_person_active", False)
                try:
                    from vr import is_vr_active
                    _is_vr = is_vr_active()
                except Exception:
                    _is_vr = False
                centered = (first_person_active or _is_vr) and player_voice_spatial
                print(f"[PlayerTTS] first_person_active={first_person_active}, is_vr={_is_vr}, player_voice_spatial={player_voice_spatial}, centered={centered}")

                still_synthesizing = synth_thread.is_alive()
                success = play_buffered_player_tts(
                    tts_stream=tts_stream,
                    visemes=visemes,
                    turn_id=turn_id,
                    player_name=player_name,
                    abort_check=is_cancelled,
                    epoch=current_epoch,
                    streaming_visemes=still_synthesizing,
                    positions=positions,
                    reverb_auxbus=reverb_auxbus,
                    reverb_send=reverb_send,
                    centered=centered,
                    sentence_boundaries=sentence_boundaries,
                    sentence_subtitles=False,
                )

                # Ensure synthesis thread finishes cleanly
                synth_thread.join(timeout=5.0)

                if success:
                    _profiler.mark("player TTS complete")
                    print(f"[Chat] Player voice turn complete")
                elif is_cancelled():
                    print(f"[Chat] Player voice cancelled")
                else:
                    print(f"[Chat] Player voice turn failed")

            except Exception as e:
                player_tts_error[0] = str(e)
                print(f"[PlayerTTS] Error: {e}")
                import traceback
                traceback.print_exc()
            finally:
                player_tts_ready.set()
                player_tts_done.set()

        player_tts_thread = threading.Thread(target=player_tts_full_flow, daemon=True)
        player_tts_thread.start()

        # Show player subtitle immediately (fire-and-forget)
        lua_socket.send_player_message(player_name, player_display_text)

    # Load dialogue history (pass context directly instead of callback)
    dialogue_history = load_dialogue_history(game_context)

    # Run target selection agent
    print("[Chat] Running target selection agent...")
    nearby_npcs_raw = game_context.get('nearbyNpcs', [])
    player_in_stealth = game_context.get('inStealth', False)

    # Filter NPCs to only those within earshot.
    # Companion gets extra grace range while actively following, and a much larger one on broom.
    nearby_npcs = filter_npcs_by_earshot(
        nearby_npcs_raw,
        player_in_stealth=player_in_stealth,
        player_on_broom=game_context.get('isOnMount', False),
        companion_on_broom=game_context.get('companionIsOnBroom', False),
        companion_id=game_context.get('companionId'),
        companion_following=game_context.get('hasCompanion', False)
    )
    print(f"[Chat] NPCs within earshot: {len(nearby_npcs)} (of {len(nearby_npcs_raw)} total){' [STEALTH]' if player_in_stealth else ''}")

    # Check for preview lock (NPC locked when chat/STT started - more reliable than isLookedAt)
    preview_locked_npc = game_context.get('previewLockedNpc')
    preview_lock_state = game_context.get('previewLockState')
    if preview_locked_npc:
        print(f"[Chat] Preview locked NPC: {preview_locked_npc} (state={preview_lock_state})")

    # Show player message immediately (if not already shown above)
    # Skip in prompt mode - director prompts aren't player dialogue
    if not player_voice_enabled and mode != 'prompt':
        lua_socket.send_player_message(player_name, player_display_text)

    # Find the looked-at NPC - prefer preview lock if available
    looked_at_npc = None

    # First priority: use preview locked NPC if they're in nearby list
    if preview_locked_npc:
        for npc in nearby_npcs:
            if npc.get('name') == preview_locked_npc:
                looked_at_npc = npc
                looked_at_npc['isLookedAt'] = True  # Mark as looked at for target agent
                print(f"[Chat] Using preview locked NPC as target: {preview_locked_npc}")
                break
        if not looked_at_npc:
            print(f"[Chat] Preview locked NPC '{preview_locked_npc}' not in nearby list")

    # Fallback: use isLookedAt from current crosshair
    if not looked_at_npc:
        for npc in nearby_npcs:
            if npc.get('isLookedAt'):
                looked_at_npc = npc
                break

    # Check for cancellation before expensive target selection
    if is_cancelled():
        print("[Chat] Cancelled before target selection")
        conv_state.reset()
        lua_socket.send_conversation_state("idle")
        # NOTE: Player TTS continues - they spoke, we just don't have an NPC response
        return {"status": "cancelled", "message": "Cancelled before target selection"}

    # === PROMPT MODE (Director Mode) - NPC-to-NPC conversations ===
    move_command = False  # Set by target selection action output
    # (mode was already read earlier for player TTS check)
    if mode == "prompt":
        print("[Chat] Director/Prompt mode - parsing prompt for NPC-to-NPC conversation")
        _profiler.mark("prompt_parser start")

        # Parse the director prompt
        parsed = run_prompt_parser_agent(user_input, nearby_npcs, player_name)
        _profiler.mark("prompt_parser done")

        if not parsed or not parsed.get('participants'):
            print("[Chat] Prompt parser found no valid participants")
            conv_state.state = "idle"
            lua_socket.send_conversation_state("idle")
            lua_socket.send_notification("No NPCs found matching your prompt")
            return {"status": "no_participants", "message": "No valid NPC participants found"}

        # Set conversation state for prompt mode
        conv_state.prompt_mode = True
        conv_state.prompt_participants = parsed['participants']
        conv_state.prompt_scenario = parsed.get('scenario', '')
        conv_state.prompt_include_player = parsed.get('include_player', False)

        # First participant is the speaker, second is the target
        # Single NPC without player: NPC speaks to the player (not to themselves)
        speaker_id = parsed['participants'][0]
        if len(parsed['participants']) > 1:
            target_id = parsed['participants'][1]
        else:
            target_id = "player"
            # Single NPC talking to player needs player context (observer_mode=False)
            parsed['include_player'] = True

        print(f"[Chat] Prompt mode - participants: {conv_state.prompt_participants}, "
              f"include_player: {conv_state.prompt_include_player}, "
              f"scenario: {conv_state.prompt_scenario[:50]}...")
        print(f"[Chat] First turn: {speaker_id} -> {target_id}")

    else:
        # === NORMAL CHAT MODE - Run target selection ===
        _profiler.mark("target_selection start")

        # Crosshair shortcut: bypass target LLM if enabled and player is looking at an NPC
        # If move keywords detected and companion is nearby, skip shortcut so LLM can detect action
        use_crosshair = conv_settings.get('target_selection_use_crosshair', True)
        if use_crosshair and looked_at_npc and looked_at_npc.get('name'):
            if conv_settings.get('companion_move_enabled', True):
                companion_id = game_context.get('companionId')
                if companion_id:
                    from utils.text_utils import has_move_keyword
                    language = game_context.get('language', 'EN_US')
                    if has_move_keyword(user_input, language):
                        print(f"[Chat] Move keyword detected with companion nearby — skipping crosshair shortcut for LLM action detection")
                        use_crosshair = False
        if use_crosshair and looked_at_npc and looked_at_npc.get('name'):
            speaker_id = looked_at_npc['name']
            target_id = "player"
            print(f"[Chat] Crosshair target shortcut: {speaker_id}>player (skipping target LLM)")
            _profiler.mark("target_selection done (crosshair)")
        else:
            current_location = game_context.get('zoneLocation') or game_context.get('location', 'Unknown Location')
            target_result = run_target_selection_agent(
                user_input,
                looked_at_npc,
                nearby_npcs,
                dialogue_history,
                player_name,
                current_location,
                companion_id=game_context.get('companionId'),
                follower_ids=game_context.get('followers', [])
            )
            _profiler.mark("target_selection done")

            # Parse target result - agents return IDs (e.g., "SebastianSallow", not "Sebastian Sallow")
            speaker_id, target_id = parse_target_result(target_result)

    # Convert display names to IDs if spaces detected (LLM sometimes returns display names)
    if speaker_id and ' ' in speaker_id and speaker_id.lower() != player_name.lower():
        original = speaker_id
        speaker_id = find_npc_id_by_name(speaker_id, nearby_npcs)
        if speaker_id != original:
            print(f"[Chat] Converted speaker display name '{original}' to ID '{speaker_id}'")

    if target_id and ' ' in target_id and target_id.lower() not in ('player', player_name.lower()):
        original = target_id
        target_id = find_npc_id_by_name(target_id, nearby_npcs)
        if target_id != original:
            print(f"[Chat] Converted target display name '{original}' to ID '{target_id}'")

    # Normalize target_id if it's the player's name
    if target_id and target_id.lower().replace(' ', '') == player_name.lower().replace(' ', ''):
        target_id = "player"

    if not speaker_id:
        print("[Chat] No target selected - falling back to legacy flow")
        if character_name:
            speaker_id = character_name  # character_name from HTTP input, treated as ID
            target_id = "player"
        else:
            conv_state.state = "idle"
            lua_socket.send_conversation_state("idle")
            return {"status": "no_target", "message": "No NPC to talk to"}

    # Validate speaker is in nearby list
    if not validate_speaker_in_nearby(speaker_id, nearby_npcs, load_localization):
        print(f"[Chat] REJECTED: '{speaker_id}' is not in nearby list - ending conversation")
        conv_state.state = "idle"
        lua_socket.send_conversation_state("idle")
        return {"status": "invalid_speaker", "message": f"Selected speaker '{speaker_id}' is not nearby"}

    print(f"[Chat] Target selected: {speaker_id} > {target_id}")

    # Clear attention stare lock when player engages with NPC
    if speaker_id and speaker_id in _attention_stare_locks:
        del _attention_stare_locks[speaker_id]
        print(f"[Attention] Stare lock cleared for {speaker_id}")

    # Classify move command: if keywords triggered and target is companion, ask the LLM
    if not move_command and conv_settings.get('companion_move_enabled', True) and speaker_id:
        companion_id = game_context.get('companionId')
        if companion_id and speaker_id.lower() == companion_id.lower():
            from utils.text_utils import has_move_keyword
            language = game_context.get('language', 'EN_US')
            if has_move_keyword(user_input, language):
                target_model = conv_settings.get('target_selection_model', 'meta-llama/llama-4-scout:nitro')
                speaker_display = get_display_name(speaker_id)
                move_command = run_move_classifier(user_input, player_name, speaker_display, model=target_model)

    # Handle companion move command — short-circuit before chat pipeline
    if move_command and speaker_id and conv_settings.get('companion_move_enabled', True):
        companion_id = game_context.get('companionId')
        if companion_id and speaker_id.lower() == companion_id.lower():
            print(f"[Chat] Move command for companion {companion_id}")
            lua_socket.send({"type": "move_companion", "npc_id": companion_id})
            conv_state.state = "idle"
            lua_socket.send_conversation_state("idle")
            return {"status": "move_command", "companion": companion_id}
        else:
            print(f"[Chat] +move detected but {speaker_id} is not companion ({companion_id}) — treating as normal chat")

    # Lock NPCs immediately after target selection so they don't wander during LLM + TTS
    # Speaker faces target (may already be preview-locked — LockNPCToTarget handles re-lock)
    lua_socket.send_lock_npc(speaker_id, target_id)
    # Target faces speaker (only if target is an NPC, not the player)
    if target_id and target_id.lower() != "player":
        lua_socket.send_lock_npc(target_id, speaker_id)

    # NOTE: Vision capture runs in parallel - started early by input handlers:
    # - Text input: triggered when chat opens (input/text.py)
    # - Voice/STT: triggered when user starts speaking (input/voice.py)
    # We wait for it later, right before the LLM call that needs it

    # Reset conversation state (but preserve speakers/turns if continuing)
    if is_continuation:
        # Just update state, keep tracked speakers and turn count
        conv_state.state = "processing"
    else:
        # Full reset for new conversation
        conv_state.reset()
        conv_state.state = "processing"
        conv_state.max_turns = conv_settings.get('max_turns', 6)

    # Re-apply prompt mode fields after reset (reset() clears them, but we stored in local var)
    if mode == "prompt":
        conv_state.prompt_mode = True
        conv_state.prompt_participants = parsed['participants']
        conv_state.prompt_scenario = parsed.get('scenario', '')
        conv_state.prompt_include_player = parsed.get('include_player', False)

    if is_continuation:
        conversation_topics.continue_scene(current_epoch)
    else:
        initial_topic = "NONE" if mode == "prompt" else load_persisted_topic(speaker_id)
        conversation_topics.begin_scene(current_epoch, player_name, initial_topic)
    if mode != "prompt":
        conversation_topics.append_line(player_name, player_display_text)
    scene_topic_query = conversation_topics.await_current()

    # Get display name from ID
    speaker_name = get_display_name(speaker_id)
    print(f"[Chat] Speaker: {speaker_name} (ID: {speaker_id})")

    # Track speaker for chapter evaluation at conversation end
    current_location = game_context.get('locationName', 'Unknown')
    conv_state.track_speaker(speaker_id, location=current_location, context=game_context)

    # Get character prompt
    # In prompt mode: speaking_to = target NPC, not player (unless player is included)
    if conv_state.prompt_mode:
        target_name = get_display_name(target_id) if target_id and target_id != "player" else player_name
        speaker_name, base_prompt = get_character(
            speaker_id, game_context,
            speaking_to=target_name,
            prompt_mode=True,
            scenario=conv_state.prompt_scenario
        )
    else:
        speaker_name, base_prompt = get_character(speaker_id, game_context, speaking_to=player_name)
    print(f"[Chat] Display name: {speaker_name}")

    # Refresh house points if talking to a professor (fresh data for context)
    from utils import mods as mods_module
    hp_settings = settings.get('game_mods', {}).get('house_points', {})
    if (hp_settings.get('context_enabled', True) and
        mods_module.is_mod_installed('house_points') and
        mods_module.is_professor(speaker_id)):
        lua_socket.refresh_house_points(timeout=0.5)

    # === THREE-LAYER MESSAGE STRUCTURE FOR CACHING ===
    # Layer 1: Static system message (cacheable across turns)
    observer_mode = conv_state.prompt_mode and not conv_state.prompt_include_player
    static_ctx = format_static_context(game_context, current_speaker=speaker_id, observer_mode=observer_mode)

    system_parts = [base_prompt]
    if static_ctx:
        system_parts.append(static_ctx)

    # Layer 2: User/assistant message pairs from dialogue history
    y, m, d = game_context.get('year'), game_context.get('month'), game_context.get('day')
    current_game_date = f"{y}/{int(m):02d}/{int(d):02d}" if y and m and d else game_context.get('dateFormatted', '')
    current_game_time = game_context.get('timeFormatted', '') or game_context.get('time', '')
    history_messages, event_entries = format_dialogue_as_messages(
        dialogue_history,
        for_npc_id=speaker_id,
        current_game_date=current_game_date
    )
    print(f"[Chat] Dialogue history: {len(history_messages)} messages, {len(event_entries)} events")

    # Check for cancellation before expensive memory operations
    if is_cancelled():
        print("[Chat] Cancelled before memory ops")
        conv_state.reset()
        lua_socket.send_conversation_state("idle")
        return {"status": "cancelled", "message": "Cancelled before memory ops"}

    memory_search_results = None
    memory_enabled = settings.get('memory', {}).get('enabled', True)
    if memory_enabled:
        _profiler.mark("memory_ops start")
        try:
            current_location = game_context.get('locationName', '')
            nearby_npc_names = [
                npc.get('name') or npc.get('id', '')
                for npc in nearby_npcs
                if (npc.get('id') or npc.get('name', '')).lower() != speaker_id.lower()
            ][:3]

            def fetch_contextual_memory():
                return get_contextual_memory(
                    npc_id=speaker_id, npc_name=speaker_name,
                    player_name=player_name, current_location=current_location,
                    nearby_npcs=nearby_npc_names, mentioned_entities=[]
                )

            def fetch_relevant_facts():
                if user_input and user_input.strip():
                    return search_relevant_facts(
                        npc_id=speaker_id, query=user_input,
                        npc_name=speaker_name, player_name=player_name,
                        current_game_date=current_game_date,
                        current_game_time=current_game_time,
                        topic_query=scene_topic_query,
                    )
                return None

            memory_block = None
            relevant_facts = None
            executor = ThreadPoolExecutor(max_workers=2)
            futures = {
                executor.submit(fetch_contextual_memory): 'contextual',
                executor.submit(fetch_relevant_facts): 'search'
            }
            try:
                for future in as_completed(futures, timeout=5.0):
                    name = futures[future]
                    try:
                        result = future.result()
                        if name == 'contextual':
                            memory_block = result
                            _profiler.mark("memory_contextual done")
                        else:
                            relevant_facts = result
                            _profiler.mark("memory_search done")
                    except Exception as e:
                        print(f"[Chat] Memory {name} failed: {e}")
            except TimeoutError:
                unfinished = [futures[f] for f in futures if not f.done()]
                print(f"[Chat] Memory error: {len(unfinished)} futures unfinished")
            finally:
                executor.shutdown(wait=False, cancel_futures=True)

            # Contextual memory -> system message (stable across turns)
            if memory_block:
                system_parts.append(memory_block)
                print(f"[Chat] Injected contextual memory for {speaker_id} (~{len(memory_block)//4} tokens)")

            # Search results -> dynamic context (query-dependent)
            if relevant_facts:
                memory_search_results = "### Relevant Memories\n" + "\n".join(f"- {fact}" for fact in relevant_facts)
                print(f"[Chat] {len(relevant_facts)} relevant facts for dynamic context")

            _profiler.mark("memory_ops done")
        except ImportError:
            pass
        except Exception as e:
            print(f"[Chat] Memory error: {e}")
    else:
        print("[Chat] Long-term memory disabled; skipping contextual memory/search")

    # Add action instructions based on enabled settings
    from utils import mods as mods_module

    # Build unified action instructions based on what's available
    npc_actions_enabled = conv_settings.get('actions_enabled', False)
    followers_enabled = npc_actions_enabled and conv_settings.get('followers_enabled', True)
    hp_settings = settings.get('game_mods', {}).get('house_points', {})
    house_points_enabled = (
        hp_settings.get('teacher_actions', True) and
        mods_module.is_mod_installed('house_points') and
        mods_module.is_professor(speaker_id)
    )

    # Commitment action instructions (gated by commitments.enabled)
    commitments_enabled = settings.get('commitments', {}).get('enabled', False)

    action_parts = []
    current_companion_id = str(game_context.get('companionId', '') or '')
    has_active_companion = bool(game_context.get('hasCompanion')) and bool(current_companion_id)
    is_current_companion = has_active_companion and speaker_id.lower() == current_companion_id.lower()
    current_followers = [f.lower() for f in game_context.get('followers', [])]
    is_current_follower = speaker_id.lower() in current_followers
    end_conversation_available = not is_current_companion and not is_current_follower

    has_any_consent_actions = npc_actions_enabled or followers_enabled or commitments_enabled or end_conversation_available
    director_action_exception = ""
    if conv_state.prompt_mode and conv_state.prompt_include_player:
        director_action_exception = ' A current `[DIRECTOR]` instruction is an exception to explicit request/consent requirements.'

    if has_any_consent_actions or house_points_enabled:
        # Header - explain format
        if house_points_enabled and not has_any_consent_actions:
            # House points only — these are authority actions, no consent needed
            action_parts.append(
                "**Actions:** You may include actions at the END of your response using `[Action: X]` format. "
                "When awarding or deducting house points, you MUST announce it verbally in your dialogue before the action tag."
            )
        else:
            hp_note = " House point actions are an exception — those are commands of authority and do not require consent." if house_points_enabled else ""
            action_parts.append(
                f"**Actions:** You may OPTIONALLY include an action at the END of your response using `[Action: X]` format. "
                f"ONLY use an action when {player_name} explicitly requests, agrees to, or consents to something — or when you have proposed something and {player_name} has clearly accepted. "
                f"Never add actions during casual conversation. The vast majority of responses should have NO action tag."
                + director_action_exception + hp_note
                + (" When awarding or deducting house points, you MUST announce it verbally in your dialogue before the action tag." if house_points_enabled else "")
            )

        # Companion actions - show only the relevant action based on current state
        if npc_actions_enabled:
            if is_current_companion:
                action_parts.append(
                    f"- `[Action: LeaveCompanion]` - Stop being {player_name}'s companion. "
                    f"ONLY use when {player_name} explicitly asks you to leave, stop following, or go away.{director_action_exception} Do NOT use for casual goodbyes or conversation pauses."
                )
            elif not has_active_companion:
                action_parts.append(
                    f"- `[Action: JoinAsCompanion]` - Become {player_name}'s traveling companion, following them on adventures. "
                    f"ONLY use when {player_name} explicitly invites you to come along, or when you offer and {player_name} clearly accepts.{director_action_exception}"
                )

        # Follower actions - show Follow or StopFollowing based on current state (not for companions)
        if followers_enabled and not is_current_companion:
            if is_current_follower:
                action_parts.append(
                    f"- `[Action: StopFollowing]` - You are currently following {player_name}. Use this to stop following and return to your normal routine. "
                    f"Use when {player_name} tells you to stop following, stay here, or go back — or when you decide to part ways on your own terms.{director_action_exception}"
                )
            else:
                action_parts.append(
                    f"- `[Action: Follow]` - Follow {player_name} around, walking with them on their adventures. "
                    f"ONLY use when {player_name} explicitly asks you to come along or follow them, or when you offer and {player_name} clearly accepts.{director_action_exception}"
                )

        if end_conversation_available:
            action_parts.append(
                f"- `[Action: EndConversation]` - End the conversation cleanly after your spoken reply. "
                f"ONLY use when {player_name} clearly indicates they are leaving, saying goodbye, or ending the conversation, "
                f"AND your spoken reply itself is a clear farewell or parting line.{director_action_exception} "
                f"Your spoken reply and action must agree. Do NOT use this after greetings, openings, offers, questions, invitations, or any reply that expects {player_name} to answer."
            )

        # House points actions (teachers only)
        if house_points_enabled:
            player_house = game_context.get('playerHouse', 'unknown')
            role_title = "Headmaster" if speaker_id == "PhineasBlack" else "professor"
            action_parts.append(
                f"- `[Action: AwardPoints House Amount]` - As {role_title}, award house points (e.g., `[Action: AwardPoints {player_house} 10]`). "
                f"Award for genuine merit: clever answers, bravery, helpfulness. Typically 5-20 points. Announce it in dialogue. ({player_name} is in {player_house})."
            )
            action_parts.append(
                f"- `[Action: DeductPoints House Amount]` - Deduct house points (e.g., `[Action: DeductPoints Slytherin 5]`). "
                f"Deduct for rule-breaking or disrespect. Typically 5-20 points. Announce it in dialogue. ({player_name} is in {player_house})."
            )
    elif commitments_enabled:
        # No companion/HP/follower actions — add header for commitments only
        action_parts.append(
            f"**Actions:** You may OPTIONALLY include an action at the END of your response using `[Action: X]` format. "
            f"ONLY use an action when {player_name} explicitly requests, agrees to, or consents to something — or when you have proposed something and {player_name} has clearly accepted. "
            f"Never add actions during casual conversation. The vast majority of responses should have NO action tag."
            + director_action_exception
        )

    # Commitment actions (only when enabled)
    if commitments_enabled:
        from utils.commitments import build_commitment_action_instructions
        action_parts.extend(build_commitment_action_instructions(player_name, is_current_companion=is_current_companion, npc_id=speaker_id))

    if action_parts:
        system_parts.append("\n".join(action_parts))

    # Finalize system message
    system_message = "\n\n".join(system_parts)

    # NOTE: Player TTS is running in parallel (started right after context_refresh)
    # It buffers, does Lua handshake, and plays as soon as ready - independently of this flow
    # We wait for it to complete later, before NPC speaks

    # Check for cancellation before LLM call
    if is_cancelled():
        print("[Chat] Cancelled before LLM call")
        conv_state.reset()
        lua_socket.send_conversation_state("idle")
        return {"status": "cancelled", "message": "Cancelled before LLM"}

    # Wait for vision capture to complete (it ran in parallel with target/memory ops)
    # By now it should already be done, so this should be ~0ms
    vision_settings = settings.get('agents', {}).get('vision', {})
    if (VISION_AGENT_AVAILABLE and vision_settings.get('wait_for_capture', False)
            and not is_llm_provider_feature_disabled('vision', settings)):
        _profiler.mark("vision_wait start")
        try:
            agent = vision_agent.get_agent()
            if agent:
                wait_timeout = float(vision_settings.get('wait_timeout_seconds', 5))
                agent.wait_for_capture(timeout=wait_timeout)  # Should already be done
        except Exception as e:
            print(f"[Chat] Vision wait error: {e}")
        _profiler.mark("vision_wait done")

    # Build LLM user input
    # In prompt mode, put the scene direction in the user message where the LLM will
    # give it the most attention, rather than buried in the system prompt.
    if conv_state.prompt_mode:
        target_name_display = get_display_name(target_id) if target_id and target_id != "player" else player_name
        scenario = conv_state.prompt_scenario or "have a conversation"
        director_prefix = "[DIRECTOR] " if conv_state.prompt_include_player else ""
        llm_user_input = f"({director_prefix}Scene direction: {scenario}. Speak as {speaker_name} to {target_name_display}.)"
    else:
        # Add time gap header so the LLM knows how long it's been since they last spoke
        current_time_str = game_context.get('timeFormatted', '') or game_context.get('time', '')
        gap_minutes, _ = get_time_since_last_interaction(
            dialogue_history, speaker_id, current_game_date, current_time_str, player_name=player_name
        )
        gap_str = format_time_gap(gap_minutes)
        date_formatted = game_context.get('dateFormatted', '')

        # Check if this NPC has an active commitment within the meeting window
        meeting_note = ""
        if commitments_enabled:
            try:
                from utils.commitments import has_active_commitment
                active_commitment = has_active_commitment(speaker_id, game_context)
                if active_commitment:
                    meeting_note = f", agreed to meet {player_name} here"
            except Exception:
                pass

        if gap_str and date_formatted and current_time_str:
            llm_user_input = f"--- {date_formatted} - {current_time_str} ({gap_str} since you last spoke with {player_name}{meeting_note}) ---\n{user_input}"
        elif date_formatted and current_time_str:
            llm_user_input = f"--- {date_formatted} - {current_time_str} ---\n{user_input}"
        else:
            llm_user_input = user_input

    # Determine NPC response target (needed early for streaming path)
    if conv_state.prompt_mode:
        npc_target_name = get_display_name(target_id) if target_id and target_id.lower() != "player" else player_name
    else:
        npc_target_name = player_name

    # Layer 3: Final user message with dynamic context
    if conv_state.prompt_mode:
        other_participants = [get_display_name(p) for p in conv_state.prompt_participants if p.lower() != speaker_id.lower()]
        other_participants = [p for p in other_participants if p]
        if conv_state.prompt_include_player:
            other_participants.append(player_name)
        dynamic_ctx = format_dynamic_context(
            game_context, current_speaker=speaker_id,
            participants=other_participants, observer_mode=observer_mode,
            event_entries=event_entries
        )
    else:
        dynamic_ctx = format_dynamic_context(
            game_context, current_speaker=speaker_id,
            event_entries=event_entries
        )

    # Assemble final user message: player input + separator + dynamic context + reminder
    final_user_parts = [llm_user_input]
    if dynamic_ctx:
        final_user_parts.append("---")
        final_user_parts.append(dynamic_ctx)
    if memory_search_results:
        final_user_parts.append(memory_search_results)
    if conv_settings.get('narration_enabled', False):
        narration_reminder = (
            'Remember: the timestamped history lines are context, not an output format. '
            'Follow the current narration format. Do not prefix your response with timestamps, your name, or "(to ...)".'
        )
        if conv_settings.get('spatial_grounding_enabled', True) and dynamic_ctx and '**What you can see:**' in dynamic_ctx:
            narration_reminder += (
                ' When visual context gives a character\'s current location, keep any narration spatially consistent with that '
                'location. You may invent fitting actions, but do not relocate the character to a different part of the scene.'
            )
        final_user_parts.append(narration_reminder)
    else:
        final_user_parts.append(
            'Remember: the timestamped history lines are context, not an output format. '
            'Respond with dialogue only. Do not prefix your response with timestamps, your name, or "(to ...)".'
        )
    if _needs_narration_format_nudge(history_messages, conv_settings):
        final_user_parts.append(
            'Narration mode is currently ON. Some recent history may use the old spoken-dialogue-only format; '
            'ignore that older formatting and follow the current narration format exactly: put spoken words in '
            'double quotes, put optional narration or visible action in *asterisks*, and always include at least '
            'one quoted spoken line.'
        )
    final_user_message = "\n\n".join(final_user_parts)

    # Assemble full messages array
    chat_messages = [{"role": "system", "content": system_message}]
    chat_messages.extend(history_messages)
    chat_messages.append({"role": "user", "content": final_user_message})
    kv_cache_prefix = [chat_messages[0]]

    pre_llm_context = _request_guard_context(groups=_FULL_GUARD_CONTEXT_GROUPS, timeout=1.0)
    ok, reason, refreshed_npcs = _validate_live_conversation_context(
        pre_llm_context,
        speaker_id=speaker_id,
        target_id=target_id,
    )
    if not ok:
        print(f"[Chat] Aborted before LLM ({reason})")
        guard_result = _abort_due_to_guard_failure(reason, speaker_id=speaker_id, target_id=target_id, speaker_name=speaker_name)
        if guard_result["status"] == "error":
            return {"error": guard_result["message"]}
        return {"status": guard_result["status"], "message": guard_result["message"]}

    game_context = pre_llm_context
    nearby_npcs = refreshed_npcs
    _streaming_pending_entry = None

    # Check if we can use streaming LLM→TTS pipeline
    use_streaming = _can_use_streaming_tts() and speaker_id
    if use_streaming:
        print(f"[Chat] Starting streaming LLM→TTS pipeline for {speaker_name}...")
    else:
        print(f"[Chat] Calling LLM for {speaker_name}...")

    # ─── Streaming path: LLM streams → sentences → WS TTS (parallel) ───
    if use_streaming:
        _profiler.mark("llm_stream start")
        _sentence_subtitles = conv_settings.get('sentence_subtitles', True)
        full_text_holder = {'text': '', 'raw': ''}
        llm_done = threading.Event()
        _narration_enabled = conv_settings.get('narration_enabled', False)
        session_result = start_streaming_playback_session(
            sentence_iterable=build_live_sentence_stream(
                messages=chat_messages,
                full_text_holder=full_text_holder,
                llm_done=llm_done,
                stream_sentences_func=stream_sentences,
                strip_action_tag_func=strip_action_tag,
                narration_enabled=_narration_enabled,
                log_prefix="[StreamGen]",
                speaker_id=speaker_id,
                kv_cache_prefix=kv_cache_prefix,
                kv_cache_context="npc_chat",
            ),
            speaker_id=speaker_id,
            full_text_holder=full_text_holder,
            epoch=current_epoch,
            run_streaming_tts_async=run_streaming_tts_async,
            tts_service=tts,
            lua_socket=lua_socket,
            load_settings_func=load_settings,
            can_use_streaming_tts_func=_can_use_streaming_tts,
            is_conversation_valid_func=is_conversation_valid,
            require_voice_prefetch=True,
        )
        if not session_result.get("success"):
            use_streaming = False
        else:
            voice_id = session_result.get("voice_id")
            streaming_session = session_result["session"]

    if use_streaming:
        # ─── Phase 1: Wait for TTS buffer ready (not LLM done!) ───
        # This fires as soon as the first sentence is synthesized and buffered,
        # typically 1-2s into the LLM stream rather than waiting for full completion.
        if not streaming_session.wait_for_buffer(timeout=120.0):
            print("[Chat] TTS buffer ready timeout")
            conv_state.reset()
            lua_socket.send_conversation_state("idle")
            return {"error": "TTS buffer ready timeout"}

        _profiler.mark("buffer_ready")

        # Check if LLM produced any actual dialogue (not just action tags)
        # e.g. "[Action: JoinAsCompanion]" → stripped text is empty, nothing to speak
        _streaming_no_dialogue = not full_text_holder.get('text', '').strip()
        if _streaming_no_dialogue:
            print("[Chat] Streaming: no dialogue text (action-only response), skipping play_turn")
            streaming_session.abort()
            use_streaming = False  # Fall through to non-streaming finalization for action handling

        # Wait for player TTS to complete before starting NPC TTS
        if not _streaming_no_dialogue and player_tts_thread is not None:
            print(f"[Chat] Waiting for player voice to finish...")
            player_tts_done.wait(timeout=60.0)
            print(f"[Chat] Player voice done, starting NPC response")

        if not _streaming_no_dialogue:
            # Check for cancellation
            if is_cancelled():
                print("[Chat] Cancelled after buffer ready")
                conv_state.reset()
                lua_socket.send_conversation_state("idle")
                streaming_session.abort()
                return {"status": "cancelled", "message": "Cancelled after buffer ready"}

            # Re-check all guards before NPC playback begins
            fresh_context = _request_guard_context(groups=_PLAYBACK_GUARD_CONTEXT_GROUPS, timeout=1.0)
            ok, reason, _fresh_npcs = _validate_live_conversation_context(
                fresh_context,
                speaker_id=speaker_id,
                target_id=target_id,
                require_no_playback=True,
            )
            if not ok:
                print(f"[Chat] ABORT before streaming playback ({reason})")
                streaming_session.abort()
                guard_result = _abort_due_to_guard_failure(reason, speaker_id=speaker_id, target_id=target_id, speaker_name=speaker_name)
                if guard_result["status"] == "error":
                    return {"error": guard_result["message"]}
                return {"status": guard_result["status"], "message": guard_result["message"]}

            # Set up conversation state (text may be partial but that's just internal tracking)
            conv_state.add_to_queue(speaker_name, npc_target_name, full_text_holder.get('text', ''), speaker_id=speaker_id)
            conv_state.turn_count = 1
            conv_state.state = "playing"
            if not (player_voice_enabled and TTS_AVAILABLE):
                lua_socket.send_conversation_state("playing")

            # Add pending history entry NOW (before audio starts) so interrupts can trim it
            # Text is partial but will be updated after LLM finishes
            _streaming_game_time = game_context.get('timeFormatted', '') or game_context.get('time', '')
            _y, _m, _d = game_context.get('year'), game_context.get('month'), game_context.get('day')
            _streaming_game_date = f"{_y}/{int(_m):02d}/{int(_d):02d}" if _y and _m and _d else game_context.get('dateFormatted', '')
            _streaming_earshot = get_earshot_witnesses(nearby_npcs, speaker_id)
            _streaming_pending_entry = {
                "timestamp": int(time.time()),
                "gameTime": _streaming_game_time,
                "gameDate": _streaming_game_date,
                "speaker": speaker_name,
                "voiceName": speaker_id,
                "target": npc_target_name,
                "targetId": "Player" if target_id and target_id.lower() == "player" else target_id,
                "text": full_text_holder.get('text', ''),
                "isAIResponse": True,
                "isPlayer": False,
                "type": "dialogue",
                "earshot": _streaming_earshot,
                "_playback_completed": False,
            }
            streaming_session.setup_data['_history_entry'] = _streaming_pending_entry

            playback_result = streaming_session.start_playback(
                display_name=speaker_name,
                text=full_text_holder.get('text', ''),
                turn_index=conv_state.turn_count,
                target_id=target_id,
                remove_unpaired_double_quotes=remove_unpaired_double_quotes,
                pending_history_entry=_streaming_pending_entry,
                add_pending_history=conv_state.add_pending_history,
            )
            if not playback_result.get("success"):
                conv_state.reset()
                lua_socket.send_conversation_state("idle")
                return {"error": playback_result.get("error", "play_turn failed")}

            turn_result = playback_result["turn_result"]

            _profiler.mark("streaming_setup_sent")

        # ─── Phase 2: Wait for LLM to finish (audio is playing in parallel) ───
        if not llm_done.wait(timeout=120.0):
            print("[Chat] LLM streaming timeout (audio may still be playing)")
            # Don't return error - audio is playing, just log it

        _profiler.mark("llm_stream done")
        raw_response = full_text_holder.get('raw', '')
        response = full_text_holder.get('text', '')

        if conv_state.queue:
            conv_state.queue[-1]["full_text"] = response
            if conv_state.queue[-1].get("segments"):
                conv_state.queue[-1]["segments"][0]["text"] = response

        # Update the pending history entry with final complete text
        if not _streaming_no_dialogue:
            # Strip narration markers for clean history
            hist_text = response
            if _narration_enabled:
                import re
                hist_text = re.sub(r'\*([^*]+)\*', r'\1', hist_text)
            _streaming_pending_entry['text'] = hist_text

            # Full-text subtitle mode: send complete text now that LLM is done
            if not _sentence_subtitles and response.strip() and turn_result.get("turn_id"):
                subtitle_text = response
                # Strip narration markers (*...*) for clean display
                if _narration_enabled:
                    import re
                    subtitle_text = re.sub(r'\*([^*]+)\*', r'\1', subtitle_text)
                subtitle_text = remove_unpaired_double_quotes(subtitle_text)
                lua_socket.send({
                    "type": "subtitle_update",
                    "turn_id": turn_result["turn_id"],
                    "text": subtitle_text,
                    "sentence_idx": 0,
                    "total_sentences": 1,
                    "is_narration": False,
                })

        if not raw_response:
            lua_socket.send_notification("LLM request failed - check API key")
            conv_state.state = "idle"
            lua_socket.send_conversation_state("idle")
            return {"error": "LLM request failed"}

    # ─── Non-streaming path: LLM blocks → full text → TTS ───
    else:
        _profiler.mark("llm_response start")
        raw_response = call_llm_messages(
            chat_messages,
            speaker_id=speaker_id,
            kv_cache_prefix=kv_cache_prefix,
            kv_cache_context="npc_chat",
        )
        _profiler.mark("llm_response done")
        if raw_response and raw_response != LLM_ERROR_FALLBACK:
            raw_response = strip_response_metadata(raw_response)

        if is_cancelled():
            print("[Chat] Cancelled after LLM call - discarding response")
            conv_state.reset()
            lua_socket.send_conversation_state("idle")
            return {"status": "cancelled", "message": "Cancelled after LLM"}

        if raw_response is None:
            lua_socket.send_notification("LLM request failed - check API key")
            conv_state.state = "idle"
            lua_socket.send_conversation_state("idle")
            return {"error": "LLM request failed"}

    # Parse and filter actions based on settings
    # NPC actions (JoinAsCompanion/LeaveCompanion) and house point actions are independent
    from utils import mods

    all_actions = parse_actions(raw_response)
    response = strip_response_metadata(strip_action_tag(raw_response))

    # Settings for each action type
    npc_actions_enabled = conv_settings.get('actions_enabled', False)
    followers_action_enabled = npc_actions_enabled and conv_settings.get('followers_enabled', True)
    hp_settings = settings.get('game_mods', {}).get('house_points', {})
    teacher_actions_enabled = (
        hp_settings.get('teacher_actions', True) and
        mods.is_mod_installed('house_points') and
        mods.is_professor(speaker_id)
    )
    current_companion_id = str(game_context.get('companionId', '') or '')
    has_active_companion = bool(game_context.get('hasCompanion')) and bool(current_companion_id)
    is_current_companion = has_active_companion and speaker_id.lower() == current_companion_id.lower()
    current_followers = [str(f).lower() for f in game_context.get('followers', [])]
    is_current_follower = speaker_id.lower() in current_followers

    # Filter and categorize actions
    action = "None"  # Legacy single action for JoinAsCompanion/LeaveCompanion/Follow/StopFollowing
    terminal_action = None
    house_point_actions = []  # List of parsed house point actions

    for act in all_actions:
        # Check for NPC companion actions
        if act in ("JoinAsCompanion", "LeaveCompanion"):
            companion_action_allowed = (
                (act == "JoinAsCompanion" and not has_active_companion) or
                (act == "LeaveCompanion" and is_current_companion)
            )
            if npc_actions_enabled and companion_action_allowed:
                action = act
                print(f"[Chat] NPC Action: {act}")
            elif npc_actions_enabled:
                print(f"[Chat] Ignored invalid NPC Action for current companion state: {act}")
        # Check for follower actions
        elif act in ("Follow", "StopFollowing"):
            if followers_action_enabled:
                action = act
                print(f"[Chat] Follower Action: {act}")
        elif act == "EndConversation":
            if not is_current_companion and not is_current_follower:
                terminal_action = "EndConversation"
                print(f"[Chat] Conversation Action: {act}")
        # Check for house point actions
        elif act.startswith("AwardPoints") or act.startswith("DeductPoints"):
            if teacher_actions_enabled:
                parsed = mods.parse_house_point_action(act)
                if parsed:
                    house_point_actions.append(parsed)
                    print(f"[Chat] House Points Action: {parsed['action']} {parsed['house']} {parsed['amount']}")

    history_action = action if action != "None" else None
    if use_streaming:
        streaming_history_entry = streaming_session.setup_data.get('_history_entry') if streaming_session and getattr(streaming_session, "setup_data", None) else None
        if streaming_history_entry is not None:
            if history_action:
                streaming_history_entry["action"] = history_action
            else:
                streaming_history_entry.pop("action", None)
            if terminal_action:
                streaming_history_entry["terminalAction"] = terminal_action
            else:
                streaming_history_entry.pop("terminalAction", None)

    # Process commitment actions from LLM response
    if settings.get('commitments', {}).get('enabled', False):
        try:
            from utils.commitments import process_commitment_actions, check_player_arrival
            process_commitment_actions(raw_response, speaker_id, player_name, game_context, lua_socket)
            check_player_arrival(speaker_id, game_context)
        except Exception as e:
            print(f"[Chat] Commitment processing error: {e}")

    print(f"[Chat] LLM Response: \"{response}\"")

    # Record conversation for owl post cooldown tracking
    if _owl_mail_orchestrator._running:
        _owl_mail_orchestrator.record_conversation(speaker_id)

    # Save to dialogue history
    game_time = game_context.get('timeFormatted', '') or game_context.get('time', '')
    # Use short date format (YYYY/MM/DD) consistent with Lua-originated entries
    y, m, d = game_context.get('year'), game_context.get('month'), game_context.get('day')
    game_date = f"{y}/{int(m):02d}/{int(d):02d}" if y and m and d else game_context.get('dateFormatted', '')
    now = int(time.time())

    # Get witnesses (named NPCs in earshot, excluding speaker)
    npc_earshot = get_earshot_witnesses(nearby_npcs, speaker_id)

    from utils.dialogue_db import append_entry
    from utils.tts_archive import flush_history_entry_archive

    # In prompt mode, record the director prompt instead of player dialogue
    if conv_state.prompt_mode:
        prompt_entry = {
            "timestamp": now,
            "gameTime": game_time,
            "gameDate": game_date,
            "speaker": "Director",
            "voiceName": "Director",
            "target": "",
            "targetId": None,
            "text": user_input,
            "isAIResponse": False,
            "isPlayer": False,
            "type": "prompt",  # Special type for director prompts
            "earshot": []
        }
        dialogue_history.append(prompt_entry)
        append_entry(prompt_entry)
    else:
        # Normal mode: save player input as dialogue
        player_earshot = get_earshot_witnesses(nearby_npcs, "Player")
        player_entry = {
            "timestamp": now,
            "gameTime": game_time,
            "gameDate": game_date,
            "speaker": player_name,
            "voiceName": "Player",
            "target": speaker_name,
            "targetId": speaker_id,
            "text": player_display_text,
            "isAIResponse": False,
            "isPlayer": True,
            "type": "dialogue",
            "earshot": player_earshot
        }
        dialogue_history.append(player_entry)
        append_entry(player_entry)
        player_tts_archive_holder.update({
            "sourceEntryIds": list(player_entry.get("sourceEntryIds", [])),
            "speaker": player_entry.get("speaker"),
            "voiceName": player_entry.get("voiceName"),
            "text": player_entry.get("text"),
        })
        flush_history_entry_archive(player_tts_archive_holder)

    # NPC response is pending until audio actually plays
    # Skip history for error fallback responses - TTS still plays but it shouldn't pollute history
    # Streaming path: pending entry already added in Phase 1 (with partial text, updated in Phase 2)
    non_streaming_pending_entry = None
    if not use_streaming and raw_response != LLM_ERROR_FALLBACK and response.strip():
        non_streaming_pending_entry = {
            "timestamp": int(time.time()),
            "gameTime": game_time,
            "gameDate": game_date,
            "speaker": speaker_name,
            "voiceName": speaker_id,
            "target": npc_target_name,
            "targetId": "Player" if target_id and target_id.lower() == "player" else target_id,
            "text": response,
            "isAIResponse": True,
            "isPlayer": False,
            "type": "dialogue",
            "earshot": npc_earshot,
            "_playback_completed": False,
        }
        if history_action:
            non_streaming_pending_entry["action"] = history_action
        if terminal_action:
            non_streaming_pending_entry["terminalAction"] = terminal_action

    def schedule_response_topic(topic_history_entry):
        if (
            not topic_history_entry
            or topic_history_entry.get("_topic_update_id")
            or not settings.get('memory', {}).get('enabled', True)
        ):
            return
        topic_listeners = filter_memory_enabled_npc_ids(
            [speaker_id] + list(topic_history_entry.get("earshot") or []),
            settings=settings,
        )
        if topic_listeners:
            topic_update_id = conversation_topics.schedule_reply(
                speaker_name,
                topic_history_entry.get("text") or response,
                topic_listeners,
            )
            topic_history_entry["_topic_update_id"] = topic_update_id

    if use_streaming:
        schedule_response_topic(_streaming_pending_entry)

    # Update server state
    state["current_character"] = speaker_id
    state["last_response"] = response
    state["last_action"] = action
    voice_id = None

    if use_streaming:
        # Streaming path: play_turn + setup already sent in Phase 1 (at buffer_ready).
        # Audio is playing in parallel. Send deferred actions if any were found.
        if (action != "None" or house_point_actions) and turn_result.get("success"):
            lua_socket.send({
                "type": "turn_actions",
                "turn_id": turn_result.get("turn_id"),
                "action": action,
                "house_point_actions": house_point_actions if house_point_actions else None,
            })
            print(f"[Chat] Sent deferred actions: action={action}, hp_actions={len(house_point_actions) if house_point_actions else 0}")
    else:
        # Non-streaming path: full flow (queue, re-check, play_turn, sentence-streaming TTS start)
        conv_state.add_to_queue(speaker_name, npc_target_name, response, speaker_id=speaker_id)
        conv_state.turn_count = 1
        conv_state.state = "playing"

        # Only send conversation_state if not already sent (player voice path sends it early)
        # Sending it again would clear the queue which already has the player turn
        if not (player_voice_enabled and TTS_AVAILABLE):
            lua_socket.send_conversation_state("playing")

        def before_non_streaming_play():
            fresh_context = _request_guard_context(groups=_PLAYBACK_GUARD_CONTEXT_GROUPS, timeout=1.0)
            ok, reason, _fresh_npcs = _validate_live_conversation_context(
                fresh_context,
                speaker_id=speaker_id,
                target_id=target_id,
                require_no_playback=True,
            )
            if not ok:
                print(f"[Chat] ABORT before non-streaming playback ({reason})")
                guard_result = _abort_due_to_guard_failure(reason, speaker_id=speaker_id, target_id=target_id, speaker_name=speaker_name)
                return {"success": False, "status": guard_result["status"], "message": guard_result["message"]}
            if is_cancelled():
                print("[Chat] Cancelled before TTS")
                conv_state.reset()
                lua_socket.send_conversation_state("idle")
                return {"success": False, "status": "cancelled", "message": "Cancelled before TTS"}
            return None

        playback_result = start_completed_response_streaming_playback(
            response,
            speaker_id,
            speaker_name,
            target_id,
            conv_state.turn_count,
            current_epoch,
            raw_response=raw_response,
            action=action,
            house_point_actions=house_point_actions,
            pending_history_entry=non_streaming_pending_entry,
            wait_for_prior_tts=player_tts_done if player_tts_thread is not None else None,
            before_play_callback=before_non_streaming_play,
        )
        if playback_result.get("success"):
            voice_id = playback_result.get("voice_id")
            schedule_response_topic(non_streaming_pending_entry)
        elif playback_result.get("status"):
            return {
                "status": playback_result.get("status"),
                "message": playback_result.get("message", playback_result.get("error", "")),
            }
        elif not playback_result.get("fallback_required"):
            conv_state.state = "idle"
            conv_state.queue = []
            conv_state.turn_count = 0
            lua_socket.send_conversation_state("idle")
            return {
                "error": playback_result.get("error", "TTS failed"),
                "response": response,
                "character": speaker_name,
            }
        elif speaker_id:
            try:
                abort_result = before_non_streaming_play()
                if abort_result:
                    return {
                        "status": abort_result.get("status"),
                        "message": abort_result.get("message", abort_result.get("error", "")),
                    }
                print(f"[Chat] Falling back to legacy TTS path for: {speaker_id}")
                voice = tts.get_or_create_voice(speaker_id, lua_socket=lua_socket)
                if voice:
                    voice_id = voice.get("voiceId")
                subtitle_seed_text = remove_unpaired_double_quotes(response)
                turn_result = lua_socket.send_play_turn(
                    speaker_id=speaker_id,
                    display_name=speaker_name,
                    text=subtitle_seed_text,
                    turn_index=conv_state.turn_count,
                    target_id=target_id,
                    action=action,
                    house_point_actions=house_point_actions if house_point_actions else None,
                    streaming_subtitles=False
                )
                if not turn_result.get("success"):
                    conv_state.pending_history_entries = []
                    conv_state.reset()
                    lua_socket.send_conversation_state("idle")
                    return {
                        "error": "play_turn failed",
                        "response": response,
                        "character": speaker_name,
                    }
                if non_streaming_pending_entry:
                    conv_state.add_pending_history(non_streaming_pending_entry)
                    schedule_response_topic(non_streaming_pending_entry)
                if player_tts_thread is not None:
                    print(f"[Chat] Waiting for player voice to finish...")
                    player_tts_done.wait(timeout=60.0)
                    print(f"[Chat] Player voice done, starting NPC response")
                if TTS_AVAILABLE and response.strip():
                    tts_thread = threading.Thread(
                        target=run_tts_async,
                        args=(response, speaker_id, turn_result.get("positions"), turn_result.get("turn_id"), current_epoch, non_streaming_pending_entry),
                        daemon=True
                    )
                    tts_thread.start()
            except Exception as e:
                print(f"[Chat] TTS error: {e}")
                lua_socket.send_notification(f"TTS failed: {e}")
                _discard_pending_history_if_any("[Chat]", "TTS failure")
                conv_state.state = "idle"
                conv_state.queue = []
                conv_state.turn_count = 0
                lua_socket.send_conversation_state("idle")
                return {
                    "error": f"TTS failed: {e}",
                    "response": response,
                    "character": speaker_name,
                }

    # Start interjection loop with current epoch (captured at function start)
    # The epoch is checked throughout - if a new conversation starts, this loop exits
    interjection_thread = threading.Thread(
        target=interjection_loop_worker,
        args=(game_context, current_epoch),
        daemon=True
    )
    interjection_thread.start()

    # Print conversation turn profiling summary (dev mode only)
    _profiler.conversation_summary()

    return {
        "response": response,
        "action": action,
        "character": speaker_name,
        "voice_id": voice_id,
        "tts_status": "streaming" if voice_id else "unavailable",
        "queue": conv_state.queue,
    }


def interjection_loop_worker(game_context, my_epoch):
    """Background worker with pre-buffering for smooth conversation flow.

    Args:
        game_context: Initial game context
        my_epoch: Conversation epoch - if this changes, we're stale and must exit
    """
    print(f"[Interjection] Loop started with pre-buffering (epoch={my_epoch})")
    settings = load_settings()
    pre_buffer = PreBuffer()
    _stale_logged = [False]  # Track if we've logged staleness (mutable for closure)
    pending_audio_played = [True]  # Main turn's audio is always playing when loop starts

    def is_stale(checkpoint=None):
        """Check if this conversation has been superseded.

        Args:
            checkpoint: Optional label for where the check occurred (for debugging)
        """
        if is_conversation_valid(my_epoch):
            return False
        # Log only once per loop, with checkpoint context
        if not _stale_logged[0]:
            _stale_logged[0] = True
            ctx = f" at {checkpoint}" if checkpoint else ""
            print(f"[Interjection] Epoch {my_epoch} is STALE{ctx} - new conversation took over")
        return True

    try:
        while True:
            # Stop conditions - check epoch first (most reliable)
            if is_stale("loop_start"):
                pre_buffer.abort()
                break

            # Reactive mode check - respects mode changes mid-conversation
            current_mode = mode_hotkey.get_current_mode() if MODE_HOTKEY_AVAILABLE else "default"
            if current_mode == "1to1":
                print("[Interjection] 1-to-1 mode - skipping interjections")
                break
            elif current_mode != "continuous":
                # Default mode: check turn limit
                if conv_state.turn_count >= conv_state.max_turns:
                    print(f"[Interjection] Max turns ({conv_state.max_turns}) reached")
                    break
            # continuous mode: no turn limit, continues indefinitely

            if conv_state.state != "playing":
                print("[Interjection] Not playing")
                break

            # Wait for download to complete
            print("[Interjection] Waiting for download complete...")
            if not wait_for_download_complete(timeout=60.0):
                print("[Interjection] Download wait timeout")
                break

            if is_stale("after_download_wait"):
                pre_buffer.abort()
                break

            # Run interjection agent
            last = conv_state.queue[-1] if conv_state.queue else None
            if not last:
                break

            # Request npcs + state/companion for broom-aware distance filtering
            game_context = lua_socket.request_context_refresh(
                groups=["npcs", "player", "state", "companion"],
                timeout=1.0
            )
            dialogue_history = load_dialogue_history(game_context)
            # Include pending history entries (current turn that hasn't been committed yet)
            # This ensures the interjection agent sees the latest dialogue
            if conv_state.pending_history_entries:
                dialogue_history = dialogue_history + conv_state.pending_history_entries
            # Debug: check for non-dict entries
            bad_entries = [(i, type(e).__name__, repr(e)[:100]) for i, e in enumerate(dialogue_history) if not isinstance(e, dict)]
            if bad_entries:
                print(f"[Interjection] WARNING: Found {len(bad_entries)} non-dict entries in dialogue_history!")
                for idx, typ, val in bad_entries[:3]:
                    print(f"  [{idx}] {typ}: {val}")
            player_name = game_context.get('playerName', 'Player')
            nearby_npcs_raw = game_context.get('nearbyNpcs', [])
            player_in_stealth = game_context.get('inStealth', False)

            nearby_npcs = filter_npcs_by_earshot(
                nearby_npcs_raw,
                player_in_stealth=player_in_stealth,
                player_on_broom=game_context.get('isOnMount', False),
                companion_on_broom=game_context.get('companionIsOnBroom', False),
                companion_id=game_context.get('companionId'),
                companion_following=game_context.get('hasCompanion', False)
            )
            print(f"[Interjection] NPCs within earshot: {len(nearby_npcs)} (of {len(nearby_npcs_raw)} total){' [STEALTH]' if player_in_stealth else ''}")

            if not nearby_npcs:
                print("[Interjection] No NPCs within earshot - ending conversation")
                break

            last_speaker_id = last.get('speakerId', last.get('speaker', 'Unknown'))
            last_speaker_name = get_display_name(last_speaker_id)
            last_target_name = last.get('target', player_name)

            # Skip interjection if the only nearby NPC is the one who just spoke
            non_speaker_npcs = [n for n in nearby_npcs if n.get('name', '') != last_speaker_id]
            if not non_speaker_npcs:
                print(f"[Interjection] Only {last_speaker_name} nearby - no one else to interject")
                break

            # Abort check before LLM call
            if is_stale("before_interjection_agent"):
                pre_buffer.abort()
                break

            print(f"[Interjection] Checking who responds to {last_speaker_name}...")
            interjection = run_interjection_agent(
                last_speaker_id,
                last_speaker_name,
                last_target_name,
                last.get('full_text', ''),
                nearby_npcs,
                dialogue_history,
                player_name,
                # Pass prompt mode parameters for director-prompted conversations
                prompt_mode=conv_state.prompt_mode,
                prompt_participants=conv_state.prompt_participants,
                include_player=conv_state.prompt_include_player,
                companion_id=game_context.get('companionId'),
                follower_ids=game_context.get('followers', [])
            )

            if interjection == "0":
                print("[Interjection] No one wants to speak")
                break

            # Agents return IDs (e.g., "SebastianSallow", not "Sebastian Sallow")
            speaker_id, target_id = parse_target_result(interjection)
            if not speaker_id:
                break

            # Convert display names to IDs if spaces detected (LLM sometimes returns display names)
            if speaker_id and ' ' in speaker_id and speaker_id.lower() != player_name.lower():
                original = speaker_id
                speaker_id = find_npc_id_by_name(speaker_id, nearby_npcs)
                if speaker_id != original:
                    print(f"[Interjection] Converted speaker display name '{original}' to ID '{speaker_id}'")

            if target_id and ' ' in target_id and target_id.lower() not in ('player', player_name.lower()):
                original = target_id
                target_id = find_npc_id_by_name(target_id, nearby_npcs)
                if target_id != original:
                    print(f"[Interjection] Converted target display name '{original}' to ID '{target_id}'")

            # Normalize target_id if it's the player's name
            if target_id and target_id.lower().replace(' ', '') == player_name.lower().replace(' ', ''):
                target_id = "player"

            # Safety check: don't let agent select player
            speaker_lower = speaker_id.lower().replace(' ', '')
            player_lower = player_name.lower().replace(' ', '')
            if speaker_lower == player_lower or speaker_lower == 'player':
                print(f"[Interjection] Agent selected player - ending")
                break

            # Validate speaker
            if not validate_speaker_in_nearby(speaker_id, nearby_npcs, load_localization):
                print(f"[Interjection] REJECTED: '{speaker_id}' is not in nearby list - ending conversation")
                break

            speaker_name = get_display_name(speaker_id)
            print(f"[Interjection] {speaker_name} ({speaker_id}) will respond")

            scene_topic_query = conversation_topics.await_current()

            # LOCK NPC IMMEDIATELY after decision - don't let them walk away during LLM generation!
            lua_socket.send_lock_npc(speaker_id, target_id)

            # Get full context for LLM response (state may have changed since check)
            # position needed for landmark beacons in format_game_context
            full_context = lua_socket.request_context_refresh(
                groups=["position", "state", "player", "time", "zone", "npcs", "gear", "companion", "mission"],
                timeout=1.0
            )

            # Abort check before LLM response generation
            if is_stale("before_generate_response"):
                pre_buffer.abort()
                break

            # Generate LLM response (pass pending entries for most recent context)
            response = generate_interjection_response(
                speaker_id,
                target_id,
                full_context,
                conv_state.pending_history_entries,
                topic_query=scene_topic_query,
            )
            if not response:
                break

            # Abort check after LLM (response may have taken time)
            if is_stale("after_generate_response"):
                pre_buffer.abort()
                break

            # Track speaker for chapter evaluation at conversation end
            interjection_location = full_context.get('locationName', 'Unknown')
            conv_state.track_speaker(speaker_id, location=interjection_location, context=full_context)

            if is_stale("after_track_speaker"):
                pre_buffer.abort()
                break

            # Re-check NPCs are still nearby before playing turn
            # NOTE: require_no_playback=False because the interjection loop intentionally
            # overlaps LLM generation and TTS buffering with the previous turn's audio.
            # The actual playback wait happens later at wait_for_pipeline_stop().
            # Include state and companion groups for broom-aware distance filtering
            fresh_context = _request_guard_context(groups=_PLAYBACK_GUARD_CONTEXT_GROUPS, timeout=1.0)
            ok, reason, fresh_npcs = _validate_live_conversation_context(
                fresh_context,
                speaker_id=speaker_id,
                target_id=target_id,
                require_no_playback=False,
            )
            if not ok:
                print(f"[Interjection] ABORT before play_turn ({reason})")
                if reason == "speaker_left":
                    lua_socket.send_notification(f"{speaker_name} walked away")
                elif reason == "target_left":
                    target_name = get_display_name(target_id)
                    lua_socket.send_notification(f"{target_name} walked away")
                break

            # CRITICAL: Abort check before sending turn to Lua
            # This prevents queuing new turns after conversation has been stopped
            if is_stale("before_send_play_turn"):
                pre_buffer.abort()
                break

            # Send play_turn (target_id already set from parse_target_result)
            target_name = get_display_name(target_id) if target_id.lower() != "player" else player_name
            sentence_subtitles = load_settings().get('conversation', {}).get('sentence_subtitles', True)
            subtitle_seed_text = remove_unpaired_double_quotes(response)
            conv_state.add_to_queue(speaker_name, target_name, response, speaker_id=speaker_id)
            conv_state.turn_count += 1
            turn_result = lua_socket.send_play_turn(
                speaker_id=speaker_id,
                display_name=speaker_name,
                text=subtitle_seed_text,
                turn_index=conv_state.turn_count,
                target_id=target_id,
                streaming_subtitles=sentence_subtitles
            )
            if not turn_result.get("success"):
                print(f"[Interjection] play_turn failed for {speaker_id}")
                break

            # Buffer TTS
            pre_buffer.start_buffering(
                speaker_name, speaker_id, target_id, response,
                positions=turn_result.get("positions"),
                turn_id=turn_result.get("turn_id"),
                reverb_auxbus=turn_result.get("reverb_auxbus"),
                reverb_send=turn_result.get("reverb_send", 1.0)
            )

            interjection_pending_entry = _create_unsolicited_history_entry(
                speaker_id,
                target_id,
                response,
                full_context,
                nearby_npcs,
            )
            if settings.get('memory', {}).get('enabled', True):
                topic_listeners = filter_memory_enabled_npc_ids(
                    [speaker_id] + list(interjection_pending_entry.get("earshot") or []),
                    settings=settings,
                )
                if topic_listeners:
                    topic_update_id = conversation_topics.schedule_reply(
                        speaker_name,
                        response,
                        topic_listeners,
                    )
                    interjection_pending_entry["_topic_update_id"] = topic_update_id

            def buffer_tts():
                if is_stale("buffer_tts_start") or pre_buffer.abort_flag:
                    signal_download_complete()  # Unblock next iteration even on early abort
                    return

                ready_signaled = [False]

                def on_buffer_ready(tts_stream, word_timings, visemes, sentence_boundaries=None):
                    if not ready_signaled[0]:
                        ready_signaled[0] = True
                        pre_buffer.mark_ready(
                            tts_stream, word_timings, visemes,
                            sentence_boundaries=sentence_boundaries
                        )

                # TTS abort check - called frequently during synthesis
                def tts_abort_check():
                    return is_stale() or pre_buffer.abort_flag  # No checkpoint - too frequent

                result = tts.prepare_tts(
                    response,
                    speaker_id,
                    abort_check=tts_abort_check,
                    on_ready=on_buffer_ready,
                    lua_socket=lua_socket,
                    history_entry=interjection_pending_entry,
                )

                if result and not ready_signaled[0]:
                    tts_stream, word_timings, visemes = result[:3]
                    sentence_boundaries = result[3] if len(result) > 3 else []
                    pre_buffer.mark_ready(
                        tts_stream, word_timings, visemes,
                        sentence_boundaries=sentence_boundaries
                    )
                elif not result:
                    if tts_abort_check():
                        print("[Interjection] Buffer preparation aborted (stale/stop)")
                    else:
                        print("[Interjection] Buffer preparation failed")

                # Signal download complete AFTER prepare_tts returns (synthesis finished)
                # This prevents the next buffer_thread from starting while we're still
                # receiving chunks (race condition with shared response queue)
                signal_download_complete()

            buffer_thread = threading.Thread(target=buffer_tts, daemon=True)
            buffer_thread.start()

            # Wait for pipeline (synthesis+playback) to finish
            print("[Interjection] Waiting for pipeline to finish...")
            lua_socket.wait_for_pipeline_stop(timeout=60.0)

            # Abort check after wait - if stale, stop_conversation already committed
            if is_stale("after_playback_wait"):
                pre_buffer.abort()
                break

            # Commit pending history now that audio finished (only if not stale)
            if conv_state.pending_history_entries:
                _commit_pending_history_if_any("[Interjection]")

            # Wait for buffer
            if not pre_buffer.ready_event.wait(timeout=15.0):
                print("[Interjection] Buffer timeout")
                pre_buffer.abort()
                break

            # Abort check after buffer wait
            if is_stale("after_buffer_wait"):
                pre_buffer.abort()
                break

            # Play buffered audio
            buffered = pre_buffer.consume()
            if not buffered:
                print("[Interjection] Buffer empty")
                break
            buffered["history_entry"] = interjection_pending_entry

            # Abort check before playing - don't start new audio if stale
            if is_stale("before_play_prebuffered"):
                if buffered.get("tts_stream"):
                    buffered["tts_stream"].clean_up()
                break

            pending_audio_played[0] = False  # Reset before this interjection's audio
            play_prebuffered_response(buffered, blocking=False, epoch=my_epoch)
            pending_audio_played[0] = True  # Audio started for this entry

            # Abort check before adding to history - if stale, don't add unplayed entry
            if is_stale("after_play_prebuffered"):
                break

            # Add to pending history (committed when audio completes)
            # Use full_context for time (more recent than game_context from loop start)
            conv_state.add_pending_history(interjection_pending_entry)

            print(f"[Interjection] Turn {conv_state.turn_count}: {speaker_name}")

    except Exception as e:
        print(f"[Interjection] ERROR: {e}")
        import traceback
        traceback.print_exc()
        pre_buffer.abort()

    finally:
        print(f"[Interjection] Loop exiting (epoch={my_epoch})")

        # If we're stale, a new conversation has taken over - abort and don't touch shared state
        if is_stale("finally_block"):
            pre_buffer.abort()
            return  # Message already logged by is_stale()

        # Always wait for any active pipeline before sending idle.
        # With streaming TTS, the initial NPC response audio may still be playing
        # when the interjection loop exits (synthesis finishes before playback).
        if lua_socket.pipeline_active:
            print("[Interjection] Waiting for pipeline to finish before idle...")
            lua_socket.wait_for_pipeline_stop(timeout=60.0)
        elif _is_tts_activity_active():
            # Race: player playback can briefly clear the shared pipeline flag
            # before the next NPC turn has a chance to assert its own lipsync_start.
            print("[Interjection] TTS active, waiting for pipeline sync...")
            deadline = time.time() + 1.0
            while time.time() < deadline and _is_tts_activity_active() and not lua_socket.pipeline_active:
                time.sleep(0.05)
            if lua_socket.pipeline_active:
                lua_socket.wait_for_pipeline_stop(timeout=60.0)

        if is_stale("finally_after_playback_wait"):
            pre_buffer.abort()
            return

        # NOT stale - commit pending entries if audio played, otherwise discard
        if conv_state.pending_history_entries:
            if pending_audio_played[0]:
                if not is_stale("finally_commit"):
                    _commit_pending_history_if_any("[Interjection]")
            else:
                _discard_pending_history_if_any("[Interjection]", "never played")

        conversation_topics.ensure_current_ready()

        # Clear cancellation flag when done
        clear_cancel()

        if conv_state.pending_player_input:
            print("[Interjection] Processing pending player input (continuation)")
            pending = conv_state.pending_player_input
            conv_state.pending_player_input = None
            conv_state.state = "idle"
            conv_state.interrupted = False
            lua_socket.send_conversation_state("idle")
            process_chat_request({"user_input": pending}, is_continuation=True)
        else:
            # Conversation truly ended - evaluate chapters for all speakers
            final_ai_entry = conv_state.last_completed_ai_entry.copy() if conv_state.last_completed_ai_entry else None
            speakers = set(conv_state.conversation_speakers)
            evaluate_conversation_chapters(conv_state, load_dialogue_history, get_display_name)
            conv_state.state = "idle"
            conv_state.interrupted = False
            terminal_action = str((final_ai_entry or {}).get("terminalAction") or "")
            if terminal_action == "EndConversation":
                print("[Chat] EndConversation requested by NPC response - skipping follow-up and linger")
                lua_socket.send_conversation_state("idle", end_behavior="release_all")
                lua_socket.send_conversation_finished(speakers)
            else:
                lua_socket.send_conversation_state("idle")
                follow_up_scheduled = question_follow_up.schedule_if_eligible(final_ai_entry)
                if not follow_up_scheduled:
                    # No follow-up pending; conversation is truly finished.
                    lua_socket.send_conversation_finished(speakers)

def generate_unsolicited_response(speaker_id, target_id, game_context, pending_entries=None,
                                  mode="interjection", topic=None, recent_events=None,
                                  topic_query=None):
    """Generate an unsolicited NPC response outside the normal player request flow."""
    if mode == "commentary":
        try:
            response = generate_commentary_response(
                speaker_id,
                target_id,
                game_context,
                topic=topic,
                recent_events=recent_events,
                lua_socket=lua_socket,
            )
            if response is None:
                lua_socket.send_notification("LLM request failed")
            return response
        except Exception as e:
            print(f"[Commentary] Error generating response: {e}")
            lua_socket.send_notification(f"Commentary error: {e}")
            return None

    if mode == "follow_up":
        try:
            response = generate_follow_up_response(
                speaker_id,
                target_id,
                game_context,
                last_question_text=topic or "",
                lua_socket=lua_socket,
            )
            if response is None:
                lua_socket.send_notification("LLM request failed")
            return response
        except Exception as e:
            print(f"[FollowUp] Error generating response: {e}")
            lua_socket.send_notification(f"Follow-up error: {e}")
            return None

    try:
        settings = load_settings()
        player_name = game_context.get('playerName', 'Unknown')
        target_name = get_display_name(target_id) if target_id.lower() != "player" else player_name

        # Check if we're in prompt mode (director-prompted NPC-to-NPC conversation)
        prompt_mode = (mode == "interjection") and conv_state.prompt_mode
        observer_mode = prompt_mode and not conv_state.prompt_include_player

        # Get character prompt (speaking_to = who they're responding to)
        if prompt_mode:
            speaker_name, base_prompt = get_character(
                speaker_id, game_context,
                speaking_to=target_name,
                prompt_mode=True,
                scenario=conv_state.prompt_scenario
            )
        else:
            speaker_name, base_prompt = get_character(speaker_id, game_context, speaking_to=target_name)

        prompt = base_prompt
        # Build participants list based on mode
        if prompt_mode:
            # In prompt mode, participants are from the prompt
            participants = [get_display_name(p) for p in conv_state.prompt_participants if p.lower() != speaker_id.lower()]
            if conv_state.prompt_include_player:
                participants.append(player_name)
        else:
            # Normal mode: player + target NPC
            participants = [player_name, target_name] if player_name and player_name != "Unknown" else [target_name]

        context_str = format_game_context(game_context, current_speaker=speaker_id, participants=participants, observer_mode=observer_mode)
        if context_str:
            prompt = f"{base_prompt}\n\n{context_str}"

        # Use passed context directly instead of re-fetching
        dialogue_history = load_dialogue_history(game_context)
        # Include pending entries (current conversation not yet committed to DB)
        if pending_entries:
            dialogue_history = dialogue_history + pending_entries
        y, m, d = game_context.get('year'), game_context.get('month'), game_context.get('day')
        current_game_date = f"{y}/{int(m):02d}/{int(d):02d}" if y and m and d else game_context.get('dateFormatted', '')
        current_game_time = game_context.get('timeFormatted', '') or game_context.get('time', '')
        dialogue_str = format_dialogue_history(dialogue_history, for_npc_id=speaker_id, current_game_date=current_game_date)

        if settings.get('memory', {}).get('enabled', True):
            # Inject long-term memory if available (contextual)
            try:
                current_location = game_context.get('locationName') or game_context.get('zoneLocation') or game_context.get('location', '')

                # For interjections, the "nearby" NPC is whoever they're responding to
                nearby_for_interjection = [target_name] if target_id.lower() != "player" else []

                memory_block = get_contextual_memory(
                    npc_id=speaker_id,
                    npc_name=speaker_name,
                    player_name=player_name,
                    current_location=current_location,
                    nearby_npcs=nearby_for_interjection,
                    mentioned_entities=[]
                )
                if memory_block:
                    dialogue_str = f"{memory_block}\n\n{dialogue_str}" if dialogue_str else memory_block
                    print(f"[Interjection] Injected contextual memory (~{len(memory_block)//4} tokens)")

                # Dynamic search: use last dialogue entry as query (what they're reacting to)
                # Prefer pending entries (most recent), fall back to file history
                last_entry_text = ""
                if pending_entries:
                    # Pending entries are most recent (not yet committed to file)
                    last_entry_text = pending_entries[-1].get('text', '')
                elif dialogue_history:
                    last_entry_text = dialogue_history[-1].get('text', '')

                if last_entry_text and last_entry_text.strip():
                    print(f"[Interjection] Searching memories based on: '{last_entry_text[:60]}...'")
                    search_start = time.time()
                    relevant_facts = search_relevant_facts(
                        npc_id=speaker_id,
                        query=last_entry_text,
                        npc_name=speaker_name,
                        player_name=player_name,
                        current_game_date=current_game_date,
                        current_game_time=current_game_time,
                        topic_query=topic_query,
                    )
                    search_elapsed = (time.time() - search_start) * 1000  # ms

                    if relevant_facts:
                        facts_block = "### Relevant Memories\n" + "\n".join(f"- {fact}" for fact in relevant_facts)
                        dialogue_str = f"{facts_block}\n\n{dialogue_str}" if dialogue_str else facts_block
                        print(f"[Interjection] Added {len(relevant_facts)} relevant facts (~{len(facts_block)//4} tokens) in {search_elapsed:.0f}ms")
                    else:
                        print(f"[Interjection] Memory search returned no results ({search_elapsed:.0f}ms)")
            except Exception as e:
                print(f"[Interjection] Memory error: {e}")
        else:
            print("[Interjection] Long-term memory disabled; skipping contextual memory/search")

        if mode == "commentary":
            commentary_lines = []
            for event_summary in recent_events or []:
                commentary_lines.append(f"- {event_summary}")
            if topic:
                commentary_lines.append(f"Topic hook: {topic}")
            commentary_lines.append(
                f"This is an unprompted comment to {player_name} after a recent event. No conversation is currently active."
            )
            prompt = f"{prompt}\n\n### Recent Event Context\n" + "\n".join(commentary_lines)

        if dialogue_str:
            prompt = f"{prompt}\n\n{dialogue_str}"

        # Add commitment action instructions (only when enabled)
        if settings.get('commitments', {}).get('enabled', False):
            try:
                from utils.commitments import build_commitment_action_instructions
                commitment_parts = build_commitment_action_instructions(player_name, npc_id=speaker_id)
                if commitment_parts:
                    action_header = "**Actions:** You may optionally include an action at the END of your response using `[Action: X]` format. Most responses need no action."
                    prompt = f"{prompt}\n\n{action_header}\n" + "\n".join(commitment_parts)
            except Exception as e:
                print(f"[Interjection] Error adding commitment actions: {e}")

        if mode == "commentary":
            user_input = (
                f"(You are making a brief unprompted remark as {speaker_name}. "
                f"You are speaking to {target_name}. React to the recent moment naturally in 1-2 sentences.)"
            )
        else:
            user_input = f"(You are reacting to the conversation. Respond as {speaker_name}. You are speaking to {target_name}.)"

        raw_response = call_llm(prompt, user_input, speaker_id=speaker_id)

        # Handle LLM error
        if raw_response is None:
            lua_socket.send_notification("LLM request failed")
            return None

        # Process commitment actions before stripping tags
        if settings.get('commitments', {}).get('enabled', False):
            try:
                from utils.commitments import process_commitment_actions, check_player_arrival
                process_commitment_actions(raw_response, speaker_id, player_name, game_context, lua_socket)
                check_player_arrival(speaker_id, game_context)
            except Exception as e:
                print(f"[Interjection] Commitment processing error: {e}")

        response = strip_response_metadata(strip_action_tag(raw_response))

        print(f"[{mode.title()}] {speaker_id} response: {response}")
        return response

    except Exception as e:
        print(f"[{mode.title()}] Error generating response: {e}")
        lua_socket.send_notification(f"{mode.title()} error: {e}")
        return None


def generate_interjection_response(speaker_id, target_id, game_context, pending_entries=None,
                                   topic_query=None):
    """Generate a response for an interjecting NPC."""
    return generate_unsolicited_response(
        speaker_id,
        target_id,
        game_context,
        pending_entries=pending_entries,
        mode="interjection",
        topic_query=topic_query,
    )


def play_unsolicited_commentary_turn(speaker_id, target_id, response, game_context, topic, trigger_event_type):
    """Play a single unsolicited commentary line without starting an interjection chain."""
    def before_play_guard():
        playback_context = _request_guard_context(groups=_PLAYBACK_GUARD_CONTEXT_GROUPS, timeout=1.0)
        playback_ok, playback_reason, _ = _validate_unsolicited_guard_context(
            playback_context,
            speaker_id=speaker_id,
            target_id=target_id,
            require_companion_speaker=True,
            require_no_playback=False,
        )
        if not playback_ok:
            print(f"[Commentary] Aborted before playback ({playback_reason})")
            return False
        return True

    return play_commentary_response(
        speaker_id,
        target_id,
        response,
        game_context,
        topic,
        trigger_event_type,
        lua_socket=lua_socket,
        conv_state=conv_state,
        start_new_conversation=start_new_conversation,
        clear_cancel=clear_cancel,
        is_conversation_valid=is_conversation_valid,
        play_completed_response_streaming=start_completed_response_streaming_playback,
        run_tts_async=run_tts_async,
        tts_available=TTS_AVAILABLE,
        cancel_pending_follow_up=question_follow_up.cancel_pending,
        before_play_guard=before_play_guard,
    )


def stream_unsolicited_commentary_turn(speaker_id, target_id, game_context, topic, trigger_event_type, recent_events=None):
    """Canonical commentary runtime entrypoint used by event commentary."""
    if not _try_begin_unsolicited_generation("Commentary"):
        return False

    try:
        fresh_context = _request_guard_context(groups=_FULL_GUARD_CONTEXT_GROUPS, timeout=1.0)
        ok, reason, _ = _validate_unsolicited_guard_context(
            fresh_context,
            speaker_id=speaker_id,
            target_id=target_id,
            require_companion_speaker=True,
        )
        if not ok:
            print(f"[Commentary] Aborted before generation ({reason})")
            return False

        def before_play_guard():
            playback_context = _request_guard_context(groups=_PLAYBACK_GUARD_CONTEXT_GROUPS, timeout=1.0)
            playback_ok, playback_reason, _ = _validate_unsolicited_guard_context(
                playback_context,
                speaker_id=speaker_id,
                target_id=target_id,
                require_companion_speaker=True,
                require_no_playback=False,
            )
            if not playback_ok:
                print(f"[Commentary] Aborted before playback ({playback_reason})")
                return False
            return True

        return run_commentary_turn(
            speaker_id,
            target_id,
            fresh_context,
            topic,
            trigger_event_type,
            recent_events=recent_events,
            lua_socket=lua_socket,
            conv_state=conv_state,
            start_new_conversation=start_new_conversation,
            clear_cancel=clear_cancel,
            is_conversation_valid=is_conversation_valid,
            can_use_streaming_tts=_can_use_streaming_tts,
            run_streaming_tts_async=run_streaming_tts_async,
            tts_service=tts,
            load_settings_func=load_settings,
            stream_sentences_func=stream_sentences,
            play_completed_response_streaming_func=start_completed_response_streaming_playback,
            run_tts_async=run_tts_async,
            tts_available=TTS_AVAILABLE,
            cancel_pending_follow_up=question_follow_up.cancel_pending,
            before_play_guard=before_play_guard,
        )
    finally:
        _end_unsolicited_generation()


event_commentary = EventCommentaryOrchestrator(
    lua_socket=lua_socket,
    conv_state=conv_state,
    load_dialogue_history_func=load_dialogue_history,
    generate_response_func=generate_unsolicited_response,
    play_commentary_turn_func=play_unsolicited_commentary_turn,
    stream_commentary_turn_func=stream_unsolicited_commentary_turn,
)
def _safe_thread_target(func, *args):
    """Wrapper to ensure exceptions in daemon threads get logged."""
    try:
        func(*args)
    except Exception:
        import traceback
        traceback.print_exc()

def _dispatch_game_event(raw_event):
    """Route game events to appropriate handlers."""
    event_name = raw_event.get("event", "")
    if event_name == "attention:threshold":
        # Run in background thread to avoid blocking socket receive
        threading.Thread(
            target=_safe_thread_target,
            args=(handle_attention_threshold, raw_event.get("data", {})),
            daemon=True,
        ).start()
    elif event_name == "loading:finished":
        # Send owl post summary notification after loading screen
        threading.Thread(target=send_owl_post_summary, daemon=True).start()
    elif event_name == "linger:goodbye_due":
        threading.Thread(
            target=linger_goodbye.handle_due_event,
            args=(raw_event.get("data", {}),),
            daemon=True,
        ).start()
    else:
        event_commentary.handle_event(raw_event)

lua_socket.set_game_event_callback(_dispatch_game_event)


# ============================================
# Request Gating (per-player safety net)
# ============================================
_PER_PLAYER_ROUTE_PREFIXES = ('/chat', '/api/dialogue-history', '/api/commitments',
                               '/api/memories', '/owlpost', '/api/conversation')

@app.before_request
def _check_player_loaded():
    from utils import player_context
    if not player_context.is_ready():
        if any(request.path.startswith(p) for p in _PER_PLAYER_ROUTE_PREFIXES):
            return jsonify({"error": "Player context not ready"}), 503


# ============================================
# Endpoints
# ============================================
@app.route('/health', methods=['GET'])
def health():
    # Get game time from cached context
    context = lua_socket.get_game_context()
    game_time = {
        "year": context.get("year"),
        "month": context.get("month"),
        "day": context.get("day"),
        "dayOfWeek": context.get("dayOfWeek"),
        "hour": context.get("hour"),
        "minute": context.get("minute"),
        "gameTime": context.get("gameTime"),
        "available": bool(context.get("gameTime"))
    }

    # VR status
    vr_info = {"active": False}
    try:
        from vr import is_vr_active, get_vr_tracker
        if is_vr_active():
            vr_info["active"] = True
            tracker = get_vr_tracker()
            if tracker and tracker._backend:
                vr_info["backend"] = tracker._backend.name
    except Exception:
        pass

    # Companion info
    companion = None
    if context.get('hasCompanion') and context.get('companionId'):
        cid = context['companionId']
        companion = {"id": cid, "name": get_display_name(cid)}

    # NPC followers
    followers = []
    for fid in context.get('followers', []):
        followers.append({"id": fid, "name": get_display_name(fid)})

    return jsonify({
        "status": "ok",
        "version": VERSION,
        "tts": TTS_AVAILABLE,
        "tts_provider": tts.get_provider_name() if TTS_AVAILABLE else None,
        "audio3d": AUDIO3D_AVAILABLE,
        "game_time": game_time,
        "vr": vr_info,
        "companion": companion,
        "followers": followers,
        "player_house": context.get("playerHouse", ""),
    })


@app.route('/chat', methods=['POST'])
def chat():
    print("\n" + "=" * 40)
    print("[Chat] HTTP Request received")

    data = request.get_json() or {}
    result = process_chat_request(data)

    if "error" in result:
        return jsonify(result), 400

    print(f"[Chat] Returning: {result}")
    print("=" * 40 + "\n")

    return jsonify(result)


@app.route('/status', methods=['GET'])
def status():
    return jsonify({
        "tts_active": state["tts_active"],
        "current_character": state["current_character"],
        "last_response": state["last_response"],
        "last_action": state["last_action"],
    })


@app.route('/stop', methods=['POST'])
def stop():
    return jsonify({"status": "ok"})


# ============================================
# Conversation State Endpoints
# ============================================
@app.route('/api/conversation/state', methods=['GET'])
def get_conversation_state():
    return jsonify({
        "state": conv_state.state,
        "queue": conv_state.queue,
        "current_index": conv_state.current_index,
        "turn_count": conv_state.turn_count,
        "max_turns": conv_state.max_turns,
        "interrupted": conv_state.interrupted,
        "pending_player_input": conv_state.pending_player_input is not None
    })


@app.route('/api/conversation/state', methods=['POST'])
def update_conversation_state():
    data = request.get_json() or {}

    if 'current_index' in data:
        conv_state.current_index = data['current_index']
    if 'state' in data:
        conv_state.state = data['state']

    if data.get('playback_complete'):
        if conv_state.pending_player_input:
            pending = conv_state.pending_player_input
            conv_state.pending_player_input = None
            conv_state.state = "idle"
            lua_socket.send_conversation_state("idle")
            return jsonify({"status": "pending_input", "input": pending})
        else:
            conv_state.state = "idle"
            lua_socket.send_conversation_state("idle")

    return jsonify({"status": "ok"})


@app.route('/api/conversation/interrupt', methods=['POST'])
def interrupt_conversation():
    data = request.get_json() or {}
    player_input = data.get('input', '')

    if conv_state.state == "playing":
        conv_state.pending_player_input = player_input
        conv_state.interrupted = True
        lua_socket.send_conversation_state("playing", interrupted=True)
        return jsonify({"status": "queued_interrupt"})
    else:
        return jsonify({"status": "not_playing"})


@app.route('/api/conversation/queue', methods=['GET'])
def get_conversation_queue():
    return jsonify({
        "queue": conv_state.queue,
        "current_index": conv_state.current_index,
        "state": conv_state.state
    })


def _cleanup_resources():
    """Shared cleanup for all shutdown paths (restart, shutdown, finally).

    Returns memory_shutdown_ok (bool).
    """
    # Stop orchestrators
    try:
        _owl_mail_orchestrator.stop()
    except:
        pass
    try:
        _owl_board_orchestrator.stop()
    except:
        pass

    # Stop hotkeys and overlays
    if OWLPOST_HOTKEY_AVAILABLE:
        try:
            owlpost_hotkey.stop_capture()
        except:
            pass
        try:
            grimoire_hotkey.stop_capture()
        except:
            pass
        try:
            if _overlay_manager:
                _overlay_manager.close_all()
        except:
            pass

    # Disconnect TTS WebSocket
    if TTS_AVAILABLE:
        try:
            provider = tts.get_provider()
            if hasattr(provider, 'disconnect_websocket'):
                provider.disconnect_websocket()
        except Exception:
            pass

    # Audio
    if AUDIO3D_AVAILABLE:
        try:
            audio_shutdown()
        except:
            pass

    # Input/STT
    if INPUT_CAPTURE_AVAILABLE:
        try:
            input_capture.stop_capture()
        except:
            pass
    if STT_AVAILABLE:
        try:
            stt_capture.stop_capture()
        except:
            pass
    if STOP_CAPTURE_AVAILABLE:
        try:
            stop_capture.stop_capture()
        except:
            pass

    # Vision agent
    if VISION_AGENT_AVAILABLE:
        try:
            vision_agent.stop_agent()
        except:
            pass

    # Lua socket
    try:
        lua_socket.stop()
    except:
        pass

    # Memory (returns success/failure)
    memory_shutdown_ok = True
    try:
        from utils.memory_queue import graceful_shutdown as graceful_memory_shutdown
        memory_shutdown_ok = graceful_memory_shutdown(max_wait=30.0)
    except Exception as e:
        memory_shutdown_ok = False
        print(f"[Server] Memory shutdown error: {e}")

    # Owl Post DB
    try:
        from utils.owl_post_db import close_all_connections as close_owl_db
        close_owl_db()
    except Exception:
        pass

    return memory_shutdown_ok


@app.route('/restart', methods=['POST'])
def restart_server():
    """Signal restart - clears lock files so Lua can restart immediately."""
    print("[Server] Restart requested")

    try:
        # Signal batch heartbeat to stop
        stop_file = os.path.join(SONORUS_DIR, "server.lock.stop")
        with open(stop_file, "w") as f:
            f.write("stop")
        print(f"[Server] Stop signal written to {stop_file}")

        # Delete lock file so Lua doesn't wait 60s
        lock_file = os.path.join(SONORUS_DIR, "server.lock")
        if os.path.exists(lock_file):
            os.remove(lock_file)
            print("[Server] Lock file removed")

        memory_shutdown_ok = _cleanup_resources()
        if not memory_shutdown_ok:
            print("[Server] Restart aborted - memory shutdown did not complete cleanly")
            return jsonify({"error": "Memory shutdown did not complete cleanly. Restart aborted to avoid graph corruption."}), 503

        # Schedule exit - os._exit is clean, no cleanup handlers
        def force_exit():
            print("[Server] Exiting...")
            os._exit(0)

        from threading import Timer
        Timer(0.3, force_exit).start()

        print("[Server] Exiting in 0.3s...")
        return jsonify({"status": "restarting"})
    except Exception as e:
        print(f"[Server] Restart error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/shutdown', methods=['POST'])
def shutdown():
    print("[Server] Shutdown requested")

    memory_shutdown_ok = _cleanup_resources()
    if not memory_shutdown_ok:
        print("[Server] Shutdown aborted - memory shutdown did not complete cleanly")
        return jsonify({"error": "Memory shutdown did not complete cleanly. Shutdown aborted to avoid graph corruption."}), 503

    func = request.environ.get('werkzeug.server.shutdown')
    if func:
        func()
    else:
        os._exit(0)

    return jsonify({"status": "shutting_down"})


# NOTE: Config, Setup, Memory, and Dialogue API endpoints have been moved to routes/ modules
# See: routes/config.py, routes/setup.py, routes/memory.py, routes/dialogue.py


# ============================================
# Main
# ============================================
@app.route('/api/conversation/reset', methods=['POST'])
def reset_conversation():
    conv_state.reset()
    lua_socket.send_conversation_state("idle")
    print("[Server] Conversation state reset to idle")
    return jsonify({"status": "ok", "message": "Conversation state reset"})


@app.route('/api/stt/restart', methods=['POST'])
def restart_stt():
    """Restart STT capture with fresh settings (e.g., after enabling/disabling open mic)."""
    try:
        from input import voice as stt_capture
        success = stt_capture.restart_capture()
        if success:
            return jsonify({"success": True, "message": "STT capture restarted"})
        else:
            return jsonify({"success": False, "error": "STT not available or not configured"})
    except Exception as e:
        print(f"[Server] Error restarting STT: {e}")
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/dismiss-companion', methods=['POST'])
def dismiss_companion():
    lua_socket.send_dismiss_companion()
    return jsonify({"status": "ok"})


@app.route('/api/dismiss-follower', methods=['POST'])
def dismiss_follower():
    data = request.get_json() or {}
    voice_name = data.get('voice_name')
    if not voice_name:
        return jsonify({"error": "voice_name required"}), 400
    lua_socket.send_dismiss_follower(voice_name)
    return jsonify({"status": "ok"})


def main():
    # Set up logging to file for troubleshooting support
    from datetime import datetime
    logs_dir = os.path.join(SONORUS_DIR, "logs")
    os.makedirs(logs_dir, exist_ok=True)

    # Find next session number for today
    today = datetime.now().strftime("%Y-%m-%d")
    session = 1
    while os.path.exists(os.path.join(logs_dir, f"server_{today}_{session}.log")):
        session += 1
    log_path = os.path.join(logs_dir, f"server_{today}_{session}.log")

    class TeeLogger:
        def __init__(self, file_handle, original):
            self.file = file_handle
            self.original = original
        def write(self, msg):
            self.original.write(msg)
            self.original.flush()
            try:
                self.file.write(msg)
                self.file.flush()
            except:
                pass
        def flush(self):
            self.original.flush()
            try:
                self.file.flush()
            except:
                pass
    try:
        log_file = open(log_path, 'w', encoding='utf-8')
        sys.stdout = TeeLogger(log_file, sys.__stdout__)
        sys.stderr = TeeLogger(log_file, sys.__stderr__)
        print(f"[Server] Logging to {log_path}")
    except Exception as e:
        print(f"[Server] Warning: Could not set up log file: {e}")

    port = int(os.getenv("SONORUS_SERVER_PORT", "5400"))

    # Initialize runtime databases
    # dialogue_db init deferred to first player handshake via PlayerContext
    event_logger.init_db()

    # Start game monitor
    start_game_monitor()

    # Start mod checker (detects installed game mods)
    from utils import mods
    mods.start_mod_checker()

    # Start socket server
    lua_socket.start()

    # Start heartbeat thread
    def heartbeat_loop():
        running_file = os.path.join(SONORUS_DIR, "server.heartbeat")
        temp_file = running_file + ".tmp"
        while True:
            try:
                # Atomic write: write to temp file, then rename
                # This prevents race condition where Lua reads truncated/empty file
                with open(temp_file, "w") as f:
                    f.write(str(int(time.time())))
                os.replace(temp_file, running_file)
            except:
                pass
            time.sleep(1)

    heartbeat_thread = threading.Thread(target=heartbeat_loop, daemon=True)
    heartbeat_thread.start()

    # Start setup reminder thread - reminds user to complete setup every 30 seconds
    def setup_reminder_loop():
        while True:
            time.sleep(30)
            if not is_setup_complete():
                print("")
                print("=" * 60)
                print("  ⚠️  SETUP NOT COMPLETE  ⚠️")
                print("")
                print("  Please complete the setup wizard in your browser:")
                print(f"  http://localhost:{port}/#chapterSetup")
                print("")
                print("  (This message will stop once setup is complete)")
                print("=" * 60)
                print("")

    setup_reminder_thread = threading.Thread(target=setup_reminder_loop, daemon=True)
    setup_reminder_thread.start()

    # Fetch model capabilities (for reasoning support detection)
    try:
        llm.fetch_model_capabilities()
    except Exception as e:
        print(f"[Server] Failed to fetch model capabilities: {e}")

    # Pre-warm LLM client connection pool
    try:
        llm.prewarm_client()
    except Exception as e:
        print(f"[Server] LLM pre-warm failed: {e}")

    # Start vision agent
    if VISION_AGENT_AVAILABLE:
        try:
            vision_agent.start_agent()
            print("[Server] Vision agent started")
        except Exception as e:
            print(f"[Server] Vision agent failed to start: {e}")

    # Start input capture
    if INPUT_CAPTURE_AVAILABLE:
        settings = load_settings()
        input_settings = settings.get('input', {})

        if input_settings.get('chat_enabled', True):
            hotkey = input_settings.get('chat_hotkey', 'enter')

            def check_game_paused():
                # This callback runs inside the low-level Windows keyboard hook.
                # Use the socket receiver's thread-safe cache; never block here.
                context = lua_socket.get_game_context()
                player_loaded = context.get('playerLoaded', False)
                is_paused = context.get('isGamePaused', False)
                if not player_loaded:
                    print(f"[InputCapture] check_pause: playerLoaded={player_loaded}, blocking chat")
                    return True
                if is_paused:
                    print(f"[InputCapture] check_pause: isGamePaused={is_paused}, blocking chat")
                    return True
                # Block in cinematic (but NOT combat - allow spell voice commands)
                if context.get('inCinematic'):
                    print("[InputCapture] Blocked - in cinematic")
                    return True
                # Combat check moved to process_chat_request (after spell detection)
                return False

            def on_chat_input(msg):
                msg_type = msg.get("type")
                active = msg.get("active", False)
                text = msg.get("text", "")
                # print(f"[InputCapture] Sending to Lua: type={msg_type} active={active} text='{text[:20]}'")
                send_result = lua_socket.send(msg)
                if not send_result:
                    print(f"[InputCapture] WARNING: lua_socket.send() returned False - message not sent!")

                if (msg_type == "chat_input" and active) or msg_type == "chat_submit":
                    question_follow_up.cancel_pending(f"chat_input:{msg_type}")
                    linger_goodbye.cancel_pending(f"chat_input:{msg_type}")

                if msg_type == "chat_submit":
                    if check_game_paused():
                        print("[InputCapture] Submit blocked - game is paused")
                        return
                    text = msg.get("text", "").strip()
                    mode = msg.get("mode", "chat")  # "chat" or "prompt" (director mode)
                    if text:
                        print(f"[InputCapture] Processing {mode}: {text}")
                        threading.Thread(
                            target=process_chat_request,
                            args=({"user_input": text, "mode": mode},),
                            daemon=True
                        ).start()

            try:
                input_capture.start_capture(on_chat_input, hotkey, check_pause=check_game_paused)
                print(f"[Server] Input capture started (hotkey: {hotkey})")
            except Exception as e:
                print(f"[Server] Input capture failed to start: {e}")
        else:
            print("[Server] Input capture disabled in settings")

    # Start STT capture if enabled (always register callbacks for hot-reload)
    if STT_AVAILABLE:
        settings = load_settings()
        stt_settings = settings.get('stt', {})

        def check_stt_paused():
            """Check if STT should be blocked (called before recording starts).
            Returns a string reason if blocked, empty string if not blocked.
            Falsy empty string == not paused, truthy reason string == paused.
            """
            # Quick state-only handshake (no periodic polling anymore)
            context = lua_socket.request_state_only(timeout=0.2)
            player_loaded = context.get('playerLoaded', False)
            is_paused = context.get('isGamePaused', False)
            if not player_loaded:
                return "player not loaded"
            if is_paused:
                return "game paused/UI shown"
            # Block in cinematic
            if context.get('inCinematic'):
                stt_capture.play_error_sound()
                return "in cinematic"
            # Block combat only if voice_spells is disabled (no point transcribing)
            # If voice_spells is enabled, allow STT during combat for spell casting
            if context.get('inCombat'):
                settings = load_settings()
                if not settings.get('stt', {}).get('voice_spells', True):
                    stt_capture.play_error_sound()
                    return "in combat (voice_spells disabled)"
            return ""

        def on_stt_transcribe(text):
            """Handle transcribed speech - same as typed text but skip player voice TTS."""
            if text:
                # Check for director mode prefix ("direct ..." or localized equivalent)
                language = load_settings().get('setup', {}).get('language', 'EN_US')
                text, mode = extract_director_prefix(text, language)
                if not text:
                    return
                print(f"[STT] Processing ({mode}): {text}")
                threading.Thread(
                    target=process_chat_request,
                    args=({"user_input": text, "mode": mode, "from_stt": True},),
                    daemon=True
                ).start()

        def on_stt_error(error_msg):
            """Show STT errors as in-game notifications."""
            lua_socket.send_notification(error_msg)

        def on_stt_interrupt():
            """Handle interruption when new speech is detected (open mic mode)."""
            # Only interrupt if conversation is active
            question_follow_up.cancel_pending("stt_interrupt")
            linger_goodbye.cancel_pending("stt_interrupt")
            if conv_state.state == "idle" and not lua_socket.pipeline_active:
                return
            stop_conversation(source="open_mic_interrupt", notify=False)

        def on_stt_soft_interrupt():
            """Pause audio when open mic VAD triggers (soft interrupt)."""
            question_follow_up.cancel_pending("stt_soft_interrupt")
            linger_goodbye.cancel_pending("stt_soft_interrupt")
            if conv_state.state == "idle" and not lua_socket.pipeline_active:
                return
            if AUDIO3D_AVAILABLE and audio_get_player:
                try:
                    player = audio_get_player()
                    if player:
                        player.pause()
                except Exception as e:
                    print(f"[Server] Pause error: {e}")
            try:
                from audio.playback import get_coordinator
                coordinator = get_coordinator()
                if coordinator:
                    coordinator.pause()
            except Exception as e:
                print(f"[Server] Coordinator pause error: {e}")

        def on_stt_soft_interrupt_cancel():
            """Resume audio after VAD false positive (no transcription)."""
            pause_duration = 0.0
            if AUDIO3D_AVAILABLE and audio_get_player:
                try:
                    player = audio_get_player()
                    if player:
                        pause_duration = player.resume()
                except Exception as e:
                    print(f"[Server] Resume error: {e}")
            if pause_duration > 0:
                try:
                    from audio.playback import get_coordinator
                    coordinator = get_coordinator()
                    if coordinator:
                        coordinator.resume(pause_duration)
                except Exception as e:
                    print(f"[Server] Coordinator resume error: {e}")

        # Always register callbacks (enables hot-reload from disabled state)
        stt_capture.register_callbacks(on_stt_transcribe, check_pause=check_stt_paused, on_error=on_stt_error, on_interrupt=on_stt_interrupt)

        if stt_service.is_available():
            stt_hotkey = stt_settings.get('hotkey', 'middle_mouse')
            try:
                stt_capture.start_capture(on_stt_transcribe, stt_hotkey, check_pause=check_stt_paused, on_error=on_stt_error, on_interrupt=on_stt_interrupt)
                # Wire soft interrupt callbacks for open mic pause/resume
                capture = stt_capture.get_capture()
                if capture:
                    capture.on_speech_start = lambda: (
                        question_follow_up.cancel_pending("stt_speech_start"),
                        linger_goodbye.cancel_pending("stt_speech_start")
                    )
                    capture.on_soft_interrupt = on_stt_soft_interrupt
                    capture.on_soft_interrupt_cancel = on_stt_soft_interrupt_cancel
                print(f"[Server] STT capture started (hotkey: {stt_hotkey})")
            except Exception as e:
                print(f"[Server] STT capture failed to start: {e}")
        else:
            provider = stt_settings.get('provider', 'none')
            if provider == 'none':
                print("[Server] STT disabled (provider: none)")
            else:
                print(f"[Server] STT provider '{provider}' not configured (missing API key)")

    # Start stop conversation hotkey capture
    if STOP_CAPTURE_AVAILABLE:
        settings = load_settings()
        input_settings = settings.get('input', {})
        stop_hotkey = input_settings.get('stop_hotkey', 'delete')

        def check_stop_paused():
            # Quick state-only handshake (no periodic polling anymore)
            context = lua_socket.request_state_only(timeout=0.2)
            player_loaded = context.get('playerLoaded', False)
            is_paused = context.get('isGamePaused', False)
            return is_paused or not player_loaded

        def on_stop_pressed():
            """Handle stop conversation hotkey press."""
            stop_conversation(source="hotkey", notify=True)

        try:
            stop_capture.start_capture(on_stop_pressed, stop_hotkey, check_pause=check_stop_paused)
            print(f"[Server] Stop capture started (hotkey: {stop_hotkey})")
        except Exception as e:
            print(f"[Server] Stop capture failed to start: {e}")

    # Start conversation mode hotkey capture
    if MODE_HOTKEY_AVAILABLE:
        settings = load_settings()
        input_settings = settings.get('input', {})
        mode_hotkey_key = input_settings.get('mode_hotkey', 'home')

        def check_mode_paused():
            # Quick state-only handshake (same as check_stop_paused)
            context = lua_socket.request_state_only(timeout=0.2)
            player_loaded = context.get('playerLoaded', False)
            is_paused = context.get('isGamePaused', False)
            return is_paused or not player_loaded

        mode_hotkey.set_lua_socket(lua_socket)
        try:
            mode_hotkey.start_capture(mode_hotkey_key, check_pause=check_mode_paused)
            print(f"[Server] Mode hotkey capture started (hotkey: {mode_hotkey_key})")
        except Exception as e:
            print(f"[Server] Mode hotkey capture failed to start: {e}")

    # Start first-person view hotkey capture
    if FPV_HOTKEY_AVAILABLE:
        settings = load_settings()
        input_settings = settings.get('input', {})
        fpv_hotkey_key = input_settings.get('fpv_hotkey', 'insert')

        def check_fpv_paused():
            context = lua_socket.request_state_only(timeout=0.2)
            player_loaded = context.get('playerLoaded', False)
            is_paused = context.get('isGamePaused', False)
            return is_paused or not player_loaded

        fpv_hotkey.set_lua_socket(lua_socket)
        try:
            fpv_hotkey.start_capture(fpv_hotkey_key, check_pause=check_fpv_paused)
            print(f"[Server] FPV hotkey capture started (hotkey: {fpv_hotkey_key})")
        except Exception as e:
            print(f"[Server] FPV hotkey capture failed to start: {e}")

    # Start Owl Post hotkey capture
    if OWLPOST_HOTKEY_AVAILABLE:
        settings = load_settings()
        input_settings = settings.get('input', {})
        owlpost_hotkey_key = input_settings.get('owlpost_hotkey', 'backquote')

        def check_owlpost_paused():
            settings = load_settings()
            if is_llm_provider_feature_disabled('owl_post', settings):
                return True
            if not settings.get('owl_post', {}).get('enabled', True):
                return True
            context = lua_socket.request_state_only(timeout=0.2)
            player_loaded = context.get('playerLoaded', False)
            is_paused = context.get('isGamePaused', False)
            return is_paused or not player_loaded

        global _overlay_manager
        _overlay_manager = OverlayManager()

        _overlay_manager.register('owlpost', BrowserOverlay(
            url=f"http://localhost:{port}/owlpost/?v=1.2",
            title_match="owl post",
            profile_name="owlpost_overlay",
            width_pct=0.5,
            height_pct=0.75,
        ))
        _overlay_manager.register('grimoire', BrowserOverlay(
            url=f"http://localhost:{port}/?overlay=true&v=1.2",
            title_match="grimoire (overlay)",
            profile_name="grimoire_overlay",
            width_pct=0.5,
            height_pct=0.75,
        ))

        # Owl Post hotkey toggles through the manager (closes grimoire if open)
        owlpost_hotkey.set_overlay(_overlay_manager.toggler('owlpost'), title_match="owl post")
        try:
            owlpost_hotkey.start_capture(owlpost_hotkey_key, check_pause=check_owlpost_paused)
            print(f"[Server] Owl Post hotkey capture started (hotkey: {owlpost_hotkey_key})")
        except Exception as e:
            print(f"[Server] Owl Post hotkey capture failed to start: {e}")

        # Grimoire hotkey (hold Escape) toggles through the manager (closes owl post if open)
        grimoire_hotkey.set_overlay(_overlay_manager.toggler('grimoire'), title_match="grimoire (overlay)")
        try:
            grimoire_hotkey.start_capture(check_pause=check_owlpost_paused)
        except Exception as e:
            print(f"[Server] Grimoire hotkey capture failed to start: {e}")

    print("=" * 50)
    print("Sonorus Server")
    print("=" * 50)
    print(f"[Server] PID: {os.getpid()}")
    print(f"[Server] Port: {port}")
    print(f"[Server] TTS: {TTS_AVAILABLE}")
    print(f"[Server] Audio3D: {AUDIO3D_AVAILABLE}")

    # Detect GPU early (cached for later use by TTS/model selection)
    detect_gpu()

    # Initialize VR headset tracking early (before first audio playback)
    if AUDIO3D_AVAILABLE:
        try:
            from vr import init_vr_tracker, set_stop_conversation_callback
            init_vr_tracker(lua_socket=lua_socket)
            set_stop_conversation_callback(lambda: stop_conversation(source="vr_gesture_hold", notify=True))
        except Exception as e:
            print(f"[Server] VR tracker init failed: {e}")

    # Initialize TTS voice cache
    if TTS_AVAILABLE:
        print(f"[Server] Loading TTS voice cache ({tts.get_provider_name()})...")
        try:
            tts.init()
            # Clean up any duplicate voices detected during cache load
            provider = tts.get_provider()
            if provider:
                provider.cleanup_duplicate_voices()
        except Exception as e:
            print(f"[Server] TTS init failed: {e}")
            print("[Server] TTS will attempt to initialize on first use.")

        # Connect Inworld WebSocket for persistent TTS streaming
        if tts.get_provider_name().lower() == 'inworld':
            try:
                provider = tts.get_provider()
                if hasattr(provider, 'connect_websocket'):
                    if provider.connect_websocket():
                        print("[Server] Inworld WebSocket TTS connected")
                    else:
                        print("[Server] Inworld WebSocket TTS unavailable (falling back to HTTP)")
            except Exception as e:
                print(f"[Server] Inworld WebSocket init failed: {e}")

        # Preload Pocket TTS model synchronously if it's the active provider
        # (prevents lag on first TTS use)
        if tts.get_provider_name().lower() == 'pocket':
            print("[Server] Preloading Pocket TTS model...")
            try:
                from services.pocket_tts_onnx import warm_up
                warm_up()
                print("[Server] Pocket TTS model loaded")
            except Exception as e:
                print(f"[Server] Pocket TTS preload failed: {e}")

    # Register background worker lifecycle with PlayerContext
    # (memory init and orchestrator starts are deferred to first player handshake)
    from utils import player_context

    def _stop_workers():
        """Gracefully stop background workers before a player switch."""
        try:
            _owl_mail_orchestrator.stop()
        except Exception as e:
            print(f"[Server] Error stopping mail orchestrator: {e}")
        try:
            _owl_board_orchestrator.stop()
        except Exception as e:
            print(f"[Server] Error stopping board orchestrator: {e}")
        try:
            from utils.memory_queue import graceful_shutdown
            graceful_shutdown(max_wait=30.0)
        except Exception as e:
            print(f"[Server] Error in memory graceful shutdown: {e}")

    def _start_workers():
        """Start background workers after a player switch."""
        try:
            from utils.memory import init_memory, is_memory_available
            if is_memory_available():
                init_memory()
        except Exception as e:
            print(f"[Server] Error initializing memory: {e}")
        try:
            _owl_mail_orchestrator.start()
            _owl_board_orchestrator.start()
        except Exception as e:
            print(f"[Server] Error starting orchestrators: {e}")

    player_context.get_context().set_worker_lifecycle(_stop_workers, _start_workers)

    print(f"[Server] Starting on http://localhost:{port}")
    print(f"[Server] Config page: http://localhost:{port}/")
    print("[Server] Ready!")

    # Validate or generate freeform emote vectors without blocking server startup.
    ensure_emote_index_async()

    # Preload ONNX models in background (VAD + turn detection)
    # This prevents game lag when open mic is first activated
    def preload_speech_models():
        try:
            settings = load_settings()

            # Preload VAD + turn detection models only if open_mic is enabled
            if settings.get('open_mic', {}).get('enabled', False):
                try:
                    from services.vad import VADProcessor
                    # VADProcessor lazy-loads model on first use
                    _ = VADProcessor(threshold=0.5, sample_rate=16000)
                except Exception as e:
                    print(f"[Server] VAD preload failed: {e}")
                try:
                    from services.turn_detection import TurnDetector
                    detector = TurnDetector()
                    # Warm up with dummy prediction - forces ONNX session + feature extractor load
                    # turn_detection.py will print status messages when loading
                    import numpy as np
                    dummy_audio = np.zeros(16000, dtype=np.float32)
                    detector.predict(dummy_audio)
                except Exception as e:
                    print(f"[Server] Turn detection preload failed: {e}")

            # Preload Deepgram client if it's the active STT provider
            if settings.get('stt', {}).get('provider') == 'deepgram':
                try:
                    from services.deepgram_stt import warm_up as warmup_deepgram
                    warmup_deepgram()
                except Exception as e:
                    print(f"[Server] Deepgram STT preload failed: {e}")

            # Preload Parakeet STT worker if it's the active STT provider
            if settings.get('stt', {}).get('provider') == 'parakeet':
                try:
                    from services.parakeet_stt import warm_up as warmup_parakeet
                    warmup_parakeet()
                    print("[Server] Parakeet STT worker preloaded")
                except Exception as e:
                    print(f"[Server] Parakeet STT preload failed: {e}")

            # Preload Canary STT worker if it's the active STT provider
            if settings.get('stt', {}).get('provider') == 'canary':
                try:
                    from services.canary_stt import warm_up as warmup_canary
                    warmup_canary()
                    print("[Server] Canary STT worker preloaded")
                except Exception as e:
                    print(f"[Server] Canary STT preload failed: {e}")

            # Preload Moonshine STT worker if it's the active STT provider
            if settings.get('stt', {}).get('provider') == 'moonshine':
                try:
                    from services.moonshine_stt import warm_up as warmup_moonshine
                    warmup_moonshine()
                    print("[Server] Moonshine STT worker preloaded")
                except Exception as e:
                    print(f"[Server] Moonshine STT preload failed: {e}")

            # Preload spell detection models if voice spells enabled
            if settings.get('stt', {}).get('voice_spells', True):
                try:
                    from services.spell_detector import warm_up as warmup_spells
                    warmup_spells()
                except Exception as e:
                    print(f"[Server] Spell detection preload failed: {e}")

            # NOTE: Pocket TTS now preloads synchronously above (alongside voice cache)
            # to prevent race condition where TTS is needed before background thread runs
        except Exception as e:
            print(f"[Server] Model preload error: {e}")

    preload_thread = threading.Thread(target=preload_speech_models, daemon=True)
    preload_thread.start()

    # Show setup reminder immediately if setup not complete
    if not is_setup_complete():
        print("")
        print("=" * 60)
        print("  ⚠️  SETUP REQUIRED  ⚠️")
        print("")
        print("  Complete the setup wizard to begin using Sonorus:")
        print(f"  http://localhost:{port}/#chapterSetup")
        print("=" * 60)
        print("")

    # Auto-open config page
    settings = load_settings()
    if settings.get('server', {}).get('auto_open_config', True):
        def open_browser():
            time.sleep(1.0)
            url = f"http://localhost:{port}/"
            print(f"[Server] Opening config page in browser...")
            webbrowser.open(url)
        browser_thread = threading.Thread(target=open_browser, daemon=True)
        browser_thread.start()

    # Run Flask (debug=False reduces idle CPU usage)
    app.run(host='127.0.0.1', port=port, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[Server] Interrupted")
    except Exception as e:
        print(f"[Server] Fatal error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        _cleanup_resources()
