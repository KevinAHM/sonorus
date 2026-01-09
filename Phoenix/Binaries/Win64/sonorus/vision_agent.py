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
VISION_PROMPT = """You are describing what is currently visible in this Hogwarts Legacy screenshot. Your description will be used by NPCs to understand what they can see and comment on. Be specific and vivid enough that someone could have a conversation about any element you mention.

## Context:
- Location: {location}
- Time: {time_of_day}

{player_section}

{visible_npcs_section}
{nearby_landmarks_section}
## CORE INSTRUCTIONS:

**Perspective:** Third-person view following the player character.

**Description Priorities:**

1. **The Player Character** (IMPORTANT):
   - The player character is the figure the camera follows (typically center or slightly off-center)
   - Use the player info above to identify them - describe what they're doing, their pose, position in the scene
   - Example: "Harry stands near the entrance, his Gryffindor robes visible beneath his dark cloak"

2. **Environment & Objects** (ESSENTIAL - be specific and descriptive):
   - Spatial scale and overall layout
   - Architecture: materials, style, condition (weathered stone, polished wood, ornate carvings)
   - **Notable objects deserve rich detail**: If there's a fireplace, describe its style, carvings, what's on the mantle, the quality of the flames. If there's a painting, describe its subject and frame. If there's a desk, note what's on it.
   - Decorative elements: tapestries (what they depict), suits of armor (style/condition), statues (who/what), candles/torches (lit/unlit), plants
   - Magical elements: floating candles, moving portraits, enchanted objects, Floo Flames, House banners, magical creatures, potion ingredients, spell effects
   - Colors and color schemes - dominant hues, contrasts
   - Object states: doors open/closed, books open/stacked, cauldrons bubbling/empty

3. **Other Characters** (NPCs):
   - ONLY describe what you can CLEARLY SEE - pose, gesture, position, what they're doing
   - DO NOT invent or assume actions unless unmistakably visible
   - Placement via scene fixtures (standing by the window, seated near the fire)

4. **Atmosphere**:
   - Lighting quality: warm firelight, cold moonlight, bright daylight, dim torchlight
   - Weather effects (if outdoors)
   - Overall mood and energy

**CRITICAL - NPC Identification:**
- **"VISIBLE" list is authoritative**: Characters listed under "VISIBLE" ARE confirmed in the screenshot.
- **Name tags for identification only**: Use floating name tags to identify WHO a character is, but don't describe the name tag itself in your output.
- **Cross-reference**: If you see a character and "Sebastian Sallow" is in the VISIBLE list, that character is Sebastian - just describe them by name.
- **Extra characters**: If you see more characters than are in the VISIBLE list, describe them generically:
  - "A student" / "Two students" (if no house visible)
  - "A Hufflepuff student" (if house robes/colors clearly visible)
  - "A professor in dark robes"
- **"NEARBY but not visible"**: These characters are NOT in the screenshot - don't describe them

**Output Format:**

**Scene:** [4-6 sentences. Describe the space and its contents with enough detail that someone could comment on specific elements. What would catch someone's eye? What makes this space distinctive? Include materials, colors, decorative details.]

**Player:** [1-2 sentences describing where {player_name} is positioned in the scene and what they appear to be doing. Reference their attire if distinctive.]

**Notable details:** [2-3 specific elements worth mentioning - an interesting object, decoration, or feature. Describe each with 1-2 sentences of vivid detail. These are things someone might point at and say "look at that" or ask about.]

**Visible characters:** [For each visible NPC (not the player), 1-2 sentences on what is clearly visible - their name (if name tag visible), pose, clothing, position, apparent activity. Skip if none visible besides the player.]

**Atmosphere:** [1-2 sentences on lighting quality, mood, ambient details.]

**Style Rules:**
- Active, present tense; concrete, specific details
- Describe objects as if you might discuss them - "an ornate silver candelabra" not just "a candelabra"
- Include colors, materials, conditions, decorative features
- ONLY describe what is CLEARLY visible - when in doubt, leave it out
- **Ignore UI elements**: Don't mention name tags, interaction prompts ("F TALK"), health bars, minimaps, button hints, or any game interface elements - describe only the world and characters themselves

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

        # Activity state tracking (for Lua - foreground status only, idle handled by Lua)
        self._last_sent_foreground = None
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
        """Background loop - sends foreground state to Lua for ambient dialog gating"""
        print("[VisionAgent] Foreground state loop started")

        while self.running:
            try:
                # Send foreground state to Lua (idle detection now handled by Lua)
                self._send_activity_state()

                # Poll every 2 seconds (just foreground check, no position needed)
                time.sleep(2.0)

            except Exception as e:
                print(f"[VisionAgent] Error in loop: {e}")
                time.sleep(2.0)

        print("[VisionAgent] Foreground state loop ended")

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
        """Async wrapper for capture - does handshake for fresh context"""
        try:
            settings = get_vision_settings()

            # Request fresh context for capture (handshake replaces periodic polling)
            # "vision" group does line trace visibility checks for on-screen NPCs
            # "player" group gets playerName/playerHouse, "gear" gets playerGear
            if _lua_socket:
                game_context = _lua_socket.request_context_refresh(
                    groups=["position", "state", "time", "zone", "player", "gear", "npcs", "vision"],
                    timeout=0.5
                )
            else:
                game_context = {}

            # Extract position from fresh context
            x = game_context.get('x')
            y = game_context.get('y')
            z = game_context.get('z')

            if x is not None and y is not None and z is not None:
                current_pos = {
                    'x': x,
                    'y': y,
                    'z': z,
                    'timestamp': time.time(),
                    'location': game_context.get('location', 'Unknown'),
                }
                self._do_capture(current_pos, settings, game_context)
            else:
                print("[VisionAgent] No position data - skipping capture")
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

    def _send_activity_state(self, force=False):
        """Send foreground state to Lua if changed (for ambient dialog gating).
        Idle detection is now handled by Lua directly.
        """
        if not _lua_socket:
            return

        # Check for socket reconnect - force sync on new connection
        conn_id = _lua_socket.get_connection_id()
        if conn_id != self._last_connection_id:
            self._last_connection_id = conn_id
            force = True  # New connection, force send current state

        foreground = is_game_foreground()

        # Only send if state changed (or forced)
        if force or foreground != self._last_sent_foreground:
            _lua_socket.send({
                "type": "activity_state",
                "foreground": foreground
            })
            self._last_sent_foreground = foreground

    def _read_game_context(self):
        """Read game context from socket cache"""
        if _lua_socket:
            return _lua_socket.get_game_context()
        return {}

    def _do_capture(self, current_pos, settings, game_context=None):
        """Capture screenshot and generate description"""
        # Check if game is in foreground
        if not is_game_foreground():
            return  # Silently skip - no need to spam logs

        # Use passed context (from handshake in _do_capture_async)
        if game_context is None:
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
                # Try to get game window bounds, fall back to full screen
                monitor = get_game_window_rect()
                if monitor:
                    print(f"[VisionAgent] Capturing window: {monitor['width']}x{monitor['height']}")
                else:
                    # Fullscreen mode - capture primary monitor
                    monitor = sct.monitors[1]
                    print(f"[VisionAgent] Fullscreen mode - capturing: {monitor['width']}x{monitor['height']}")

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

        # Player section - name, house, and gear
        player_name = game_context.get('playerName', 'the player')
        player_house = game_context.get('playerHouse', '')
        player_gear = game_context.get('playerGear', '')

        player_lines = [f"## Player Character: {player_name}"]
        if player_house:
            player_lines.append(f"- House: {player_house}")
        if player_gear:
            player_lines.append(f"- Current attire: {player_gear}")
        # Add status info if relevant
        if game_context.get('hoodUp'):
            player_lines.append("- Hood is up")
        if game_context.get('inStealth'):
            player_lines.append("- Disillusionment charm active (semi-transparent/shimmering)")
        if game_context.get('isOnBroom'):
            player_lines.append("- Flying on a broom")
        player_section = "\n".join(player_lines)

        # Visible NPCs section - line trace confirmed visible (not occluded)
        visible = game_context.get('visibleNpcs', [])
        nearby = game_context.get('nearbyNpcs', [])

        npc_lines = []

        # Visible NPCs - confirmed visible via line trace (not blocked by walls)
        if visible:
            npc_lines.append("## Characters VISIBLE (confirmed on-screen, look for their name tags):")
            for npc in visible[:5]:
                name = npc.get('name', 'Unknown')
                distance = npc.get('distance', 0)
                npc_lines.append(f"- {name} ({distance:.0f} units away)")

        # Nearby but not visible - either off-screen or occluded
        if nearby:
            visible_names = {npc.get('name', '').lower() for npc in visible}
            not_visible = [npc for npc in nearby if npc.get('name', '').lower() not in visible_names]
            if not_visible:
                npc_lines.append("## Characters NEARBY but not visible (off-screen or behind walls):")
                for npc in not_visible[:3]:
                    name = npc.get('name', 'Unknown')
                    distance = npc.get('distance', 0)
                    npc_lines.append(f"- {name} ({distance:.0f} units away)")

        visible_npcs_section = "\n".join(npc_lines) if npc_lines else "## Nearby: (none detected)"

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
            nearby_landmarks_section = "\n".join(lm_lines) + "\n"
        else:
            nearby_landmarks_section = "\n"

        # Format prompt
        prompt = VISION_PROMPT.format(
            location=location,
            time_of_day=time_of_day,
            player_name=player_name,
            player_section=player_section,
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
                    elif part.strip() == 'Player:' and i+1 < len(parts):
                        context['player'] = parts[i+1].strip().strip(':').strip()
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
