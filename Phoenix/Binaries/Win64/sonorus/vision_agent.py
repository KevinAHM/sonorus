"""
Vision Agent - Background agent that captures screenshots and generates scene descriptions.

Runs independently on the server, triggered by:
- Player movement (distance threshold)
- Time elapsed (max interval)
With a minimum cooldown between captures.
"""

import os
import sys
import json
import time
import math
import base64
import threading
from io import BytesIO
from datetime import datetime

# Screenshot library
try:
    import mss
    from PIL import Image
    MSS_AVAILABLE = True
except ImportError:
    MSS_AVAILABLE = False
    print("[VisionAgent] Warning: mss or pillow not installed. Run: pip install mss pillow")

# Windows foreground check
try:
    import win32gui
    WIN32_AVAILABLE = True
except ImportError:
    WIN32_AVAILABLE = False
    print("[VisionAgent] Warning: pywin32 not installed. Run: pip install pywin32")

GAME_WINDOW_TITLE = "Hogwarts Legacy"


def is_game_foreground():
    """Check if Hogwarts Legacy is the foreground window"""
    if not WIN32_AVAILABLE:
        return True
    try:
        hwnd = win32gui.GetForegroundWindow()
        title = win32gui.GetWindowText(hwnd)
        return GAME_WINDOW_TITLE in title
    except:
        return True


def get_game_window_rect():
    """Get the game window rect. Returns None if game not found or not in foreground."""
    if not WIN32_AVAILABLE:
        return None
    try:
        # Find exact match only
        hwnd = win32gui.FindWindow(None, GAME_WINDOW_TITLE)
        if not hwnd:
            return None  # Game not running or wrong title

        # Must be foreground
        if win32gui.GetForegroundWindow() != hwnd:
            return None  # Game not in foreground, skip capture

        # Must not be minimized
        if win32gui.IsIconic(hwnd):
            return None

        # Get client area
        client_rect = win32gui.GetClientRect(hwnd)
        left, top = win32gui.ClientToScreen(hwnd, (0, 0))
        width = client_rect[2]
        height = client_rect[3]

        if width < 640 or height < 480:
            print(f"[VisionAgent] Window too small ({width}x{height}), skipping")
            return None

        return {"left": left, "top": top, "width": width, "height": height}

    except Exception as e:
        print(f"[VisionAgent] Window error: {e}")
    return None

import llm

# Directory paths
SONORUS_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SONORUS_DIR, "data")

# File paths
LANDMARK_FILE = os.path.join(SONORUS_DIR, "landmark_locations.json")
IDLE_STATE_FILE = os.path.join(DATA_DIR, "idle_state.json")

# Import shared constants
from constants import LANDMARK_VERTICAL_THRESHOLD, LANDMARK_MAX_DISTANCE

# Socket reference for game context (set by server.py)
_lua_socket = None

def set_lua_socket(socket):
    """Set the lua socket reference for reading game context."""
    global _lua_socket
    _lua_socket = socket

