"""
Vulkan GPU detection for ggml workers.

Shared by any feature that runs ggml/Vulkan workers (TTS today, STT later).
No Vulkan SDK Python dependency. Results cached after first call, with
explicit refresh for re-enumeration.

Primary source: the on-demand installed ggml DLLs themselves (ctypes, in a throwaway
subprocess so the server never locks the DLL files). This is authoritative -
the device names ("Vulkan0", "Vulkan1", ...) are exactly what the ggml worker
resolves for GGML_BACKEND, and ggml deduplicates GPUs that expose multiple
Vulkan ICDs. On Windows, DXGI maps those devices to the system-wide
``GPU Adapter Memory`` performance counters so usage includes the game and
other processes, not merely the querying Vulkan process.

Fallback: parsing `vulkaninfo --summary` when the DLLs are not installed yet.
Caveat: vulkaninfo lists one entry per ICD, so a GPU with two drivers appears
twice and indices can drift from ggml's - good enough for a preview before
the runtime is installed, but the ggml path wins whenever it is available.
"""

import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from ctypes import wintypes
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

_PDH_MORE_DATA = 0x800007D2
_PDH_FMT_LARGE = 0x00000400
_GPU_DEDICATED_USAGE_COUNTER = r"\GPU Adapter Memory(*)\Dedicated Usage"


class _Guid(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _Luid(ctypes.Structure):
    _fields_ = [
        ("LowPart", ctypes.c_uint32),
        ("HighPart", ctypes.c_int32),
    ]


class _DxgiAdapterDesc1(ctypes.Structure):
    _fields_ = [
        ("Description", ctypes.c_wchar * 128),
        ("VendorId", ctypes.c_uint32),
        ("DeviceId", ctypes.c_uint32),
        ("SubSysId", ctypes.c_uint32),
        ("Revision", ctypes.c_uint32),
        ("DedicatedVideoMemory", ctypes.c_size_t),
        ("DedicatedSystemMemory", ctypes.c_size_t),
        ("SharedSystemMemory", ctypes.c_size_t),
        ("AdapterLuid", _Luid),
        ("Flags", ctypes.c_uint32),
    ]


class _PdhCounterValueUnion(ctypes.Union):
    _fields_ = [
        ("longValue", ctypes.c_long),
        ("doubleValue", ctypes.c_double),
        ("largeValue", ctypes.c_longlong),
        ("ansiStringValue", ctypes.c_char_p),
        ("wideStringValue", ctypes.c_wchar_p),
    ]


class _PdhCounterValue(ctypes.Structure):
    _anonymous_ = ("value",)
    _fields_ = [
        ("CStatus", wintypes.DWORD),
        ("value", _PdhCounterValueUnion),
    ]

# Runs in a subprocess: enumerate devices and total capacity straight from the
# ggml DLLs so the reported names/order are exactly what the worker sees. The
# ggml "free" value is intentionally not exposed as system-free VRAM because it
# reflects the querying process's Vulkan budget. Prints one JSON line.
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
try:
    dev_memory = fn("ggml_backend_dev_memory", None, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_size_t)])
except AttributeError:
    dev_memory = None
load_all(bin_dir.encode())
out = []
for i in range(dev_count()):
    d = dev_get(i)
    item = {"name": dev_name(d).decode(), "description": dev_desc(d).decode()}
    if dev_memory is not None:
        free_bytes = ctypes.c_size_t()
        total_bytes = ctypes.c_size_t()
        dev_memory(d, ctypes.byref(free_bytes), ctypes.byref(total_bytes))
        item.update({"free_bytes": free_bytes.value, "total_bytes": total_bytes.value})
    out.append(item)
