"""
UEVR shared memory backend for VR headset tracking.

Reads HMD + left/right controller poses, grip states, and the actual in-game
camera rotation from named shared memory written by the sonorus_vr_bridge UEVR
plugin.  Works regardless of whether UEVR uses OpenVR or OpenXR internally.

Supports v2 (80-byte, pose-only), v3 (112-byte, adds left controller + grip
states), and v4 (132-byte, adds game-world HMD transform) shared memory layouts.
"""
import math
import mmap
import struct
from typing import Tuple, Optional

from vr.base import VRBackend, PoseData

# Shared memory layout v2 (80 bytes) — legacy DLL, no grip data
# version:       uint32  (4)
# frame_counter: uint32  (4)
# hmd_pos:       3×float (12)
# hmd_rot:       4×float (16)  — w, x, y, z quaternion
# ctrl_pos:      3×float (12)
# ctrl_rot:      4×float (16)  — w, x, y, z quaternion
# cam_pitch:     float   (4)
# cam_yaw:       float   (4)
# cam_roll:      float   (4)
# ctrl_valid:    uint8   (1)
# hmd_active:    uint8   (1)
# is_openxr:     uint8   (1)
# cam_valid:     uint8   (1)
# Total: 80 bytes

# Shared memory layout v3 (112 bytes) — adds left controller + grip states
# [all v2 fields above]
# left_pos:      3×float (12)
# left_rot:      4×float (16)  — w, x, y, z quaternion
# left_valid:    uint8   (1)
# grip_right:    uint8   (1)
# grip_left:     uint8   (1)
# pad:           uint8   (1)
# Total: 112 bytes

_SHM_NAME    = "SonorusVRData"
_SHM_SIZE_V2 = 80
_SHM_SIZE_V3 = 112
_SHM_SIZE_V4 = 132
_FMT_V2 = "<II3f4f3f4f3fBBBB"       # 80 bytes
_FMT_V3 = "<II3f4f3f4f3fBBBB3f4fBBBB"  # 112 bytes
_FMT_V4 = "<II3f4f3f4f3fBBBB3f4fBBBB3f2f"  # 132 bytes

# Field indices (same for v2/v3/v4 up to index 22):
# [0]=version [1]=frame_counter
# [2-4]=hmd_pos [5-8]=hmd_rot(w,x,y,z)
# [9-11]=ctrl_pos [12-15]=ctrl_rot(w,x,y,z)
# [16]=cam_pitch [17]=cam_yaw [18]=cam_roll
# [19]=ctrl_valid [20]=hmd_active [21]=is_openxr [22]=cam_valid
# v3 additional:
# [23-25]=left_pos [26-29]=left_rot(w,x,y,z)
# [30]=left_valid [31]=grip_right [32]=grip_left [33]=pad
# v4 additional:
# [34-36]=hmd_world_pos(x,y,z) [37]=hmd_world_yaw [38]=hmd_world_pitch


def _try_open_shm(size: int) -> Optional[mmap.mmap]:
    """Open named shared memory read-only; return None on failure."""
    try:
        return mmap.mmap(-1, size, tagname=_SHM_NAME, access=mmap.ACCESS_READ)
    except Exception:
        return None


