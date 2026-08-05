"""
Vision Agent - Captures screenshots and generates scene descriptions.

Triggered when the player initiates conversation (opens chat or starts speaking with mic).
Has a minimum cooldown between captures.
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

# Windows API via ctypes (no pywin32 dependency)
import ctypes
from ctypes import wintypes

user32 = ctypes.windll.user32

from constants import GAME_WINDOW_TITLE
from utils.localization import get_display_name
from utils.settings import is_llm_provider_feature_disabled, load_settings

try:
    from vr import is_vr_active
except ImportError:
    def is_vr_active():
        return False


def _get_window_text(hwnd):
    """Get window title text using ctypes."""
    length = user32.GetWindowTextLengthW(hwnd) + 1
    buf = ctypes.create_unicode_buffer(length)
    user32.GetWindowTextW(hwnd, buf, length)
    return buf.value


def is_game_foreground():
    """Check if Hogwarts Legacy is the foreground window"""
    try:
        hwnd = user32.GetForegroundWindow()
        title = _get_window_text(hwnd)
        return GAME_WINDOW_TITLE in title
    except:
        return True


def get_game_monitor():
    """Get the monitor rect where the game window is displayed (for fullscreen fallback)."""
    try:
        hwnd = user32.FindWindowW(None, GAME_WINDOW_TITLE)
        if not hwnd:
            return None

        MONITOR_DEFAULTTONEAREST = 2
        hmonitor = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        if not hmonitor:
            return None

        class MONITORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD),
            ]

        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)

        if not user32.GetMonitorInfoW(hmonitor, ctypes.byref(mi)):
            return None

        rc = mi.rcMonitor
        return {
            "left": rc.left,
            "top": rc.top,
            "width": rc.right - rc.left,
            "height": rc.bottom - rc.top,
        }
    except Exception as e:
        print(f"[VisionAgent] Monitor detection error: {e}")
        return None


def get_game_window_rect():
    """Get the game window rect. Returns None if game not found or not in foreground."""
    try:
        # Find exact match only
        hwnd = user32.FindWindowW(None, GAME_WINDOW_TITLE)
        if not hwnd:
            return None  # Game not running or wrong title

        # Must be foreground
        if user32.GetForegroundWindow() != hwnd:
            return None  # Game not in foreground, skip capture

        # Must not be minimized
        if user32.IsIconic(hwnd):
            return None

        # Get client area
        class RECT(ctypes.Structure):
            _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                        ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

        class POINT(ctypes.Structure):
            _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

        rect = RECT()
        user32.GetClientRect(hwnd, ctypes.byref(rect))

        point = POINT(0, 0)
        user32.ClientToScreen(hwnd, ctypes.byref(point))

        width = rect.right - rect.left
        height = rect.bottom - rect.top

        if width < 640 or height < 480:
            print(f"[VisionAgent] Window too small ({width}x{height}), skipping")
            return None

        return {"left": point.x, "top": point.y, "width": width, "height": height}

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

# Vision system prompts are split by camera perspective so first-person captures never
# include player-character description instructions or output fields.
VISION_SYSTEM_PROMPT_BASE = """You are describing what is currently visible in a Hogwarts Legacy screenshot. Your description will be used by characters to understand what they can see and comment on. Be specific and vivid enough that someone could have a conversation about any element you mention.

The user message contains the screenshot image and per-capture context: location, time of day, perspective mode, and which characters are confirmed visible.

## CORE INSTRUCTIONS:

**Location accuracy:** Use the location name provided in the context EXACTLY. Do not add "classroom", "corridor", or other qualifiers unless you can clearly see that specific room type. "Defence Against the Dark Arts Tower" is the tower area, not necessarily the classroom. Describe what you SEE, not what you assume the space is.

**CRITICAL - Floo Flame Identification:**
- In Hogwarts Legacy, a woman's stone bust, head, or face mounted on or above a small stone arch, alcove, shrine, or wall fixture, together with a bright green hovering light, orb, flame, fire symbol, or "F"-shaped symbol, IS a Floo Flame fast-travel location.
- The green marker may look like a glowing orb or generic interaction light rather than literal fire, and the stone fixture may resemble an arched doorway. Use the combined visual pattern to identify it as a Floo Flame instead of hedging.
- Name it explicitly as a Floo Flame and identify the woman's likeness as Ignatia Wildsmith when visible. Never describe this combination only as an entrance, archway, generic magical orb, or interactive element. Example: "A Floo Flame fast-travel point glows green beneath Ignatia Wildsmith's stone bust."
- The Floo marker may be used for identification despite the general instruction to omit UI. Do not mention unrelated interface elements or input prompts.