print(json.dumps(out))
"""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _make_guid(data1, data2, data3, data4):
    return _Guid(
        data1,
        data2,
        data3,
        (ctypes.c_ubyte * 8)(*data4),
    )


def _enumerate_dxgi_adapters() -> List[dict]:
    """Return DXGI adapter names and LUIDs without a third-party package."""
    if os.name != "nt":
        return []

    factory = ctypes.c_void_p()
    iid_factory1 = _make_guid(
        0x770AAE78,
        0xF26F,
        0x4DBA,
        (0xA8, 0x29, 0x25, 0x3C, 0x83, 0xD1, 0xB3, 0x87),
    )
    create_factory = ctypes.windll.dxgi.CreateDXGIFactory1
    create_factory.argtypes = [ctypes.POINTER(_Guid), ctypes.POINTER(ctypes.c_void_p)]
    create_factory.restype = ctypes.c_long
    if create_factory(ctypes.byref(iid_factory1), ctypes.byref(factory)) != 0:
        return []

    adapters = []
    factory_vtable = ctypes.cast(
        factory,
        ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)),
    ).contents
    enum_adapters = ctypes.WINFUNCTYPE(
        ctypes.c_long,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
    )(factory_vtable[12])
    release_factory = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(factory_vtable[2])

    try:
        index = 0
        while True:
            adapter = ctypes.c_void_p()
            if enum_adapters(factory, index, ctypes.byref(adapter)) != 0:
                break
            adapter_vtable = ctypes.cast(
                adapter,
                ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)),
            ).contents
            get_desc = ctypes.WINFUNCTYPE(
                ctypes.c_long,
                ctypes.c_void_p,
                ctypes.POINTER(_DxgiAdapterDesc1),
            )(adapter_vtable[10])
            release_adapter = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(adapter_vtable[2])
            try:
                desc = _DxgiAdapterDesc1()
                if get_desc(adapter, ctypes.byref(desc)) == 0:
                    high_part = ctypes.c_uint32(desc.AdapterLuid.HighPart).value
                    adapters.append({
                        "name": desc.Description.strip(),
                        "luid": f"luid_0x{high_part:08x}_0x{desc.AdapterLuid.LowPart:08x}",
                    })
            finally:
                release_adapter(adapter)
            index += 1
    finally:
        release_factory(factory)

    return adapters


def _query_dedicated_usage_by_luid() -> dict:
    """Read system-wide dedicated GPU usage from Windows' native PDH API."""
    if os.name != "nt":
        return {}

    pdh = ctypes.WinDLL("pdh.dll")
    expand_path = pdh.PdhExpandWildCardPathW
    expand_path.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.DWORD,
    ]
    expand_path.restype = wintypes.DWORD

    path_size = wintypes.DWORD(0)
    status = expand_path(
        None,
        _GPU_DEDICATED_USAGE_COUNTER,
        None,
        ctypes.byref(path_size),
        0,
    )
    if status != _PDH_MORE_DATA or path_size.value <= 1:
        return {}
    path_buffer = ctypes.create_unicode_buffer(path_size.value)
    if expand_path(
        None,
        _GPU_DEDICATED_USAGE_COUNTER,
        path_buffer,
        ctypes.byref(path_size),
        0,
    ) != 0:
        return {}
    counter_paths = [part for part in path_buffer[:path_size.value].split("\0") if part]
    if not counter_paths:
        return {}

    open_query = pdh.PdhOpenQueryW
    open_query.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    open_query.restype = wintypes.DWORD
    add_counter = pdh.PdhAddEnglishCounterW
    add_counter.argtypes = [
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    add_counter.restype = wintypes.DWORD
    collect_query = pdh.PdhCollectQueryData
    collect_query.argtypes = [ctypes.c_void_p]
    collect_query.restype = wintypes.DWORD
    get_value = pdh.PdhGetFormattedCounterValue
    get_value.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(_PdhCounterValue),
    ]
    get_value.restype = wintypes.DWORD
    close_query = pdh.PdhCloseQuery
    close_query.argtypes = [ctypes.c_void_p]
    close_query.restype = wintypes.DWORD

    query = ctypes.c_void_p()
    if open_query(None, None, ctypes.byref(query)) != 0:
        return {}
    counters = []
    try:
        for counter_path in counter_paths:
            counter = ctypes.c_void_p()
            if add_counter(query, counter_path, None, ctypes.byref(counter)) == 0:
                counters.append((counter_path, counter))
        if not counters or collect_query(query) != 0:
            return {}

        usage_by_luid = defaultdict(int)
        for counter_path, counter in counters:
            match = re.search(
                r"GPU Adapter Memory\((luid_0x[0-9a-f]+_0x[0-9a-f]+)_phys_\d+\)",
                counter_path,
                flags=re.IGNORECASE,
            )
            if not match:
                continue
            counter_type = wintypes.DWORD()
            value = _PdhCounterValue()
            if get_value(
                counter,
                _PDH_FMT_LARGE,
                ctypes.byref(counter_type),
                ctypes.byref(value),
            ) == 0 and value.CStatus == 0:
                usage_by_luid[match.group(1).lower()] += max(0, int(value.largeValue))
        return dict(usage_by_luid)
    finally:
        close_query(query)