# Vision prompt template
VISION_PROMPT = """You are describing what is currently visible in this Hogwarts Legacy screenshot. Provide a grounded, immediate description focusing on the environment, visible objects, and character actions.

## Context:
- Location: {location}
- Time: {time_of_day}

{visible_npcs_section}

{nearby_landmarks_section}

## CORE INSTRUCTIONS:

**Perspective:** Third-person view. The player character model in view is a camera artifact—DO NOT mention or describe it.

**Description Priorities:**

1. **Environment & Objects** (ESSENTIAL):
   - Spatial scale and overall layout
   - Architecture, materials, textures, and light sources
   - Specific visible objects: furniture, doors, windows, decorations
   - Use fixtures to anchor locations (doorway, hearth, pillar, shelf)
   - NAME recognizable Wizarding World elements: Floo Flames, House banners, magical creatures, potion ingredients, spell effects, broomsticks, etc. Don't describe these generically.

2. **Character Actions** (only for visible NPCs):
   - ONLY describe what you can CLEARLY SEE - pose, gesture, position
   - DO NOT invent or assume actions (sitting, reading, etc.) unless unmistakably visible
   - Placement via scene fixtures only

3. **Atmosphere**:
   - Weather effects (if outdoors), lighting mood
   - Overall energy and feel

**CRITICAL - Character Identification:**
- NEVER name a character unless you are 100% certain (unique/distinctive appearance like main story characters)
- For generic students/NPCs, use descriptive labels based ONLY on visible details:
  - "A student" / "Two students" (if no house visible)
  - "A Hufflepuff student" (if house robes/colors clearly visible)
  - "A male Gryffindor student and a female Ravenclaw student"
  - "A professor in dark robes"
- The "Nearby" section lists characters/creatures NEAR the player by game data - they may not be visible in the screenshot. Do not assume a visible character is someone from this list unless you can verify their identity visually

**Output Format:**

**Scene:** [3-5 sentences. Describe what you see at the given location. Layout, specific objects with visual details, strictly factual and present tense. Include specific positional details if relevant (e.g., "near the fountain", "by the staircase").]

**Visible characters:** [For each visible NPC, 1-2 sentences on ONLY what is clearly visible - pose, clothing, position. Use generic descriptions unless identity is certain. Skip if none visible.]

**Atmosphere:** [1-2 sentences on lighting, mood, energy.]

**Style Rules:**
- Active, present tense; concrete details
- Anchor with scene fixtures, no distances or directions
- ONLY describe what is CLEARLY visible on-screen - when in doubt, leave it out

**If Unable to Describe:**
If the screenshot is a loading screen, too dark, blurry, obscured by UI/menus, shows too limited an area (e.g., staring at a wall/corner), or is otherwise impossible to describe meaningfully, respond with ONLY: `UNCLEAR: <brief reason>`"""


def get_vision_settings():
    """Get vision agent settings with defaults"""
    from utils.settings import load_settings
    settings = load_settings()
    vision = settings.get('agents', {}).get('vision', {})

    return {
        'enabled': vision.get('enabled', True),
        'cooldown_seconds': vision.get('cooldown_seconds', 5),
        'llm': vision.get('llm', {})
    }


def calculate_distance(pos1, pos2):
    """Calculate 3D distance between two positions"""
    if not pos1 or not pos2:
        return float('inf')
    try:
        dx = pos1.get('x', 0) - pos2.get('x', 0)
        dy = pos1.get('y', 0) - pos2.get('y', 0)
        dz = pos1.get('z', 0) - pos2.get('z', 0)
        return math.sqrt(dx*dx + dy*dy + dz*dz)
    except:
        return float('inf')


def format_distance_meters(distance_units):
    """Format distance in meters (UE4: ~100 units = 1 meter)"""
    meters = distance_units / 100
    if meters < 1000:
        return f"{int(meters)}m"
    return f"{meters/1000:.1f}km"


def get_cardinal_direction(from_pos, to_pos):
    """Get cardinal direction from one point to another"""
    dx = to_pos.get('x', 0) - from_pos.get('x', 0)
    dy = to_pos.get('y', 0) - from_pos.get('y', 0)

    if dx == 0 and dy == 0:
        return ""

    angle = math.degrees(math.atan2(dy, dx))
    if angle < 0:
        angle += 360

    # Map angle to cardinal (E=0, SE=45, S=90, SW=135, W=180, NW=225, N=270, NE=315)
    if angle >= 337.5 or angle < 22.5:
        return "east"
    elif 22.5 <= angle < 67.5:
        return "southeast"
    elif 67.5 <= angle < 112.5:
        return "south"
    elif 112.5 <= angle < 157.5:
        return "southwest"
    elif 157.5 <= angle < 202.5:
        return "west"
    elif 202.5 <= angle < 247.5:
        return "northwest"
    elif 247.5 <= angle < 292.5:
        return "north"
    else:
        return "northeast"


