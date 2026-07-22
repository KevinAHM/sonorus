"""
Lua socket server for Sonorus.
Provides bidirectional TCP communication with UE4SS Lua.
"""

import json
import os
import time
import socket as sock_lib
import struct
import threading

from . import mods
from .dialogue_db import (
    _format_game_date,
    _format_game_time,
    _minutes_to_game_datetime,
    get_latest_recorded_game_time_candidates as get_latest_dialogue_game_time_candidates,
    get_latest_recorded_game_minutes as get_latest_dialogue_game_minutes,
)
from .game_settings import get_game_subtitles_enabled
from .owl_post_db import (
    get_current_game_minutes,
    get_latest_recorded_game_time_candidates as get_latest_owl_post_game_time_candidates,
    get_latest_recorded_game_minutes as get_latest_owl_post_game_minutes,
)
from .text_utils import is_significant_npc


TIME_SYNC_WARNING_THRESHOLD_MINUTES = 60


def _format_game_minutes_for_log(total_minutes: int) -> str:
    """Format absolute game minutes for readable server logs."""
    if total_minutes <= 0:
        return "unknown"
    date_tuple, time_tuple = _minutes_to_game_datetime(total_minutes)
    return f"{_format_game_date(*date_tuple)} {_format_game_time(*time_tuple)} ({total_minutes}m)"


def _format_dialogue_candidate_for_log(candidate: dict) -> str:
    """Format a dialogue-history candidate row for mismatch diagnostics."""
    return (
        f"id={candidate.get('id')} source={candidate.get('source')} "
        f"time={candidate.get('gameDate')} {candidate.get('gameTime')} ({candidate.get('minutes')}m) "
        f"type={candidate.get('type')} speaker={candidate.get('speaker') or '?'} "
        f"voice={candidate.get('voiceName') or '?'} count={candidate.get('count')} "
        f"line={candidate.get('lineID') or '-'} location={candidate.get('location') or '-'} "
        f"text={repr(candidate.get('text') or '')}"
    )


def _format_owl_candidate_for_log(candidate: dict) -> str:
    """Format an Owl Post candidate row for mismatch diagnostics."""
    base = (
        f"id={candidate.get('id')} source={candidate.get('source')} "
        f"time={_format_game_minutes_for_log(int(candidate.get('minutes') or 0))} "
        f"kind={candidate.get('kind')}"
    )
    if candidate.get("kind") == "mail":
        return (
            f"{base} sender={candidate.get('sender') or '?'} "
            f"recipient={candidate.get('recipient') or '?'} subject={repr(candidate.get('subject') or '')}"
        )
    return (
        f"{base} author={candidate.get('author') or '?'} board={candidate.get('boardId')} "
        f"root={candidate.get('rootPostId')} title={repr(candidate.get('title') or '')}"
    )