class UEVRBackend(VRBackend):
    """Reads VR poses (and grip states) from UEVR plugin shared memory."""

    name = "UEVR"

    def __init__(self):
        self._shm = None
        self._shm_version = 0
        self._last_frame = 0
        # Public attributes updated by poll()
        self.grip_right: bool = False
        self.grip_left:  bool = False
        self.left_pos:   tuple = (0.0, 0.0, 0.0)
        self.left_valid: bool = False
        self.hmd_rot:    tuple = (1.0, 0.0, 0.0, 0.0)  # w,x,y,z quaternion
        self.cam_yaw:    float = 0.0
        # v4: game-world HMD transform (computed by C++ plugin from standing_origin + rotation_offset)
        self.hmd_world_pos:   tuple = (0.0, 0.0, 0.0)
        self.hmd_world_yaw:   float = 0.0
        self.hmd_world_pitch: float = 0.0
        self.cam_pitch:  float = 0.0
        self.cam_valid:  bool = False

    @staticmethod
    def _read_version(size: int) -> int:
        """Peek at the version field in shared memory; returns 0 on failure."""
        try:
            shm = mmap.mmap(-1, size, tagname=_SHM_NAME, access=mmap.ACCESS_READ)
            ver = struct.unpack("<I", shm[:4])[0]
            shm.close()
            return ver
        except Exception:
            return 0

    @staticmethod
    def is_available() -> bool:
        """Check if the UEVR plugin shared memory exists with valid data.

        On Windows mmap(-1, ..., tagname=) CREATES the mapping if it doesn't
        exist (filled with zeros), so we must read the version field to
        distinguish 'plugin running' (version 2-4) from empty mapping.
        """
        # Try v4 first, fall back to v3, then v2
        ver = UEVRBackend._read_version(_SHM_SIZE_V4)
        if ver in (2, 3, 4):
            return True
        ver = UEVRBackend._read_version(_SHM_SIZE_V3)
        if ver in (2, 3):
            return True
        ver = UEVRBackend._read_version(_SHM_SIZE_V2)
        return ver == 2

    def is_runtime_ready(self) -> bool:
        return self.is_available()

    def init(self) -> bool:
        # Try v4 layout first (game-world HMD transform)
        shm = _try_open_shm(_SHM_SIZE_V4)
        if shm:
            try:
                ver = struct.unpack("<I", shm[:4])[0]
                if ver == 4:
                    data = struct.unpack(_FMT_V4, shm[:_SHM_SIZE_V4])
                    frame = data[1]
                    hmd_active = data[20]
                    if hmd_active:
                        self._shm = shm
                        self._shm_version = 4
                        self._last_frame = frame
                        runtime = "OpenXR" if data[21] else "OpenVR"
                        print(f"[VR/UEVR] Connected v4 (runtime={runtime}, frame={frame})")
                        return True
                    shm.close()
                else:
                    shm.close()
            except Exception:
                try:
                    shm.close()
                except Exception:
                    pass

        # Try v3 layout
        shm = _try_open_shm(_SHM_SIZE_V3)
        if shm:
            try:
                ver = struct.unpack("<I", shm[:4])[0]
                if ver == 3:
                    data = struct.unpack(_FMT_V3, shm[:_SHM_SIZE_V3])
                    frame = data[1]
                    hmd_active = data[20]
                    if hmd_active:
                        self._shm = shm
                        self._shm_version = 3
                        self._last_frame = frame
                        runtime = "OpenXR" if data[21] else "OpenVR"
                        print(f"[VR/UEVR] Connected v3 (runtime={runtime}, frame={frame})")
                        return True
                    shm.close()
                elif ver == 2:
                    # Old DLL created 80-byte mapping; reopen at correct size
                    shm.close()
                    shm = _try_open_shm(_SHM_SIZE_V2)
                    if shm:
                        data = struct.unpack(_FMT_V2, shm[:_SHM_SIZE_V2])
                        frame = data[1]
                        hmd_active = data[20]
                        if hmd_active:
                            self._shm = shm
                            self._shm_version = 2
                            self._last_frame = frame
                            runtime = "OpenXR" if data[21] else "OpenVR"
                            print(f"[VR/UEVR] Connected v2 (runtime={runtime}, frame={frame})")
                            return True
                        shm.close()
                else:
                    shm.close()
            except Exception:
                try:
                    shm.close()
                except Exception:
                    pass

        # Fall back: try v2 directly
        shm = _try_open_shm(_SHM_SIZE_V2)
        if shm:
            try:
                data = struct.unpack(_FMT_V2, shm[:_SHM_SIZE_V2])
                if data[0] == 2 and data[20]:  # version==2 and hmd_active
                    self._shm = shm
                    self._shm_version = 2
                    self._last_frame = data[1]
                    print(f"[VR/UEVR] Connected v2 fallback")
                    return True
                shm.close()
            except Exception:
                try:
                    shm.close()
                except Exception:
                    pass

        return False

    @staticmethod
    def _quat_to_yaw_pitch(w, x, y, z) -> Tuple[float, float]:
        """Convert quaternion (w,x,y,z) to (yaw, pitch) in degrees.

        UEVR get_pose() returns quaternions in Y-up tracking space (OpenXR).
        Uses forward vector: forward = -Z, right = +X, up = +Y.
        Yaw positive = left (matches UE convention). Pitch positive = up.
        """
        # Forward vector = -Z column of rotation matrix
        fx = -2.0 * (x * z + w * y)
        fy =  2.0 * (w * x - y * z)
        fz =  2.0 * (x * x + y * y) - 1.0

        # Yaw: horizontal angle in XZ plane
        yaw = math.degrees(math.atan2(-fx, -fz))

        # Pitch: elevation from horizontal
        pitch = math.degrees(math.asin(max(-1.0, min(1.0, fy))))

        return yaw, pitch

    def poll(self) -> Tuple[PoseData, Optional[PoseData]]:
        hmd_pose = PoseData()
        ctrl_pose = None

        if not self._shm:
            return hmd_pose, ctrl_pose

        try:
            self._shm.seek(0)
            if self._shm_version == 4:
                raw = self._shm.read(_SHM_SIZE_V4)
                data = struct.unpack(_FMT_V4, raw)
            elif self._shm_version == 3:
                raw = self._shm.read(_SHM_SIZE_V3)
                data = struct.unpack(_FMT_V3, raw)
            else:
                raw = self._shm.read(_SHM_SIZE_V2)
                data = struct.unpack(_FMT_V2, raw)

            frame = data[1]
            hmd_active = data[20]

            # Always update HMD quaternion + grip/left state (even for stale pose frames)
            self.hmd_rot = (data[5], data[6], data[7], data[8])  # w,x,y,z

            # v4: game-world HMD transform (always update, even for stale frames)
            if self._shm_version == 4:
                self.hmd_world_pos = (data[34], data[35], data[36])
                self.hmd_world_yaw = data[37]
                self.hmd_world_pitch = data[38]

            if self._shm_version >= 3:
                self.grip_right = bool(data[31])
                self.grip_left  = bool(data[32])
                self.left_valid = bool(data[30])
                if self.left_valid:
                    self.left_pos = (data[23], data[24], data[25])
                else:
                    self.left_pos = (0.0, 0.0, 0.0)
            else:
                self.grip_right = False
                self.grip_left  = False
                self.left_valid = False
                self.left_pos   = (0.0, 0.0, 0.0)

            if not hmd_active or frame == self._last_frame:
                return hmd_pose, ctrl_pose
            self._last_frame = frame

            # HMD pose (tracking space)
            hmd_pos = (data[2], data[3], data[4])
            hmd_w, hmd_x, hmd_y, hmd_z = data[5], data[6], data[7], data[8]
            hmd_yaw, hmd_pitch = self._quat_to_yaw_pitch(hmd_w, hmd_x, hmd_y, hmd_z)
            hmd_pose = PoseData(yaw=hmd_yaw, pitch=hmd_pitch, position=hmd_pos, valid=True)

            # Right controller (tracking space)
            ctrl_valid = data[19]
            if ctrl_valid:
                ctrl_pos = (data[9], data[10], data[11])
                ctrl_w, ctrl_x, ctrl_y, ctrl_z = data[12], data[13], data[14], data[15]
                ctrl_yaw, ctrl_pitch = self._quat_to_yaw_pitch(ctrl_w, ctrl_x, ctrl_y, ctrl_z)
                ctrl_pose = PoseData(yaw=ctrl_yaw, pitch=ctrl_pitch, position=ctrl_pos, valid=True)

            # Store camera rotation for manager to read
            cam_valid = data[22]
            if cam_valid:
                self.cam_yaw   = data[17]
                self.cam_pitch = data[16]
                self.cam_valid = True
            else:
                self.cam_valid = False

        except Exception as e:
            print(f"[VR/UEVR] Poll error: {e}")

        return hmd_pose, ctrl_pose

    def get_debug_info(self) -> Optional[dict]:
        if not self._shm:
            return None
        try:
            self._shm.seek(0)
            if self._shm_version == 3:
                raw = self._shm.read(_SHM_SIZE_V3)
                data = struct.unpack(_FMT_V3, raw)
            else:
                raw = self._shm.read(_SHM_SIZE_V2)
                data = struct.unpack(_FMT_V2, raw)

            runtime = "OpenXR" if data[21] else "OpenVR"
            info = {
                "backend": f"UEVR v{self._shm_version} ({runtime})",
                "frame": data[1],
                "hmd_active": bool(data[20]),
            }
            if data[22]:  # cam_valid
                info["cam"] = {"yaw": round(data[17], 1), "pitch": round(data[16], 1)}
            hmd_w, hmd_x, hmd_y, hmd_z = data[5], data[6], data[7], data[8]
            yaw, pitch = self._quat_to_yaw_pitch(hmd_w, hmd_x, hmd_y, hmd_z)
            info["hmd"] = {"yaw": round(yaw, 1), "pitch": round(pitch, 1)}
            if data[19]:  # ctrl_valid
                ctrl_w, ctrl_x, ctrl_y, ctrl_z = data[12], data[13], data[14], data[15]
                cy, cp = self._quat_to_yaw_pitch(ctrl_w, ctrl_x, ctrl_y, ctrl_z)
                info["ctrl"] = {"yaw": round(cy, 1), "pitch": round(cp, 1)}
            if self._shm_version == 3:
                info["grip"] = {"right": bool(data[31]), "left": bool(data[32])}
            return info
        except Exception:
            return None

    def shutdown(self) -> None:
        if self._shm:
            try:
                self._shm.close()
            except Exception:
                pass
            self._shm = None