**Environment & Objects** (ESSENTIAL - be specific and descriptive):
- **Magical elements FIRST** (this is a magical world - these are the most eye-catching): floating/enchanted objects, self-playing instruments, moving portraits, ghosts, magical creatures, spell effects, enchanted ceiling/sky, Floo Flames, flying books, animated suits of armor, glowing runes, candles floating without holders, stairs that move. If something supernatural is happening (e.g. violins playing themselves mid-air), it MUST be described prominently.
- Spatial scale and overall layout
- Architecture: materials, style, condition (weathered stone, polished wood, ornate carvings)
- **Notable objects deserve rich detail**: If there's a fireplace, describe its style, carvings, what's on the mantle, the quality of the flames. If there's a painting, describe its subject and frame. If there's a desk, note what's on it.
- Animals and creatures: owls, cats, dogs, spiders, hippogriffs, phoenixes, house-elves, or any other creatures visible in the scene. Describe what they're doing and where they are.
- Decorative elements: tapestries (what they depict), suits of armor (style/condition), statues (who/what), candles/torches (lit/unlit), plants
- Colors and color schemes - dominant hues, contrasts
- Object states: doors open/closed, books open/stacked, cauldrons bubbling/empty

**Other Characters:**
- ONLY describe what you can CLEARLY SEE - pose, gesture, position, what they're doing
- DO NOT invent or assume actions unless unmistakably visible
- Placement via scene fixtures (standing by the window, seated near the fire)

**Atmosphere**:
- Lighting quality: warm firelight, cold moonlight, bright daylight, dim torchlight
- Weather effects (if outdoors)
- Overall mood and energy

**CRITICAL - Character Identification:**
- **"VISIBLE" list is authoritative**: Characters listed under "VISIBLE" ARE confirmed in the screenshot.
- **Name tags for identification only**: Use floating name tags to identify WHO a character is, but don't describe the name tag itself in your output.
- **Cross-reference**: If you see a character and "Sebastian Sallow" is in the VISIBLE list, that character is Sebastian - just describe them by name.
- **Extra characters**: If you see more characters than are in the VISIBLE list, describe them generically:
  - "A student" / "Two students" (if no house visible)
  - "A Hufflepuff student" (if house robes/colors clearly visible)
  - "A professor in dark robes"
- **"NEARBY but not visible"**: These characters are NOT in the screenshot - don't describe them

**Style Rules:**
- Active, present tense; concrete, specific details
- Describe objects as if you might discuss them - "an ornate silver candelabra" not just "a candelabra"
- Include colors, materials, conditions, decorative features
- ONLY describe what is CLEARLY visible - when in doubt, leave it out
- **Partial view**: You see a limited field of view, not the whole room. Never describe the location as a whole - describe only what's in frame. Say "this part of the hall" not "the hall is"
- **Ignore UI elements**: Don't mention name tags, interaction prompts ("F TALK"), health bars, minimaps, button hints, or any game interface elements - describe only the world and characters themselves

**If Unable to Describe:**
If the screenshot is a loading screen, too dark, blurry, obscured by UI/menus, shows too limited an area (e.g., staring at a wall/corner), or is otherwise impossible to describe meaningfully, respond with ONLY: `UNCLEAR: <brief reason>`"""


VISION_SYSTEM_PROMPT_THIRD_PERSON = VISION_SYSTEM_PROMPT_BASE + """

## THIRD-PERSON CAMERA:

The camera follows the player character, who is visible in the screenshot. The user context includes the player character's name, house, and visually prominent attire so you can identify and describe that visible figure.

**Description Priorities:**
1. **The Player Character**: Describe where the player character is positioned in the scene, what they are visibly doing, their pose, and distinctive visible attire.
2. **Environment & Objects**: Prioritize magical/supernatural/unusual elements, then architecture, furnishings, creatures, decorative elements, colors, and object states.
3. **Other Characters**: Describe confirmed visible non-player characters and any clearly visible extra characters.
4. **Atmosphere**: Describe lighting, weather if outdoors, and mood.

**Output Format:**

**Scene:** [4-6 sentences. Describe what is visible from this vantage point - you can only see part of the location, so ground your description in what's actually in frame (e.g. "This corner of the Great Hall..." not "The Great Hall is..."). What would catch someone's eye? Include materials, colors, decorative details.]

**Player:** [1-2 sentences describing where the player character is positioned in the scene and what they appear to be doing. Reference their attire if distinctive.]

**Notable details:** [2-3 specific elements worth mentioning. PRIORITIZE magical/supernatural/unusual things first (enchanted objects, self-playing instruments, floating items, magical creatures, moving paintings) over mundane architecture. Describe each with 1-2 sentences of vivid detail. These are things someone would point at and say "look at that!" or ask about.]

**Visible characters:** [For each visible character other than the player, 1-2 sentences on what is clearly visible - their name, pose, clothing, position, apparent activity. Skip if none visible besides the player.]

