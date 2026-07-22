/*
Sonorus VR Bridge — UEVR Plugin
Reads HMD + left/right controller poses, grip action states, and computes
the game-world HMD transform (via UEVR's standing_origin + rotation_offset)
for 3D audio spatialization. Writes all data to named shared memory
("SonorusVRData") for the Sonorus Python server.

Place the compiled DLL in UEVR's plugins/ folder (global, not profile-specific).

MIT License — Copyright (c) 2024-2026 Sonorus contributors
*/
#include <Windows.h>
#include <cmath>
#include <cstdint>
#include <cstring>

#include "uevr/API.h"

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

/* ── Shared memory layout v4 (must match Python struct format) ───── */
/* v4 adds game-world HMD position and rotation (computed from         */
/* UEVR's standing_origin + rotation_offset + tracking HMD pose)       */
#pragma pack(push, 1)
struct SonorusVRData {
    uint32_t version;       /* Layout version (4) */
    uint32_t frame_counter; /* Increments each tick — lets Python detect staleness */
    /* HMD (tracking space) */
    float hmd_pos[3];       /* x, y, z */
    float hmd_rot[4];       /* w, x, y, z (quaternion) */
    /* Right controller (tracking space) */
    float ctrl_pos[3];
    float ctrl_rot[4];
    /* Actual in-game camera rotation (UE world space, from stereo view offset) */
    float cam_pitch;        /* degrees */
    float cam_yaw;          /* degrees */
    float cam_roll;         /* degrees */
    /* Flags (v2) */
    uint8_t ctrl_valid;     /* 1 if right controller index != -1 */
    uint8_t hmd_active;     /* 1 if UEVR reports HMD active */
    uint8_t is_openxr;      /* 1 if OpenXR runtime, 0 if OpenVR */
    uint8_t cam_valid;      /* 1 if stereo callback has fired at least once */
    /* Left controller (tracking space) — v3 */
    float left_pos[3];
    float left_rot[4];
    /* Input states — v3 */
    uint8_t left_valid;     /* 1 if left controller index != -1 */
    uint8_t grip_right;     /* 1 if right grip action is active */
    uint8_t grip_left;      /* 1 if left grip action is active */
    uint8_t pad;            /* reserved */
    /* Game-world HMD transform — v4 */
    float hmd_world_pos[3]; /* x, y, z in UE world space */
    float hmd_world_yaw;    /* degrees, HMD yaw in game world */
    float hmd_world_pitch;  /* degrees, HMD pitch in game world */
};
#pragma pack(pop)

/* ── Globals ───────────────────────────────────────────────────────── */
static const UEVR_PluginInitializeParam* g_param = nullptr;
static HANDLE g_mapping = nullptr;
static SonorusVRData* g_data = nullptr;

/* Cached grip action handle — fetched on first successful lookup */
static UEVR_ActionHandle g_grip_handle = nullptr;

/* world_to_meters from stereo callback (default 100 for UE4 cm) */
static float g_world_to_meters = 100.0f;

/* ── Math helpers ──────────────────────────────────────────────────── */

static UEVR_Quaternionf quat_mul(const UEVR_Quaternionf& a, const UEVR_Quaternionf& b) {
    UEVR_Quaternionf r;
    r.w = a.w*b.w - a.x*b.x - a.y*b.y - a.z*b.z;
    r.x = a.w*b.x + a.x*b.w + a.y*b.z - a.z*b.y;
    r.y = a.w*b.y - a.x*b.z + a.y*b.w + a.z*b.x;
    r.z = a.w*b.z + a.x*b.y - a.y*b.x + a.z*b.w;
    return r;
}

static UEVR_Vector3f quat_rotate_vec(const UEVR_Quaternionf& q, const UEVR_Vector3f& v) {
    /* v' = q * v * q^-1 via optimized formula */
    float cx = q.y*v.z - q.z*v.y;
    float cy = q.z*v.x - q.x*v.z;
    float cz = q.x*v.y - q.y*v.x;
    float cx2 = q.y*cz - q.z*cy;
    float cy2 = q.z*cx - q.x*cz;
    float cz2 = q.x*cy - q.y*cx;
    UEVR_Vector3f r;
    r.x = v.x + 2.0f*(q.w*cx + cx2);
    r.y = v.y + 2.0f*(q.w*cy + cy2);
    r.z = v.z + 2.0f*(q.w*cz + cz2);
    return r;
}