def _get_system_vram_usage_by_name(log: bool) -> dict:
    """Match system-wide PDH usage counters to human-readable DXGI names."""
    try:
        usage_by_luid = _query_dedicated_usage_by_luid()
        usage_by_name = defaultdict(list)
        for adapter in _enumerate_dxgi_adapters():
            luid = adapter["luid"].lower()
            if luid in usage_by_luid:
                usage_by_name[_normalize_gpu_name(adapter["name"])].append(usage_by_luid[luid])
        return dict(usage_by_name)
    except Exception as exc:
        if log:
            print(f"[VulkanGPU] Windows VRAM usage query failed: {exc}")
        return {}


def _normalize_gpu_name(name: str) -> str:
    return " ".join(str(name or "").casefold().split())

def _detect_via_ggml(log: bool) -> List[dict] | None:
    """Enumerate devices from the installed ggml DLLs (authoritative).

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

    usage_by_name = _get_system_vram_usage_by_name(log)
    gpus = []
    for dev in devices:
        name = dev.get("name", "")
        match = re.fullmatch(r"Vulkan(\d+)", name)
        if not match:
            continue  # CPU / BLAS / other backends
        total_bytes = dev.get("total_bytes")
        if not isinstance(total_bytes, int) or total_bytes <= 0:
            total_bytes = None

        total_gb = round(total_bytes / (1024 ** 3), 2) if total_bytes is not None else None
        matching_usage = usage_by_name.get(_normalize_gpu_name(dev.get("description") or name), [])
        system_used_bytes = matching_usage.pop(0) if matching_usage else None
        if total_bytes is not None and system_used_bytes is not None:
            system_used_bytes = min(max(0, system_used_bytes), total_bytes)
            system_free_bytes = total_bytes - system_used_bytes
            free_gb = round(system_free_bytes / (1024 ** 3), 2)
            used_gb = round(system_used_bytes / (1024 ** 3), 2)
        else:
            free_gb = None
            used_gb = None
        gpus.append({
            "index": int(match.group(1)),
            "device": name,
            "name": (dev.get("description") or name).strip(),
            "device_type": "GGML_VULKAN",
            "vram_total_gb": total_gb,
            "vram_free_gb": free_gb,
            "vram_used_gb": used_gb,
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
    """Detect Vulkan GPUs, preferring the installed ggml DLLs over vulkaninfo.

    Returns device identity and, when the installed ggml runtime and Windows
    performance counters are available, system-wide memory telemetry in GiB.
    """
    global _cached_vulkan_gpus, _detection_done

    if _detection_done and not force_refresh:
        return _cached_vulkan_gpus

    should_log = log
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
            "vram_total_gb": None,
            "vram_free_gb": None,
            "vram_used_gb": None,
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
