"""
Vulkan GPU detection for ggml workers.

Shared by any feature that runs ggml/Vulkan workers (TTS today, STT later).
No Vulkan SDK Python dependency. Results cached after first call, with
explicit refresh for re-enumeration.

Primary source: the bundled ggml DLLs themselves (ctypes, in a throwaway
subprocess so the server never locks the DLL files). This is authoritative -
the device names ("Vulkan0", "Vulkan1", ...) are exactly what the ggml worker
resolves for GGML_BACKEND, and ggml deduplicates GPUs that expose multiple
Vulkan ICDs.

Fallback: parsing `vulkaninfo --summary` when the DLLs are not installed yet.
Caveat: vulkaninfo lists one entry per ICD, so a GPU with two drivers appears
twice and indices can drift from ggml's - good enough for a preview before
the runtime is installed, but the ggml path wins whenever it is available.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List

# Vulkan device types considered usable GPUs. CPU/software rasterizers
# (e.g. llvmpipe, PHYSICAL_DEVICE_TYPE_CPU) are skipped.
_GPU_DEVICE_TYPES = {
    "PHYSICAL_DEVICE_TYPE_DISCRETE_GPU",
    "PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU",
    "PHYSICAL_DEVICE_TYPE_VIRTUAL_GPU",
}

# ---------------------------------------------------------------------------
# Module-level cache
# ---------------------------------------------------------------------------

_cached_vulkan_gpus: List[dict] = []
_detection_done: bool = False


# Directory holding the omnivoice.cpp runtime DLLs (ggml.dll etc.)
_GGML_BIN_DIR = Path(__file__).resolve().parent.parent / "omnivoice_cpp" / "bin"

# Runs in a subprocess: enumerate devices straight from the ggml DLLs so the
# reported names/order are exactly what the worker sees. Prints one JSON line.
_GGML_ENUM_SNIPPET = r"""
import ctypes, json, os, sys
bin_dir = sys.argv[1]
os.add_dll_directory(bin_dir)
os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
dlls = [ctypes.CDLL(os.path.join(bin_dir, n)) for n in ("ggml.dll", "ggml-base.dll")]
def fn(name, restype, argtypes):
    for d in dlls:
        try:
            f = getattr(d, name)
        except AttributeError:
            continue
        f.restype = restype
        f.argtypes = argtypes
        return f
    raise AttributeError(name)
load_all = fn("ggml_backend_load_all_from_path", None, [ctypes.c_char_p])
dev_count = fn("ggml_backend_dev_count", ctypes.c_size_t, [])
dev_get = fn("ggml_backend_dev_get", ctypes.c_void_p, [ctypes.c_size_t])
dev_name = fn("ggml_backend_dev_name", ctypes.c_char_p, [ctypes.c_void_p])
dev_desc = fn("ggml_backend_dev_description", ctypes.c_char_p, [ctypes.c_void_p])
load_all(bin_dir.encode())
out = []
for i in range(dev_count()):
    d = dev_get(i)
    out.append({"name": dev_name(d).decode(), "description": dev_desc(d).decode()})