/* Extract yaw/pitch from quaternion in Y-up tracking space (OpenXR).
   Uses forward vector approach: forward = -Z, right = +X, up = +Y.
   Yaw: positive = left (matches UE convention).
   Pitch: positive = looking up. */
static void quat_to_yaw_pitch(const UEVR_Quaternionf& q, float* out_yaw, float* out_pitch) {
    /* Forward vector = -Z column of rotation matrix */
    float fx = -2.0f * (q.x * q.z + q.w * q.y);
    float fy =  2.0f * (q.w * q.x - q.y * q.z);
    float fz =  2.0f * (q.x * q.x + q.y * q.y) - 1.0f;

    /* Yaw: horizontal angle in XZ plane */
    *out_yaw = atan2f(-fx, -fz) * (float)(180.0 / M_PI);

    /* Pitch: elevation from horizontal */
    float p = fmaxf(-1.0f, fminf(1.0f, fy));
    *out_pitch = asinf(p) * (float)(180.0 / M_PI);
}

/* ── Shared memory setup ──────────────────────────────────────────── */
static bool create_shared_memory() {
    g_mapping = CreateFileMappingA(
        INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE,
        0, sizeof(SonorusVRData), "SonorusVRData"
    );
    if (!g_mapping) return false;

    g_data = (SonorusVRData*)MapViewOfFile(
        g_mapping, FILE_MAP_ALL_ACCESS, 0, 0, sizeof(SonorusVRData)
    );
    if (!g_data) {
        CloseHandle(g_mapping);
        g_mapping = nullptr;
        return false;
    }

    memset(g_data, 0, sizeof(SonorusVRData));
    g_data->version = 4;
    return true;
}

/* ── Stereo view offset callback (actual camera rotation) ────────── */
static void on_post_stereo_view_offset(
    UEVR_StereoRenderingDeviceHandle, int view_index,
    float world_to_meters, UEVR_Vector3f*, UEVR_Rotatorf* rotation, bool)
{
    /* Only capture from view_index 1 (matches Hogwarts UEVR profile convention) */
    if (!g_data || view_index != 1 || !rotation) return;

    g_data->cam_pitch = rotation->pitch;
    g_data->cam_yaw   = rotation->yaw;
    g_data->cam_roll  = rotation->roll;
    g_data->cam_valid = 1;

    /* Capture world_to_meters for position scaling */
    g_world_to_meters = world_to_meters;
}

