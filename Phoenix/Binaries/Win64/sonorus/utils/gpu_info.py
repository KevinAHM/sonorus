"""
GPU detection via nvidia-smi subprocess.
No torch dependency. Results cached after first call, with explicit refresh for
live VRAM readings.
"""

import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import List, Optional

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class GpuInfo:
    index: int
    name: str
    driver_cuda_version: str
    vram_total_mb: int
    vram_free_mb: int
    vram_used_mb: int
    cuda_compatible: bool  # True when driver CUDA >= 12.6


# ---------------------------------------------------------------------------
# Module-level cache
# ---------------------------------------------------------------------------

_cached_gpu_info: Optional[GpuInfo] = None
_cached_gpus: List[GpuInfo] = []
_detection_done: bool = False

# Minimum CUDA version considered compatible
_MIN_CUDA = (12, 6)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_cuda_version(nvidia_smi_path: str) -> Optional[str]:
    """Run plain `nvidia-smi` and extract CUDA Version from the header."""
    try:
        result = subprocess.run(
            [nvidia_smi_path],
            capture_output=True, text=True, timeout=10,
        )
        match = re.search(r"CUDA Version:\s*(\d+\.\d+)", result.stdout)
        if match:
            return match.group(1)
    except Exception:
        pass
    return None


def _cuda_ge_min(version_str: str) -> bool:
    """Return True if *version_str* (e.g. '12.6') >= _MIN_CUDA."""
    try:
        parts = version_str.split(".")
        major, minor = int(parts[0]), int(parts[1])
        return (major, minor) >= _MIN_CUDA
    except Exception:
        return False


def _gpu_to_dict(gpu: GpuInfo) -> dict:
    return {
        "index": gpu.index,
        "device": f"cuda:{gpu.index}",
        "name": gpu.name,
        "cuda_version": gpu.driver_cuda_version,
        "vram_total_gb": round(gpu.vram_total_mb / 1024, 2),
        "vram_free_gb": round(gpu.vram_free_mb / 1024, 2),
        "vram_used_gb": round(gpu.vram_used_mb / 1024, 2),
        "cuda_compatible": gpu.cuda_compatible,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_gpus(force_refresh: bool = False, log: bool = True) -> List[GpuInfo]:
    """Detect NVIDIA GPUs via nvidia-smi."""
    global _cached_gpu_info, _cached_gpus, _detection_done

    if _detection_done and not force_refresh:
        return _cached_gpus

    should_log = log and not force_refresh
    _detection_done = True
    _cached_gpu_info = None
    _cached_gpus = []

    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi is None:
        if should_log:
            print("[GPU] nvidia-smi not found on PATH - no NVIDIA GPU detected")
        return []

    cuda_version = _parse_cuda_version(nvidia_smi)
    if should_log:
        if cuda_version:
            print(f"[GPU] Driver CUDA version: {cuda_version}")
        else:
            print("[GPU] Could not parse CUDA version from nvidia-smi output")

    try:
        result = subprocess.run(
            [
                nvidia_smi,
                "--query-gpu=index,name,driver_version,memory.total,memory.free,memory.used",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            if log:
                print(f"[GPU] nvidia-smi query failed (rc={result.returncode}): {result.stderr.strip()}")
            return []

        compatible = _cuda_ge_min(cuda_version) if cuda_version else False
        for line in result.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 6:
                if log:
                    print(f"[GPU] Unexpected nvidia-smi output: {line}")
                continue

            _cached_gpus.append(GpuInfo(
                index=int(float(parts[0])),
                name=parts[1],
                # parts[2] is driver version (not used directly, kept for debugging)
                driver_cuda_version=cuda_version or "unknown",
                vram_total_mb=int(float(parts[3])),
                vram_free_mb=int(float(parts[4])),
                vram_used_mb=int(float(parts[5])),
                cuda_compatible=compatible,
            ))

        if not _cached_gpus:
            return []

        _cached_gpus.sort(key=lambda gpu: gpu.index)
        _cached_gpu_info = _cached_gpus[0]

        summary = ", ".join(
            f"GPU {gpu.index}: {gpu.name} ({round(gpu.vram_total_mb / 1024, 1)} GB)"
            for gpu in _cached_gpus
        )
        if should_log:
            print(f"[GPU] Detected: {summary} | CUDA compatible: {compatible}")
        return _cached_gpus

    except subprocess.TimeoutExpired:
        if log:
            print("[GPU] nvidia-smi timed out")
    except Exception as exc:
        if log:
            print(f"[GPU] Error querying nvidia-smi: {exc}")

    return []


def detect_gpu() -> Optional[GpuInfo]:
    """Detect the first NVIDIA GPU via nvidia-smi. Caches result after first call."""
    if not _detection_done:
        detect_gpus()
    return _cached_gpu_info


def get_recommended_gpu() -> Optional[GpuInfo]:
    """Return the CUDA-compatible GPU with the most currently free VRAM."""
    gpus = detect_gpus(force_refresh=True, log=False)
    compatible = [gpu for gpu in gpus if gpu.cuda_compatible]
    candidates = compatible or gpus
    if not candidates:
        return None
    return max(candidates, key=lambda gpu: gpu.vram_free_mb)


def is_nvidia_available() -> bool:
    """Return True if an NVIDIA GPU was detected."""
    if not _detection_done:
        detect_gpus()
    return _cached_gpu_info is not None


def is_cuda_compatible() -> bool:
    """Return True if the detected GPU has CUDA >= 12.6."""
    if not _detection_done:
        detect_gpus()
    return _cached_gpu_info is not None and _cached_gpu_info.cuda_compatible


def get_gpu_info_dict() -> dict:
    """Return a JSON-serializable dict summarizing GPU state."""
    gpus = detect_gpus(force_refresh=True, log=False)

    if not gpus:
        return {
            "nvidia_detected": False,
            "gpu_name": None,
            "cuda_version": None,
            "vram_total_gb": None,
            "cuda_compatible": False,
            "gpus": [],
            "recommended_device": None,
            "recommended_gpu_index": None,
        }

    compatible = [gpu for gpu in gpus if gpu.cuda_compatible]
    candidates = compatible or gpus
    recommended = max(candidates, key=lambda gpu: gpu.vram_free_mb) if candidates else None
    first_gpu = gpus[0]
    return {
        "nvidia_detected": True,
        "gpu_name": first_gpu.name,
        "cuda_version": first_gpu.driver_cuda_version,
        "vram_total_gb": round(first_gpu.vram_total_mb / 1024, 1),
        "cuda_compatible": first_gpu.cuda_compatible,
        "gpus": [_gpu_to_dict(gpu) for gpu in gpus],
        "recommended_device": f"cuda:{recommended.index}" if recommended else None,
        "recommended_gpu_index": recommended.index if recommended else None,
    }
