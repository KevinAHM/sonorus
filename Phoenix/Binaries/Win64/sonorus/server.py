"""
Sonorus Server - Persistent HTTP server for LLM + TTS

Runs in background, communicates with UE4SS Lua via HTTP and TCP socket.
"""
import os
import re
import sys
import json
import time
import subprocess
import threading
import webbrowser

# Ensure script directory is in sys.path for embedded Python
_script_dir = os.path.dirname(os.path.abspath(__file__))
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)

# Write immediate heartbeat to prevent duplicate server spawns during import
# (Lua checks this file before spawning new server)
with open(os.path.join(_script_dir, "server.heartbeat"), "w") as f:
    f.write(str(int(time.time())))

from flask import Flask, request, jsonify, send_file, Response
from dotenv import load_dotenv

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
    read_file,
    write_file,
    # Text utils
    split_into_sentences,
    parse_target_result,
    filter_npcs_by_earshot,
    validate_speaker_in_nearby,
    detect_spell_in_text,
    # Localization
    load_localization,
    get_display_name,
    find_npc_id_by_name,
    # Dialogue
    load_dialogue_history,
    save_dialogue_history,
    filter_dialogue_history,
    format_dialogue_history,
    is_named_npc,
    # Game context
    format_game_context,
    # Prompts
    get_character,
    # LLM utils
    call_llm,
    parse_action,
    strip_action_tag,
    # Agents
    run_target_selection_agent,
    run_interjection_agent,
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
from constants import CONVERSATION_EARSHOT_DISTANCE, VERSION

# Load .env
load_dotenv(os.path.join(SONORUS_DIR, ".env"))

# Import our modules
try:
    from services import tts
    TTS_AVAILABLE = True
except ImportError as e:
    print(f"[WARN] TTS service not available: {e}")
    TTS_AVAILABLE = False

try:
    from audio.spatial import shutdown as audio_shutdown, get_player as audio_get_player
    AUDIO3D_AVAILABLE = True
except ImportError as e:
    print(f"[WARN] audio.spatial module not available: {e}")
    AUDIO3D_AVAILABLE = False
    audio_get_player = None

try:
    from audio import lipsync
    LIPSYNC_AVAILABLE = True
except ImportError as e:
    print(f"[WARN] audio.lipsync module not available: {e}")
    LIPSYNC_AVAILABLE = False

try:
    import vision_agent
    VISION_AGENT_AVAILABLE = True
except ImportError as e:
    print(f"[WARN] vision_agent module not available: {e}")
    VISION_AGENT_AVAILABLE = False

import llm
import event_logger

try:
    from input import text as input_capture
    INPUT_CAPTURE_AVAILABLE = True
except ImportError as e:
    print(f"[WARN] input.text module not available: {e}")
    INPUT_CAPTURE_AVAILABLE = False

try:
    from input import voice as stt_capture
    from services import stt as stt_service
    STT_AVAILABLE = True
except ImportError as e:
    print(f"[WARN] input.voice module not available: {e}")
    STT_AVAILABLE = False

try:
    from input import hotkeys as stop_capture
    STOP_CAPTURE_AVAILABLE = True
except ImportError as e:
    print(f"[WARN] input.hotkeys module not available: {e}")
    STOP_CAPTURE_AVAILABLE = False

# ============================================
# Simple Cancellation System
# ============================================
# Separate from conv_state - just a timestamp-based flag
_cancel_timestamp = 0

def request_cancel():
    """Signal cancellation request with current timestamp."""
    global _cancel_timestamp
    _cancel_timestamp = time.time()
    print(f"[Cancel] Cancellation requested at {_cancel_timestamp}")

def is_cancelled(max_age=10):
    """Check if cancellation was requested within max_age seconds."""
    if _cancel_timestamp == 0:
        return False
    age = time.time() - _cancel_timestamp
    cancelled = age < max_age
    if cancelled:
        print(f"[Cancel] Check: cancelled (age={age:.1f}s)")
    return cancelled

def clear_cancel():
    """Clear cancellation flag (call when processing completes normally)."""
    global _cancel_timestamp
    if _cancel_timestamp > 0:
        print(f"[Cancel] Cleared")
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

# ============================================
# Global Instances
# ============================================

# Global conversation state
conv_state = ConversationState()

# Global socket server instance
lua_socket = LuaSocketServer()

# Wire up socket with external modules
if INPUT_CAPTURE_AVAILABLE:
    lua_socket.set_input_capture(input_capture)
lua_socket.set_conv_state(conv_state)

# Initialize playback coordinator
from audio.playback import init_coordinator
playback_coordinator = init_coordinator(lua_socket)

# Connect lipsync module to socket for viseme streaming
if LIPSYNC_AVAILABLE:
    lipsync.set_lua_socket(lua_socket)

# Connect vision agent to socket for game context
if VISION_AGENT_AVAILABLE:
    vision_agent.set_lua_socket(lua_socket)

# Connect landmarks module to socket for player position
set_landmarks_lua_socket(lua_socket)

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