/* ── Engine tick callback (HMD + controller poses + grip states) ─── */
static void on_post_engine_tick(UEVR_UGameEngineHandle, float) {
    if (!g_data || !g_param) return;

    auto vr = g_param->vr;
    if (!vr || !vr->is_runtime_ready()) return;

    g_data->hmd_active = vr->is_hmd_active() ? 1 : 0;
    g_data->is_openxr = vr->is_openxr() ? 1 : 0;

    /* HMD pose (tracking space) */
    auto hmd_idx = vr->get_hmd_index();
    UEVR_Vector3f hmd_pos = {};
    UEVR_Quaternionf hmd_rot = {};
    vr->get_pose(hmd_idx, &hmd_pos, &hmd_rot);

    g_data->hmd_pos[0] = hmd_pos.x;
    g_data->hmd_pos[1] = hmd_pos.y;
    g_data->hmd_pos[2] = hmd_pos.z;
    g_data->hmd_rot[0] = hmd_rot.w;
    g_data->hmd_rot[1] = hmd_rot.x;
    g_data->hmd_rot[2] = hmd_rot.y;
    g_data->hmd_rot[3] = hmd_rot.z;

    /* ── Game-world HMD transform (v4) ──────────────────────────── */
    /* rotation_offset: accumulated yaw from snap/smooth turns       */
    /* standing_origin: VR play area center in UE world space        */
    /* world_rot = rotation_offset * tracking_rot                    */
    /* world_pos = standing_origin + rotation_offset * (tracking_pos * scale) */
    {
        UEVR_Quaternionf rot_offset = {1.0f, 0.0f, 0.0f, 0.0f};
        UEVR_Vector3f standing_origin = {};
        vr->get_rotation_offset(&rot_offset);
        vr->get_standing_origin(&standing_origin);

        /* Game-world HMD rotation */
        UEVR_Quaternionf world_rot = quat_mul(rot_offset, hmd_rot);
        float world_yaw, world_pitch;
        quat_to_yaw_pitch(world_rot, &world_yaw, &world_pitch);
        g_data->hmd_world_yaw   = world_yaw;
        g_data->hmd_world_pitch = world_pitch;

        /* Game-world HMD position */
        float s = g_world_to_meters;
        UEVR_Vector3f scaled = { hmd_pos.x * s, hmd_pos.y * s, hmd_pos.z * s };
        UEVR_Vector3f rotated = quat_rotate_vec(rot_offset, scaled);
        g_data->hmd_world_pos[0] = standing_origin.x + rotated.x;
        g_data->hmd_world_pos[1] = standing_origin.y + rotated.y;
        g_data->hmd_world_pos[2] = standing_origin.z + rotated.z;
    }

    /* Right controller pose */
    auto ctrl_idx = vr->get_right_controller_index();
    if (ctrl_idx != (UEVR_TrackedDeviceIndex)-1) {
        UEVR_Vector3f ctrl_pos = {};
        UEVR_Quaternionf ctrl_rot = {};
        vr->get_pose(ctrl_idx, &ctrl_pos, &ctrl_rot);

        g_data->ctrl_pos[0] = ctrl_pos.x;
        g_data->ctrl_pos[1] = ctrl_pos.y;
        g_data->ctrl_pos[2] = ctrl_pos.z;
        g_data->ctrl_rot[0] = ctrl_rot.w;
        g_data->ctrl_rot[1] = ctrl_rot.x;
        g_data->ctrl_rot[2] = ctrl_rot.y;
        g_data->ctrl_rot[3] = ctrl_rot.z;
        g_data->ctrl_valid = 1;
    } else {
        g_data->ctrl_valid = 0;
    }

    /* Left controller pose */
    auto left_idx = vr->get_left_controller_index();
    if (left_idx != (UEVR_TrackedDeviceIndex)-1) {
        UEVR_Vector3f left_pos = {};
        UEVR_Quaternionf left_rot = {};
        vr->get_pose(left_idx, &left_pos, &left_rot);

        g_data->left_pos[0] = left_pos.x;
        g_data->left_pos[1] = left_pos.y;
        g_data->left_pos[2] = left_pos.z;
        g_data->left_rot[0] = left_rot.w;
        g_data->left_rot[1] = left_rot.x;
        g_data->left_rot[2] = left_rot.y;
        g_data->left_rot[3] = left_rot.z;
        g_data->left_valid = 1;
    } else {
        g_data->left_valid = 0;
    }

    /* Grip action states — cached handle, works on Meta/Index/Vive via UEVR action layer */
    if (!g_grip_handle) {
        g_grip_handle = vr->get_action_handle("/actions/default/in/Grip");
    }

    if (g_grip_handle) {
        auto right_src = vr->get_right_joystick_source();
        auto left_src  = vr->get_left_joystick_source();
        g_data->grip_right = (right_src && vr->is_action_active(g_grip_handle, right_src)) ? 1 : 0;
        g_data->grip_left  = (left_src  && vr->is_action_active(g_grip_handle, left_src))  ? 1 : 0;
    } else {
        g_data->grip_right = 0;
        g_data->grip_left  = 0;
    }

    g_data->frame_counter++;
}

/* ── Plugin entry points ──────────────────────────────────────────── */
extern "C" __declspec(dllexport) void uevr_plugin_required_version(UEVR_PluginVersion* version) {
    version->major = UEVR_PLUGIN_VERSION_MAJOR;
    version->minor = UEVR_PLUGIN_VERSION_MINOR;
    version->patch = UEVR_PLUGIN_VERSION_PATCH;
}

extern "C" __declspec(dllexport) bool uevr_plugin_initialize(const UEVR_PluginInitializeParam* param) {
    g_param = param;

    if (!create_shared_memory()) {
        if (param->functions && param->functions->log_error) {
            param->functions->log_error("[SonorusVR] Failed to create shared memory");
        }
        return false;
    }

    /* Register callbacks */
    param->sdk->callbacks->on_post_engine_tick(on_post_engine_tick);
    param->sdk->callbacks->on_post_calculate_stereo_view_offset(on_post_stereo_view_offset);

    if (param->functions && param->functions->log_info) {
        param->functions->log_info("[SonorusVR] Bridge v4 active — shared memory ready (132 bytes)");
    }
    return true;
}

BOOL APIENTRY DllMain(HANDLE, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_DETACH) {
        if (g_data) {
            UnmapViewOfFile(g_data);
            g_data = nullptr;
        }
        if (g_mapping) {
            CloseHandle(g_mapping);
            g_mapping = nullptr;
        }
    }
    return TRUE;
}
