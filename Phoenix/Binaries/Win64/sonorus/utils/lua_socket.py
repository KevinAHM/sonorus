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
        # Position data from Lua (camera + NPC positions for 3D audio)
        self._positions = {
            "camX": 0, "camY": 0, "camZ": 0,
            "camYaw": 0, "camPitch": 0,
            "npcX": 0, "npcY": 0, "npcZ": 0
        }
        # Callbacks for external modules
        self._input_capture = None  # Will be set by server.py
        self._conv_state = None  # Will be set by server.py

    def set_input_capture(self, input_capture_module):
        """Set the input_capture module for force_close handling."""
        self._input_capture = input_capture_module

    def set_conv_state(self, conv_state):
        """Set the conversation state for reset handling."""
        self._conv_state = conv_state

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

    def send_lipsync_start(self, speaker: str = None, start_time: float = None, turn_id: str = None, visemes: list = None, scale: float = None):
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

    def send_reload_history(self):
        """Tell Lua to reload dialogue history from disk."""
        self.send({"type": "reload_history"})

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
            print(f"[Socket] Game context received: {len(self._game_context)} fields")
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
            # Lua signaled reset (F8 key)
            if self._conv_state:
                self._conv_state.reset()
            self.send_conversation_state("idle")
            print("[Socket] Reset received from Lua - conversation state reset")
        elif msg_type == "shutdown":
            # Lua requested server shutdown
            print("[Socket] Shutdown requested from Lua")
            # Cleanup audio if available
            try:
                from sonorus.audio3d import shutdown as audio_shutdown
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

            # Store initial positions so get_positions() returns them immediately
            # This ensures first speaker has valid 3D position before audio starts
            # For player speaker, we skip 3D positioning (audio plays centered)
            if has_positions and not is_player_speaker:
                with self._context_lock:
                    self._positions = initial_positions.copy()
                print(f"[Socket] Turn ready: {turn_id} (actor_found={actor_found}) "
                      f"npc_pos=({initial_positions['npcX']:.0f},{initial_positions['npcY']:.0f},{initial_positions['npcZ']:.0f})")
            elif is_player_speaker:
                print(f"[Socket] Turn ready (PLAYER): {turn_id} - skipping 3D positioning")
            else:
                print(f"[Socket] Turn ready: {turn_id} (actor_found={actor_found}) NO POSITIONS")

            with self._turn_ready_lock:
                self._turn_ready_result = {
                    "turn_id": turn_id,
                    "actor_found": actor_found,
                    "has_positions": has_positions,
                    "is_player_speaker": is_player_speaker,
                    "positions": initial_positions if not is_player_speaker else {}
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
        elif msg_type == "turn_complete":
            # Lua signals that mouth animation for current turn is fully closed
            print("[Socket] Turn complete - mouth closed")
            self._turn_complete_event.set()

    def wait_for_turn_complete(self, timeout: float = 2.0) -> bool:
        """Wait for previous turn's mouth animation to complete.
        Returns True if complete, False on timeout."""
        if self._turn_complete_event.wait(timeout=timeout):
            return True
        print(f"[Socket] Turn complete timeout after {timeout}s")
        return False

    def mark_turn_started(self):
        """Mark that a new turn is starting (clear complete event)."""
        self._turn_complete_event.clear()

    def get_positions(self):
        """Get cached positions (thread-safe)."""
        with self._context_lock:
            return self._positions.copy()

    def get_game_context(self):
        """Get cached game context (thread-safe)."""
        with self._context_lock:
            return self._game_context.copy()

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

    def send_play_turn(self, speaker_id: str, display_name: str, text: str,
                       turn_index: int = 1, target_id: str = None,
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

        # Send play_turn message
        success = self.send({
            "type": "play_turn",
            "turn_id": turn_id,
            "speaker_id": speaker_id,
            "display_name": display_name,
            "text": text,
            "turn_index": turn_index,
            "target_id": target_id or "player"  # Default to player if not specified
        })

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
            "positions": result.get("positions", {})  # Pass positions through!
        }

    def stop(self):
        """Shutdown server."""
        self.running = False
        with self.lock:
            if self.client:
                self.client.close()
        if self.server:
            self.server.close()