print(json.dumps(out))
"""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _detect_via_ggml(log: bool) -> List[dict] | None:
    """Enumerate devices from the bundled ggml DLLs (authoritative).

    Returns None when the DLLs are missing or enumeration fails, so the
    caller can fall back to vulkaninfo.
    """
    if not (_GGML_BIN_DIR / "ggml.dll").is_file():
        return None
    try:
        result = subprocess.run(
            [sys.executable, "-c", _GGML_ENUM_SNIPPET, str(_GGML_BIN_DIR)],
            capture_output=True, text=True, timeout=30,
        )
    except Exception as exc:
        if log:
            print(f"[VulkanGPU] ggml enumeration failed: {exc}")
        return None
    if result.returncode != 0:
        if log:
            print(f"[VulkanGPU] ggml enumeration failed (rc={result.returncode}): "
                  f"{(result.stderr or '').strip()[:200]}")
        return None
    try:
        json_line = next(
            line for line in reversed(result.stdout.splitlines())
            if line.startswith("[")
        )
        devices = json.loads(json_line)
    except (StopIteration, ValueError) as exc:
        if log:
            print(f"[VulkanGPU] Could not parse ggml enumeration output: {exc}")
        return None

    gpus = []
    for dev in devices:
        name = dev.get("name", "")
        match = re.fullmatch(r"Vulkan(\d+)", name)
        if not match:
            continue  # CPU / BLAS / other backends
        gpus.append({
            "index": int(match.group(1)),
            "device": name,
            "name": (dev.get("description") or name).strip(),
            "device_type": "GGML_VULKAN",
        })
    return gpus


def _parse_summary_devices(output: str) -> List[dict]:
    """Parse the Devices section of `vulkaninfo --summary` output.

    Entries look like:
        Devices:
        ========
        GPU0:
            deviceType         = PHYSICAL_DEVICE_TYPE_DISCRETE_GPU
            deviceName         = NVIDIA GeForce RTX 5080

    Returns raw entries (deviceName/deviceType) in order of appearance.
    """
    entries = []
    in_devices = False
    current = None
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not in_devices:
            if line.startswith("Devices:"):
                in_devices = True
            continue
        if re.match(r"^GPU\d+:", line):
            current = {"deviceName": "", "deviceType": ""}
            entries.append(current)
            continue
        if current is not None and "=" in line:
            key, _, value = line.partition("=")
            key = key.strip()
            if key in ("deviceName", "deviceType"):
                current[key] = value.strip()
    return entries


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_vulkan_gpus(force_refresh: bool = False, log: bool = True) -> List[dict]:
    """Detect Vulkan GPUs, preferring the bundled ggml DLLs over vulkaninfo.

    Returns a list of dicts:
        {"index": int, "device": "Vulkan<N>", "name": str, "device_type": str}
    """
    global _cached_vulkan_gpus, _detection_done

    if _detection_done and not force_refresh:
        return _cached_vulkan_gpus

    should_log = log and not force_refresh
    _detection_done = True
    _cached_vulkan_gpus = []

    ggml_gpus = _detect_via_ggml(log)
    if ggml_gpus is not None:
        _cached_vulkan_gpus = ggml_gpus
        if should_log:
            if ggml_gpus:
                summary = ", ".join(f"{gpu['device']}: {gpu['name']}" for gpu in ggml_gpus)
                print(f"[VulkanGPU] Detected via ggml: {summary}")
            else:
                print("[VulkanGPU] ggml reports no Vulkan GPUs")
        return ggml_gpus

    vulkaninfo = shutil.which("vulkaninfo")
    if vulkaninfo is None:
        if should_log:
            print("[VulkanGPU] vulkaninfo not found on PATH - no Vulkan GPU detected")
        return []

    try:
        result = subprocess.run(
            [vulkaninfo, "--summary"],
            capture_output=True, text=True, timeout=15,
        )
    except subprocess.TimeoutExpired:
        if log:
            print("[VulkanGPU] vulkaninfo timed out")
        return []
    except Exception as exc:
        if log:
            print(f"[VulkanGPU] Error querying vulkaninfo: {exc}")
        return []

    if result.returncode != 0:
        if log:
            print(f"[VulkanGPU] vulkaninfo failed (rc={result.returncode}): {result.stderr.strip()[:200]}")
        return []

    try:
        entries = _parse_summary_devices(result.stdout)
    except Exception as exc:
        if log:
            print(f"[VulkanGPU] Error parsing vulkaninfo output: {exc}")
        return []

    gpus = []
    for index, entry in enumerate(entries):
        name = entry.get("deviceName") or f"Vulkan device {index}"
        device_type = entry.get("deviceType") or ""
        if device_type not in _GPU_DEVICE_TYPES:
            if should_log:
                print(f"[VulkanGPU] Skipping non-GPU Vulkan device {index}: {name} ({device_type or 'unknown type'})")
            continue
        gpus.append({
            "index": index,
            "device": f"Vulkan{index}",
            "name": name,
            "device_type": device_type,
        })

    _cached_vulkan_gpus = gpus
    if should_log:
        if gpus:
            summary = ", ".join(f"{gpu['device']}: {gpu['name']}" for gpu in gpus)
            print(f"[VulkanGPU] Detected: {summary}")
        else:
            print("[VulkanGPU] No Vulkan GPUs detected")
    return gpus


def is_vulkan_available() -> bool:
    """Return True if at least one Vulkan GPU was detected."""
    if not _detection_done:
        detect_vulkan_gpus()
    return bool(_cached_vulkan_gpus)
