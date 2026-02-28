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

from .text_utils import is_significant_npc

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

    def set_input_capture(self, input_capture_module):
        """Set the input_capture module for force_close handling."""
        self._input_capture = input_capture_module

    def set_conv_state(self, conv_state):
        """Set the conversation state for reset handling."""
        self._conv_state = conv_state

    def set_interrupt_callback(self, callback):
        """Set the callback for conversation interrupts (e.g., cinematic start)."""
        self._interrupt_callback = callback

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
                self.send_significant_npcs()
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
                # Companion callout blocking: 0 = disabled, -1 = never repeat, >0 = N game minutes
                # Default: 1440 (24 hours = 1 game day)
                "companion_callout_block_minutes": conversation.get('companion_callout_block_minutes', 1440),
                "dev_mode": is_dev_mode(),
                # Game language for localization file loading
                "language": setup.get('language', 'EN_US'),
                # Preview lock: lock NPC while typing/speaking (before sending message)
                "preview_lock": input_settings.get('preview_lock', True),
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
            })
        except Exception as e:
            print(f"[Socket] Error sending tracking settings: {e}")

    def send_significant_npcs(self):
        """Send list of significant NPC names to Lua for client-side filtering.

        Sends both voice names (internal IDs) and generated display names,
        since Lua only has access to display names from GetActorDisplayName().
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

    def send_lipsync_start(self, speaker: str = None, start_time: float = None, turn_id: str = None, visemes: list = None, scale: float = None, fallback: bool = False):
        """Signal audio playback starting."""
        self.playback_active = True
        self.playback_event.clear()
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
        msg["fallback"] = fallback
        self.send(msg)

    def send_lipsync_stop(self):
        """Signal audio playback ended."""
        self.playback_active = False
        self.playback_event.set()
        self.send({"type": "lipsync_stop"})

    def wait_for_playback_stop(self, timeout: float = 60.0) -> bool:
        """Wait for playback to stop. Returns True if stopped, False on timeout."""
        return self.playback_event.wait(timeout=timeout)

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

    def send_conversation_state(self, state: str, interrupted: bool = False):
        """Push conversation state change to Lua."""
        self.send({
            "type": "conversation_state",
            "state": state,
            "interrupted": interrupted
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

    def send_reload_history(self):
        """Tell Lua to reload dialogue history from disk."""
        self.send({"type": "reload_history"})

    def send_activate_commitment(self, npc_id: str, activity_id: str, location_id: str):
        """Send schedule override activation to Lua."""
        self.send({
            "type": "activate_commitment",
            "npc_id": npc_id,
            "activity_id": activity_id,
            "location_id": location_id,
        })

    def send_deactivate_commitment(self, npc_id: str, activity_id: str):
        """Send schedule override deactivation to Lua."""
        self.send({
            "type": "deactivate_commitment",
            "npc_id": npc_id,
            "activity_id": activity_id,
        })

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
        msg_type = msg.get("type")
        if msg_type == "game_context":
            with self._context_lock:
                self._game_context = msg.get("data", {})
                # filter nearby npcs
                nearby_npcs = self._game_context.get("nearbyNpcs", [])
                if nearby_npcs:
                    nearby_npcs = [npc for npc in nearby_npcs if is_significant_npc(npc.get("id", "") or npc.get("name", ""))]
                    self._game_context["nearbyNpcs"] = nearby_npcs
            # Signal any pending context refresh
            if self._context_refresh_pending:
                self._context_refresh_pending = False
                self._context_refresh_event.set()
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

    def request_context_refresh(self, groups: list = None, timeout: float = 2.0) -> dict:
        """
        Request game context from Lua with optional group filtering.

        Serialized with a lock to prevent concurrent requests from receiving
        each other's responses (single shared event/pending flag).

        Args:
            groups: List of context groups to request (e.g., ["state", "npcs"]).
                   If None or empty, requests all context (backwards compatible).
                   Valid groups: position, state, time, player, gear, npcs, zone, mission, companion, mods
            timeout: How long to wait for Lua response

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
            "reverb_send": result.get("reverb_send", 1.0)
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
            "reverb_send": result.get("reverb_send", 1.0)
        }

    def stop(self):
        """Shutdown server."""
        self.running = False
        with self.lock:
            if self.client:
                self.client.close()
        if self.server:
            self.server.close()