def get_nearby_landmarks(player_pos, world_name=None, count=5, exclude_names=None):
    """Get nearby landmarks for vision context

    Args:
        player_pos: Player position dict with x, y, z
        world_name: World/region name for filtering (e.g., "Hogwarts")
        count: Max number of landmarks to return
        exclude_names: List of location names to exclude (player's current location)
    """
    try:
        if not os.path.exists(LANDMARK_FILE):
            return []

        with open(LANDMARK_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)

        landmarks = data.get('landmarks', [])
        if not landmarks:
            return []

        results = []
        player_world = (world_name or '').lower()

        # Normalize exclusion list for case-insensitive matching
        exclude_lower = set()
        if exclude_names:
            for name in exclude_names:
                if name:
                    exclude_lower.add(name.lower().strip())

        for lm in landmarks:
            # Skip landmarks matching current location
            lm_name = lm.get('name', '').lower().strip()
            if lm_name in exclude_lower:
                continue
            lm_world = lm.get('world', '').lower()

            # Filter by world - only same or connected worlds
            if player_world:
                is_same = lm_world in player_world or player_world in lm_world
                is_connected = ('hogwarts' in player_world and lm_world == 'overland') or \
                              ('overland' in player_world and 'hogwarts' in lm_world) or \
                              ('hogsmeade' in player_world and lm_world == 'overland')
                if not (is_same or is_connected):
                    continue

            lm_pos = {'x': lm.get('x', 0), 'y': lm.get('y', 0), 'z': lm.get('z', 0)}
            dist = calculate_distance(player_pos, lm_pos)

            if dist == float('inf') or dist > LANDMARK_MAX_DISTANCE:
                continue

            # Direction
            direction = get_cardinal_direction(player_pos, lm_pos)

            # Vertical
            z_diff = lm_pos['z'] - player_pos.get('z', 0)
            vertical = ""
            if abs(z_diff) > LANDMARK_VERTICAL_THRESHOLD:
                vertical = "above" if z_diff > 0 else "below"

            # Combine
            if vertical and direction:
                full_dir = f"{vertical}, {direction}"
            elif vertical:
                full_dir = vertical
            else:
                full_dir = direction

            results.append({
                'name': lm.get('name', 'Unknown'),
                'distance': format_distance_meters(dist),
                'direction': full_dir,
                'raw_distance': dist
            })

        # Sort by distance
        results.sort(key=lambda x: x['raw_distance'])
        return results[:count]

    except Exception as e:
        print(f"[VisionAgent] Error loading landmarks: {e}")
        return []