class LuaSocketServer:
    """TCP server for bidirectional Lua communication."""

    def __init__(self, port=8173):
        self.port = port
        self.server = None
        self.client = None
        self.lock = threading.Lock()
        self.running = False
        self._connection_id = 0  # Incremented on each new client connection
        # Playback state tracking (for interjection loop)
        self.playback_active = False
        self.playback_event = threading.Event()
        # Pipeline state tracking (full TTS lifecycle: synthesis → playback)
        self.pipeline_active = False
        self.pipeline_event = threading.Event()
        self.pipeline_event.set()
        # Game context cache (received from Lua)
        self._game_context = {}
        self._context_lock = threading.Lock()
        # Speaker ready handshake (for async-safe actor caching)
        self._speaker_ready_event = threading.Event()
        self._speaker_ready_result = {"found": False}
        self._speaker_ready_lock = threading.Lock()
        # Turn-based system (replaces separate prepare_speaker + queue_item)
        self._turn_counter = 0
        self._turn_ready_event = threading.Event()
        self._turn_ready_result = {"turn_id": "", "actor_found": False}
        self._turn_ready_lock = threading.Lock()
        self._last_turn_id = None  # Track last turn for lipsync_start
        # Turn complete handshake (Lua signals when mouth animation is done)
        self._turn_complete_event = threading.Event()
        self._turn_complete_event.set()  # Initially complete (no pending turn)
        # House points refresh handshake (on-demand refresh for professor conversations)
        self._house_points_event = threading.Event()
        self._house_points_result = False
        self._house_points_lock = threading.Lock()
        # Context refresh handshake (request fresh nearbyNpcs from Lua)
        self._context_refresh_event = threading.Event()
        self._context_refresh_pending = False
        self._context_refresh_lock = threading.Lock()  # Serialize concurrent refresh requests
        # Position data from Lua (camera + NPC positions for 3D audio)
        self._positions = {
            "camX": 0, "camY": 0, "camZ": 0,
            "camYaw": 0, "camPitch": 0,
            "npcX": 0, "npcY": 0, "npcZ": 0
        }
        # Reverb data from Lua (for audio effects)
        self._current_reverb = {"auxbus": None, "send": 1.0}
        self._reverb_callback = None  # Callback for live reverb updates
        # Callbacks for external modules
        self._input_capture = None  # Will be set by server.py
        self._conv_state = None  # Will be set by server.py
        self._interrupt_callback = None  # Will be set by server.py (stop_conversation)
        self._game_event_callback = None  # Will be set by server.py (event commentary)
        self._pending_time_sync_check = False

    def set_input_capture(self, input_capture_module):
        """Set the input_capture module for force_close handling."""
        self._input_capture = input_capture_module

    def set_conv_state(self, conv_state):
        """Set the conversation state for reset handling."""
        self._conv_state = conv_state

    def set_interrupt_callback(self, callback):
        """Set the callback for conversation interrupts (e.g., cinematic start)."""
        self._interrupt_callback = callback

    def set_game_event_callback(self, callback):
        """Set the callback for lightweight gameplay events."""
        self._game_event_callback = callback

    def start(self):
        """Start socket server in background thread."""
        if self.running:
            return
        self.running = True
        thread = threading.Thread(target=self._server_loop, daemon=True)
        thread.start()
        print(f"[Socket] Server starting on port {self.port}")

    def _server_loop(self):
        """Accept connections (runs in background thread)."""
        self.server = sock_lib.socket(sock_lib.AF_INET, sock_lib.SOCK_STREAM)
        self.server.setsockopt(sock_lib.SOL_SOCKET, sock_lib.SO_REUSEADDR, 1)
        # SO_LINGER with timeout 0 allows immediate port rebind after crash/restart
        self.server.setsockopt(sock_lib.SOL_SOCKET, sock_lib.SO_LINGER, struct.pack('ii', 1, 0))
        self.server.bind(("127.0.0.1", self.port))
        self.server.listen(1)
        self.server.settimeout(1.0)  # Check running flag every second

        while self.running:
            try:
                client, addr = self.server.accept()
                with self.lock:
                    if self.client:
                        self.client.close()
                    self.client = client
                    self.client.settimeout(0.1)  # Non-blocking receives
                    self._connection_id += 1  # Track new connection for state sync
                print(f"[Socket] Lua connected from {addr}")
                # Start receive thread for this client
                recv_thread = threading.Thread(target=self._receive_loop, daemon=True)
                recv_thread.start()
                # Send initial settings and data
                self.send_tracking_settings()
                # send_significant_npcs and resync_active_commitments deferred
                # until player_handshake (they require per-player DBs)
                # Wire up VR tracker to push offsets to Lua
                try:
                    from vr import set_vr_lua_socket
                    set_vr_lua_socket(self)
                except Exception:
                    pass
            except sock_lib.timeout:
                continue
            except Exception as e:
                if self.running:
                    print(f"[Socket] Accept error: {e}")

    def send(self, data: dict):
        """Send JSON message to Lua (thread-safe)."""
        with self.lock:
            if not self.client:
                return False
            try:
                msg = json.dumps(data) + "\n"
                self.client.sendall(msg.encode())  # sendall ensures complete delivery
                return True
            except Exception as e:
                print(f"[Socket] Send failed: {e}")
                self.client = None
                return False

    # Cache: display name (lowered) -> canonical mod key
    _location_reverse_map = None

    def _resolve_location_id(self, display_name):
        """Reverse-lookup a localized display name to a canonical mod key.
        Uses location_registry.json + main_localization.json.
        Falls back to the display name itself if no match found."""
        if self._location_reverse_map is None:
            try:
                from .settings import SONORUS_DIR
                from .localization import load_localization
                import os
                reg_path = os.path.join(SONORUS_DIR, "data", "location_registry.json")
                with open(reg_path, 'r', encoding='utf-8') as f:
                    registry = json.load(f)
                loc = load_localization()
                self.__class__._location_reverse_map = {}
                for mod_key, entry in registry.items():
                    loc_id = entry.get("localized_id")
                    if loc_id and loc_id in loc:
                        display = loc[loc_id]
                        self.__class__._location_reverse_map[display.lower()] = mod_key
            except Exception as e:
                print(f"[Socket] Could not load location reverse map: {e}")
                self.__class__._location_reverse_map = {}
        return self._location_reverse_map.get(display_name.lower(), display_name)

    def send_tracking_settings(self):
        """Send dialogue tracking settings to Lua."""
        try:
            from .settings import load_settings, is_dev_mode
            settings = load_settings()
            server = settings.get('server', {})
            history = settings.get('history', {})
            conversation = settings.get('conversation', {})
            setup = settings.get('setup', {})
            input_settings = settings.get('input', {})
            time_dilation = settings.get('time_dilation', {})
            self.send({
                "type": "tracking_settings",
                # Master toggle - when off, Lua disables all mod functions except communication
                "mod_enabled": server.get('enabled', True),
                "track_ambient": history.get('track_ambient', True),
                "track_cutscene": history.get('track_cutscene', True),
                "auto_mute_ambient": conversation.get('auto_mute_ambient', True),
                "dev_mode": is_dev_mode(),
                # Game language for localization file loading
                "language": setup.get('language', 'EN_US'),
                # Preview lock: lock NPC while typing/speaking (before sending message)
                "preview_lock": input_settings.get('preview_lock', True),
                # Game subtitle setting from Hogwarts Legacy GameUserSettings.ini
                "subtitles_enabled": get_game_subtitles_enabled(),
                # Time dilation settings (rates as realtime multipliers: 1.0 = realtime, 3.0 = 3x faster)
                "time_dilation": {
                    "enabled": time_dilation.get('enabled', True),
                    "day_rate": time_dilation.get('day_rate', 3.0),
                    "night_rate": time_dilation.get('night_rate', 3.0),
                    "day_start_hour": time_dilation.get('day_start_hour', 6),
                    "night_start_hour": time_dilation.get('night_start_hour', 18),
                },
                # TTS provider ("none" = disabled, shows bracketed text in subtitles)
                "tts_provider": settings.get('tts', {}).get('provider', ''),
                # Companion follow distance in meters (converted to UU on Lua side)
                "companion_follow_distance_m": conversation.get('companion_follow_distance_m', 2.0),
                # NPC followers enabled (gated by actions_enabled on config page)
                "followers_enabled": conversation.get('followers_enabled', True),
                # Third-party mod integration flags
                "floo_companions_installed": mods.is_mod_installed('floo_companions'),
                "conversation_fpv": conversation.get('conversation_fpv', False),
                "conversation_fpv_transition": conversation.get('conversation_fpv_transition', 'normal'),
                "conversation_look_at_speaker": conversation.get('conversation_look_at_speaker', False),
                # Attention meter settings
                "attention_meter_enabled": conversation.get('attention_meter_enabled', True),
                "attention_cold_approach_enabled": conversation.get('attention_cold_approach_enabled', True),
                "gaze_enabled": conversation.get('gaze_enabled', False),
            })
        except Exception as e:
            print(f"[Socket] Error sending tracking settings: {e}")

    def send_significant_npcs(self):
        """Send list of significant NPC names to Lua for client-side filtering.

        Sends both voice names (internal IDs) and generated display names,
        since Lua only has access to display names from GetActorDisplayName().
        Also sends the ambient dialogue blocklist.
        """
        try:
            from .text_utils import get_significant_npc_names, INSIGNIFICANT_PREFIXES
            voice_names, display_names = get_significant_npc_names()
            self.send({
                "type": "sync_significant_npcs",
                "voice_names": voice_names,
                "display_names": display_names,
                "insignificant_prefixes": list(INSIGNIFICANT_PREFIXES),
            })
        except Exception as e:
            print(f"[Socket] Error sending significant NPCs: {e}")
        # Send ambient blocklist alongside
        self.send_ambient_blocklist()

    # NPCs exempt from auto-mute: portraits and ghosts have limited/unique
    # ambient lines and aren't the repetitive quest-callout type.
    _AMBIENT_MUTE_EXEMPT = {
        # Portraits
        "fatlady", "marydunne", "lethiaburbley",
        "sircadogan", "musicconductor", "sylviapembroke", "ogletheportrait",
        # Ghosts & poltergeist
        "nearlyheadlessnick", "fatfriar", "bloodybaron",
        "extraghostcharacterm2", "cuthbertbinns", "peeves",
        # Vendors
        "augustushill", "gerboldollivander", "albieweekes", "parrypippin", "timothyteasdale",
        "thomasbrown", "calliopsnelling", "sironaryan", "sirona", "thaddeustravers",
        "vendorcauldronshop", "vendorjokeshop", "vendormusicshop",
        "vendorquillshop", "vendorsecondhandshop1", "vendorteashop",
        # Other
        "jemimacollins"
    }

    def _build_ambient_blocklist(self) -> dict:
        """Build per-NPC blocklist of heard ambient dialogue text hashes.

        Returns: { "GarrethWeasley": [hash1, hash2, ...], ... }
        Only includes significant NPCs with at least one ambient line.
        Skips portrait/ghost NPCs listed in _AMBIENT_MUTE_EXEMPT.
        """
        from .dialogue_db import get_connection
        from .text_utils import get_significant_npc_names

        voice_names, _ = get_significant_npc_names()
        sig_set = {v.lower() for v in voice_names}

        blocklist = {}
        try:
            with get_connection() as conn:
                rows = conn.execute(
                    "SELECT voice_name, line_id FROM dialogue_entries "
                    "WHERE entry_type = 'chatter' AND text IS NOT NULL AND text != '' "
                    "AND is_player = 0 AND is_ai_response = 0 "
                    "AND line_id IS NOT NULL AND line_id != ''"
                ).fetchall()
            for row in rows:
                vn = row['voice_name']
                if not vn or vn.lower() not in sig_set:
                    continue
                if vn.lower() in self._AMBIENT_MUTE_EXEMPT:
                    continue
                line_id = row['line_id']
                # Extract numeric suffix: "GarrethWeasley_10946" -> 10946
                parts = line_id.rsplit('_', 1)
                if len(parts) == 2 and parts[1].isdigit():
                    num = int(parts[1])
                else:
                    continue
                if vn not in blocklist:
                    blocklist[vn] = set()
                blocklist[vn].add(num)
        except Exception as e:
            print(f"[Socket] Error building ambient blocklist: {e}")

        # Convert sets to sorted lists for JSON
        return {vn: sorted(hashes) for vn, hashes in blocklist.items()}

    def send_ambient_blocklist(self):
        """Send ambient dialogue blocklist to Lua. Called on connect and after new lines.
        Sends empty blocklist if auto_mute_ambient is disabled."""
        try:
            from .settings import load_settings
            settings = load_settings()
            enabled = settings.get('conversation', {}).get('auto_mute_ambient', True)

            if not enabled:
                # Disabled — send empty blocklist to clear any existing one on Lua side
                self._ambient_blocklist_cache = {}
                self.send({"type": "ambient_blocklist", "data": {}})
                print("[Socket] Ambient blocklist disabled (auto_mute_ambient=false)")
                return

            blocklist = self._build_ambient_blocklist()
            if not blocklist:
                return
            self._ambient_blocklist_cache = blocklist
            total = sum(len(v) for v in blocklist.values())
            self.send({
                "type": "ambient_blocklist",
                "data": blocklist,
            })
            print(f"[Socket] Sent ambient blocklist: {len(blocklist)} NPCs, {total} line IDs")
        except Exception as e:
            print(f"[Socket] Error sending ambient blocklist: {e}")

    def resync_active_commitments(self):
        """Re-send activate_commitment for all active commitments on reconnect."""
        try:
            from .commitments_db import get_active_commitments
            active = get_active_commitments()
            for c in active:
                print(f"[Socket] Resync commitment: {c['npc_id']} -> {c['location_id']}")
                self.send_activate_commitment(c["npc_id"], c["activity_id"], c["location_id"])
            if active:
                print(f"[Socket] Resynced {len(active)} active commitments")
        except Exception as e:
            print(f"[Socket] Error resyncing commitments: {e}")

    def _send_deferred_connect_data(self):
        """Send data that requires per-player DBs. Called after player_handshake."""
        try:
            self.send_significant_npcs()
        except Exception as e:
            print(f"[LuaSocket] Error sending significant NPCs: {e}")
        try:
            self.resync_active_commitments()
        except Exception as e:
            print(f"[LuaSocket] Error resyncing commitments: {e}")

    def _maybe_warn_out_of_sync_game_time(self, game_context: dict):
        """Notify once after handshake if stored data is far ahead of the loaded save."""
        if True or not self._pending_time_sync_check:
            return

        current_game_minutes = get_current_game_minutes(game_context)
        if current_game_minutes <= 0:
            return

        self._pending_time_sync_check = False
        threshold_minutes = current_game_minutes + TIME_SYNC_WARNING_THRESHOLD_MINUTES
        ahead_sources = []
        mismatch_details = []

        try:
            owl_post_minutes = get_latest_owl_post_game_minutes()
            if owl_post_minutes >= threshold_minutes:
                ahead_sources.append("Owl Post")
                mismatch_details.append(
                    f"Owl Post latest={_format_game_minutes_for_log(owl_post_minutes)} "
                    f"(+{owl_post_minutes - current_game_minutes}m)"
                )
                for candidate in get_latest_owl_post_game_time_candidates(limit=3, min_minutes=threshold_minutes):
                    print(f"[LuaSocket] Owl Post mismatch candidate: {_format_owl_candidate_for_log(candidate)}")
        except Exception as e:
            print(f"[LuaSocket] Error checking Owl Post time sync: {e}")

        try:
            dialogue_minutes = get_latest_dialogue_game_minutes()
            if dialogue_minutes >= threshold_minutes:
                ahead_sources.append("dialogue history")
                mismatch_details.append(
                    f"dialogue history latest={_format_game_minutes_for_log(dialogue_minutes)} "
                    f"(+{dialogue_minutes - current_game_minutes}m)"
                )
                for candidate in get_latest_dialogue_game_time_candidates(limit=3, min_minutes=threshold_minutes):
                    print(f"[LuaSocket] Dialogue mismatch candidate: {_format_dialogue_candidate_for_log(candidate)}")
        except Exception as e:
            print(f"[LuaSocket] Error checking dialogue history time sync: {e}")

        if not ahead_sources:
            return

        if len(ahead_sources) == 2:
            sources_text = "Owl Post and dialogue history"
            verb = "are"
        else:
            sources_text = ahead_sources[0]
            verb = "is"

        self.send_notification(
            f"Sonorus: {sources_text} {verb} from a later in-game time than this save. "
            f"You may have loaded an older save, so history may be out of sync."
        )
        print(
            f"[LuaSocket] Time sync mismatch: current={_format_game_minutes_for_log(current_game_minutes)}; "
            + "; ".join(mismatch_details)
        )
        print(f"[LuaSocket] Sent out-of-sync game time warning for {sources_text}")

    def send_lipsync_start(self, speaker: str = None, start_time: float = None, turn_id: str = None, visemes: list = None, scale: float = None):
        """Signal audio playback starting."""
        self.playback_active = True
        self.playback_event.clear()
        self.pipeline_active = True
        self.pipeline_event.clear()
        msg = {
            "type": "lipsync_start",
            "speaker": speaker or "",
            "start_time": start_time or time.time(),
            "turn_id": turn_id or self._last_turn_id  # Use last turn_id if not provided
        }
        if visemes is not None:
            msg["visemes"] = visemes
        if scale is not None:
            msg["scale"] = scale
        self.send(msg)

    def send_lipsync_stop(self):
        """Signal audio playback ended."""
        self.playback_active = False
        self.playback_event.set()
        self.pipeline_active = False
        self.pipeline_event.set()
        self.send({"type": "lipsync_stop"})

    def wait_for_playback_stop(self, timeout: float = 60.0) -> bool:
        """Wait for playback to stop. Returns True if stopped, False on timeout."""
        return self.playback_event.wait(timeout=timeout)

    def wait_for_pipeline_stop(self, timeout: float = 60.0) -> bool:
        """Wait for full TTS pipeline (synthesis+playback) to stop. Returns True if stopped, False on timeout."""
        return self.pipeline_event.wait(timeout=timeout)

    def send_visemes(self, frames: list):
        """Send batch of viseme frames."""
        self.send({
            "type": "visemes",
            "frames": frames
        })

    def send_queue_item(self, item: dict):
        """Push new queue item to Lua."""
        self.send({
            "type": "queue_item",
            "item": item
        })

    def send_conversation_state(self, state: str, interrupted: bool = False, end_behavior: str = None):
        """Push conversation state change to Lua."""
        payload = {
            "type": "conversation_state",
            "state": state,
            "interrupted": interrupted
        }
        if end_behavior:
            payload["end_behavior"] = end_behavior
        self.send(payload)

    def send_conversation_finished(self, speaker_ids):
        """Notify Lua that a conversation has fully ended (no follow-up pending)."""
        self.send({
            "type": "conversation_finished",
            "speakers": list(speaker_ids) if speaker_ids else []
        })

    def send_linger_goodbye_claim(self, generation: int, speaker_ids):
        """Claim a pending linger goodbye batch and keep only selected speakers frozen."""
        return self.send({
            "type": "linger_goodbye_claim",
            "generation": generation,
            "speaker_ids": list(speaker_ids) if speaker_ids else [],
        })

    def send_linger_goodbye_abort(self, generation: int, reason: str = "unknown"):
        """Abort a pending linger goodbye batch and release any held speakers."""
        return self.send({
            "type": "linger_goodbye_abort",
            "generation": generation,
            "reason": reason,
        })

    def send_player_message(self, player_name: str, message: str):
        """Send player message for immediate subtitle display."""
        self.send({
            "type": "player_message",
            "speaker": player_name,
            "text": message
        })

    def send_reset(self):
        """Send reset command to Lua to stop all conversations."""
        self.send({"type": "reset"})

    def send_notification(self, text: str):
        """Send in-game notification to display in HUD."""
        self.send({
            "type": "notification",
            "text": text
        })

    def send_conversation_mode(self, mode: str):
        """Send conversation mode change to Lua for visual feedback."""
        self.send({
            "type": "conversation_mode",
            "mode": mode
        })

    def send_toggle_fpv(self):
        """Send first-person view toggle to Lua."""
        self.send({"type": "toggle_fpv"})

    def send_reload_history(self):
        """Tell Lua to reload dialogue history from disk."""
        self.send({"type": "reload_history"})

    def send_activate_commitment(self, npc_id: str, activity_id: str, location_id: str, spot_label: str = None):
        """Send schedule override activation to Lua."""
        msg = {
            "type": "activate_commitment",
            "npc_id": npc_id,
            "activity_id": activity_id,
            "location_id": location_id,
        }
        if spot_label:
            msg["spot_label"] = spot_label
        self.send(msg)

    def send_deactivate_commitment(self, npc_id: str, activity_id: str):
        """Send schedule override deactivation to Lua."""
        self.send({
            "type": "deactivate_commitment",
            "npc_id": npc_id,
            "activity_id": activity_id,
        })

    def send_dismiss_companion(self):
        """Tell Lua to dismiss the current companion."""
        self.send({"type": "dismiss_companion"})

    def send_dismiss_follower(self, voice_name: str):
        """Tell Lua to remove an NPC follower."""
        self.send({"type": "dismiss_follower", "voice_name": voice_name})

    def _receive_loop(self):
        """Receive messages from Lua client using length-prefixed framing."""
        # Capture the client at thread start - exit if client changes
        with self.lock:
            my_client = self.client
        if not my_client:
            return

        buffer = b""  # Bytes buffer for length-prefixed protocol
        try:
            while self.running:
                # Exit if we're no longer the active client
                with self.lock:
                    if self.client is not my_client:
                        print("[Socket] Newer client connected - exiting old receive thread")
                        return  # Don't close - we're no longer the owner
                try:
                    data = my_client.recv(4096)
                    if not data:
                        print("[Socket] Client disconnected")
                        break
                    buffer += data

                    # Process complete frames: [4-byte big-endian length][message]
                    while len(buffer) >= 4:
                        # Read length prefix
                        msg_len = (buffer[0] << 24) | (buffer[1] << 16) | (buffer[2] << 8) | buffer[3]

                        # Sanity check - messages shouldn't be > 1MB
                        if msg_len > 1_000_000:
                            print(f"[Socket] Invalid message length: {msg_len} - resetting buffer")
                            hex_dump = ' '.join(f'{b:02x}' for b in buffer[:20])
                            print(f"[Socket] Buffer hex: {hex_dump}")
                            buffer = b""
                            break

                        # Wait for complete message
                        if len(buffer) < 4 + msg_len:
                            break  # Need more data

                        # Extract message
                        msg_bytes = buffer[4:4 + msg_len]
                        buffer = buffer[4 + msg_len:]

                        # Parse JSON
                        try:
                            msg_str = msg_bytes.decode('utf-8')
                            msg = json.loads(msg_str)
                            self._handle_message(msg)
                        except (json.JSONDecodeError, UnicodeDecodeError) as e:
                            hex_dump = ' '.join(f'{b:02x}' for b in msg_bytes[:50])
                            print(f"[Socket] Invalid message (len={msg_len}): {e}")
                            print(f"[Socket] Hex dump: {hex_dump}")

                except sock_lib.timeout:
                    continue
                except Exception as e:
                    if self.running:
                        print(f"[Socket] Receive error: {e}")
                    break
        finally:
            # ALWAYS close the socket when thread exits (unless superseded by new client)
            with self.lock:
                if self.client is my_client:
                    # We're still the active client - close and clear
                    try:
                        my_client.close()
                    except:
                        pass
                    self.client = None
                    print("[Socket] Receive thread exiting - socket closed")
                else:
                    # New client connected, don't close my_client (already replaced)
                    print("[Socket] Receive thread exiting - superseded by new connection")

    def _handle_message(self, msg):
        """Handle incoming message from Lua."""
        if not isinstance(msg, dict):
            print(f"[Socket] Ignoring non-dict message: {type(msg).__name__}")
            return
        msg_type = msg.get("type")
        if msg_type == "player_handshake":
            player_name = msg.get("data", {}).get("playerName", "")
            if player_name:
                from . import player_context
                print(f"[LuaSocket] Player handshake: '{player_name}'")
                try:
                    player_context.switch(player_name)
                except Exception as e:
                    print(f"[LuaSocket] Player switch failed: {e}")
                self._pending_time_sync_check = True
                self.send({"type": "player_ready"})
                self._send_deferred_connect_data()
            else:
                print("[LuaSocket] Empty player_handshake, ignoring")
                self._pending_time_sync_check = False
                self.send({"type": "player_ready"})
            return
        if msg_type == "game_context":
            merged_context = None
            with self._context_lock:
                data = msg.get("data", {})
                if not isinstance(data, dict):
                    print(f"[Socket] Ignoring non-dict game_context data: {type(data).__name__}")
                    return
                # Lua frequently sends partial context payloads (e.g. state-only or selective group refreshes).
                # Merge into the cached snapshot rather than replacing it wholesale, or unrelated fields like
                # companion state disappear and downstream gates make incorrect decisions.
                merged = self._game_context.copy()
                merged.update(data)
                self._game_context = merged
                merged_context = merged.copy()
                # filter nearby npcs
                nearby_npcs = self._game_context.get("nearbyNpcs", [])
                if nearby_npcs:
                    nearby_npcs = [npc for npc in nearby_npcs if is_significant_npc(npc.get("id", "") or npc.get("name", ""))]
                    self._game_context["nearbyNpcs"] = nearby_npcs
                    merged_context["nearbyNpcs"] = nearby_npcs
            # Signal any pending context refresh
            if self._context_refresh_pending:
                self._context_refresh_pending = False
                self._context_refresh_event.set()
            self._maybe_warn_out_of_sync_game_time(merged_context or {})
            # Extract live mod data and pass to mods module
            mods_data = self._game_context.get("mods", {})
            if mods_data:
                try:
                    from . import mods
                    # House Points - extract live point values
                    hp_data = mods_data.get("housePoints", {})
                    if hp_data.get("points"):
                        mods.update_live_data("house_points", {"points": hp_data["points"]})
                        print(f"[Socket] House points data updated: {list(hp_data['points'].keys())}")
                except Exception as e:
                    print(f"[Socket] Error updating mod live data: {e}")
            print(f"[Socket] Game context received: {len(self._game_context)} fields")
            # NPC Schedule: check for period transitions and notify player
            try:
                from . import mods
                from .settings import load_settings as _load_settings
                from .game_context import check_schedule_transition
                _ns_settings = _load_settings().get('game_mods', {}).get('npc_schedule', {})
                if _ns_settings.get('notifications_enabled', True) and mods.is_mod_installed('npc_schedule'):
                    note = check_schedule_transition(self._game_context)
                    if note:
                        self.send_notification(note)
            except Exception as e:
                print(f"[Socket] Schedule notification error: {e}")
            # Check commitment timers (throttled internally to 5s)
            try:
                from .commitments import check_commitment_timers
                check_commitment_timers(self._game_context, self)
            except Exception as e:
                print(f"[Socket] Commitment timer error: {e}")
        elif msg_type == "pause_state":
            # Immediate pause state update (more responsive than full context)
            paused = msg.get("paused", False)
            with self._context_lock:
                self._game_context["isGamePaused"] = paused
            print(f"[Socket] Pause state updated: {paused}")
        elif msg_type == "force_close_chat":
            # Lua is telling us to close chat (e.g., game paused while typing)
            reason = msg.get("reason", "unknown")
            if self._input_capture:
                capture = self._input_capture.get_capture()
                if capture:
                    capture.force_close(reason)
        elif msg_type == "reset":
            # Lua signaled reset (F8 key or response to our reset)
            # DON'T call reset() here - it clears the interrupted flag
            # The processing code will handle cleanup when it sees the flag
            self.send_conversation_state("idle")
            print("[Socket] Reset received from Lua")
        elif msg_type == "shutdown":
            # Lua requested server shutdown
            print("[Socket] Shutdown requested from Lua")
            # Cleanup audio if available
            try:
                from audio import shutdown as audio_shutdown
                audio_shutdown()
            except:
                pass
            try:
                from .memory_queue import graceful_shutdown
                memory_shutdown_ok = graceful_shutdown(max_wait=30.0)
            except Exception as e:
                memory_shutdown_ok = False
                print(f"[Socket] Memory shutdown error: {e}")

            if not memory_shutdown_ok:
                print("[Socket] Memory shutdown incomplete - forcing exit as last resort")
            # Exit the process
            os._exit(0)
        elif msg_type == "speaker_ready":
            # Lua has cached the speaker actor (or failed to find it)
            speaker_id = msg.get("speaker_id", "")
            found = msg.get("found", False)
            with self._speaker_ready_lock:
                self._speaker_ready_result = {"speaker_id": speaker_id, "found": found}
            self._speaker_ready_event.set()
            print(f"[Socket] Speaker ready: {speaker_id} (found={found})")
        elif msg_type == "turn_ready":
            # Lua has processed play_turn and cached the actor
            turn_id = msg.get("turn_id", "")
            actor_found = msg.get("actor_found", False)
            has_positions = msg.get("has_positions", False)
            is_player_speaker = msg.get("is_player_speaker", False)
            first_person_active = msg.get("first_person_active", False)

            # Extract initial positions (for first speaker 3D audio)
            initial_positions = {
                "camX": msg.get("camX", 0),
                "camY": msg.get("camY", 0),
                "camZ": msg.get("camZ", 0),
                "camYaw": msg.get("camYaw", 0),
                "camPitch": msg.get("camPitch", 0),
                "npcX": msg.get("npcX", 0),
                "npcY": msg.get("npcY", 0),
                "npcZ": msg.get("npcZ", 0),
            }

            # NOTE: We do NOT update _positions here anymore! That was causing audio
            # to briefly jump to the NEXT speaker's position while the current speaker
            # is still playing (because turn_ready comes in during pre-buffering).
            # Instead, positions are passed through send_play_turn() return value and
            # set via set_initial_positions() when the turn actually starts playing.
            # The continuous "positions" messages from Lua handle updates during playback.
            if has_positions:
                source_type = "PLAYER" if is_player_speaker else "NPC"
                print(f"[Socket] Turn ready ({source_type}): {turn_id} (actor_found={actor_found}) "
                      f"source_pos=({initial_positions['npcX']:.0f},{initial_positions['npcY']:.0f},{initial_positions['npcZ']:.0f})")
            else:
                print(f"[Socket] Turn ready: {turn_id} (actor_found={actor_found}) NO POSITIONS")

            # Extract reverb info for audio effects
            reverb_auxbus = msg.get("reverb_auxbus")
            reverb_send = msg.get("reverb_send", 1.0)
            if reverb_auxbus:
                # Update cached reverb
                with self._context_lock:
                    self._current_reverb = {"auxbus": reverb_auxbus, "send": reverb_send}
                print(f"[Socket] Reverb: {reverb_auxbus} (send={reverb_send:.2f})")

            with self._turn_ready_lock:
                self._turn_ready_result = {
                    "turn_id": turn_id,
                    "actor_found": actor_found,
                    "has_positions": has_positions,
                    "is_player_speaker": is_player_speaker,
                    "first_person_active": first_person_active,
                    "positions": initial_positions if has_positions else {},
                    "reverb_auxbus": reverb_auxbus,
                    "reverb_send": reverb_send
                }
            self._turn_ready_event.set()
        elif msg_type == "lipsync_ready":
            # Lua acknowledges lipsync_start - ready to start audio playback
            turn_id = msg.get("turn_id", "")
            print(f"[Socket] Lipsync ready: {turn_id}")
            # Notify the coordinator
            from audio.playback import get_coordinator
            coordinator = get_coordinator()
            if coordinator:
                coordinator.on_lipsync_ready(turn_id)
        elif msg_type == "positions":
            # Real-time position updates from Lua (camera + NPC) for 3D audio
            with self._context_lock:
                self._positions = {
                    "camX": msg.get("camX", 0),
                    "camY": msg.get("camY", 0),
                    "camZ": msg.get("camZ", 0),
                    "camYaw": msg.get("camYaw", 0),
                    "camPitch": msg.get("camPitch", 0),
                    "npcX": msg.get("npcX", 0),
                    "npcY": msg.get("npcY", 0),
                    "npcZ": msg.get("npcZ", 0),
                }
        elif msg_type == "reverb_update":
            # Lua signals reverb change (location transition)
            auxbus = msg.get("auxBus")
            send = msg.get("sendLevel", 1.0)
            zone = msg.get("zone", "")
            with self._context_lock:
                self._current_reverb = {"auxbus": auxbus, "send": send}
            print(f"[Socket] Reverb update: {auxbus} (zone={zone}, send={send:.2f})")
            # Notify audio player to update reverb if playing
            if self._reverb_callback:
                try:
                    self._reverb_callback(auxbus, send)
                except Exception as e:
                    print(f"[Socket] Reverb callback error: {e}")

        elif msg_type == "turn_complete":
            # Lua signals that mouth animation for current turn is fully closed
            print("[Socket] Turn complete - mouth closed")
            self._turn_complete_event.set()

        elif msg_type == "interrupt_conversation":
            # Lua requests immediate conversation stop (e.g., cinematic started)
            reason = msg.get("reason", "unknown")
            print(f"[Socket] Interrupt requested: {reason}")
            if self._interrupt_callback:
                try:
                    self._interrupt_callback(source=reason, notify=False)
                except Exception as e:
                    print(f"[Socket] Interrupt callback error: {e}")

        elif msg_type == "game_event":
            event_name = msg.get("event")
            data = msg.get("data", {})
            if not isinstance(event_name, str) or not event_name:
                print(f"[Socket] Ignoring malformed game_event name: {event_name!r}")
                return
            if isinstance(data, list) and len(data) == 0:
                # Lua/JSON encodes empty tables as arrays, but event payloads expect an object.
                data = {}
            if not isinstance(data, dict):
                print(f"[Socket] Ignoring malformed game_event payload for {event_name}: {type(data).__name__}")
                data = {}
            if self._game_event_callback:
                try:
                    self._game_event_callback({
                        "event": event_name,
                        "data": data
                    })
                except Exception as e:
                    print(f"[Socket] Game event callback error ({event_name}): {e}")

        elif msg_type == "record_dialogue":
            # Lua sends dialogue entries for Python to persist
            entry = msg.get("entry")
            if entry and isinstance(entry, dict):
                self._record_dialogue_entry(entry)
            elif entry:
                print(f"[Socket] WARNING: record_dialogue received non-dict entry: {type(entry).__name__} = {repr(entry)[:100]}")

        elif msg_type == "house_points_data":
            # Lua sends updated house points data (after refresh triggers)
            points = msg.get("points", {})
            if points:
                try:
                    from . import mods
                    mods.update_live_data("house_points", {"points": points})
                    # Log actual season values for debugging
                    season_vals = {h: d.get('season', 0) for h, d in points.items()}
                    print(f"[Socket] House points updated: {season_vals}")
                except Exception as e:
                    print(f"[Socket] Error updating house points: {e}")

        elif msg_type == "house_points_refreshed":
            # Lua acknowledges on-demand refresh request
            has_data = msg.get("has_data", False)
            with self._house_points_lock:
                self._house_points_result = has_data
            self._house_points_event.set()

        elif msg_type == "commitment_status":
            # ACK from Lua for commitment override apply/release
            npc_id = msg.get("npc_id", "")
            action = msg.get("action", "")
            success = msg.get("success", False)
            error = msg.get("error")
            if success:
                print(f"[Socket] Commitment {action} OK: {npc_id}")
            else:
                print(f"[Socket] Commitment {action} FAILED: {npc_id} - {error}")

        elif msg_type == "register_commitment_spot":
            display_name = msg.get("location", "")
            x = msg.get("x", 0)
            y = msg.get("y", 0)
            z = msg.get("z", 0)
            yaw = msg.get("yaw", 0)
            if display_name:
                # Reverse-lookup: display name → internal location ID
                # (display names are localized and change with language; IDs are stable)
                from .settings import load_commitment_spots, save_commitment_spots, SONORUS_DIR
                location = self._resolve_location_id(display_name)
                spots = load_commitment_spots()
                if location not in spots:
                    spots[location] = []
                spots[location].append({"x": round(x, 2), "y": round(y, 2), "z": round(z, 2), "yaw": round(yaw, 2)})
                save_commitment_spots(spots)
                print(f"[Socket] Registered commitment spot at {location} (from '{display_name}') ({x:.0f}, {y:.0f}, {z:.0f} yaw={yaw:.0f}) — {len(spots[location])} spots total")
                # Reload Python location matching to include new spot locations
                try:
                    from .commitments import reload_teleport_locations
                    reload_teleport_locations()
                except Exception as e:
                    print(f"[Socket] Could not reload teleport locations: {e}")
            else:
                print("[Socket] register_commitment_spot: missing location")

    def wait_for_turn_complete(self, timeout: float = 2.0) -> bool:
        """Wait for previous turn's mouth animation to complete.
        Returns True if complete, False on timeout."""
        if self._turn_complete_event.wait(timeout=timeout):
            return True
        print(f"[Socket] Turn complete timeout after {timeout}s")
        return False

    def _record_dialogue_entry(self, entry):
        """Append a dialogue entry from Lua to the database.

        This is the sole writer for dialogue history - Lua sends entries
        here instead of writing directly to avoid race conditions.
        Uses SQLite for atomic writes and proper concurrency.
        """
        try:
            from .dialogue_db import append_entry, get_last_entry

            # Dedup location entries - skip if last entry is same location
            # This handles the case where Lua's _G.LastRecordedLocation resets on reload
            if entry.get("type") == "location":
                last_entry = get_last_entry()
                if (last_entry and
                    last_entry.get("type") == "location" and
                    last_entry.get("location") == entry.get("location")):
                    print(f"[Socket] Skipping duplicate location entry: {entry.get('location')}")
                    return

            # Append new entry (atomic SQLite insert)
            append_entry(entry)

            # Update ambient blocklist if this is a new chatter line from a significant NPC
            if (entry.get("type") == "chatter"
                    and not entry.get("isPlayer")
                    and not entry.get("isAIResponse")):
                vn = entry.get("voiceName", "")
                line_id = entry.get("lineID", "")
                if vn and line_id and is_significant_npc(vn):
                    cached = getattr(self, '_ambient_blocklist_cache', None)
                    if cached is None or cached == {}:
                        pass
                    else:
                        parts = line_id.rsplit('_', 1)
                        if len(parts) == 2 and parts[1].isdigit():
                            num = int(parts[1])
                            if vn not in cached or num not in cached.get(vn, []):
                                self.send_ambient_blocklist()
        except Exception as e:
            print(f"[Socket] Error recording dialogue entry: {e}")

    def mark_turn_started(self):
        """Mark that a new turn is starting (clear complete event)."""
        self._turn_complete_event.clear()

    def get_positions(self):
        """Get cached positions (thread-safe)."""
        with self._context_lock:
            return self._positions.copy()

    def get_current_reverb(self):
        """Get cached reverb info (thread-safe)."""
        with self._context_lock:
            return self._current_reverb.copy()

    def set_reverb_callback(self, callback):
        """Set callback for live reverb updates during playback.
        Callback signature: callback(auxbus: str, send: float)"""
        self._reverb_callback = callback

    def get_game_context(self):
        """Get cached game context (thread-safe)."""
        with self._context_lock:
            return self._game_context.copy()

    def request_context_refresh(self, groups: list = None, timeout: float = 2.0, params: dict = None) -> dict:
        """
        Request game context from Lua with optional group filtering.

        Serialized with a lock to prevent concurrent requests from receiving
        each other's responses (single shared event/pending flag).

        Args:
            groups: List of context groups to request (e.g., ["state", "npcs"]).
                   If None or empty, requests all context (backwards compatible).
                   Valid groups: position, state, time, player, gear, npcs, zone, mission, companion, mods, nearby_lean
            timeout: How long to wait for Lua response
            params: Optional dict of per-group parameters (e.g., {"nearby_lean_distance": 10000})

        Returns:
            Context dict (may be partial if groups specified), or empty dict on timeout
        """
        with self._context_refresh_lock:
            # Clear event and mark pending
            self._context_refresh_event.clear()
            self._context_refresh_pending = True

            # Build request message
            msg = {"type": "request_context"}
            if groups:
                msg["groups"] = groups
            if params:
                msg["params"] = params

            # Send request to Lua
            success = self.send(msg)
            if not success:
                print("[Socket] Failed to send context refresh request")
                self._context_refresh_pending = False
                return self.get_game_context()  # Return cached on failure

            # Wait for game_context message
            if self._context_refresh_event.wait(timeout=timeout):
                groups_str = ", ".join(groups) if groups else "all"
                print(f"[Socket] Fresh context received ({groups_str})")
                return self.get_game_context()
            else:
                print(f"[Socket] Context refresh timeout after {timeout}s - using cached context")
                self._context_refresh_pending = False
                return self.get_game_context()

    def request_state_only(self, timeout: float = 0.2) -> dict:
        """
        Quick state-only context refresh for input capture checks.
        Returns just combat/cinematic/pause state fields.
        """
        return self.request_context_refresh(groups=["state"], timeout=timeout)

    def refresh_house_points(self, timeout: float = 1.0) -> bool:
        """
        Request on-demand house points refresh from Lua.
        Used before professor conversations to get fresh standings.

        Returns:
            True if Lua found house points data, False otherwise
        """
        # Clear event
        self._house_points_event.clear()

        # Send request to Lua
        success = self.send({"type": "refresh_house_points"})
        if not success:
            print("[Socket] Failed to send house points refresh request")
            return False

        # Wait for house_points_refreshed response
        if self._house_points_event.wait(timeout=timeout):
            with self._house_points_lock:
                result = self._house_points_result
            print(f"[Socket] House points refresh complete (has_data={result})")
            return result
        else:
            print(f"[Socket] House points refresh timeout after {timeout}s")
            return False

    def get_connection_id(self):
        """Get current connection ID (increments on each new client connection)."""
        with self.lock:
            return self._connection_id

    def prepare_speaker(self, speaker_id: str, speaker_name: str = None, timeout: float = 3.0) -> bool:
        """
        Send prepare_speaker message to Lua and wait for speaker_ready response.

        This allows Lua to cache the speaker actor BEFORE TTS starts, ensuring
        WritePositions() and lip sync will work correctly.

        Args:
            speaker_id: Internal ID like "NellieOggspire"
            speaker_name: Display name like "Nellie Oggspire" (optional)
            timeout: How long to wait for Lua response

        Returns:
            True if speaker was found and cached, False otherwise
        """
        # Clear any previous result
        self._speaker_ready_event.clear()
        with self._speaker_ready_lock:
            self._speaker_ready_result = {"found": False}

        # Send prepare message to Lua
        success = self.send({
            "type": "prepare_speaker",
            "speaker_id": speaker_id,
            "speaker_name": speaker_name or speaker_id
        })

        if not success:
            print(f"[Socket] Failed to send prepare_speaker for {speaker_id}")
            return False

        # Wait for Lua to respond
        print(f"[Socket] Waiting for speaker_ready ({speaker_id})...")
        if not self._speaker_ready_event.wait(timeout=timeout):
            print(f"[Socket] Speaker ready timeout for {speaker_id} - proceeding anyway")
            return False

        with self._speaker_ready_lock:
            result = self._speaker_ready_result
            found = result.get("found", False)

        if found:
            print(f"[Socket] Speaker actor cached: {speaker_id}")
        else:
            print(f"[Socket] Speaker actor NOT found: {speaker_id} - 3D audio/lipsync may fail")

        return found

    def send_lock_npc(self, speaker_id: str, target_id: str = "player") -> bool:
        """
        Lock an NPC in place early, before generating response.

        Call this immediately after deciding who will speak, so the NPC doesn't
        walk away during the LLM response generation + TTS preparation.

        Args:
            speaker_id: Internal ID of NPC to lock (e.g., "AbrahamRonen")
            target_id: Who they should face ("player" or another NPC ID)

        Returns:
            True if lock message sent successfully
        """
        message = {
            "type": "lock_npc",
            "speaker_id": speaker_id,
            "target_id": target_id or "player"
        }
        success = self.send(message)
        if success:
            print(f"[Socket] Sent lock_npc: {speaker_id} -> {target_id}")
        else:
            print(f"[Socket] Failed to send lock_npc for {speaker_id}")
        return success

    def send_play_turn(self, speaker_id: str, display_name: str, text: str,
                       turn_index: int = 1, target_id: str = None,
                       action: str = "None", house_point_actions: list = None,
                       streaming_subtitles: bool = False,
                       timeout: float = 10.0) -> dict:
        """
        Send atomic play_turn message to Lua and wait for turn_ready response.

        This combines the old prepare_speaker + queue_item into a single atomic
        message that Lua processes entirely on the game thread, eliminating race conditions.

        Args:
            speaker_id: Internal ID like "NellieOggspire"
            display_name: Display name like "Nellie Oggspire"
            text: The dialogue text to display
            turn_index: Which turn in the conversation (1-indexed)
            target_id: Who the speaker is addressing ("player" or NPC internal ID)
            action: NPC action to execute ("JoinAsCompanion", "LeaveCompanion", or "None")
            house_point_actions: List of house point actions [{action, house, amount}, ...]
            timeout: How long to wait for Lua response

        Returns:
            dict with {"turn_id": str, "actor_found": bool, "success": bool}
        """
        # Generate unique turn ID
        self._turn_counter += 1
        turn_id = f"turn_{self._turn_counter:04d}"

        # Clear any previous result
        self._turn_ready_event.clear()
        with self._turn_ready_lock:
            self._turn_ready_result = {"turn_id": "", "actor_found": False}

        # Build message
        message = {
            "type": "play_turn",
            "turn_id": turn_id,
            "speaker_id": speaker_id,
            "display_name": display_name,
            "text": text,
            "turn_index": turn_index,
            "target_id": target_id or "player",  # Default to player if not specified
            "action": action,
            "streaming_subtitles": streaming_subtitles
        }

        # Add house point actions if any
        if house_point_actions:
            message["house_point_actions"] = house_point_actions

        # Send play_turn message
        success = self.send(message)

        if not success:
            print(f"[Socket] Failed to send play_turn for {speaker_id}")
            return {"turn_id": turn_id, "actor_found": False, "success": False}

        # Wait for Lua to respond
        print(f"[Socket] Waiting for turn_ready ({turn_id}: {speaker_id})...")
        if not self._turn_ready_event.wait(timeout=timeout):
            print(f"[Socket] Turn ready timeout for {turn_id} - proceeding anyway")
            return {"turn_id": turn_id, "actor_found": False, "success": False}

        with self._turn_ready_lock:
            result = self._turn_ready_result.copy()

        actor_found = result.get("actor_found", False)
        if actor_found:
            print(f"[Socket] Turn ready with actor: {turn_id} ({speaker_id})")
        else:
            print(f"[Socket] Turn ready WITHOUT actor: {turn_id} ({speaker_id}) - 3D audio may fail")

        # Store for lipsync_start to use
        self._last_turn_id = turn_id
        return {
            "turn_id": turn_id,
            "actor_found": actor_found,
            "success": True,
            "positions": result.get("positions", {}),  # Pass positions through!
            "reverb_auxbus": result.get("reverb_auxbus"),
            "reverb_send": result.get("reverb_send", 1.0),
            "first_person_active": result.get("first_person_active", False),
        }

    def send_player_turn_start(self, player_name: str, text: str, timeout: float = 1.0) -> dict:
        """
        Lightweight player turn setup for lip sync (skips heavy NPC scanning).

        This is a fast path for player speech that skips the expensive GetNearbyNPCs()
        scan since we already know the speaker is the player. Used when player TTS
        is buffered early and we just need to set up lip sync.

        Args:
            player_name: Display name like "Kevin"
            text: The dialogue text
            timeout: How long to wait for Lua response (should be <10ms)

        Returns:
            dict with {"turn_id": str, "actor_found": bool, "success": bool}
        """
        # Generate unique turn ID
        self._turn_counter += 1
        turn_id = f"turn_{self._turn_counter:04d}"

        # Clear any previous result
        self._turn_ready_event.clear()
        with self._turn_ready_lock:
            self._turn_ready_result = {"turn_id": "", "actor_found": False}

        # Send lightweight player_turn_start message
        success = self.send({
            "type": "player_turn_start",
            "turn_id": turn_id,
            "player_name": player_name,
            "text": text
        })

        if not success:
            print(f"[Socket] Failed to send player_turn_start")
            return {"turn_id": turn_id, "actor_found": False, "success": False}

        # Wait for Lua to respond (should be very fast - no NPC scanning)
        print(f"[Socket] Waiting for turn_ready ({turn_id}: PLAYER)...")
        if not self._turn_ready_event.wait(timeout=timeout):
            print(f"[Socket] Player turn ready timeout for {turn_id} - proceeding anyway")
            return {"turn_id": turn_id, "actor_found": False, "success": False}

        with self._turn_ready_lock:
            result = self._turn_ready_result.copy()

        actor_found = result.get("actor_found", False)
        if actor_found:
            print(f"[Socket] Player turn ready: {turn_id}")
        else:
            print(f"[Socket] Player turn ready WITHOUT actor: {turn_id}")

        # Store for lipsync_start to use
        self._last_turn_id = turn_id
        return {
            "turn_id": turn_id,
            "actor_found": actor_found,
            "success": True,
            "positions": result.get("positions", {}),
            "reverb_auxbus": result.get("reverb_auxbus"),
            "reverb_send": result.get("reverb_send", 1.0),
            "first_person_active": result.get("first_person_active", False),
        }

    def stop(self):
        """Shutdown server."""
        self.running = False
        with self.lock:
            if self.client:
                self.client.close()
        if self.server:
            self.server.close()