# ============================================
# Earshot Witness Tracking
# ============================================
def get_earshot_witnesses(nearby_npcs, speaker_id):
    """Get list of named NPC IDs within earshot, excluding speaker and player.

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
        # Skip generic NPCs (only track named characters)
        if not is_named_npc(npc_id):
            continue
        witnesses.append(npc_id)
    return witnesses


# ============================================
# TTS Thread
# ============================================
def run_tts_async(text, character_name, positions=None, turn_id=None):
    """Run TTS in background thread with download complete signaling for pre-buffering."""
    global state
    state["tts_active"] = True

    # CRITICAL: Mark playback as active BEFORE starting
    # This ensures wait_for_playback_stop() will block until audio finishes
    lua_socket.playback_active = True
    lua_socket.playback_event.clear()

    def on_stop():
        # Send via socket only
        lua_socket.send_lipsync_stop()
        print("[TTS] Playback ended - sent via socket")

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
                turn_id=turn_id
            )
            if result["success"]:
                print(f"[TTS] Complete")
            else:
                print(f"[TTS] Failed: {result.get('error')}")
                lua_socket.send_lipsync_stop()
        else:
            print("[TTS] Inworld not available")
            lua_socket.send_lipsync_stop()
    except Exception as e:
        print(f"[TTS] Error: {e}")
        lua_socket.send_lipsync_stop()
    finally:
        state["tts_active"] = False


def run_player_tts(text, turn_id, game_context=None, abort_check=None):
    """
    Run TTS for player's spoken line (blocking).
    Called when player_voice_enabled is True.
    Uses non-3D audio since the player is the listener.

    Args:
        abort_check: Callable that returns True if we should abort

    Returns True on success, False on failure.
    """
    global state

    # Check for abort before starting
    if abort_check and abort_check():
        print("[PlayerTTS] Aborted before starting")
        return False

    settings = load_settings()
    conv_settings = settings.get('conversation', {})

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

    # Verify voice exists (will auto-clone if reference file exists)
    voice = tts.get_or_create_voice(player_voice_name, lua_socket=lua_socket)
    if not voice:
        print(f"[PlayerTTS] No voice available for '{player_voice_name}' - skipping player TTS")
        return False

    print(f"[PlayerTTS] Speaking as player ({player_voice_name}): \"{text[:50]}...\"")
    state["tts_active"] = True

    try:
        if TTS_AVAILABLE:
            # Note: We don't signal download_complete for player TTS
            # That signal is for NPC pre-buffering, which shouldn't start until NPC speaks
            # No 3D positioning - player voice plays centered (non-spatial)
            result = tts.speak(
                text,
                player_voice_name,
                on_stop=lambda: lua_socket.send_lipsync_stop(),
                on_download_complete=None,  # Don't signal - this is player turn
                lua_socket=lua_socket,
                initial_positions=None,  # No 3D audio for player voice
                turn_id=turn_id,
                abort_check=abort_check
            )
            if result["success"]:
                print(f"[PlayerTTS] Complete")
                return True
            else:
                print(f"[PlayerTTS] Failed: {result.get('error')}")
                lua_socket.send_lipsync_stop()
                return False
        else:
            print("[PlayerTTS] Inworld not available")
            lua_socket.send_lipsync_stop()
            return False
    except Exception as e:
        print(f"[PlayerTTS] Error: {e}")
        lua_socket.send_lipsync_stop()
        return False
    finally:
        state["tts_active"] = False


def play_prebuffered_response(buffered, blocking=True):
    """
    Play a pre-buffered TTS stream with lipsync.

    Uses PlaybackCoordinator for synchronized handshake:
    1. Send lipsync_start with accumulated visemes
    2. Wait for lipsync_ready from Lua
    3. Start audio playback
    4. Send audio_sync during playback for drift correction
    """
    speaker = buffered["speaker"]
    speaker_id = buffered["speaker_id"]
    tts_stream = buffered["tts_stream"]
    visemes = buffered.get("visemes", [])
    positions = buffered.get("positions", {})
    turn_id = buffered.get("turn_id")

    print(f"[PlayBuffer] Playing: {speaker} (turn={turn_id}, {len(visemes)} visemes)")

    # Mark playback as active BEFORE signaling download complete
    lua_socket.playback_active = True
    lua_socket.playback_event.clear()

    # Signal download complete immediately - for pre-buffered audio, download
    # is already done, so the next interjection can start buffering right away
    signal_download_complete()

    def do_playback():
        try:
            from audio.spatial import get_player
            from audio.playback import get_coordinator

            player = get_player()
            coordinator = get_coordinator()

            # Connect position reader to socket for real-time position updates
            player.position_reader.set_socket(lua_socket)

            # Set initial 3D positions DIRECTLY (eliminates race condition)
            # use_3d based on whether positions are provided (check key exists, not value truthiness)
            use_3d = bool(positions) and positions.get("npcX") is not None
            if use_3d:
                cam = (positions.get("camX", 0), positions.get("camY", 0), positions.get("camZ", 0))
                npc = (positions.get("npcX", 0), positions.get("npcY", 0), positions.get("npcZ", 0))
                yaw = positions.get("camYaw", 0)
                player.position_reader.set_initial_positions(cam, yaw, npc)

            # Create turn with pre-computed visemes
            turn = coordinator.create_turn(turn_id, speaker_id=speaker_id, use_3d=use_3d)
            turn.audio_stream = tts_stream

            if visemes:
                turn.add_visemes(visemes)
                print(f"[PlayBuffer] Using {len(turn.viseme_buffer)} pre-computed visemes for turn {turn_id}")

            # Use coordinator for synchronized playback
            coordinator.play_turn(turn_id, player, blocking=True)

            print(f"[PlayBuffer] Complete: {speaker}")
        except Exception as e:
            print(f"[PlayBuffer] Error: {e}")
            import traceback
            traceback.print_exc()
        finally:
            lua_socket.send_lipsync_stop()

    if blocking:
        do_playback()
    else:
        playback_thread = threading.Thread(target=do_playback, daemon=True)
        playback_thread.start()


# ============================================
# Chat Processing
# ============================================
def process_chat_request(data):
    """Process a chat request - called by HTTP endpoint or file queue"""
    global state

    # Clear any stale cancellation from previous request
    clear_cancel()

    # Mark as processing
    conv_state.state = "processing"

    user_input = data.get('user_input', '').strip()
    character_name = data.get('character_name', '')
    character_id = data.get('character_id', 'unknown')

    # Check for voice spell command FIRST (if enabled)
    settings = load_settings()
    if settings.get('stt', {}).get('voice_spells', True):
        spell_name, matched_text = detect_spell_in_text(user_input)
        if spell_name:
            print(f"[Chat] Spell detected: '{matched_text}' -> {spell_name}")
            # Send cast_spell command to Lua - it handles unlock check and casting
            lua_socket.send({
                "type": "cast_spell",
                "spell": spell_name,
                "text": user_input
            })
            return {"status": "spell_cast", "spell": spell_name}

    # Request fresh game context from Lua (selective groups for efficiency)
    # position needed for landmark beacons in format_game_context
    game_context = lua_socket.request_context_refresh(
        groups=["position", "state", "player", "time", "zone", "npcs", "gear", "companion", "mission"],
        timeout=1.0
    )

    # Block if in cinematic or combat
    if game_context.get('inCinematic'):
        print("[Chat] Blocked - in cinematic")
        return {"error": "In cinematic"}
    if game_context.get('inCombat'):
        lua_socket.send_notification("Cannot talk during combat")
        print("[Chat] Blocked - in combat")
        return {"error": "In combat"}

    print(f"[Chat] User: \"{user_input}\"")

    if not user_input:
        print("[Chat] ERROR: No user input!")
        return {"error": "No user_input provided"}

    # Load conversation state and settings
    settings = load_settings()
    conv_settings = settings.get('conversation', {})
    conv_state.max_turns = conv_settings.get('max_turns', 6)

    # Handle interruption - player spoke during playback
    if conv_state.state == "playing":
        print("[Chat] Interrupting current playback")
        conv_state.pending_player_input = user_input
        conv_state.interrupted = True
        player_name = game_context.get('playerName', 'Player')
        lua_socket.send_player_message(player_name, user_input)
        lua_socket.send_conversation_state("playing", interrupted=True)
        return {"status": "queued_interrupt", "message": "Input queued, interrupting current conversation"}

    # Load dialogue history (pass context directly instead of callback)
    dialogue_history = load_dialogue_history(game_context)

    # Run target selection agent
    print("[Chat] Running target selection agent...")
    nearby_npcs_raw = game_context.get('nearbyNpcs', [])
    player_name = game_context.get('playerName', 'Player')
    player_in_stealth = game_context.get('inStealth', False)

    # Filter NPCs to only those within earshot (reduced when player is invisible)
    nearby_npcs = filter_npcs_by_earshot(nearby_npcs_raw, player_in_stealth=player_in_stealth)
    print(f"[Chat] NPCs within earshot: {len(nearby_npcs)} (of {len(nearby_npcs_raw)} total){' [STEALTH]' if player_in_stealth else ''}")

    # Show player message immediately (as subtitle)
    lua_socket.send_player_message(player_name, user_input)

    # Check if player voice is enabled (skip for STT since player already spoke)
    is_from_stt = data.get('from_stt', False)
    player_voice_enabled = conv_settings.get('player_voice_enabled', False) and not is_from_stt

    # Find the looked-at NPC
    looked_at_npc = None
    for npc in nearby_npcs:
        if npc.get('isLookedAt'):
            looked_at_npc = npc
            break

    target_result = run_target_selection_agent(
        user_input,
        looked_at_npc,
        nearby_npcs,
        dialogue_history,
        player_name
    )

    # Parse target result - agents return IDs (e.g., "SebastianSallow", not "Sebastian Sallow")
    speaker_id, target_id = parse_target_result(target_result)

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

    # Wait for any in-progress vision capture to complete before building prompt
    # Only if wait_for_capture is enabled (for slow models or reasoning mode)
    vision_settings = settings.get('agents', {}).get('vision', {})
    if VISION_AGENT_AVAILABLE and vision_settings.get('wait_for_capture', False):
        try:
            agent = vision_agent.get_agent()
            if agent:
                agent.wait_for_capture(timeout=8.0)  # Wait up to 8s for fresh context
        except Exception as e:
            print(f"[Chat] Vision wait error: {e}")

    # Reset conversation state
    conv_state.reset()
    conv_state.state = "processing"
    conv_state.max_turns = conv_settings.get('max_turns', 6)

    # Get display name from ID
    speaker_name = get_display_name(speaker_id)
    print(f"[Chat] Speaker: {speaker_name} (ID: {speaker_id})")

    # Get character prompt
    speaker_name, base_prompt = get_character(speaker_id, game_context)
    print(f"[Chat] Display name: {speaker_name}")

    # Build prompt with context (do this before player TTS so LLM can run in parallel)
    prompt = base_prompt
    context_str = format_game_context(game_context, current_speaker=speaker_id)
    if context_str:
        prompt = f"{base_prompt}\n\n{context_str}"

    # Add dialogue history (filtered to what this NPC witnessed)
    dialogue_str = format_dialogue_history(dialogue_history, for_npc_id=speaker_id)
    if dialogue_str:
        prompt = f"{prompt}\n\n{dialogue_str}"
        print(f"[Chat] Dialogue history: {len(dialogue_history)} entries")

    # ============================================
    # Player Voice Turn (if enabled) + Parallel LLM
    # ============================================
    # When player voice is enabled, we run player TTS and LLM in parallel
    # to minimize wait time between player speaking and NPC response
    player_tts_thread = None
    player_tts_done = threading.Event()

    if player_voice_enabled and TTS_AVAILABLE:
        print(f"[Chat] Player voice enabled - starting parallel player TTS + LLM")

        # Set conversation state to playing for player turn
        conv_state.state = "playing"
        lua_socket.send_conversation_state("playing")

        # Send play_turn for player (speaker is "player", target is the NPC)
        player_turn_result = lua_socket.send_play_turn(
            speaker_id="player",
            display_name=player_name,
            text=user_input,
            turn_index=0,  # Turn 0 = player's turn
            target_id=speaker_id  # Player is addressing this NPC
        )

        # Start player TTS in background thread (non-3D audio)
        def player_tts_worker():
            try:
                success = run_player_tts(
                    text=user_input,
                    turn_id=player_turn_result.get("turn_id"),
                    game_context=game_context,
                    abort_check=is_cancelled
                )
                if success:
                    print(f"[Chat] Player voice turn complete")
                elif is_cancelled():
                    print(f"[Chat] Player voice cancelled")
                else:
                    print(f"[Chat] Player voice turn failed")
            finally:
                player_tts_done.set()

        player_tts_thread = threading.Thread(target=player_tts_worker, daemon=True)
        player_tts_thread.start()

    # Check for cancellation before LLM call
    if is_cancelled():
        print("[Chat] Cancelled before LLM call")
        conv_state.reset()
        lua_socket.send_conversation_state("idle")
        return {"status": "cancelled", "message": "Cancelled before LLM"}

    # Call LLM (runs in parallel with player TTS if enabled)
    print(f"[Chat] Calling LLM for {speaker_name}...")
    raw_response = call_llm(prompt, user_input)

    # Check for cancellation after LLM call
    if is_cancelled():
        print("[Chat] Cancelled after LLM call - discarding response")
        conv_state.reset()
        lua_socket.send_conversation_state("idle")
        return {"status": "cancelled", "message": "Cancelled after LLM"}

    # Handle LLM error
    if raw_response is None:
        lua_socket.send_notification("LLM request failed - check API key")
        conv_state.state = "idle"
        lua_socket.send_conversation_state("idle")
        return {"error": "LLM request failed"}

    # Only parse actions if enabled in settings
    actions_enabled = conv_settings.get('actions_enabled', False)
    if actions_enabled:
        action = parse_action(raw_response)
        response = strip_action_tag(raw_response)
        print(f"[Chat] Action: {action}")
    else:
        action = "None"
        response = strip_action_tag(raw_response)

    print(f"[Chat] LLM Response: \"{response}\"")

    # Save player input to dialogue history immediately (player said this)
    game_time = game_context.get('timeFormatted', '')
    now = int(time.time())

    # Get witnesses (named NPCs in earshot, excluding speaker)
    player_earshot = get_earshot_witnesses(nearby_npcs, "Player")
    npc_earshot = get_earshot_witnesses(nearby_npcs, speaker_id)

    dialogue_history.append({
        "timestamp": now,
        "gameTime": game_time,
        "speaker": player_name,
        "voiceName": "Player",
        "target": speaker_name,
        "text": user_input,
        "isAIResponse": False,
        "isPlayer": True,
        "type": "dialogue",
        "earshot": player_earshot
    })
    save_dialogue_history(dialogue_history)

    # NPC response is pending until audio actually plays
    conv_state.add_pending_history({
        "timestamp": now,
        "gameTime": game_time,
        "speaker": speaker_name,
        "voiceName": speaker_id,
        "target": player_name,
        "text": response,
        "isAIResponse": True,
        "isPlayer": False,
        "type": "dialogue",
        "earshot": npc_earshot
    })

    # Update server state
    state["current_character"] = speaker_id
    state["last_response"] = response
    state["last_action"] = action

    # Add to conversation queue
    conv_state.add_to_queue(speaker_name, player_name, response, speaker_id=speaker_id)
    conv_state.turn_count = 1
    conv_state.state = "playing"

    lua_socket.send_conversation_state("playing")

    # Re-check NPCs are still nearby before playing turn
    fresh_context = lua_socket.request_context_refresh(groups=["npcs", "player"], timeout=0.5)
    fresh_stealth = fresh_context.get('inStealth', False)
    fresh_npcs = filter_npcs_by_earshot(fresh_context.get('nearbyNpcs', []), player_in_stealth=fresh_stealth)
    if not validate_speaker_in_nearby(speaker_id, fresh_npcs, load_localization):
        print(f"[Chat] ABORT: Speaker '{speaker_id}' no longer nearby")
        conv_state.state = "idle"
        conv_state.queue = []
        conv_state.turn_count = 0
        lua_socket.send_conversation_state("idle")
        lua_socket.send_notification(f"{speaker_name} walked away")
        return {"status": "aborted", "message": "Speaker left the area"}
    if target_id.lower() != "player" and not validate_speaker_in_nearby(target_id, fresh_npcs, load_localization):
        print(f"[Chat] ABORT: Target '{target_id}' no longer nearby")
        conv_state.state = "idle"
        conv_state.queue = []
        conv_state.turn_count = 0
        lua_socket.send_conversation_state("idle")
        target_name = get_display_name(target_id)
        lua_socket.send_notification(f"{target_name} walked away")
        return {"status": "aborted", "message": "Target left the area"}

    # Send play_turn message (target_id already set from parse_target_result)
    turn_result = lua_socket.send_play_turn(
        speaker_id=speaker_id,
        display_name=speaker_name,
        text=response,
        turn_index=conv_state.turn_count,
        target_id=target_id
    )

    # Wait for player TTS to complete before starting NPC TTS
    if player_tts_thread is not None:
        print(f"[Chat] Waiting for player voice to finish...")
        player_tts_done.wait(timeout=60.0)
        print(f"[Chat] Player voice done, starting NPC response")

    # Check for cancellation before TTS
    if is_cancelled():
        print("[Chat] Cancelled before TTS")
        conv_state.reset()
        lua_socket.send_conversation_state("idle")
        return {"status": "cancelled", "message": "Cancelled before TTS"}

    # Start NPC TTS
    voice_id = None
    if TTS_AVAILABLE and speaker_id:
        try:
            print(f"[Chat] Getting voice for: {speaker_id}")
            voice = tts.get_or_create_voice(speaker_id, lua_socket=lua_socket)
            if voice:
                voice_id = voice.get("voiceId")
                print(f"[Chat] Voice ID: {voice_id}")
                tts_thread = threading.Thread(
                    target=run_tts_async,
                    args=(response, speaker_id, turn_result.get("positions"), turn_result.get("turn_id")),
                    daemon=True
                )
                tts_thread.start()
        except Exception as e:
            print(f"[Chat] TTS error: {e}")
            lua_socket.send_notification(f"TTS failed: {e}")
            # Reset conversation state so user can try again
            conv_state.state = "idle"
            conv_state.queue = []
            conv_state.turn_count = 0
            lua_socket.send_conversation_state("idle")
            return {
                "error": f"TTS failed: {e}",
                "response": response,
                "character": speaker_name,
            }

    # Start interjection loop
    interjection_thread = threading.Thread(
        target=interjection_loop_worker,
        args=(game_context,),
        daemon=True
    )
    interjection_thread.start()

    return {
        "response": response,
        "action": action,
        "character": speaker_name,
        "voice_id": voice_id,
        "tts_status": "streaming" if voice_id else "unavailable",
        "queue": conv_state.queue,
    }


def interjection_loop_worker(game_context):
    """Background worker with pre-buffering for smooth conversation flow."""
    print("[Interjection] Loop started with pre-buffering")
    pre_buffer = PreBuffer()

    try:
        while True:
            # Stop conditions
            if is_cancelled():
                print("[Interjection] Cancelled")
                pre_buffer.abort()
                break
            if conv_state.turn_count >= conv_state.max_turns:
                print(f"[Interjection] Max turns ({conv_state.max_turns}) reached")
                break
            if conv_state.state != "playing":
                print("[Interjection] Not playing")
                break

            # Wait for download to complete
            print("[Interjection] Waiting for download complete...")
            if not wait_for_download_complete(timeout=60.0):
                print("[Interjection] Download wait timeout")
                break

            if is_cancelled():
                pre_buffer.abort()
                break

            # Run interjection agent
            last = conv_state.queue[-1] if conv_state.queue else None
            if not last:
                break

            # Request only npcs for interjection check (cheap)
            game_context = lua_socket.request_context_refresh(
                groups=["npcs", "player"],
                timeout=0.5
            )
            dialogue_history = load_dialogue_history(game_context)
            # Debug: check for non-dict entries
            bad_entries = [(i, type(e).__name__, repr(e)[:100]) for i, e in enumerate(dialogue_history) if not isinstance(e, dict)]
            if bad_entries:
                print(f"[Interjection] WARNING: Found {len(bad_entries)} non-dict entries in dialogue_history!")
                for idx, typ, val in bad_entries[:3]:
                    print(f"  [{idx}] {typ}: {val}")
            player_name = game_context.get('playerName', 'Player')
            nearby_npcs_raw = game_context.get('nearbyNpcs', [])
            player_in_stealth = game_context.get('inStealth', False)

            nearby_npcs = filter_npcs_by_earshot(nearby_npcs_raw, player_in_stealth=player_in_stealth)
            print(f"[Interjection] NPCs within earshot: {len(nearby_npcs)} (of {len(nearby_npcs_raw)} total){' [STEALTH]' if player_in_stealth else ''}")

            if not nearby_npcs:
                print("[Interjection] No NPCs within earshot - ending conversation")
                break

            last_speaker_id = last.get('speakerId', last.get('speaker', 'Unknown'))
            last_speaker_name = get_display_name(last_speaker_id)
            last_target_name = last.get('target', player_name)
            print(f"[Interjection] Checking who responds to {last_speaker_name}...")
            interjection = run_interjection_agent(
                last_speaker_id,
                last_speaker_name,
                last_target_name,
                last.get('full_text', ''),
                nearby_npcs,
                dialogue_history,
                player_name
            )

            if interjection == "0":
                print("[Interjection] No one wants to speak")
                break

            # Agents return IDs (e.g., "SebastianSallow", not "Sebastian Sallow")
            speaker_id, target_id = parse_target_result(interjection)
            if not speaker_id:
                break

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

            # Get full context for LLM response (state may have changed since check)
            # position needed for landmark beacons in format_game_context
            full_context = lua_socket.request_context_refresh(
                groups=["position", "state", "player", "time", "zone", "npcs", "gear", "companion", "mission"],
                timeout=1.0
            )

            # Generate LLM response
            response = generate_interjection_response(speaker_id, target_id, full_context)
            if not response:
                break

            if is_cancelled():
                pre_buffer.abort()
                break

            # Re-check NPCs are still nearby before playing turn
            fresh_context = lua_socket.request_context_refresh(groups=["npcs", "player"], timeout=0.5)
            fresh_stealth = fresh_context.get('inStealth', False)
            fresh_npcs = filter_npcs_by_earshot(fresh_context.get('nearbyNpcs', []), player_in_stealth=fresh_stealth)
            if not validate_speaker_in_nearby(speaker_id, fresh_npcs, load_localization):
                print(f"[Interjection] ABORT: Speaker '{speaker_id}' no longer nearby")
                lua_socket.send_notification(f"{speaker_name} walked away")
                break
            if target_id.lower() != "player" and not validate_speaker_in_nearby(target_id, fresh_npcs, load_localization):
                print(f"[Interjection] ABORT: Target '{target_id}' no longer nearby")
                target_name = get_display_name(target_id)
                lua_socket.send_notification(f"{target_name} walked away")
                break

            # Send play_turn (target_id already set from parse_target_result)
            target_name = get_display_name(target_id) if target_id.lower() != "player" else player_name
            conv_state.add_to_queue(speaker_name, target_name, response, speaker_id=speaker_id)
            conv_state.turn_count += 1
            turn_result = lua_socket.send_play_turn(
                speaker_id=speaker_id,
                display_name=speaker_name,
                text=response,
                turn_index=conv_state.turn_count,
                target_id=target_id
            )

            # Buffer TTS
            pre_buffer.start_buffering(
                speaker_name, speaker_id, target_id, response,
                positions=turn_result.get("positions"),
                turn_id=turn_result.get("turn_id")
            )

            def buffer_tts():
                if is_cancelled() or pre_buffer.abort_flag:
                    return

                ready_signaled = [False]

                def on_buffer_ready(tts_stream, word_timings, visemes):
                    if not ready_signaled[0]:
                        ready_signaled[0] = True
                        pre_buffer.mark_ready(tts_stream, word_timings, visemes)

                result = tts.prepare_tts(
                    response,
                    speaker_id,
                    abort_check=lambda: is_cancelled() or pre_buffer.abort_flag,
                    on_ready=on_buffer_ready,
                    lua_socket=lua_socket
                )

                if result and not ready_signaled[0]:
                    tts_stream, word_timings, visemes = result
                    pre_buffer.mark_ready(tts_stream, word_timings, visemes)
                elif not result:
                    print("[Interjection] Buffer preparation failed")

            buffer_thread = threading.Thread(target=buffer_tts, daemon=True)
            buffer_thread.start()

            # Wait for playback to finish
            print("[Interjection] Waiting for playback to finish...")
            lua_socket.wait_for_playback_stop(timeout=60.0)

            # Commit pending history now that audio finished
            if conv_state.pending_history_entries:
                dialogue_history = load_dialogue_history(load_game_context)
                count = conv_state.commit_pending_history(dialogue_history, save_dialogue_history)
                print(f"[Interjection] Committed {count} history entries")

            if is_cancelled():
                pre_buffer.abort()
                break

            # Wait for buffer
            if not pre_buffer.ready_event.wait(timeout=15.0):
                print("[Interjection] Buffer timeout")
                pre_buffer.abort()
                break

            # Play buffered audio
            buffered = pre_buffer.consume()
            if not buffered:
                print("[Interjection] Buffer empty")
                break

            play_prebuffered_response(buffered, blocking=False)

            # Add to pending history (committed when audio completes)
            interjection_earshot = get_earshot_witnesses(nearby_npcs, speaker_id)
            conv_state.add_pending_history({
                "timestamp": int(time.time()),
                "gameTime": game_context.get('timeFormatted', ''),
                "speaker": speaker_name,
                "voiceName": speaker_id,
                "target": target_name,
                "text": response,
                "isAIResponse": True,
                "isPlayer": False,
                "type": "dialogue",
                "earshot": interjection_earshot
            })

            print(f"[Interjection] Turn {conv_state.turn_count}: {speaker_name}")

    except Exception as e:
        print(f"[Interjection] ERROR: {e}")
        import traceback
        traceback.print_exc()
        pre_buffer.abort()

    finally:
        pre_buffer.abort()
        print("[Interjection] Loop exiting")

        # Wait for final audio and commit if it completes
        if lua_socket.playback_active:
            print("[Interjection] Waiting for final audio to complete...")
            lua_socket.wait_for_playback_stop(timeout=60.0)
            # Audio finished - user heard it, commit
            if conv_state.pending_history_entries:
                dialogue_history = load_dialogue_history(load_game_context)
                count = conv_state.commit_pending_history(dialogue_history, save_dialogue_history)
                print(f"[Interjection] Committed {count} history entries")
        elif conv_state.pending_history_entries:
            # No audio was playing - discard unplayed entries
            print(f"[Interjection] Discarded {len(conv_state.pending_history_entries)} pending entries (never played)")
            conv_state.pending_history_entries = []

        # Clear cancellation flag when done
        clear_cancel()

        if conv_state.pending_player_input:
            print("[Interjection] Processing pending player input")
            pending = conv_state.pending_player_input
            conv_state.pending_player_input = None
            conv_state.state = "idle"
            conv_state.interrupted = False
            lua_socket.send_conversation_state("idle")
            process_chat_request({"user_input": pending})
        else:
            conv_state.state = "idle"
            conv_state.interrupted = False
            lua_socket.send_conversation_state("idle")


def generate_interjection_response(speaker_id, target_id, game_context):
    """Generate a response for an interjecting NPC.

    Args:
        speaker_id: Internal NPC ID (e.g., "SebastianSallow")
        target_id: Internal ID of who they're responding to (e.g., "NellieOggspire" or "player")
        game_context: Current game context dict (pre-fetched, passed directly)
    """
    try:
        speaker_name, base_prompt = get_character(speaker_id, game_context)
        target_name = get_display_name(target_id) if target_id.lower() != "player" else game_context.get('playerName', 'Player')

        prompt = base_prompt
        # Build participants list: player + target NPC
        player_name = game_context.get('playerName', 'Unknown')
        participants = [player_name, target_name] if player_name and player_name != "Unknown" else [target_name]
        context_str = format_game_context(game_context, current_speaker=speaker_id, participants=participants)
        if context_str:
            prompt = f"{base_prompt}\n\n{context_str}"

        # Use passed context directly instead of re-fetching
        dialogue_history = load_dialogue_history(game_context)
        dialogue_str = format_dialogue_history(dialogue_history, for_npc_id=speaker_id)
        if dialogue_str:
            prompt = f"{prompt}\n\n{dialogue_str}"

        user_input = f"(You are reacting to the conversation. Respond as {speaker_name} to what {target_name} just said.)"

        raw_response = call_llm(prompt, user_input)

        # Handle LLM error
        if raw_response is None:
            lua_socket.send_notification("LLM request failed")
            return None

        response = strip_action_tag(raw_response)

        print(f"[Interjection] {speaker_id} response: {response}")
        return response

    except Exception as e:
        print(f"[Interjection] Error generating response: {e}")
        lua_socket.send_notification(f"Interjection error: {e}")
        return None


# ============================================
# Endpoints
# ============================================
@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        "status": "ok",
        "version": VERSION,
        "tts": TTS_AVAILABLE,
        "tts_provider": tts.get_provider_name() if TTS_AVAILABLE else None,
        "audio3d": AUDIO3D_AVAILABLE,
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

        # Cleanup resources
        if AUDIO3D_AVAILABLE:
            try:
                audio_shutdown()
            except:
                pass

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

    if AUDIO3D_AVAILABLE:
        try:
            audio_shutdown()
        except:
            pass

    func = request.environ.get('werkzeug.server.shutdown')
    if func:
        func()
    else:
        os._exit(0)

    return jsonify({"status": "shutting_down"})


# ============================================
# Config Page & API
# ============================================
@app.route('/')
def config_page():
    if os.path.exists(CONFIG_HTML):
        return send_file(CONFIG_HTML)
    return "Config page not found", 404


@app.route('/js/<path:filename>')
def serve_js(filename):
    """Serve static JS files from sonorus/js/ folder."""
    js_dir = os.path.join(SONORUS_DIR, "js")
    js_file = os.path.join(js_dir, filename)
    if os.path.exists(js_file):
        return send_file(js_file, mimetype='application/javascript')
    return "File not found", 404


@app.route('/api/config', methods=['GET'])
def get_config():
    settings = load_settings()
    masked = json.loads(json.dumps(settings))
    if masked.get('llm', {}).get('api_key'):
        masked['llm']['api_key'] = '********'
    tts_providers_with_keys = ['inworld', 'elevenlabs', 'openai']
    for provider in tts_providers_with_keys:
        if masked.get('tts', {}).get(provider, {}).get('api_key'):
            masked['tts'][provider]['api_key'] = '********'
    return jsonify(masked)


@app.route('/api/config', methods=['POST'])
def save_config():
    new_settings = request.get_json() or {}
    existing = load_settings()

    if new_settings.get('llm', {}).get('api_key') == '********':
        if 'llm' not in new_settings:
            new_settings['llm'] = {}
        new_settings['llm']['api_key'] = existing.get('llm', {}).get('api_key', '')

    # Check if TTS provider itself changed
    new_tts_provider = new_settings.get('tts', {}).get('provider', '')
    existing_tts_provider = existing.get('tts', {}).get('provider', '')
    tts_provider_switched = new_tts_provider and new_tts_provider != existing_tts_provider

    # Track which TTS providers had API key or workspace changes
    tts_providers_changed = []
    tts_providers_with_keys = ['inworld', 'elevenlabs', 'openai']
    for provider in tts_providers_with_keys:
        new_key = new_settings.get('tts', {}).get(provider, {}).get('api_key', '')
        existing_key = existing.get('tts', {}).get(provider, {}).get('api_key', '')

        if new_key == '********':
            # Masked value - preserve existing key
            if 'tts' not in new_settings:
                new_settings['tts'] = {}
            if provider not in new_settings['tts']:
                new_settings['tts'][provider] = {}
            new_settings['tts'][provider]['api_key'] = existing_key
        elif new_key and new_key != existing_key:
            # API key changed - mark for cache refresh
            tts_providers_changed.append(provider)
            print(f"[Settings] API key changed for TTS provider: {provider}")

    # Also check if Inworld workspace_id changed
    new_workspace = new_settings.get('tts', {}).get('inworld', {}).get('workspace_id', '')
    existing_workspace = existing.get('tts', {}).get('inworld', {}).get('workspace_id', '')
    if new_workspace and new_workspace != existing_workspace and 'inworld' not in tts_providers_changed:
        tts_providers_changed.append('inworld')
        print("[Settings] Workspace ID changed for TTS provider: inworld")

    merged = deep_merge(DEFAULT_SETTINGS.copy(), new_settings)
    if save_settings(merged):
        print("[Settings] Configuration saved")

        # Handle TTS provider switch
        if tts_provider_switched:
            print(f"[Settings] TTS provider changed: {existing_tts_provider} -> {new_tts_provider}")
            try:
                from services import tts
                # Clear old provider cache
                if existing_tts_provider:
                    tts.clear_provider_cache(existing_tts_provider)
                # Pre-load new provider's voices
                print(f"[Settings] Loading voices for {new_tts_provider}...")
                tts.clear_provider_cache(new_tts_provider)  # Ensure fresh instance
                voice_list = tts.list_voices()
                print(f"[Settings] Loaded {len(voice_list) if voice_list else 0} voices from {new_tts_provider}")
            except Exception as e:
                print(f"[Settings] Error switching TTS provider: {e}")

        # Refresh voice cache for providers with changed API keys
        elif tts_providers_changed:
            try:
                from services import tts
                for provider in tts_providers_changed:
                    # Clear the cached provider so it re-initializes with new key
                    tts.clear_provider_cache(provider)
            except Exception as e:
                print(f"[Settings] Error refreshing TTS cache: {e}")

        # Hot-reload STT settings
        new_stt = new_settings.get('stt', {})
        existing_stt = existing.get('stt', {})
        stt_provider_changed = new_stt.get('provider') != existing_stt.get('provider')
        stt_hotkey_changed = new_stt.get('hotkey') != existing_stt.get('hotkey')
        stt_api_key_changed = (
            new_stt.get('deepgram', {}).get('api_key') != existing_stt.get('deepgram', {}).get('api_key') or
            new_stt.get('whisper', {}).get('api_key') != existing_stt.get('whisper', {}).get('api_key')
        )

        if stt_provider_changed or stt_api_key_changed:
            # Provider or API key changed - restart capture with new settings
            try:
                from input import voice as stt_capture_module
                stt_capture_module.restart_capture()
            except Exception as e:
                print(f"[Settings] Error restarting STT: {e}")
        elif stt_hotkey_changed:
            # Just hotkey changed - update on running instance
            try:
                from input import voice as stt_capture_module
                stt_capture_module.set_capture_hotkey(new_stt.get('hotkey', 'middle_mouse'))
            except Exception as e:
                print(f"[Settings] Error updating STT hotkey: {e}")

        # Hot-reload chat hotkey
        new_input = new_settings.get('input', {})
        existing_input = existing.get('input', {})
        if new_input.get('chat_hotkey') != existing_input.get('chat_hotkey'):
            try:
                from input import text as chat_capture_module
                chat_capture_module.set_capture_hotkey(new_input.get('chat_hotkey', 'enter'))
                print(f"[Settings] Chat hotkey updated: {new_input.get('chat_hotkey')}")
            except Exception as e:
                print(f"[Settings] Error updating chat hotkey: {e}")

        # Hot-reload stop conversation hotkey
        if new_input.get('stop_hotkey') != existing_input.get('stop_hotkey'):
            try:
                from input import hotkeys as stop_capture_module
                stop_capture_module.set_hotkey(new_input.get('stop_hotkey', 'delete'))
                print(f"[Settings] Stop hotkey updated: {new_input.get('stop_hotkey')}")
            except Exception as e:
                print(f"[Settings] Error updating stop hotkey: {e}")

        # Sync tracking settings to Lua if history settings changed
        new_history = new_settings.get('history', {})
        existing_history = existing.get('history', {})
        if (new_history.get('track_ambient') != existing_history.get('track_ambient') or
            new_history.get('track_cutscene') != existing_history.get('track_cutscene')):
            lua_socket.send_tracking_settings()

        return jsonify({"status": "ok"})
    return jsonify({"error": "Failed to save"}), 500


@app.route('/api/config/reset', methods=['POST'])
def reset_config():
    if save_settings(DEFAULT_SETTINGS.copy()):
        print("[Settings] Reset to defaults")
        return jsonify({"status": "ok"})
    return jsonify({"error": "Failed to reset"}), 500


@app.route('/api/conversation/reset', methods=['POST'])
def reset_conversation():
    conv_state.reset()
    lua_socket.send_conversation_state("idle")
    print("[Server] Conversation state reset to idle")
    return jsonify({"status": "ok", "message": "Conversation state reset"})


@app.route('/api/dialogue-history', methods=['GET'])
def get_dialogue_history():
    history = load_dialogue_history(load_game_context)
    filtered = filter_dialogue_history(history)
    return jsonify(filtered)


@app.route('/api/dialogue-history', methods=['DELETE'])
def clear_dialogue_history():
    save_dialogue_history([])
    print("[History] Cleared")
    return jsonify({"status": "ok"})


@app.route('/api/dialogue-history/export', methods=['GET'])
def export_dialogue_history():
    history = load_dialogue_history(load_game_context)
    response = Response(
        json.dumps(history, indent=2),
        mimetype='application/json',
        headers={'Content-Disposition': 'attachment;filename=dialogue_history.json'}
    )
    return response


@app.route('/api/dialogue-history/import', methods=['POST'])
def import_dialogue_history():
    """Import dialogue history from JSON file, merging with existing"""
    try:
        data = request.get_json()
        if not isinstance(data, list):
            return jsonify({"error": "Invalid format - expected array"}), 400

        # Load existing history
        existing = load_dialogue_history(load_game_context)

        # Create set of existing entry signatures for dedup
        existing_sigs = set()
        for entry in existing:
            sig = (entry.get('timestamp', 0), entry.get('voiceName', ''), entry.get('text', ''))
            existing_sigs.add(sig)

        # Add new entries that don't already exist
        added = 0
        for entry in data:
            sig = (entry.get('timestamp', 0), entry.get('voiceName', ''), entry.get('text', ''))
            if sig not in existing_sigs:
                existing.append(entry)
                existing_sigs.add(sig)
                added += 1

        # Sort by timestamp
        existing.sort(key=lambda x: x.get('timestamp', 0))

        save_dialogue_history(existing)
        print(f"[History] Imported {added} new entries")
        return jsonify({"status": "ok", "added": added, "total": len(existing)})
    except Exception as e:
        print(f"[History] Import error: {e}")
        return jsonify({"error": str(e)}), 400


@app.route('/api/dialogue-history/clear-npc/<npc_id>', methods=['DELETE'])
def clear_npc_from_history(npc_id):
    """Remove an NPC from all dialogue history (earshot arrays and as speaker).

    This removes the NPC from memory - they won't remember conversations they witnessed,
    and conversations where they spoke will be deleted entirely.
    """
    try:
        dialogue_history = load_dialogue_history(load_game_context)

        entries_removed = 0
        updated_history = []

        for entry in dialogue_history:
            # If NPC was the speaker, remove entire entry
            if entry.get('voiceName') == npc_id:
                entries_removed += 1
                continue

            # Remove NPC from earshot array
            earshot = entry.get('earshot', [])
            if npc_id in earshot:
                earshot = [e for e in earshot if e != npc_id]
                entry['earshot'] = earshot

                # If no witnesses left and not player/AI entry, remove entry
                if not earshot and not entry.get('isPlayer') and not entry.get('isAIResponse'):
                    entries_removed += 1
                    continue

            updated_history.append(entry)

        save_dialogue_history(updated_history)
        print(f"[History] Cleared NPC '{npc_id}' - removed {entries_removed} entries")
        return jsonify({"success": True, "entries_removed": entries_removed})
    except Exception as e:
        print(f"[History] Clear NPC error: {e}")
        return jsonify({"error": str(e)}), 400


@app.route('/api/dialogue-history/entries', methods=['DELETE'])
def delete_dialogue_entries():
    """Delete specific dialogue history entries by timestamp."""
    try:
        data = request.get_json()
        timestamps = set(data.get('timestamps', []))
        if not timestamps:
            return jsonify({"status": "error", "message": "No timestamps provided"}), 400

        dialogue_history = load_dialogue_history(load_game_context)
        original_count = len(dialogue_history)
        dialogue_history = [e for e in dialogue_history if e.get('timestamp') not in timestamps]
        deleted_count = original_count - len(dialogue_history)

        save_dialogue_history(dialogue_history)
        print(f"[History] Deleted {deleted_count} entries")
        return jsonify({"status": "ok", "deleted": deleted_count})
    except Exception as e:
        print(f"[History] Delete entries error: {e}")
        return jsonify({"error": str(e)}), 400


@app.route('/api/characters/export', methods=['GET'])
def export_characters():
    """Export character settings (bios + viseme scales)"""
    settings = load_settings()
    char_data = {
        "bios": settings.get('prompts', {}).get('bios', {}),
        "viseme_scales": settings.get('lipsync', {}).get('npc_scales', {})
    }
    response = Response(
        json.dumps(char_data, indent=2),
        mimetype='application/json',
        headers={'Content-Disposition': 'attachment;filename=character_settings.json'}
    )
    return response


@app.route('/api/characters/import', methods=['POST'])
def import_characters():
    """Import character settings, merging with existing"""
    try:
        data = request.get_json()
        if not isinstance(data, dict):
            return jsonify({"error": "Invalid format - expected object"}), 400

        settings = load_settings()

        # Merge bios
        if 'bios' in data and isinstance(data['bios'], dict):
            if 'prompts' not in settings:
                settings['prompts'] = {}
            if 'bios' not in settings['prompts']:
                settings['prompts']['bios'] = {}
            settings['prompts']['bios'].update(data['bios'])

        # Merge viseme scales
        if 'viseme_scales' in data and isinstance(data['viseme_scales'], dict):
            if 'lipsync' not in settings:
                settings['lipsync'] = {}
            if 'npc_scales' not in settings['lipsync']:
                settings['lipsync']['npc_scales'] = {}
            settings['lipsync']['npc_scales'].update(data['viseme_scales'])

        if save_settings(settings):
            bio_count = len(data.get('bios', {}))
            scale_count = len(data.get('viseme_scales', {}))
            print(f"[Settings] Imported {bio_count} bios, {scale_count} viseme scales")
            return jsonify({"status": "ok", "bios": bio_count, "viseme_scales": scale_count})
        return jsonify({"error": "Failed to save"}), 500
    except Exception as e:
        print(f"[Settings] Character import error: {e}")
        return jsonify({"error": str(e)}), 400


@app.route('/api/system-events', methods=['GET'])
def get_system_events():
    limit = request.args.get('limit', 100, type=int)
    events = event_logger.get_recent_events(limit=limit)
    return jsonify(events)


@app.route('/api/system-events', methods=['DELETE'])
def clear_system_events():
    event_logger.clear_events()
    return jsonify({"status": "cleared"})


# ============================================
# Setup API
# ============================================
_setup_running = None  # Track which setup command is running
_setup_lock = threading.Lock()
_setup_error = None  # Store last error message


def _run_setup_command(command, args=None):
    """Run a setup command in background thread."""
    global _setup_running, _setup_error

    _setup_error = None

    try:
        if command == "extract_localization":
            language = args.get("language", "EN_US") if args else "EN_US"
            script_path = os.path.join(SONORUS_DIR, "setup", "extract_localization.py")

            if not os.path.exists(script_path):
                raise FileNotFoundError(f"Setup script not found: {script_path}")

            print(f"[Setup] Running: extract_localization.py --both --language {language}")

            result = subprocess.run(
                [sys.executable, script_path, "--both", "--language", language],
                capture_output=True,
                text=True,
                cwd=SONORUS_DIR,
                timeout=600  # 10 minute timeout
            )

            if result.returncode != 0:
                error_msg = result.stderr or result.stdout or "Unknown error"
                # Make error human-readable
                if "repak.exe" in error_msg.lower() or "not found" in error_msg.lower():
                    error_msg = "Required tool 'repak.exe' is missing. Ensure the bin/ folder contains all required tools."
                elif "pak file" in error_msg.lower() or "pakchunk" in error_msg.lower():
                    error_msg = "Game files not found. Verify Hogwarts Legacy is installed correctly."
                elif "permission" in error_msg.lower():
                    error_msg = "Cannot write files. Try running as administrator or check folder permissions."
                raise Exception(error_msg)

            print(f"[Setup] extract_localization complete")

            # Save language to settings
            settings = load_settings()
            if 'setup' not in settings:
                settings['setup'] = {}
            settings['setup']['language'] = language
            save_settings(settings)

        elif command == "extract_voices":
            script_path = os.path.join(SONORUS_DIR, "setup", "extract_voices.py")

            if not os.path.exists(script_path):
                raise FileNotFoundError(f"Setup script not found: {script_path}")

            # Check if voice_manifest.json exists
            manifest_path = os.path.join(SONORUS_DIR, "data", "voice_manifest.json")
            if not os.path.exists(manifest_path):
                raise FileNotFoundError("Voice manifest not found. Ensure voice_manifest.json exists in the sonorus folder.")

            print(f"[Setup] Running: extract_voices.py --from-manifest")

            result = subprocess.run(
                [sys.executable, script_path, "--from-manifest"],
                capture_output=True,
                text=True,
                cwd=SONORUS_DIR,
                timeout=3600  # 60 minute timeout for voice extraction
            )

            if result.returncode != 0:
                error_msg = result.stderr or result.stdout or "Unknown error"
                # Make error human-readable
                if "vgmstream" in error_msg.lower():
                    error_msg = "Required tool 'vgmstream-cli.exe' is missing. Download from https://github.com/vgmstream/vgmstream/releases"
                elif "wwiser" in error_msg.lower():
                    error_msg = "Required tool 'wwiser.pyz' is missing. Ensure the bin/ folder contains all required tools."
                elif "permission" in error_msg.lower():
                    error_msg = "Cannot write files. Try running as administrator or check folder permissions."
                raise Exception(error_msg)

            print(f"[Setup] extract_voices complete")

        else:
            raise ValueError(f"Unknown setup command: {command}")

    except subprocess.TimeoutExpired:
        _setup_error = "Operation timed out. The game files may be too large or the system is busy."
    except FileNotFoundError as e:
        _setup_error = str(e)
    except Exception as e:
        _setup_error = str(e)
    finally:
        with _setup_lock:
            _setup_running = None


@app.route('/api/setup/status', methods=['GET'])
def get_setup_status():
    """Check setup completion status."""
    global _setup_running, _setup_error

    # Check which required files exist (in data/ folder)
    main_loc = os.path.exists(os.path.join(DATA_DIR, "main_localization.json"))
    subtitles = os.path.exists(os.path.join(DATA_DIR, "subtitles.json"))

    # Voice extraction progress - track both extraction and reference creation
    voice_refs_dir = os.path.join(SONORUS_DIR, "voice_references")
    extracted_audio_dir = os.path.join(SONORUS_DIR, "extracted_audio")
    manifest_path = os.path.join(DATA_DIR, "voice_manifest.json")

    voices_total = 0
    voices_extracted = 0  # Have WAV files in extracted_audio/
    voices_referenced = 0  # Have combined reference in voice_references/

    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, 'r', encoding='utf-8') as f:
                manifest = json.load(f)
            voices_total = len(manifest.get("voices", {}))

            for voice_name in manifest.get("voices", {}).keys():
                # Check for final reference file
                ref_file = os.path.join(voice_refs_dir, f"{voice_name}_reference_60s.wav")
                has_reference = os.path.exists(ref_file)
                if has_reference:
                    voices_referenced += 1

                # Check for extracted WAV files (in progress)
                voice_wav_dir = os.path.join(extracted_audio_dir, voice_name, "wav")
                has_extracted = os.path.exists(voice_wav_dir) and any(f.endswith('.wav') for f in os.listdir(voice_wav_dir))

                # "Extracted" = processed through extraction (has files OR already combined)
                # This ensures the count never drops
                if has_extracted or has_reference:
                    voices_extracted += 1
        except Exception:
            pass

    voices_complete = voices_total > 0 and voices_referenced >= voices_total

    # Get saved language from settings
    settings = load_settings()
    saved_language = settings.get('setup', {}).get('language', 'EN_US')

    # Determine localization status
    loc_status = "complete" if (main_loc and subtitles) else "not_started"
    if _setup_running == "extract_localization":
        loc_status = "running"
    elif _setup_error and not (main_loc and subtitles):
        loc_status = "error"

    # Determine voices status (based on referenced, since extracted gets cleaned up after combining)
    if voices_complete:
        voices_status = "complete"
    elif voices_referenced > 0 or voices_extracted > 0:
        voices_status = "partial"  # Some progress made
    else:
        voices_status = "not_started"

    if _setup_running == "extract_voices":
        voices_status = "running"
    elif _setup_error and not voices_complete:
        voices_status = "error"

    # TTS test status
    tts_tested = settings.get('setup', {}).get('tts_tested', False)
    tts_status = "complete" if tts_tested else "not_started"
    if _setup_running == "test_tts":
        tts_status = "running"

    # LLM test status
    llm_tested = settings.get('setup', {}).get('llm_tested', False)
    llm_status = "complete" if llm_tested else "not_started"
    if _setup_running == "test_llm":
        llm_status = "running"

    # Get configured models for display
    conv_settings = settings.get('conversation', {})
    vision_settings = settings.get('agents', {}).get('vision', {}).get('llm', {})
    models = {
        'chat': conv_settings.get('chat_model', 'google/gemini-3-flash-preview'),
        'vision': vision_settings.get('model', 'google/gemini-2.0-flash-001'),
        'target': conv_settings.get('target_selection_model', 'google/gemini-2.0-flash-001'),
        'interject': conv_settings.get('interjection_model', 'google/gemini-2.0-flash-001')
    }

    # Overall completion - all required steps must pass
    complete = (
        (main_loc and subtitles) and
        voices_complete and
        tts_tested and
        llm_tested
    )

    return jsonify({
        "complete": complete,
        "language": saved_language,
        "steps": {
            "localization": {
                "status": loc_status,
                "files": {
                    "main_localization.json": main_loc,
                    "subtitles.json": subtitles
                }
            },
            "voices": {
                "status": voices_status,
                "total": voices_total,
                "extracted": voices_extracted,
                "referenced": voices_referenced
            },
            "tts": {
                "status": tts_status,
                "tested": tts_tested
            },
            "llm": {
                "status": llm_status,
                "tested": llm_tested,
                "models": models
            }
        },
        "running_command": _setup_running,
        "last_error": _setup_error
    })


@app.route('/api/setup/extract-localization', methods=['POST'])
def setup_extract_localization():
    """Start localization extraction."""
    global _setup_running, _setup_error

    with _setup_lock:
        if _setup_running:
            return jsonify({"error": f"Setup already running: {_setup_running}"}), 400
        _setup_running = "extract_localization"
        _setup_error = None

    data = request.get_json() or {}
    language = data.get("language", "EN_US")

    # Start extraction in background thread
    thread = threading.Thread(
        target=_run_setup_command,
        args=("extract_localization", {"language": language}),
        daemon=True
    )
    thread.start()

    return jsonify({
        "status": "started",
        "message": "Extracting localization files..."
    })


@app.route('/api/setup/extract-voices', methods=['POST'])
def setup_extract_voices():
    """Start voice reference extraction."""
    global _setup_running, _setup_error

    with _setup_lock:
        if _setup_running:
            return jsonify({"error": f"Setup already running: {_setup_running}"}), 400
        _setup_running = "extract_voices"
        _setup_error = None

    # Start extraction in background thread
    thread = threading.Thread(
        target=_run_setup_command,
        args=("extract_voices",),
        daemon=True
    )
    thread.start()

    return jsonify({
        "status": "started",
        "message": "Extracting voice references... This may take several minutes."
    })


def play_audio_system(audio_data, sample_rate=44100):
    """Play audio through system default device using sounddevice."""
    import sounddevice as sd
    import numpy as np

    # Convert bytes to numpy array (assuming 16-bit PCM)
    audio_array = np.frombuffer(audio_data, dtype=np.int16)
    audio_float = audio_array.astype(np.float32) / 32768.0

    # Play and wait for completion
    sd.play(audio_float, sample_rate)
    sd.wait()


@app.route('/api/setup/test-tts', methods=['POST'])
def setup_test_tts():
    """Test TTS by generating and playing audio through system speakers."""
    global _setup_running, _setup_error

    data = request.get_json() or {}
    text = data.get('text', 'Hello, this is a test of the voice synthesis system.')

    # Get player voice (settings > fallback to PlayerMale)
    settings = load_settings()
    conv_settings = settings.get('conversation', {})
    player_voice = conv_settings.get('player_voice_name', '') or 'PlayerMale'
    tts_settings = settings.get('tts', {})
    provider = tts_settings.get('provider', 'inworld')

    # Set running state
    with _setup_lock:
        if _setup_running:
            return jsonify({
                'success': False,
                'error': f'Another setup operation is running: {_setup_running}'
            }), 409
        _setup_running = "test_tts"
        _setup_error = None

    try:
        # Import TTS service
        from services import tts

        # Check if TTS is available
        if not tts.is_available():
            raise Exception(f"TTS not configured. Please add your {provider.title()} API key in the TTS settings.")

        # Generate audio (returns PCM bytes and sample rate)
        start_time = time.time()
        audio_data, sample_rate = tts.synthesize_to_bytes(text, player_voice)
        synthesis_ms = (time.time() - start_time) * 1000

        # Play through system audio
        play_audio_system(audio_data, sample_rate)

        # Mark TTS test as complete in settings
        settings = load_settings()
        if 'setup' not in settings:
            settings['setup'] = {}
        settings['setup']['tts_tested'] = True
        save_settings(settings)

        return jsonify({
            'success': True,
            'voice_used': player_voice,
            'provider': provider,
            'duration_ms': synthesis_ms
        })

    except Exception as e:
        error_msg = str(e)
        # Translate common errors to human-readable messages
        if 'api_key' in error_msg.lower() or 'unauthorized' in error_msg.lower():
            error_msg = f"TTS API key not found or invalid. Configure your API key in the TTS settings section."
        elif 'connection' in error_msg.lower() or 'refused' in error_msg.lower():
            error_msg = "Cannot connect to TTS service. Check your internet connection."
        # Otherwise pass through the specific error from the TTS system

        _setup_error = error_msg
        return jsonify({
            'success': False,
            'voice_used': player_voice,
            'provider': provider,
            'error': error_msg
        })

    finally:
        with _setup_lock:
            _setup_running = None


@app.route('/api/setup/test-llm', methods=['POST'])
def setup_test_llm():
    """Test all unique LLM models configured."""
    global _setup_running, _setup_error

    # Set running state
    with _setup_lock:
        if _setup_running:
            return jsonify({
                'success': False,
                'error': f'Another setup operation is running: {_setup_running}'
            }), 409
        _setup_running = "test_llm"
        _setup_error = None

    try:
        settings = load_settings()
        conv_settings = settings.get('conversation', {})
        vision_settings = settings.get('agents', {}).get('vision', {}).get('llm', {})

        # Collect models and their uses - build properly to handle duplicates
        # Include max_tokens for each use case to test reasoning properly
        model_uses = {}
        models_list = [
            (conv_settings.get('chat_model', 'google/gemini-3-flash-preview'), 'chat',
             conv_settings.get('max_tokens', 8192)),
            (conv_settings.get('target_selection_model', 'google/gemini-2.0-flash-001'), 'target',
             conv_settings.get('target_selection_max_tokens', 8192)),
            (conv_settings.get('interjection_model', 'google/gemini-2.0-flash-001'), 'interject',
             conv_settings.get('interjection_max_tokens', 8192)),
            (vision_settings.get('model', 'google/gemini-2.0-flash-001'), 'vision',
             vision_settings.get('max_tokens', 8192))
        ]

        for model_id, use, max_tokens in models_list:
            if model_id not in model_uses:
                model_uses[model_id] = {'uses': [], 'max_tokens': max_tokens}
            model_uses[model_id]['uses'].append(use)
            # Use the highest max_tokens among uses (to properly test reasoning)
            model_uses[model_id]['max_tokens'] = max(model_uses[model_id]['max_tokens'], max_tokens)

        # Test each unique model
        import llm
        test_prompt = "What is 2+2? Reply with just the number."

        results = {}
        all_success = True

        for model_id, info in model_uses.items():
            uses = info['uses']
            max_tokens = info['max_tokens']
            try:
                start_time = time.time()
                # Use the same max_tokens as production to test reasoning properly
                response = llm.chat_simple(
                    test_prompt,
                    model=model_id,
                    temperature=0.0,
                    max_tokens=max_tokens,
                    context="setup_test"
                )
                duration_ms = (time.time() - start_time) * 1000

                if response:
                    results[model_id] = {
                        'success': True,
                        'used_for': uses,
                        'response_excerpt': response[:50],
                        'duration_ms': round(duration_ms)
                    }
                else:
                    all_success = False
                    # Get the actual error from llm module
                    error_msg = llm.get_last_error() or 'No response received from model'
                    results[model_id] = {
                        'success': False,
                        'used_for': uses,
                        'error': error_msg
                    }
            except Exception as e:
                all_success = False
                error_msg = str(e)
                # Translate common errors
                if 'api_key' in error_msg.lower() or 'unauthorized' in error_msg.lower() or '401' in error_msg:
                    error_msg = "Invalid API key. Check your OpenRouter/OpenAI API key."
                elif 'not found' in error_msg.lower() or '404' in error_msg:
                    error_msg = f"Model '{model_id}' not available. Verify the model ID."
                elif 'insufficient' in error_msg.lower() or 'credits' in error_msg.lower():
                    error_msg = "API account has insufficient credits."
                elif 'timeout' in error_msg.lower():
                    error_msg = "Request timed out. Try again."

                results[model_id] = {
                    'success': False,
                    'used_for': uses,
                    'error': error_msg
                }

        # Mark LLM test as complete if all passed
        if all_success:
            settings = load_settings()
            if 'setup' not in settings:
                settings['setup'] = {}
            settings['setup']['llm_tested'] = True
            save_settings(settings)

        failed_count = sum(1 for r in results.values() if not r['success'])
        total_count = len(results)

        if not all_success:
            _setup_error = f'{failed_count} of {total_count} models failed'

        return jsonify({
            'success': all_success,
            'results': results,
            'error': f'{failed_count} of {total_count} models failed' if not all_success else None
        })

    except Exception as e:
        _setup_error = str(e)
        return jsonify({
            'success': False,
            'results': {},
            'error': str(e)
        })

    finally:
        with _setup_lock:
            _setup_running = None


# ============================================
# Main
# ============================================
def is_setup_complete():
    """Check if all 4 setup steps are complete."""
    # Check localization files
    main_loc = os.path.exists(os.path.join(DATA_DIR, "main_localization.json"))
    subtitles = os.path.exists(os.path.join(DATA_DIR, "subtitles.json"))

    # Check voice extraction
    manifest_path = os.path.join(DATA_DIR, "voice_manifest.json")
    voice_refs_dir = os.path.join(SONORUS_DIR, "voice_references")
    voices_complete = False

    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, 'r', encoding='utf-8') as f:
                manifest = json.load(f)
            voices_total = len(manifest.get("voices", {}))
            voices_referenced = 0
            for voice_name in manifest.get("voices", {}).keys():
                ref_file = os.path.join(voice_refs_dir, f"{voice_name}_reference_60s.wav")
                if os.path.exists(ref_file):
                    voices_referenced += 1
            voices_complete = voices_total > 0 and voices_referenced >= voices_total
        except Exception:
            pass

    # Check TTS and LLM tests
    settings = load_settings()
    tts_tested = settings.get('setup', {}).get('tts_tested', False)
    llm_tested = settings.get('setup', {}).get('llm_tested', False)

    return (main_loc and subtitles) and voices_complete and tts_tested and llm_tested


def main():
    port = int(os.getenv("SONORUS_SERVER_PORT", "5000"))

    # Start game monitor
    start_game_monitor()

    # Start socket server
    lua_socket.start()

    # Start heartbeat thread
    def heartbeat_loop():
        running_file = os.path.join(SONORUS_DIR, "server.heartbeat")
        while True:
            try:
                with open(running_file, "w") as f:
                    f.write(str(time.time()))
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

    # Start message queue polling
    def message_queue_loop():
        pending_file = os.path.join(SONORUS_DIR, "pending_message.json")
        while True:
            try:
                if os.path.exists(pending_file):
                    with open(pending_file, 'r', encoding='utf-8') as f:
                        content = f.read().strip()
                    if content:
                        with open(pending_file, 'w') as f:
                            f.write('')
                        data = json.loads(content)
                        print(f"\n[Queue] Processing message: {data.get('user_input', '')[:50]}...")
                        process_chat_request(data)
            except json.JSONDecodeError:
                pass
            except Exception as e:
                print(f"[Queue] Error: {e}")
            time.sleep(0.1)

    queue_thread = threading.Thread(target=message_queue_loop, daemon=True)
    queue_thread.start()
    print("[Server] Message queue polling started")

    # Fetch model capabilities (for reasoning support detection)
    try:
        llm.fetch_model_capabilities()
    except Exception as e:
        print(f"[Server] Failed to fetch model capabilities: {e}")

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
                # Quick state-only handshake (no periodic polling anymore)
                context = lua_socket.request_state_only(timeout=0.2)
                player_loaded = context.get('playerLoaded', False)
                is_paused = context.get('isGamePaused', False)
                if not player_loaded:
                    print(f"[InputCapture] check_pause: playerLoaded={player_loaded}, blocking chat")
                    return True
                if is_paused:
                    print(f"[InputCapture] check_pause: isGamePaused={is_paused}, blocking chat")
                    return True
                # Block in cinematic or combat
                if context.get('inCinematic'):
                    print("[InputCapture] Blocked - in cinematic")
                    return True
                if context.get('inCombat'):
                    lua_socket.send_notification("Cannot talk during combat")
                    print("[InputCapture] Blocked - in combat")
                    return True
                return False

            def on_chat_input(msg):
                msg_type = msg.get("type")
                active = msg.get("active", False)
                text = msg.get("text", "")
                print(f"[InputCapture] Sending to Lua: type={msg_type} active={active} text='{text[:20]}'")
                send_result = lua_socket.send(msg)
                if not send_result:
                    print(f"[InputCapture] WARNING: lua_socket.send() returned False - message not sent!")

                if msg_type == "chat_submit":
                    if check_game_paused():
                        print("[InputCapture] Submit blocked - game is paused")
                        return
                    text = msg.get("text", "").strip()
                    if text:
                        print(f"[InputCapture] Processing chat: {text}")
                        threading.Thread(
                            target=process_chat_request,
                            args=({"user_input": text},),
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
            """Check if STT should be blocked (called before recording starts)."""
            # Quick state-only handshake (no periodic polling anymore)
            context = lua_socket.request_state_only(timeout=0.2)
            player_loaded = context.get('playerLoaded', False)
            is_paused = context.get('isGamePaused', False)
            if is_paused or not player_loaded:
                return True
            # Block in cinematic or combat
            if context.get('inCinematic'):
                stt_capture.play_error_sound()
                print("[STT] Blocked - in cinematic")
                return True
            if context.get('inCombat'):
                stt_capture.play_error_sound()
                lua_socket.send_notification("Cannot talk during combat")
                print("[STT] Blocked - in combat")
                return True
            return False

        def on_stt_transcribe(text):
            """Handle transcribed speech - same as typed text but skip player voice TTS."""
            if text:
                print(f"[STT] Processing: {text}")
                threading.Thread(
                    target=process_chat_request,
                    args=({"user_input": text, "from_stt": True},),
                    daemon=True
                ).start()

        def on_stt_error(error_msg):
            """Show STT errors as in-game notifications."""
            lua_socket.send_notification(error_msg)

        # Always register callbacks (enables hot-reload from disabled state)
        stt_capture.register_callbacks(on_stt_transcribe, check_pause=check_stt_paused, on_error=on_stt_error)

        if stt_service.is_available():
            stt_hotkey = stt_settings.get('hotkey', 'middle_mouse')
            try:
                stt_capture.start_capture(on_stt_transcribe, stt_hotkey, check_pause=check_stt_paused, on_error=on_stt_error)
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

            print("[Server] Stop conversation hotkey pressed")

            # 1. Request cancellation (timestamp-based, doesn't touch conv_state)
            request_cancel()

            # 2. Abort audio playback immediately
            if AUDIO3D_AVAILABLE and audio_get_player:
                try:
                    player = audio_get_player()
                    player.abort()
                    print("[Server] Audio playback aborted")
                except Exception as e:
                    print(f"[Server] Audio abort error: {e}")

            # 3. Clear playback tracking
            lua_socket.playback_active = False
            lua_socket.playback_event.set()

            # 4. Send reset to Lua (triggers ResetState + releases NPCs)
            lua_socket.send_reset()

            # 5. Show notification
            lua_socket.send_notification("Conversation stopped")
            print("[Server] Conversation stop signal sent")

        try:
            stop_capture.start_capture(on_stop_pressed, stop_hotkey, check_pause=check_stop_paused)
            print(f"[Server] Stop capture started (hotkey: {stop_hotkey})")
        except Exception as e:
            print(f"[Server] Stop capture failed to start: {e}")

    print("=" * 50)
    print("Sonorus Server")
    print("=" * 50)
    print(f"[Server] PID: {os.getpid()}")
    print(f"[Server] Port: {port}")
    print(f"[Server] TTS: {TTS_AVAILABLE}")
    print(f"[Server] Audio3D: {AUDIO3D_AVAILABLE}")

    # Initialize TTS voice cache
    if TTS_AVAILABLE:
        print(f"[Server] Loading TTS voice cache ({tts.get_provider_name()})...")
        try:
            tts.init()
        except Exception as e:
            print(f"[Server] TTS init failed: {e}")
            print("[Server] TTS will attempt to initialize on first use.")

    print(f"[Server] Starting on http://localhost:{port}")
    print(f"[Server] Config page: http://localhost:{port}/")
    print("[Server] Ready!")

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

    # Run Flask
    app.run(host='127.0.0.1', port=port, debug=True, use_reloader=False, threaded=True)


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
        # Cleanup
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
        try:
            lua_socket.stop()
        except:
            pass
        if VISION_AGENT_AVAILABLE:
            try:
                vision_agent.stop_agent()
            except:
                pass
        if AUDIO3D_AVAILABLE:
            try:
                audio_shutdown()
            except:
                pass