**Atmosphere:** [1-2 sentences on lighting quality, mood, ambient details.]"""


VISION_SYSTEM_PROMPT_FIRST_PERSON = VISION_SYSTEM_PROMPT_BASE + """

## FIRST-PERSON CAMERA:

The screenshot is from the player's eyes. The player character is not visible. Do not describe the player's body, pose, clothes, identity, actions, or location as a visible figure. The user context intentionally omits player appearance because it cannot be seen from this perspective.

**Description Priorities:**
1. **Environment & Objects**: Prioritize magical/supernatural/unusual elements, then architecture, furnishings, creatures, decorative elements, colors, and object states.
2. **Visible Characters**: Describe confirmed visible characters and any clearly visible extra characters.
3. **Atmosphere**: Describe lighting, weather if outdoors, and mood.

**Output Format:**

**Scene:** [4-6 sentences. Describe what is visible from this first-person vantage point - you can only see part of the location, so ground your description in what's actually in frame (e.g. "This corner of the Great Hall..." not "The Great Hall is..."). What would catch someone's eye? Include materials, colors, decorative details.]

**Notable details:** [2-3 specific elements worth mentioning. PRIORITIZE magical/supernatural/unusual things first (enchanted objects, self-playing instruments, floating items, magical creatures, moving paintings) over mundane architecture. Describe each with 1-2 sentences of vivid detail. These are things someone would point at and say "look at that!" or ask about.]

**Visible characters:** [For each visible character, 1-2 sentences on what is clearly visible - their name, pose, clothing, position, apparent activity. Skip if no characters are visible.]