class VisionAgent:
    """Background agent that captures screenshots and generates scene descriptions."""

    def __init__(self):
        self.running = False
        self.thread = None

        # State tracking - start with current time to enforce initial cooldown
        self.last_capture_time = time.time()
        self.last_context = None

        # Capture-in-progress tracking
        self._capture_in_progress = False
        self._capture_complete = threading.Event()
        self._capture_complete.set()  # Initially not capturing

        # Idle detection
        self.last_known_pos = None  # For movement tracking
        self.last_movement_time = time.time()
        self.is_idle = False

        # Activity state tracking (for Lua - foreground/idle status)
        self._last_sent_foreground = None
        self._last_sent_idle = None
        self._last_connection_id = 0  # Track socket reconnects to force state sync

        # OpenAI client (for OpenRouter)
        self.client = None

        print("[VisionAgent] Initialized")

    def start(self):
        """Start the background agent loop"""
        if self.running:
            print("[VisionAgent] Already running")
            return

        self.running = True
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        print("[VisionAgent] Started background loop")

    def stop(self):
        """Stop the background agent loop"""
        self.running = False
        if self.thread:
            self.thread.join(timeout=2)
        print("[VisionAgent] Stopped")

    def _run_loop(self):
        """Background loop - sends activity state for ambient dialog gating"""
        print("[VisionAgent] Activity state loop started")

        while self.running:
            try:
                # Send activity state to Lua (foreground + idle for ambient dialog gating)
                self._send_activity_state()

                # Read current position and update idle status
                current_pos = self._read_position()
                if current_pos:
                    self._update_idle_status(current_pos)

                # Poll every 500ms
                time.sleep(0.5)

            except Exception as e:
                print(f"[VisionAgent] Error in loop: {e}")
                time.sleep(1)

        print("[VisionAgent] Activity state loop ended")

    def capture_now(self):
        """Trigger a capture if cooldown elapsed. Called from input handlers."""
        settings = get_vision_settings()

        if not settings['enabled']:
            return

        # Skip if capture already in progress
        if self._capture_in_progress:
            print("[VisionAgent] Skipping capture - already in progress")
            return

        # Check cooldown
        now = time.time()
        if now - self.last_capture_time < settings['cooldown_seconds']:
            print(f"[VisionAgent] Skipping capture - cooldown ({settings['cooldown_seconds']}s)")
            return

        # Update cooldown immediately to prevent race conditions
        self.last_capture_time = now

        # Mark capture as in progress
        self._capture_in_progress = True
        self._capture_complete.clear()

        # Run capture in background thread to not block input
        threading.Thread(target=self._do_capture_async, daemon=True).start()

    def _do_capture_async(self):
        """Async wrapper for capture - reads position and settings internally"""
        try:
            settings = get_vision_settings()
            current_pos = self._read_position()
            if current_pos:
                self._do_capture(current_pos, settings)
        finally:
            # Always mark capture as complete
            self._capture_in_progress = False
            self._capture_complete.set()

    def wait_for_capture(self, timeout=10.0):
        """Wait for any in-progress capture to complete. Returns True if capture finished, False if timed out."""
        if not self._capture_in_progress:
            return True
        print(f"[VisionAgent] Waiting for capture to complete (timeout={timeout}s)...")
        result = self._capture_complete.wait(timeout=timeout)
        if result:
            print("[VisionAgent] Capture completed")
        else:
            print("[VisionAgent] Capture wait timed out")
        return result

    def _update_idle_status(self, current_pos):
        """Check for movement and update idle status"""
        # Get idle timeout from settings
        from utils.settings import load_settings
        all_settings = load_settings()
        idle_timeout_minutes = all_settings.get('input', {}).get('idle_timeout_minutes', 20)

        # If disabled (0), never idle
        if idle_timeout_minutes == 0:
            if self.is_idle:
                self.is_idle = False
                print("[VisionAgent] Idle detection disabled")
            self.last_known_pos = current_pos.copy()
            return

        # Check for movement against last known position
        if self.last_known_pos:
            distance = calculate_distance(current_pos, self.last_known_pos)
            if distance > 50:  # Moved more than 50 units (~0.5m)
                if self.is_idle:
                    print("[VisionAgent] Movement detected - resuming AI functions")
                self.last_movement_time = time.time()
                self.is_idle = False

        # Update last known position
        self.last_known_pos = current_pos.copy()

        # Check if idle timeout exceeded
        idle_seconds = time.time() - self.last_movement_time
        idle_timeout_seconds = idle_timeout_minutes * 60

        if not self.is_idle and idle_seconds > idle_timeout_seconds:
            self.is_idle = True
            print(f"[VisionAgent] Player idle for {idle_timeout_minutes} minutes - pausing AI functions")

    def _send_activity_state(self, force=False):
        """Send foreground/idle state to Lua if changed (for ambient dialog gating)"""
        if not _lua_socket:
            return

        # Check for socket reconnect - force sync on new connection
        conn_id = _lua_socket.get_connection_id()
        if conn_id != self._last_connection_id:
            self._last_connection_id = conn_id
            force = True  # New connection, force send current state

        foreground = is_game_foreground()
        idle = self.is_idle

        # Only send if state changed (or forced)
        if force or foreground != self._last_sent_foreground or idle != self._last_sent_idle:
            _lua_socket.send({
                "type": "activity_state",
                "foreground": foreground,
                "idle": idle
            })
            self._last_sent_foreground = foreground
            self._last_sent_idle = idle

    def _read_position(self):
        """Read player position from game_context (sent via socket from Lua)"""
        try:
            game_context = self._read_game_context()
            if not game_context:
                return None

            # Position is now included in game_context
            x = game_context.get('x')
            y = game_context.get('y')
            z = game_context.get('z')

            # If no position data yet, return None
            if x is None or y is None or z is None:
                return None

            return {
                'x': x,
                'y': y,
                'z': z,
                'timestamp': time.time(),  # Use current time since context is live
                'location': game_context.get('location', 'Unknown'),
            }
        except:
            return None

    def _read_game_context(self):
        """Read game context from socket cache"""
        if _lua_socket:
            return _lua_socket.get_game_context()
        return {}

    def _do_capture(self, current_pos, settings):
        """Capture screenshot and generate description"""
        # Check if game is in foreground
        if not is_game_foreground():
            return  # Silently skip - no need to spam logs

        # Check game context
        game_context = self._read_game_context()

        # Don't capture until player has loaded into game (skip main menu)
        if not game_context.get('playerLoaded', False):
            return  # Skip - player not in game yet

        # Check if game is paused (don't capture menu screens)
        if game_context.get('isGamePaused', False):
            return  # Skip capturing pause menus

        print(f"[VisionAgent] Capturing at position ({current_pos['x']:.0f}, {current_pos['y']:.0f}, {current_pos['z']:.0f})")

        try:
            # Capture screenshot
            screenshot_b64 = self._capture_screenshot()
            if not screenshot_b64:
                print("[VisionAgent] Screenshot capture failed")
                return

            # game_context already read above for pause check

            # Build prompt
            prompt = self._build_prompt(current_pos, game_context)

            # Call vision LLM
            description = self._call_vision_llm(screenshot_b64, prompt, settings['llm'])
            if not description:
                print("[VisionAgent] Vision LLM call failed")
                return

            # Check for unclear response - don't save if model couldn't describe scene
            if description.strip().upper().startswith("UNCLEAR"):
                print(f"[VisionAgent] Scene unclear, skipping: {description.strip()}")
                return

            # Save result
            self._save_context(description, current_pos, game_context)

            print(f"[VisionAgent] Captured and described scene successfully")

        except Exception as e:
            print(f"[VisionAgent] Capture error: {e}")

    def _capture_screenshot(self):
        """Capture screenshot and return as base64"""
        if not MSS_AVAILABLE:
            print("[VisionAgent] mss not available")
            return None

        try:
            # Create mss instance fresh in this thread (not thread-safe across threads)
            with mss.mss() as sct:
                # Try to get game window bounds, fall back to primary monitor
                monitor = get_game_window_rect()
                if monitor:
                    print(f"[VisionAgent] Capturing window: {monitor['width']}x{monitor['height']}")
                else:
                    monitor = sct.monitors[1]  # Primary monitor
                    print("[VisionAgent] Window not found, skipping")
                    return None

                # Capture
                screenshot = sct.grab(monitor)

                # Convert to PIL Image
                img = Image.frombytes('RGB', screenshot.size, screenshot.bgra, 'raw', 'BGRX')

            # Resize to max 768px width for optimal vision model tokenization
            # (Gemini uses 768x768 tiles, this reduces token usage significantly)
            MAX_WIDTH = 768
            if img.width > MAX_WIDTH:
                ratio = MAX_WIDTH / img.width
                new_height = int(img.height * ratio)
                img = img.resize((MAX_WIDTH, new_height), Image.Resampling.LANCZOS)
                print(f"[VisionAgent] Resized to {MAX_WIDTH}x{new_height}")

            # Save as JPEG to buffer
            buffer = BytesIO()
            img.save(buffer, format='JPEG', quality=90)
            jpg_bytes = buffer.getvalue()

            # Convert to base64
            b64 = base64.b64encode(jpg_bytes).decode('utf-8')

            print(f"[VisionAgent] Screenshot captured: {len(jpg_bytes)} bytes")
            return b64

        except Exception as e:
            print(f"[VisionAgent] Screenshot error: {e}")
            return None

    def _build_prompt(self, position, game_context):
        """Build the vision prompt with context"""
        # Extract context - prioritize specific zone location from HUD
        zone_location = game_context.get('zoneLocation', '')
        broad_location = position.get('location', game_context.get('location', 'Unknown'))
        location = zone_location if zone_location else broad_location

        # Time of day
        hour = game_context.get('hour', 12)
        if 5 <= hour < 12:
            time_of_day = "Morning"
        elif 12 <= hour < 17:
            time_of_day = "Afternoon"
        elif 17 <= hour < 21:
            time_of_day = "Evening"
        else:
            time_of_day = "Night"

        # Nearby NPCs section
        nearby = game_context.get('nearbyNpcs', [])
        if nearby:
            npc_lines = ["## Nearby (not necessarily visible):"]
            for npc in nearby[:5]:  # Limit to 5
                name = npc.get('name', 'Unknown')
                distance = npc.get('distance', 0)
                npc_lines.append(f"- {name} ({distance:.0f} units away)")
            visible_npcs_section = "\n".join(npc_lines)
        else:
            visible_npcs_section = "## Nearby: (none detected)"

        # Nearby landmarks section (provides spatial context)
        # Exclude current location from landmarks (both specific zone and broad region)
        exclude_locs = [loc for loc in [zone_location, broad_location] if loc]
        landmarks = get_nearby_landmarks(position, world_name=broad_location, count=5, exclude_names=exclude_locs)
        if landmarks:
            lm_lines = ["## Nearby known locations:"]
            for lm in landmarks:
                if lm['direction']:
                    lm_lines.append(f"- {lm['name']}: {lm['distance']} {lm['direction']}")
                else:
                    lm_lines.append(f"- {lm['name']}: {lm['distance']}")
            nearby_landmarks_section = "\n".join(lm_lines)
        else:
            nearby_landmarks_section = ""

        # Format prompt
        prompt = VISION_PROMPT.format(
            location=location,
            time_of_day=time_of_day,
            visible_npcs_section=visible_npcs_section,
            nearby_landmarks_section=nearby_landmarks_section
        )

        return prompt

    def _call_vision_llm(self, image_b64, prompt, llm_settings):
        """Call vision LLM with screenshot via shared llm module"""
        model = llm_settings.get('model', 'google/gemini-2.0-flash-001')
        temperature = llm_settings.get('temperature', 0.7)
        max_tokens = llm_settings.get('max_tokens', 500)

        print(f"[VisionAgent] Calling {model}...")

        result = llm.chat_with_vision(
            prompt=prompt,
            image_b64=image_b64,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens
        )

        if result:
            print(f"[VisionAgent] Got response: {len(result)} chars")

        return result

    def _save_context(self, description, position, game_context):
        """Save vision context for Lua to read"""
        # Use specific zone location from HUD if available, fallback to broad location
        zone_location = game_context.get('zoneLocation', '')
        broad_location = position.get('location', game_context.get('location', 'Unknown'))

        context = {
            'timestamp': time.time(),
            'position': {
                'x': position.get('x', 0),
                'y': position.get('y', 0),
                'z': position.get('z', 0),
            },
            'location': broad_location,
            'zoneLocation': zone_location if zone_location else broad_location,
            'description': description,
        }

        # Parse structured output if present
        if '**' in description:
            try:
                parts = description.split('**')
                for i, part in enumerate(parts):
                    if part.strip() == 'Scene:' and i+1 < len(parts):
                        context['scene'] = parts[i+1].strip().strip(':').strip()
                    elif part.strip() == 'Visible characters:' and i+1 < len(parts):
                        context['characters'] = parts[i+1].strip().strip(':').strip()
                    elif part.strip() == 'Atmosphere:' and i+1 < len(parts):
                        context['atmosphere'] = parts[i+1].strip().strip(':').strip()
            except:
                pass

        # Log location
        if zone_location:
            print(f"[VisionAgent] Zone location: {zone_location}")

        self.last_context = context

    def get_current_context(self):
        """Get the current vision context (for API use)"""
        return self.last_context


# Singleton instance
_agent_instance = None

def get_agent():
    """Get or create the singleton vision agent instance"""
    global _agent_instance
    if _agent_instance is None:
        _agent_instance = VisionAgent()
    return _agent_instance

def start_agent():
    """Start the vision agent"""
    agent = get_agent()
    agent.start()

def stop_agent():
    """Stop the vision agent"""
    global _agent_instance
    if _agent_instance:
        _agent_instance.stop()
        _agent_instance = None


if __name__ == "__main__":
    # Test mode
    print("Testing VisionAgent...")
    agent = VisionAgent()

    # Test screenshot capture
    if MSS_AVAILABLE:
        print("Testing screenshot capture...")
        b64 = agent._capture_screenshot()
        if b64:
            print(f"Screenshot captured: {len(b64)} chars base64")
        else:
            print("Screenshot failed")

    # Don't start the loop in test mode
    print("Test complete. Run from server.py to start the agent loop.")
