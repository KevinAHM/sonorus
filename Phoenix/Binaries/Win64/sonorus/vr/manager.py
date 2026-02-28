"""
VR tracking manager - shared logic across backends.

Owns the active backend, computes relative HMD-controller offsets (drift-free),
detects spell-casting pose, detects hand-over-mouth mic gesture, and runs a
background loop that polls at 100ms (Lua push every 500ms).
"""
import math
import threading
import time
from typing import Optional

from vr.base import PoseData


class VRManager:
    """Central VR tracking manager.

    In UEVR, the game camera rotation = right controller (wand) direction.
    The HMD faces a different direction (head vs hand). We compute the relative
    offset between HMD and controller in VR tracking space:
        head_turn_offset = hmd_yaw - controller_yaw
    This offset is drift-free (both are in the same coordinate space) and when
    added to the game camera yaw gives the actual head direction in game world.

    Runs a background thread at 100ms for gesture detection and pushes
    relative offsets to Lua every 500ms. During audio playback, update() is
    also called at 50ms for smooth spatialization.
    """

    # Gesture thresholds (tracking-space meters, Y-up coordinate system)
    # dy = ctrl_y - hmd_y; negative means controller is below HMD.
    _GESTURE_MAX_DIST = 0.20    # max 3D distance from HMD center (~8 inches, hand near face)
    _GESTURE_DY_MIN   = -0.18   # dy lower bound: controller no more than 18 cm below HMD (covers chin)
    _GESTURE_DY_MAX   = -0.03   # dy upper bound: controller at least 3 cm below HMD (rejects forehead)
    _GESTURE_HOLD_THRESHOLD = 0.6  # seconds: tap vs hold boundary

    def __init__(self):
        self.initialized = False
        self.hmd_yaw = 0.0        # degrees, head turn relative to controller (for Lua gaze)
        self.hmd_pitch = 0.0  # degrees, head pitch relative to controller (for Lua gaze)
        self.cam_yaw = 0.0        # degrees, actual in-game camera yaw (for 3D audio)
        self.cam_pitch = 0.0      # degrees, actual in-game camera pitch (for 3D audio)
        self.vr_spell_mode = False  # True when wand is in spell-casting pose
        self._backend = None
        self._lua_socket = None
        self._stop_event = threading.Event()
        self._thread = None
        self._poll_lock = threading.Lock()  # Prevents concurrent poll() from push loop + audio thread
        self._prev_mic_gesture = False      # Previous frame gesture state
        self._gesture_start_time = 0.0      # monotonic time when gesture began
        self._gesture_hold_fired = False    # True if hold action already fired this gesture
        self._stop_conversation_cb = None   # Set by server.py via vr/__init__.py

    def init(self, lua_socket=None) -> bool:
        """Try available backends. Start push loop on success."""
        self._lua_socket = lua_socket
        backends = self._discover_backends()

        for backend_cls in backends:
            try:
                backend = backend_cls()
                if backend.init():
                    self._backend = backend
                    self.initialized = True
                    # Start background thread for continuous polling + Lua push
                    self._stop_event.clear()
                    self._thread = threading.Thread(target=self._push_loop, daemon=True)
                    self._thread.start()
                    print(f"[VRManager] Active backend: {backend.name}")
                    return True
            except Exception:
                pass

        return False

    @staticmethod
    def _discover_backends():
        """Return list of available backend classes."""
        backends = []
        try:
            from vr.uevr_backend import UEVRBackend
            if UEVRBackend.is_available():
                backends.append(UEVRBackend)
        except ImportError:
            pass
        return backends

    def update(self):
        """Poll backend and compute relative head offset. Thread-safe."""
        if not self.initialized or not self._backend:
            return

        with self._poll_lock:
            hmd_pose, ctrl_pose = self._backend.poll()

        if not hmd_pose.valid:
            return

        # Read actual in-game camera rotation from UEVR stereo callback
        if hasattr(self._backend, 'cam_valid') and self._backend.cam_valid:
            self.cam_yaw = self._backend.cam_yaw
            self.cam_pitch = self._backend.cam_pitch

        if ctrl_pose and ctrl_pose.valid:
            # Relative offset: head direction minus wand direction
            # This is drift-free (both in same tracking space)
            rel_yaw = hmd_pose.yaw - ctrl_pose.yaw
            # Normalize to [-180, 180]
            self.hmd_yaw = ((rel_yaw + 180) % 360) - 180
            self.hmd_pitch = hmd_pose.pitch - ctrl_pose.pitch
            # Check spell-casting pose
            self._check_spell_pose(hmd_pose.position, ctrl_pose.position)
        else:
            # Fallback: absolute HMD (will drift with stick turns)
            self.hmd_yaw = hmd_pose.yaw
            self.hmd_pitch = hmd_pose.pitch
            self.vr_spell_mode = False  # Can't check pose without controller

        # Mic gesture: hand over mouth + grip (v3 backend exposes grip states)
        self._update_mic_gesture(hmd_pose, ctrl_pose)

    def _check_spell_pose(self, hmd_pos, ctrl_pos):
        """Check if wand is in spell-casting pose: arm extended + wand at gaze level.

        Args:
            hmd_pos: (x, y, z) HMD position in tracking space
            ctrl_pos: (x, y, z) right controller position in tracking space
        """
        # Horizontal distance (XZ plane) between HMD and controller
        dx = ctrl_pos[0] - hmd_pos[0]
        dz = ctrl_pos[2] - hmd_pos[2]
        horiz_dist = math.sqrt(dx * dx + dz * dz)

        # Vertical angle: controller relative to HMD
        dy = ctrl_pos[1] - hmd_pos[1]
        dist_3d = math.sqrt(dx * dx + dy * dy + dz * dz)
        if dist_3d < 0.01:
            self.vr_spell_mode = False
            return
        vert_angle = math.degrees(math.asin(max(-1.0, min(1.0, dy / dist_3d))))

        # Arm must be extended (> 0.28m horizontal from head)
        # Wand must be near gaze level (within +/-35 degrees vertical)
        self.vr_spell_mode = horiz_dist >= 0.28 and abs(vert_angle) < 35.0

    @staticmethod
    def _hmd_forward(hmd_rot: tuple) -> tuple:
        """Compute HMD forward unit vector from its quaternion.

        OpenXR convention: device local -Z is the forward (viewing) direction.
        Returns (fx, fy, fz) in tracking-space world coordinates.
        """
        w, x, y, z = hmd_rot
        # -Z column of rotation matrix from quaternion
        fx = -2.0 * (x * z + w * y)
        fy =  2.0 * (w * x - y * z)
        fz =  2.0 * (x * x + y * y) - 1.0
        return (fx, fy, fz)

    def _near_mouth(self, hmd_pos: tuple, hmd_rot: tuple, ctrl_pos: tuple) -> bool:
        """Return True if ctrl_pos is within the hand-over-mouth gesture zone.

        Three conditions must all hold:
          1. Total 3D distance from HMD < _GESTURE_MAX_DIST (28 cm)
          2. Controller is 3–25 cm below HMD center (Y-axis, Y-up space)
          3. Controller is at least 5 cm in front of the HMD plane
             (forward component along HMD's -Z axis > 0.05 m)

        Condition 3 rejects side-of-head positions like ear grabs, which have
        a near-zero forward component even when close to the head.
        """
        dx = ctrl_pos[0] - hmd_pos[0]
        dy = ctrl_pos[1] - hmd_pos[1]
        dz = ctrl_pos[2] - hmd_pos[2]
        dist = math.sqrt(dx * dx + dy * dy + dz * dz)
        if dist >= self._GESTURE_MAX_DIST:
            return False
        if not (self._GESTURE_DY_MIN < dy < self._GESTURE_DY_MAX):
            return False
        # Forward check: project (ctrl - hmd) onto HMD forward vector
        fx, fy, fz = self._hmd_forward(hmd_rot)
        forward_component = dx * fx + dy * fy + dz * fz
        return forward_component > 0.03

    def _update_mic_gesture(self, hmd_pose: PoseData, ctrl_pose: Optional[PoseData]):
        """Detect hand-over-mouth gesture; tap = mic toggle, hold = stop conversation.

        Either the right hand (wand) or left hand can trigger the gesture.
        Requires: grip pressed on that hand AND controller in the mouth zone.
        Only available on v3 backend (grip_right / grip_left attributes).

        Tap (release before _GESTURE_HOLD_THRESHOLD): toggle open mic
        Hold (held >= _GESTURE_HOLD_THRESHOLD): stop conversation
        """
        backend = self._backend
        if not hmd_pose.valid or not hasattr(backend, 'grip_right'):
            self._prev_mic_gesture = False
            self._gesture_start_time = 0.0
            self._gesture_hold_fired = False
            return

        hmd_pos = hmd_pose.position
        hmd_rot = getattr(backend, 'hmd_rot', (1.0, 0.0, 0.0, 0.0))
        curr_gesture = False

        # Right hand check
        if backend.grip_right and ctrl_pose and ctrl_pose.valid:
            if self._near_mouth(hmd_pos, hmd_rot, ctrl_pose.position):
                curr_gesture = True

        # Left hand check
        if not curr_gesture and backend.grip_left and backend.left_valid:
            if self._near_mouth(hmd_pos, hmd_rot, backend.left_pos):
                curr_gesture = True

        now = time.monotonic()

        if curr_gesture:
            if not self._prev_mic_gesture:
                # Rising edge — start timing
                self._gesture_start_time = now
                self._gesture_hold_fired = False
            elif not self._gesture_hold_fired and (now - self._gesture_start_time) >= self._GESTURE_HOLD_THRESHOLD:
                # Held past threshold → stop conversation
                self._gesture_hold_fired = True
                self._trigger_stop_conversation()
        else:
            if self._prev_mic_gesture and not self._gesture_hold_fired:
                # Released before threshold → tap → toggle mic
                self._trigger_mic_gesture()
            self._gesture_start_time = 0.0
            self._gesture_hold_fired = False

        self._prev_mic_gesture = curr_gesture

    def _trigger_mic_gesture(self):
        """Toggle open-mic via the voice capture module (lazy import avoids circular dep)."""
        try:
            from input.voice import _capture_instance
            if _capture_instance is None:
                return
            if _capture_instance._mode == 'open_mic':
                _capture_instance._handle_open_mic_toggle()
                print("[VRGesture] Mic toggle triggered by tap gesture")
            else:
                print("[VRGesture] Tap detected but mic mode is not open_mic — ignored")
        except Exception as e:
            print(f"[VRGesture] Error triggering mic toggle: {e}")

    def _trigger_stop_conversation(self):
        """Stop conversation via callback set by server.py."""
        cb = self._stop_conversation_cb
        if cb:
            try:
                cb()
                print("[VRGesture] Stop conversation triggered by hold gesture")
            except Exception as e:
                print(f"[VRGesture] Error triggering stop conversation: {e}")
        else:
            print("[VRGesture] Hold gesture detected but no stop callback registered")

    def _push_loop(self):
        """Background thread: poll every 100ms, push vr_offset to Lua every 500ms."""
        _push_every = 5   # iterations between Lua pushes (5 × 100ms = 500ms)
        _push_count = 0
        while not self._stop_event.is_set():
            self.update()

            _push_count += 1
            if _push_count >= _push_every:
                _push_count = 0
                lua_sock = self._lua_socket
                if lua_sock:
                    try:
                        lua_sock.send({
                            "type": "vr_offset",
                            "yaw": round(self.hmd_yaw, 1),
                            "pitch": round(self.hmd_pitch, 1),
                        })
                    except Exception:
                        pass

            self._stop_event.wait(0.1)

    def get_debug_info(self):
        """Delegate debug info to backend."""
        if self._backend:
            return self._backend.get_debug_info()
        return None

    def shutdown(self):
        """Stop push loop and shut down backend."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self._backend:
            self._backend.shutdown()
            self._backend = None
        self.initialized = False
        self._lua_socket = None