**Atmosphere:** [1-2 sentences on lighting quality, mood, ambient details.]"""


def _vision_system_prompt_for_perspective(perspective):
    """Return the prompt variant for the current camera perspective."""
    if perspective == "first-person":
        return VISION_SYSTEM_PROMPT_FIRST_PERSON
    return VISION_SYSTEM_PROMPT_THIRD_PERSON


# Gear slots to include in vision context (visually prominent from third-person camera)
_VISION_GEAR_SLOTS = {"HEAD", "OUTFIT", "NECK", "BACK"}


def _filter_gear_for_vision(gear_text):
    """Filter playerGear string for vision: only visible slots, appearance only, no stats/rarity."""
    if not gear_text:
        return ""
    lines = gear_text.split("\n")
    filtered = []
    include_desc = False
    for line in lines:
        # Description lines start with "  - "
        if line.startswith("  - "):
            if include_desc:
                filtered.append(line)
            continue
        # Slot lines look like "HEAD: Item Name ..." — check if it's a vision-relevant slot
        include_desc = False
        colon_idx = line.find(":")
        if colon_idx == -1:
            continue
        slot = line[:colon_idx].strip()
        if slot not in _VISION_GEAR_SLOTS:
            continue
        rest = line[colon_idx + 1:].strip()
        # Skip hidden/invisible items
        if "Hidden" in rest or "invisible" in rest.lower():
            continue
        # Strip transmog stats parenthetical: "(transmogged, stats from ...)"
        paren_idx = rest.find(" (transmogged,")
        if paren_idx != -1:
            rest = rest[:paren_idx]
        # Strip rarity tags like " [Legendary]" (rfind to match last occurrence)
        bracket_idx = rest.rfind(" [")
        if bracket_idx != -1:
            rest = rest[:bracket_idx]
        filtered.append(f"{slot}: {rest.strip()}")
        include_desc = True
    return "\n".join(filtered)


def get_vision_settings():
    """Get vision agent settings with defaults"""
    settings = load_settings()
    vision = settings.get('agents', {}).get('vision', {})
    provider_disabled = is_llm_provider_feature_disabled('vision', settings)

    return {
        'enabled': vision.get('enabled', True) and not provider_disabled,
        'cooldown_seconds': vision.get('cooldown_seconds', 5),
        'wait_timeout_seconds': vision.get('wait_timeout_seconds', 5),
        'wait_for_capture': vision.get('wait_for_capture', True),
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

        # Partial streaming description (updated live during vision LLM streaming)
        self._partial_description = ""
        self._partial_lock = threading.Lock()

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
            print("[VisionAgent] Skipping capture - vision disabled")
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

        print("[VisionAgent] Starting capture")

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
                    timeout=1.0
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

            # Build context for user message
            user_context, perspective = self._build_context(current_pos, game_context)

            # Call vision LLM
            description = self._call_vision_llm(screenshot_b64, user_context, settings['llm'], perspective)
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
                    # Fullscreen mode - find which monitor the game is on
                    monitor = get_game_monitor()
                    if monitor:
                        print(f"[VisionAgent] Fullscreen - game monitor: {monitor['width']}x{monitor['height']}")
                    else:
                        monitor = sct.monitors[1]
                        print(f"[VisionAgent] Fallback - primary monitor: {monitor['width']}x{monitor['height']}")

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

    def _build_context(self, position, game_context):
        """Build the dynamic per-capture context for the user message."""
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

        # VR mode - adjust perspective (cached boolean, no OpenVR ping)
        t_vr = time.perf_counter()
        vr_mode = is_vr_active()
        vr_ms = (time.perf_counter() - t_vr) * 1000
        print(f"[VisionAgent] VR mode: {vr_mode} ({vr_ms:.3f}ms)")

        perspective = "first-person" if vr_mode else "third-person"

        player_section = ""
        if perspective == "third-person":
            # Player section - name, house, and filtered gear
            player_name = game_context.get('playerName', 'the player')
            player_house = game_context.get('playerHouse', '')
            player_gear = _filter_gear_for_vision(game_context.get('playerGear', ''))

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
            if game_context.get('isOnMount'):
                mount_type = game_context.get('mountType', 'broom')
                if mount_type == 'broom':
                    player_lines.append("- Flying on a broom")
                elif mount_type == 'hippogriff':
                    player_lines.append("- Riding a hippogriff")
                elif mount_type == 'graphorn':
                    player_lines.append("- Riding a graphorn")
                else:
                    player_lines.append(f"- Riding a {mount_type}")
            player_section = "\n".join(player_lines)

        # Visible NPCs section - line trace confirmed visible (not occluded)
        visible = game_context.get('visibleNpcs', [])
        nearby = game_context.get('nearbyNpcs', [])

        npc_lines = []

        # Visible NPCs - confirmed visible via line trace (not blocked by walls)
        if visible:
            npc_lines.append("## Characters VISIBLE (confirmed on-screen, look for their name tags):")
            for npc in visible[:5]:
                name = get_display_name(npc.get('name', 'Unknown'))
                distance = npc.get('distance', 0)
                npc_lines.append(f"- {name} ({format_distance_meters(distance)} away)")

        # Nearby but not visible - either off-screen or occluded
        if nearby:
            visible_names = {npc.get('name', '').lower() for npc in visible}
            not_visible = [npc for npc in nearby if npc.get('name', '').lower() not in visible_names]
            if not_visible:
                npc_lines.append("## Characters NEARBY but not visible (off-screen or behind walls):")
                for npc in not_visible[:3]:
                    name = get_display_name(npc.get('name', 'Unknown'))
                    distance = npc.get('distance', 0)
                    npc_lines.append(f"- {name} ({format_distance_meters(distance)} away)")

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
            nearby_landmarks_section = "\n".join(lm_lines)
        else:
            nearby_landmarks_section = ""

        # Assemble user context
        sections = [
            f"## Context:\n- Location: {location}\n- Time: {time_of_day}\n- Perspective: {perspective}",
        ]
        if player_section:
            sections.append(player_section)
        sections.append(visible_npcs_section)
        if nearby_landmarks_section:
            sections.append(nearby_landmarks_section)

        return "\n\n".join(sections), perspective

    def _call_vision_llm(self, image_b64, user_context, llm_settings, perspective):
        """Call vision LLM with streaming, updating _partial_description as chunks arrive."""
        model = llm_settings.get('model', 'google/gemini-3.1-flash-lite')
        temperature = llm_settings.get('temperature', 0.7)
        max_tokens = llm_settings.get('max_tokens', 500)

        print(f"[VisionAgent] Calling {model} (streaming)...")

        # Clear partial before starting
        with self._partial_lock:
            self._partial_description = ""

        # Perspective-specific system prompt + dynamic user context with image
        system_prompt = _vision_system_prompt_for_perspective(perspective)
        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_context},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}}
                ]
            }
        ]

        try:
            accumulated = []
            for chunk in llm.chat_stream(messages, model=model, temperature=temperature,
                                          max_tokens=max_tokens, context="vision",
                                          kv_cache_prefix=[messages[0]],
                                          kv_cache_context="vision"):
                accumulated.append(chunk)
                with self._partial_lock:
                    self._partial_description = "".join(accumulated)

            result = "".join(accumulated)
            if result:
                print(f"[VisionAgent] Got response: {len(result)} chars")

            # Clear partial now that we have the full result
            with self._partial_lock:
                self._partial_description = ""

            return result
        except Exception as e:
            print(f"[VisionAgent] Vision streaming error: {e}")
            # Return whatever we accumulated
            with self._partial_lock:
                partial = self._partial_description
                self._partial_description = ""
            return partial if partial else None

    def get_partial_description(self):
        """Get the in-progress streaming description (empty string if not streaming)."""
        with self._partial_lock:
            return self._partial_description

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
                    elif part.strip() == 'Notable details:' and i+1 < len(parts):
                        context['notable'] = parts[i+1].strip().strip(':').strip()
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
