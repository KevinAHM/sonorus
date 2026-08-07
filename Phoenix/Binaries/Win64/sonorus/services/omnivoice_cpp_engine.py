"""
OmniVoice.cpp TTS Engine

Runs OmniVoice inference through omnivoice.cpp (ggml/Vulkan) in a subprocess,
isolating the native DLL from the main Flask server.  Follows the same
process-manager pattern as omnivoice_engine.py / pocket_tts_onnx.py, but with
no torch anywhere: the worker talks to omnivoice.dll via ctypes, so it runs
on any Vulkan GPU (or CPU) without CUDA.

The ctypes structs below mirror omnivoice.h (OV_ABI_VERSION 5) field for
field.  Defaults are always populated via ov_init_default_params_v5 /
ov_tts_default_params_v5 rather than hand-filled.
"""
import ctypes as C
import gc
import hashlib
import json
import os
import queue as _queue_mod
import shutil
import subprocess
import sys
import tempfile
import time
import threading
import traceback
import wave
import zipfile
import multiprocessing as mp
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import numpy as np
import soundfile as sf

try:
    from huggingface_hub import hf_hub_download
except ImportError:
    hf_hub_download = None

from services.omnivoice_engine import (
    VOICE_DIR,
    _resolve_voice,
    ensure_voice_reference_transcript,
)
from services.omnivoice_token_cache import (
    load_omnivoice_token_cache,
    save_omnivoice_token_cache,
)
from services.omnivoice_text import preprocess_text as omni_preprocess_text
from utils.settings import load_settings

# ============================================================================
# Constants
# ============================================================================

HF_REPO_ID = "Serveurperso/OmniVoice-GGUF"
HF_REVISION = "361609388ae572a820d085185bbbe2a2aac4b30e"
# The AudioVAE is produced separately from the two upstream OmniVoice GGUFs.
# Environment overrides keep development/private mirrors testable without
# changing the distributed source.
UPSCALER_HF_REPO_ID = os.environ.get(
    "SONORUS_OMNIVOICE_UPSCALER_REPO",
    "Jrjy3/sonorus-omnivoice",
)
UPSCALER_HF_REVISION = os.environ.get(
    "SONORUS_OMNIVOICE_UPSCALER_REVISION",
    "cdcb598972c2f43e3d668d9152e35f3ecd9e8ad1",
)
UPSCALER_URL = os.environ.get(
    "SONORUS_OMNIVOICE_UPSCALER_URL",
    "https://github.com/Jrjy3/omnivoice.cpp/releases/download/"
    "voxcpm2-audiovae-v1/voxcpm2-audiovae-f16.gguf",
)
SAMPLE_RATE = 24_000  # OmniVoice native sample rate (codec output)
OUTPUT_SAMPLE_RATE = 48_000  # VoxCPM2 AudioVAE upscaled output
OV_ABI_VERSION = 5
OV_ENCODER_EAGER = 0
OV_ENCODER_LAZY = 1
OV_ENCODER_ON_DEMAND = 2

_SONORUS_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_ROOT = _SONORUS_ROOT / "omnivoice_cpp"
BIN_DIR = RUNTIME_ROOT / "bin"
LICENSE_DIR = RUNTIME_ROOT / "licenses"
MODEL_DIR = RUNTIME_ROOT / "models"

RUNTIME_VERSION = "1.1.0"
RUNTIME_RELEASE_TAG = f"sonorus-runtime-v{RUNTIME_VERSION}"
RUNTIME_ARCHIVE_FILENAME = (
    f"omnivoice-runtime-windows-x64-avx2-vulkan-v{RUNTIME_VERSION}.zip"
)
RUNTIME_URL = (
    "https://github.com/Jrjy3/omnivoice.cpp/releases/download/"
    f"{RUNTIME_RELEASE_TAG}/{RUNTIME_ARCHIVE_FILENAME}"
)
RUNTIME_ARCHIVE_EXPECTED_BYTES = 16_389_466
RUNTIME_ARCHIVE_SHA256 = "e8ed705fc441fe8276f50c42adf701f7f86d874be83d6681efe530582cd3773c"
RUNTIME_FILE_METADATA = {
    "bin/omnivoice.dll": (401_408, "7de3ca56fc60f6d40b6843c23205ff0f21c19efae090fbf05fe79fb1461035c5"),
    "bin/ggml.dll": (68_608, "d792fc740e16ade5c9486df2a3f6eb4d33f50442bb820773d4a651353d55308c"),
    "bin/ggml-base.dll": (666_112, "d11206789750a5dbb3a1342c5d8b42fa68a36c23386b7e8d6847b800e5a0b441"),
    "bin/ggml-cpu.dll": (812_032, "91446374398d2d86c3440a5d4c9089bbf239568317faa1b57edd098db074d97a"),
    "bin/ggml-vulkan.dll": (50_652_160, "a3b6d5da334555c9e6d4a3fd9a8d2770a81586eb0e6216a60a58c4cbb1032079"),
    "licenses/ggml.LICENSE": (1_099, "bcd8ec749126d45cb06737d0690295d73df4b6e7e194205bcf91190368f27285"),
    "licenses/omnivoice.cpp.LICENSE": (1_087, "cddbecd5db98ec5bd44af3e7221b6d44c3e9fed1c92940a3be5b0080c9b86475"),
}
RUNTIME_ARCHIVE_MEMBERS = {
    "omnivoice.dll": "bin/omnivoice.dll",
    "ggml.dll": "bin/ggml.dll",
    "ggml-base.dll": "bin/ggml-base.dll",
    "ggml-cpu.dll": "bin/ggml-cpu.dll",
    "ggml-vulkan.dll": "bin/ggml-vulkan.dll",
    "ggml.LICENSE": "licenses/ggml.LICENSE",
    "omnivoice.cpp.LICENSE": "licenses/omnivoice.cpp.LICENSE",
}

MODEL_FILENAME = "omnivoice-base-Q8_0.gguf"
TOKENIZER_FILENAME = "omnivoice-tokenizer-F32.gguf"
UPSCALER_FILENAME = "voxcpm2-audiovae-f16.gguf"
UPSCALER_EXPECTED_BYTES = 187_868_032
UPSCALER_SHA256 = "a5fb091c0a95172bdee2ee7230335dac7d3dc318d77ca100f095d023cabd5d97"
MODEL_FILE_METADATA = {
    MODEL_FILENAME: (656_395_008, "2882d887921798aea13d45236556bdf8012842ab6f8cd2690943eead6289f298"),
    TOKENIZER_FILENAME: (734_300_704, "83820c6316da023076af7c1d06de5e38dcd09ae9f42203675bf8b3bd9a58e330"),
    UPSCALER_FILENAME: (UPSCALER_EXPECTED_BYTES, UPSCALER_SHA256),
}
_DLL_NAMES = ("omnivoice.dll",)
RUNTIME_DLL_FILENAMES = (
    "ggml.dll",
    "ggml-base.dll",
    "ggml-cpu.dll",
    "ggml-vulkan.dll",
)
_model_validation_cache: dict[tuple[str, int, int, int], bool] = {}
_model_validation_lock = threading.Lock()
_runtime_abi_cache: dict[tuple, Optional[str]] = {}
_runtime_abi_lock = threading.Lock()
_runtime_file_validation_cache: dict[tuple[str, int, int, int], bool] = {}
_runtime_file_validation_lock = threading.Lock()

_ABI_PROBE_SNIPPET = r"""
import ctypes, json, os, sys
bin_dir, dll_path, expected_abi = sys.argv[1], sys.argv[2], int(sys.argv[3])
try:
    dll_dir_handle = None
    if hasattr(os, "add_dll_directory"):
        dll_dir_handle = os.add_dll_directory(bin_dir)
    os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
    lib = ctypes.CDLL(dll_path)
    values = {}
    for symbol, key in (
        ("ov_init_default_params_v5", "init_abi"),
        ("ov_tts_default_params_v5", "tts_abi"),
    ):
        try:
            fn = getattr(lib, symbol)
        except AttributeError:
            raise RuntimeError("missing required export " + symbol)
        fn.restype = None
        fn.argtypes = [ctypes.c_void_p]
        buffer = ctypes.create_string_buffer(4096)
        fn(ctypes.cast(buffer, ctypes.c_void_p))
        values[key] = ctypes.c_int.from_buffer(buffer).value
    if values["init_abi"] != expected_abi or values["tts_abi"] != expected_abi:
        raise RuntimeError(
            "default parameter ABI mismatch: init={init_abi}, tts={tts_abi}, expected={expected}".format(
                expected=expected_abi, **values
            )
        )
    print(json.dumps(values))
except Exception as exc:
    print(json.dumps({"error": str(exc)}))
    raise SystemExit(2)
"""


def _serialized_worker_io(func):
    def wrapper(self, *args, **kwargs):
        with self._synthesis_lock:
            return func(self, *args, **kwargs)
    return wrapper


# ============================================================================
# Availability checks (main process — lightweight, no DLL load)
# ============================================================================

def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().lower()


def _runtime_relative_path(path: Path) -> Optional[str]:
    try:
        return path.resolve().relative_to(RUNTIME_ROOT.resolve()).as_posix()
    except (OSError, ValueError):
        if path.parent.name == "bin":
            return f"bin/{path.name}"
        if path.parent.name == "licenses":
            return f"licenses/{path.name}"
        return None


def _is_valid_runtime_file(path: Path, relative_path: Optional[str] = None) -> bool:
    """Validate an official runtime file by exact size and SHA-256."""
    try:
        relative_path = relative_path or _runtime_relative_path(path)
        expected = RUNTIME_FILE_METADATA.get(relative_path or "")
        if expected is None or not path.is_file():
            return False
        file_stat = path.stat()
        cache_key = (str(path.resolve()), file_stat.st_size,
                     file_stat.st_ctime_ns, file_stat.st_mtime_ns)
        if file_stat.st_size != expected[0]:
            return False
        with _runtime_file_validation_lock:
            cached = _runtime_file_validation_cache.get(cache_key)
            if cached is not None:
                return cached
            valid = _sha256_file(path) == expected[1]
            refreshed_stat = path.stat()
            refreshed_key = (
                str(path.resolve()),
                refreshed_stat.st_size,
                refreshed_stat.st_ctime_ns,
                refreshed_stat.st_mtime_ns,
            )
            if refreshed_key != cache_key:
                return False
            for old_key in list(_runtime_file_validation_cache):
                if old_key[0] == cache_key[0]:
                    _runtime_file_validation_cache.pop(old_key, None)
            _runtime_file_validation_cache[cache_key] = valid
            return valid
    except OSError:
        return False


def _is_valid_runtime_dll(path: Path, relative_path: Optional[str] = None) -> bool:
    """Reject missing, altered, truncated, or unexpanded runtime DLLs."""
    if not _is_valid_runtime_file(path, relative_path):
        return False
    try:
        with path.open("rb") as dll_file:
            return dll_file.read(2) == b"MZ"
    except OSError:
        return False


def _find_dll(bin_dir: Optional[Path] = None) -> Optional[Path]:
    """Locate the pinned omnivoice DLL in BIN_DIR."""
    bin_dir = bin_dir or BIN_DIR
    for name in _DLL_NAMES:
        candidate = bin_dir / name
        if _is_valid_runtime_dll(candidate, f"bin/{name}"):
            return candidate
    return None


def dll_present() -> bool:
    """Check if the omnivoice.cpp DLL has been installed."""
    return _find_dll() is not None


def _runtime_dll_identity(bin_dir: Optional[Path] = None) -> tuple:
    """Return identities for every DLL that can affect ABI/load readiness."""
    bin_dir = bin_dir or BIN_DIR
    identities = []
    for name in _DLL_NAMES + RUNTIME_DLL_FILENAMES:
        path = bin_dir / name
        try:
            file_stat = path.stat()
            identity = (str(path.resolve()), file_stat.st_size,
                        file_stat.st_ctime_ns, file_stat.st_mtime_ns)
        except OSError:
            identity = (str(path.absolute()), None, None, None)
        identities.append(identity)
    return tuple(identities)


def _run_runtime_abi_probe(dll_path: Path, bin_dir: Path) -> tuple[Optional[str], bool]:
    """Run the ABI probe, returning (error, safe_to_cache)."""
    cacheable = False
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                _ABI_PROBE_SNIPPET,
                str(bin_dir),
                str(dll_path),
                str(OV_ABI_VERSION),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return "ABI readiness probe timed out", cacheable
    except Exception as exc:
        return f"ABI readiness probe could not start: {exc}", cacheable

    payload = None
    for line in reversed(result.stdout.splitlines()):
        try:
            candidate = json.loads(line)
        except ValueError:
            continue
        if isinstance(candidate, dict):
            payload = candidate
            break
    if result.returncode != 0:
        detail = payload.get("error") if payload else None
        if not detail:
            detail = (result.stderr or result.stdout or "no diagnostic output").strip()[:300]
        error = f"ABI readiness probe failed (exit code {result.returncode}): {detail}"
        cacheable = bool(
            result.returncode == 2
            and isinstance(detail, str)
            and (
                detail.startswith("missing required export ")
                or detail.startswith("default parameter ABI mismatch:")
            )
        )
    elif payload is None:
        error = "ABI readiness probe returned no result"
    elif payload.get("init_abi") != OV_ABI_VERSION or payload.get("tts_abi") != OV_ABI_VERSION:
        error = (
            "ABI readiness probe reported incompatible defaults "
            f"(init={payload.get('init_abi')}, tts={payload.get('tts_abi')}; "
            f"required={OV_ABI_VERSION})"
        )
        cacheable = True
    else:
        error = None
        cacheable = True
    return error, cacheable


def _probe_runtime_abi(dll_path: Path, bin_dir: Optional[Path] = None) -> Optional[str]:
    """Return None for an ABI-v5 runtime, otherwise a user-facing error."""
    bin_dir = bin_dir or BIN_DIR
    if bin_dir.resolve() != BIN_DIR.resolve():
        return _run_runtime_abi_probe(dll_path, bin_dir)[0]

    with _runtime_abi_lock:
        cache_key = _runtime_dll_identity(bin_dir)
        if cache_key in _runtime_abi_cache:
            return _runtime_abi_cache[cache_key]
        error, cacheable = _run_runtime_abi_probe(dll_path, bin_dir)
        if cacheable:
            _runtime_abi_cache.clear()
            _runtime_abi_cache[cache_key] = error
        return error


def missing_runtime_files() -> list[str]:
    """Return missing runtime DLLs or a meaningful ABI incompatibility."""
    missing = [
        name for name in RUNTIME_DLL_FILENAMES
        if not _is_valid_runtime_dll(BIN_DIR / name, f"bin/{name}")
    ]
    dll_path = _find_dll()
    if dll_path is None:
        missing.insert(0, _DLL_NAMES[0])
    if missing:
        return missing

    abi_error = _probe_runtime_abi(dll_path)
    if abi_error:
        return [f"{dll_path.name} ({abi_error})"]
    return []


def runtime_present() -> bool:
    """Check that the OmniVoice library and every downloaded ggml DLL are ABI-ready."""
    return not missing_runtime_files()


def _model_file_identity(path: Path) -> tuple[str, int, int, int]:
    file_stat = path.stat()
    return (
        str(path.resolve()),
        file_stat.st_size,
        file_stat.st_ctime_ns,
        file_stat.st_mtime_ns,
    )


def _is_valid_model_file(path: Path, filename: Optional[str] = None) -> bool:
    """Verify one pinned GGUF asset by exact size and SHA-256."""
    try:
        filename = filename or path.name
        expected = MODEL_FILE_METADATA.get(filename)
        if expected is None or not path.is_file():
            return False
        with _model_validation_lock:
            cache_key = _model_file_identity(path)
            if cache_key[1] != expected[0]:
                return False
            cached = _model_validation_cache.get(cache_key)
            if cached is not None:
                return cached

            valid = _sha256_file(path) == expected[1]
            if _model_file_identity(path) != cache_key:
                return False
            for old_key in list(_model_validation_cache):
                if old_key[0] == cache_key[0]:
                    _model_validation_cache.pop(old_key, None)
            _model_validation_cache[cache_key] = valid
            return valid
    except OSError:
        return False


def _is_valid_upscaler_model(path: Path) -> bool:
    """Compatibility wrapper for the separately hosted AudioVAE validator."""
    return _is_valid_model_file(path, UPSCALER_FILENAME)


def model_file_ready(filename: str) -> bool:
    """Return whether one expected model file is usable by this integration."""
    return _is_valid_model_file(MODEL_DIR / filename, filename)


def models_present() -> bool:
    """Check if all inference and 48 kHz upscaler GGUF models are present."""
    return (
        model_file_ready(MODEL_FILENAME)
        and model_file_ready(TOKENIZER_FILENAME)
        and model_file_ready(UPSCALER_FILENAME)
    )


def is_available() -> bool:
    """Check if the omnivoice.cpp backend can be started (DLL + models)."""
    return runtime_present() and models_present()


# ============================================================================
# Runtime and model download
# ============================================================================

def _download_verified_file(
    url: str,
    destination: Path,
    expected_bytes: int,
    expected_sha256: str,
) -> None:
    """Download a resumable release asset and verify its exact identity."""
    if destination.is_file():
        if destination.stat().st_size == expected_bytes and _sha256_file(destination) == expected_sha256:
            return
        destination.unlink()

    partial = destination.with_name(destination.name + ".incomplete")
    offset = partial.stat().st_size if partial.is_file() else 0
    if offset > expected_bytes:
        partial.unlink()
        offset = 0

    headers = {"User-Agent": "Sonorus-OmniVoiceCpp-Installer"}
    if offset:
        headers["Range"] = f"bytes={offset}-"

    try:
        response = urlopen(Request(url, headers=headers), timeout=60)
    except HTTPError as exc:
        if exc.code == 416 and offset:
            if partial.stat().st_size == expected_bytes and _sha256_file(partial) == expected_sha256:
                partial.replace(destination)
                return
            partial.unlink(missing_ok=True)
            return _download_verified_file(url, destination, expected_bytes, expected_sha256)
        raise

    with response:
        status = getattr(response, "status", response.getcode())
        append = bool(offset and status == 206)
        with partial.open("ab" if append else "wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)

    actual_bytes = partial.stat().st_size
    if actual_bytes != expected_bytes:
        raise RuntimeError(
            f"Incomplete download for {destination.name}: "
            f"received {actual_bytes} of {expected_bytes} bytes"
        )
    actual_sha256 = _sha256_file(partial)
    if actual_sha256 != expected_sha256:
        partial.unlink(missing_ok=True)
        raise RuntimeError(
            f"Integrity check failed for {destination.name}; expected SHA-256 "
            f"{expected_sha256}, received {actual_sha256}"
        )
    partial.replace(destination)


def _validate_runtime_archive(archive_path: Path, staging_root: Path) -> None:
    """Validate and extract the exact allowlisted runtime archive."""
    expected_paths = {"RUNTIME-MANIFEST.json", *RUNTIME_ARCHIVE_MEMBERS}
    try:
        with zipfile.ZipFile(archive_path) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)) or set(names) != expected_paths:
                raise RuntimeError("Runtime archive contains unexpected or missing files")

            manifest = json.loads(archive.read("RUNTIME-MANIFEST.json"))
            if (
                manifest.get("schema_version") != 1
                or manifest.get("version") != RUNTIME_VERSION
                or manifest.get("platform") != "windows-x64"
                or manifest.get("cpu_baseline") != "AVX2"
                or manifest.get("gpu_backend") != "Vulkan"
            ):
                raise RuntimeError("Runtime archive manifest is incompatible with this Sonorus build")

            cmake = manifest.get("cmake")
            if (
                not isinstance(cmake, dict)
                or cmake.get("GGML_NATIVE") is not False
                or cmake.get("GGML_AVX2") is not True
                or cmake.get("GGML_AVX512") is not False
                or cmake.get("GGML_AVX512_BF16") is not False
                or cmake.get("GGML_AVX512_VBMI") is not False
                or cmake.get("GGML_AVX512_VNNI") is not False
                or cmake.get("GGML_AVX_VNNI") is not False
                or cmake.get("GGML_VULKAN") is not True
            ):
                raise RuntimeError("Runtime archive does not declare the required AVX2/Vulkan build")

            manifest_files = manifest.get("files")
            expected_dlls = {
                archive_name for archive_name in RUNTIME_ARCHIVE_MEMBERS
                if archive_name.endswith(".dll")
            }
            if not isinstance(manifest_files, dict) or set(manifest_files) != expected_dlls:
                raise RuntimeError("Runtime archive manifest has an unexpected file list")

            for archive_name, relative_path in RUNTIME_ARCHIVE_MEMBERS.items():
                expected_bytes, expected_sha256 = RUNTIME_FILE_METADATA[relative_path]
                if archive_name.endswith(".dll"):
                    record = manifest_files.get(archive_name, {})
                    if (
                        record.get("size") != expected_bytes
                        or record.get("sha256") != expected_sha256
                    ):
                        raise RuntimeError(
                            f"Runtime manifest metadata mismatch for {archive_name}"
                        )
                if archive.getinfo(archive_name).file_size != expected_bytes:
                    raise RuntimeError(
                        f"Runtime archive size metadata mismatch for {archive_name}"
                    )
                destination = staging_root / Path(relative_path)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(archive_name) as source, destination.open("wb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                if not _is_valid_runtime_file(destination, relative_path):
                    raise RuntimeError(f"Runtime file integrity check failed for {relative_path}")
    except (zipfile.BadZipFile, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invalid OmniVoice runtime archive: {exc}") from exc

    staged_dll = _find_dll(staging_root / "bin")
    if staged_dll is None:
        raise RuntimeError("Runtime archive is missing a valid omnivoice.dll")
    abi_error = _probe_runtime_abi(staged_dll, staging_root / "bin")
    if abi_error:
        raise RuntimeError(f"Downloaded OmniVoice runtime is not ABI-compatible: {abi_error}")


def _activate_staged_runtime(staging_root: Path) -> None:
    """Replace the runtime only after every staged file and ABI check passes."""
    unload()
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)

    staged_bin = staging_root / "bin"
    previous_bin = staging_root / "previous-bin"
    had_previous_bin = BIN_DIR.exists()
    if had_previous_bin:
        BIN_DIR.replace(previous_bin)
    try:
        staged_bin.replace(BIN_DIR)
    except Exception:
        if had_previous_bin and previous_bin.exists() and not BIN_DIR.exists():
            previous_bin.replace(BIN_DIR)
        raise

    LICENSE_DIR.mkdir(parents=True, exist_ok=True)
    for relative_path in RUNTIME_FILE_METADATA:
        if not relative_path.startswith("licenses/"):
            continue
        staged_license = staging_root / Path(relative_path)
        staged_license.replace(LICENSE_DIR / staged_license.name)

    with _runtime_abi_lock:
        _runtime_abi_cache.clear()
    with _runtime_file_validation_lock:
        _runtime_file_validation_cache.clear()


def download_runtime(progress_cb=None) -> None:
    """Install the pinned OmniVoice/ggml Windows runtime from GitHub Releases."""
    if runtime_present():
        if progress_cb:
            progress_cb(1, 1, f"OmniVoice runtime {RUNTIME_VERSION} is already installed")
        return

    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    archive_path = RUNTIME_ROOT / RUNTIME_ARCHIVE_FILENAME
    if progress_cb:
        progress_cb(0, 1, f"Downloading OmniVoice runtime {RUNTIME_VERSION}...")
    _download_verified_file(
        RUNTIME_URL,
        archive_path,
        RUNTIME_ARCHIVE_EXPECTED_BYTES,
        RUNTIME_ARCHIVE_SHA256,
    )

    with tempfile.TemporaryDirectory(prefix=".runtime-staging-", dir=RUNTIME_ROOT) as temp_dir:
        staging_root = Path(temp_dir)
        if progress_cb:
            progress_cb(0, 1, "Verifying OmniVoice runtime...")
        _validate_runtime_archive(archive_path, staging_root)
        _activate_staged_runtime(staging_root)

    if not runtime_present():
        raise RuntimeError("OmniVoice runtime failed its post-install readiness check")
    archive_path.unlink(missing_ok=True)
    if progress_cb:
        progress_cb(1, 1, f"OmniVoice runtime {RUNTIME_VERSION} ready")


def install_dependencies(progress_cb=None) -> None:
    """Install the verified native runtime and all three required GGUF models."""
    total = 4

    def runtime_progress(current, _total, message):
        if progress_cb:
            progress_cb(current, total, message)

    def model_progress(current, _total, message):
        if progress_cb:
            progress_cb(current + 1, total, message)

    download_runtime(runtime_progress)
    download_models(model_progress)
    if progress_cb:
        progress_cb(total, total, "OmniVoice (Vulkan) is ready")


def download_models(progress_cb=None):
    """
    Download the three GGUF models from their configured sources into MODEL_DIR.

    Args:
        progress_cb: Optional callback(current, total, message) for progress.
    """
    if hf_hub_download is None:
        raise RuntimeError(
            "huggingface_hub is required to download the OmniVoice GGUF models. "
            "Install it with: pip install huggingface_hub"
        )

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    total = len(MODEL_FILE_METADATA)

    for index, filename in enumerate((MODEL_FILENAME, TOKENIZER_FILENAME)):
        if progress_cb:
            progress_cb(index, total, f"Downloading {filename}...")
        model_path = MODEL_DIR / filename
        if _is_valid_model_file(model_path, filename):
            print(f"[OmniVoiceCpp] Model already present: {filename}")
            continue
        force_download = model_path.is_file()
        if force_download:
            print(f"[OmniVoiceCpp] Removing invalid {filename}")
            model_path.unlink()
        print(f"[OmniVoiceCpp] Downloading {filename} from {HF_REPO_ID}...")
        hf_hub_download(
            repo_id=HF_REPO_ID,
            filename=filename,
            revision=HF_REVISION,
            local_dir=str(MODEL_DIR),
            force_download=force_download,
        )
        if not _is_valid_model_file(model_path, filename) and not force_download:
            model_path.unlink(missing_ok=True)
            print(f"[OmniVoiceCpp] Retrying {filename} without the local Hugging Face cache...")
            hf_hub_download(
                repo_id=HF_REPO_ID,
                filename=filename,
                revision=HF_REVISION,
                local_dir=str(MODEL_DIR),
                force_download=True,
            )
        if not _is_valid_model_file(model_path, filename):
            model_path.unlink(missing_ok=True)
            expected_bytes, expected_sha256 = MODEL_FILE_METADATA[filename]
            raise RuntimeError(
                f"Integrity check failed for {filename}; expected "
                f"{expected_bytes} bytes and SHA-256 {expected_sha256}"
            )
        print(f"[OmniVoiceCpp] Downloaded {filename}")

    upscaler_path = MODEL_DIR / UPSCALER_FILENAME
    if progress_cb:
        progress_cb(2, total, f"Downloading {UPSCALER_FILENAME}...")
    if _is_valid_upscaler_model(upscaler_path):
        print(f"[OmniVoiceCpp] Model already present: {UPSCALER_FILENAME}")
    else:
        if upscaler_path.is_file():
            print(f"[OmniVoiceCpp] Removing invalid {UPSCALER_FILENAME}")
            upscaler_path.unlink()
        if UPSCALER_HF_REPO_ID:
            print(f"[OmniVoiceCpp] Downloading {UPSCALER_FILENAME} "
                  f"from {UPSCALER_HF_REPO_ID}...")
            hf_hub_download(
                repo_id=UPSCALER_HF_REPO_ID,
                filename=UPSCALER_FILENAME,
                revision=UPSCALER_HF_REVISION,
                local_dir=str(MODEL_DIR),
                force_download=True,
            )
            print(f"[OmniVoiceCpp] Downloaded {UPSCALER_FILENAME}")
        elif UPSCALER_URL:
            _download_upscaler_url(UPSCALER_URL, upscaler_path)
        else:
            raise RuntimeError(
                "The VoxCPM2 AudioVAE download source is not configured. Set "
                "UPSCALER_HF_REPO_ID or UPSCALER_URL before packaging, or set "
                "SONORUS_OMNIVOICE_UPSCALER_REPO/SONORUS_OMNIVOICE_UPSCALER_URL "
                "for a development install."
            )

    if not _is_valid_upscaler_model(upscaler_path):
        upscaler_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"Integrity check failed for {UPSCALER_FILENAME}; expected "
            f"{UPSCALER_EXPECTED_BYTES} bytes and SHA-256 {UPSCALER_SHA256}"
        )

    if progress_cb:
        progress_cb(total, total, "OmniVoice.cpp models ready")
    print(f"[OmniVoiceCpp] Models ready in {MODEL_DIR}")


def _download_upscaler_url(url: str, destination: Path) -> None:
    """Download and verify the pinned direct-release AudioVAE asset."""
    print(f"[OmniVoiceCpp] Downloading {destination.name} from release asset...")
    expected_bytes, expected_sha256 = MODEL_FILE_METADATA[UPSCALER_FILENAME]
    _download_verified_file(
        url,
        destination,
        expected_bytes,
        expected_sha256,
    )
    print(f"[OmniVoiceCpp] Downloaded {destination.name}")


# ============================================================================
# ctypes ABI (mirrors omnivoice.h, OV_ABI_VERSION 5)
# ============================================================================

# enum ov_status
OV_STATUS_OK = 0
OV_STATUS_INVALID_PARAMS = -1
OV_STATUS_INSTRUCT_INVALID = -2
OV_STATUS_GENERATE_FAILED = -3
OV_STATUS_OOM = -4
OV_STATUS_CANCELLED = -5


class OvAudio(C.Structure):
    """struct ov_audio — mono float PCM output buffer, freed by ov_audio_free."""
    _fields_ = [
        ("samples", C.POINTER(C.c_float)),
        ("n_samples", C.c_int),
        ("sample_rate", C.c_int),
        ("channels", C.c_int),
    ]


class OvInitParams(C.Structure):
    """struct ov_init_params — populate via ov_init_default_params."""
    _fields_ = [
        ("abi_version", C.c_int),
        ("model_path", C.c_char_p),
        ("codec_path", C.c_char_p),
        ("use_fa", C.c_bool),
        ("clamp_fp16", C.c_bool),
        ("upscaler_path", C.c_char_p),
        ("encoder_mode", C.c_int),
    ]


# typedef bool (*ov_cancel_cb)(void * user_data);
OvCancelCb = C.CFUNCTYPE(C.c_bool, C.c_void_p)
# typedef bool (*ov_audio_chunk_cb)(const float * samples, int n_samples, void * user_data);
OvAudioChunkCb = C.CFUNCTYPE(C.c_bool, C.POINTER(C.c_float), C.c_int, C.c_void_p)


class OvTtsParams(C.Structure):
    """struct ov_tts_params — populate via ov_tts_default_params."""
    _fields_ = [
        ("abi_version", C.c_int),
        ("text", C.c_char_p),
        ("lang", C.c_char_p),
        ("instruct", C.c_char_p),
        ("T_override", C.c_int),
        ("chunk_duration_sec", C.c_float),
        ("chunk_threshold_sec", C.c_float),
        ("denoise", C.c_bool),
        ("preprocess_prompt", C.c_bool),
        ("mg_num_step", C.c_int),
        ("mg_guidance_scale", C.c_float),
        ("mg_t_shift", C.c_float),
        ("mg_layer_penalty_factor", C.c_float),
        ("mg_position_temperature", C.c_float),
        ("mg_class_temperature", C.c_float),
        ("mg_seed", C.c_uint64),
        ("ref_audio_tokens", C.POINTER(C.c_int32)),
        ("ref_T", C.c_int),
        ("ref_audio_24k", C.POINTER(C.c_float)),
        ("ref_n_samples", C.c_int),
        ("ref_text", C.c_char_p),
        ("dump_dir", C.c_char_p),
        ("cancel", OvCancelCb),
        ("cancel_user_data", C.c_void_p),
        ("on_chunk", OvAudioChunkCb),
        ("on_chunk_user_data", C.c_void_p),
        ("postproc", C.c_bool),
    ]


class OvVoiceRef(C.Structure):
    """struct ov_voice_ref — RVQ codes [num_codebooks, ref_T], freed by ov_voice_ref_free."""
    _fields_ = [
        ("ref_codes", C.POINTER(C.c_int32)),
        ("ref_T", C.c_int),
        ("num_codebooks", C.c_int),
    ]


def _bind_dll(lib):
    """Declare argtypes/restypes for every ov_* entry we use."""
    lib.ov_version.restype = C.c_char_p
    lib.ov_version.argtypes = []
    lib.ov_last_error.restype = C.c_char_p
    lib.ov_last_error.argtypes = []
    try:
        init_defaults_v5 = lib.ov_init_default_params_v5
    except AttributeError as exc:
        raise RuntimeError(
            "The installed omnivoice.dll does not expose ov_init_default_params_v5; "
            "install the ABI v5 runtime required for encoder unloading"
        ) from exc
    init_defaults_v5.restype = None
    init_defaults_v5.argtypes = [C.POINTER(OvInitParams)]
    try:
        tts_defaults_v5 = lib.ov_tts_default_params_v5
    except AttributeError as exc:
        raise RuntimeError(
            "The installed omnivoice.dll does not expose ov_tts_default_params_v5; "
            "install the ABI v5 runtime required for encoder unloading"
        ) from exc
    tts_defaults_v5.restype = None
    tts_defaults_v5.argtypes = [C.POINTER(OvTtsParams)]
    lib.ov_init.restype = C.c_void_p          # opaque struct ov_context *
    lib.ov_init.argtypes = [C.POINTER(OvInitParams)]
    lib.ov_free.restype = None
    lib.ov_free.argtypes = [C.c_void_p]
    lib.ov_synthesize.restype = C.c_int       # enum ov_status
    lib.ov_synthesize.argtypes = [C.c_void_p, C.POINTER(OvTtsParams), C.POINTER(OvAudio)]
    lib.ov_audio_free.restype = None
    lib.ov_audio_free.argtypes = [C.POINTER(OvAudio)]
    lib.ov_extract_voice_ref.restype = C.c_int
    lib.ov_extract_voice_ref.argtypes = [C.c_void_p, C.POINTER(C.c_float), C.c_int, C.POINTER(OvVoiceRef)]
    lib.ov_voice_ref_free.restype = None
    lib.ov_voice_ref_free.argtypes = [C.POINTER(OvVoiceRef)]
    lib.ov_release_voice_encoder.restype = None
    lib.ov_release_voice_encoder.argtypes = [C.c_void_p]
    lib.ov_voice_encoder_bytes.restype = C.c_uint64
    lib.ov_voice_encoder_bytes.argtypes = [C.c_void_p]
    return lib


# ============================================================================
# Worker Process
# ============================================================================

def _omnivoice_cpp_worker_main(
    request_queue: mp.Queue,
    response_queue: mp.Queue,
    config: Dict[str, Any],
) -> None:
    """
    Subprocess entry-point.  All DLL work happens here.

    *config* keys:
        device, num_steps, guidance_scale, seed,
        bin_dir, model_path, codec_path, upscaler_path
    """
    try:

        # --------------------------------------------------------------
        # Device selection — GGML_BACKEND must be set BEFORE the DLL loads
        # (backend.h reads it at backend init: "Vulkan0", "CPU", ...).
        # --------------------------------------------------------------
        device = str(config.get("device", "auto")).strip()
        if device and device.lower() != "auto":
            os.environ["GGML_BACKEND"] = device
            print(f"[OmniVoiceCpp] Forcing GGML backend: {device}")
        else:
            # A launcher or another ggml feature may have set this in the
            # parent environment. "Auto" must genuinely restore ggml's own
            # device selection rather than inheriting that stale override.
            os.environ.pop("GGML_BACKEND", None)
            print("[OmniVoiceCpp] Auto-selecting best GGML backend")

        # --------------------------------------------------------------
        # DLL load (BIN_DIR also holds the dependent ggml DLLs)
        # --------------------------------------------------------------
        bin_dir = Path(config.get("bin_dir", str(BIN_DIR)))
        dll_dir_handle = os.add_dll_directory(str(bin_dir))
        os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")

        dll_path = None
        for name in _DLL_NAMES:
            candidate = bin_dir / name
            if candidate.is_file():
                dll_path = candidate
                break
        if dll_path is None:
            raise FileNotFoundError(f"omnivoice DLL not found in {bin_dir}")

        lib = _bind_dll(C.CDLL(str(dll_path)))
        print(f"[OmniVoiceCpp] Loaded {dll_path.name} "
              f"(version {lib.ov_version().decode('utf-8', 'replace')})")

        def _last_error() -> str:
            msg = lib.ov_last_error()
            return msg.decode("utf-8", "replace") if msg else "unknown error"

        # --------------------------------------------------------------
        # Context init (one ov_init for the worker's lifetime)
        # --------------------------------------------------------------
        model_path = str(config["model_path"]).encode("utf-8")
        codec_path = str(config["codec_path"]).encode("utf-8")
        upscaler_path = str(config["upscaler_path"]).encode("utf-8")

        init_params = OvInitParams()
        lib.ov_init_default_params_v5(C.byref(init_params))
        if init_params.abi_version != OV_ABI_VERSION:
            raise RuntimeError(
                "Incompatible omnivoice.dll ABI "
                f"({init_params.abi_version}; Sonorus requires {OV_ABI_VERSION} for 48 kHz upscaling)"
            )
        init_params.model_path = model_path
        init_params.codec_path = codec_path
        init_params.upscaler_path = upscaler_path
        # Keep the encoder out of VRAM until a reference actually needs
        # encoding. Batch preprocessing explicitly retains it across voices.
        init_params.encoder_mode = OV_ENCODER_LAZY

        print(f"[OmniVoiceCpp] Loading models from {config['model_path']}...")
        response_queue.put({"type": "loading", "message": "Loading OmniVoice and 48 kHz upscaler GGUF models..."})
        ctx = lib.ov_init(C.byref(init_params))
        if not ctx:
            raise RuntimeError(f"ov_init failed: {_last_error()}")
        print(
            "[OmniVoiceCpp] Model loaded "
            f"(voice encoder resident: {int(lib.ov_voice_encoder_bytes(ctx)):,} bytes)."
        )
    except Exception as exc:
        traceback.print_exc()
        response_queue.put({"type": "error", "error": str(exc)})
        return

    default_num_steps = int(config.get("num_steps", 32))
    default_guidance = float(config.get("guidance_scale", 2.0))
    default_seed = int(config.get("seed", 42))

    # ------------------------------------------------------------------
    # Reference audio decode (stdlib wave, soundfile fallback for non-PCM)
    # ------------------------------------------------------------------
    def _decode_ref_audio(path: str):
        """Decode a reference file to mono float32 PCM at 24 kHz."""
        data = None
        sr = None
        if path.lower().endswith(".wav"):
            try:
                with wave.open(path, "rb") as wf:
                    sr = wf.getframerate()
                    n_ch = wf.getnchannels()
                    sampwidth = wf.getsampwidth()
                    raw = wf.readframes(wf.getnframes())
                if sampwidth == 2:
                    data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
                elif sampwidth == 4:
                    data = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
                elif sampwidth == 1:
                    data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
                if data is not None and n_ch > 1:
                    data = data.reshape(-1, n_ch).mean(axis=1)
            except Exception as exc:
                print(f"[OmniVoiceCpp] wave decode failed for {os.path.basename(path)} "
                      f"({exc}); trying soundfile")
                data = None
        if data is None:
            data, sr = sf.read(path, dtype="float32")
            if data.ndim > 1:
                data = data.mean(axis=1)
        if sr != SAMPLE_RATE:
            n_out = max(1, int(round(len(data) * SAMPLE_RATE / sr)))
            data = np.interp(
                np.linspace(0.0, len(data) - 1.0, n_out),
                np.arange(len(data)),
                data,
            )
        return np.ascontiguousarray(data, dtype=np.float32)

    # ------------------------------------------------------------------
    # Voice reference cache (RVQ codes via ov_extract_voice_ref)
    # ------------------------------------------------------------------
    _voice_ref_cache: Dict[
        str,
        tuple[tuple[str, int, int], OvVoiceRef, Optional[np.ndarray]],
    ] = {}

    def _free_voice_ref(ref: OvVoiceRef, owner: Optional[np.ndarray]) -> None:
        """Free DLL-owned codes; NumPy-backed sidecar codes free themselves."""
        if owner is None:
            lib.ov_voice_ref_free(C.byref(ref))

    def _voice_file_identity(voice_path: str) -> tuple[str, int, int]:
        resolved = Path(voice_path).resolve()
        file_stat = resolved.stat()
        return (
            os.path.normcase(str(resolved)),
            file_stat.st_size,
            file_stat.st_mtime_ns,
        )

    def _get_voice_ref(
        voice_path: str,
        ref_text: Optional[str] = None,
        require_persist: bool = False,
    ) -> OvVoiceRef:
        """Encode (or fetch cached) RVQ reference codes for a voice file."""
        if require_persist and not str(ref_text or "").strip():
            raise ValueError(
                f"Cannot pretokenize {Path(voice_path).name} without a reference transcript"
            )
        identity = _voice_file_identity(voice_path)
        cache_key = identity[0]
        allow_disk_cache = True
        cached_record = _voice_ref_cache.get(cache_key)
        if cached_record is not None:
            cached_identity, cached_ref, cached_owner = cached_record
            if cached_identity == identity:
                # A previous request may have encoded this voice before its
                # transcript was available. Explicit preprocessing must still
                # persist those resident codes instead of returning early.
                token_path = Path(voice_path).with_suffix(".tokens.pt")
                persisted = False
                if require_persist and token_path.is_file():
                    try:
                        persisted_cache = load_omnivoice_token_cache(token_path)
                        persisted = bool(str(persisted_cache.get("ref_text") or "").strip())
                    except Exception:
                        persisted = False
                if require_persist and not persisted:
                    pcm = _decode_ref_audio(voice_path)
                    ref_rms = float(np.sqrt(np.mean(np.square(pcm, dtype=np.float64))))
                    if _voice_file_identity(voice_path) != identity:
                        raise RuntimeError(
                            f"Voice reference changed while persisting {token_path.name}"
                        )
                    native_codes = np.ctypeslib.as_array(
                        cached_ref.ref_codes,
                        shape=(cached_ref.num_codebooks * cached_ref.ref_T,),
                    ).reshape(cached_ref.num_codebooks, cached_ref.ref_T)
                    save_omnivoice_token_cache(
                        token_path,
                        native_codes,
                        ref_rms,
                        str(ref_text).strip(),
                    )
                    print(f"[OmniVoiceCpp] Saved pretokenized {token_path.name}")
                return cached_ref
            _free_voice_ref(cached_ref, cached_owner)
            _voice_ref_cache.pop(cache_key, None)
            allow_disk_cache = False
            print(f"[OmniVoiceCpp] Voice reference changed; rebuilding {Path(voice_path).name}")

        t_start = time.time()
        token_path = Path(voice_path).with_suffix(".tokens.pt")
        if allow_disk_cache and token_path.is_file():
            try:
                token_cache = load_omnivoice_token_cache(token_path)
                codes = token_cache["audio_codes"]
                ref = OvVoiceRef(
                    codes.ctypes.data_as(C.POINTER(C.c_int32)),
                    int(codes.shape[1]),
                    int(codes.shape[0]),
                )
                if _voice_file_identity(voice_path) != identity:
                    raise RuntimeError(
                        f"Voice reference changed while loading {token_path.name}"
                    )
                _voice_ref_cache[cache_key] = (identity, ref, codes)
                elapsed = (time.time() - t_start) * 1000
                print(
                    f"[OmniVoiceCpp] Loaded pretokenized {token_path.name} "
                    f"(ref_T={ref.ref_T}, {elapsed:.1f}ms)"
                )
                return ref
            except Exception as exc:
                print(
                    f"[OmniVoiceCpp] Could not load {token_path.name} ({exc}); "
                    "falling back to WAV encoding"
                )

        pcm = _decode_ref_audio(voice_path)
        ref_rms = float(np.sqrt(np.mean(np.square(pcm, dtype=np.float64))))
        ref = OvVoiceRef()
        rc = lib.ov_extract_voice_ref(
            ctx,
            pcm.ctypes.data_as(C.POINTER(C.c_float)),
            len(pcm),
            C.byref(ref),
        )
        if rc != OV_STATUS_OK:
            raise RuntimeError(f"ov_extract_voice_ref failed ({rc}): {_last_error()}")
        if _voice_file_identity(voice_path) != identity:
            lib.ov_voice_ref_free(C.byref(ref))
            raise RuntimeError(
                f"Voice reference changed while it was being encoded: {Path(voice_path).name}"
            )
        _voice_ref_cache[cache_key] = (identity, ref, None)
        if ref_text and str(ref_text).strip():
            try:
                native_codes = np.ctypeslib.as_array(
                    ref.ref_codes,
                    shape=(ref.num_codebooks * ref.ref_T,),
                ).reshape(ref.num_codebooks, ref.ref_T)
                save_omnivoice_token_cache(
                    token_path,
                    native_codes,
                    ref_rms,
                    str(ref_text).strip(),
                )
                print(f"[OmniVoiceCpp] Saved pretokenized {token_path.name}")
            except Exception as exc:
                if require_persist:
                    raise
                print(f"[OmniVoiceCpp] Could not save {token_path.name}: {exc}")
        elapsed = (time.time() - t_start) * 1000
        print(f"[OmniVoiceCpp] Encoded voice reference {Path(voice_path).name} "
              f"(ref_T={ref.ref_T}, {elapsed:.0f}ms)")
        return ref

    # ------------------------------------------------------------------
    # Startup complete
    # ------------------------------------------------------------------
    response_queue.put({"type": "ready"})
    voice_encoder_batch_active = False

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    while True:
        try:
            msg = request_queue.get()
        except Exception:
            break

        # None = shutdown
        if msg is None:
            break

        msg_type = msg.get("type")

        # ---- synthesize -----------------------------------------------
        if msg_type == "synthesize":
            try:
                text: str = msg["text"]
                if not text or not text.strip():
                    raise ValueError("OmniVoice synthesis text is empty")
                voice_path: str = msg["voice_path"]

                ref_text = msg.get("ref_text")
                ref = _get_voice_ref(voice_path, ref_text=ref_text)

                params = OvTtsParams()
                lib.ov_tts_default_params_v5(C.byref(params))
                if params.abi_version != OV_ABI_VERSION:
                    raise RuntimeError(
                        "Incompatible omnivoice.dll TTS ABI "
                        f"({params.abi_version}; Sonorus requires {OV_ABI_VERSION})"
                    )
                # Keep encoded bytes alive for the duration of the call
                text_b = text.encode("utf-8")
                params.text = text_b
                params.mg_num_step = int(msg.get("num_steps", default_num_steps))
                params.mg_guidance_scale = float(msg.get("guidance_scale", default_guidance))
                params.mg_seed = int(msg.get("seed", default_seed)) & 0xFFFFFFFFFFFFFFFF
                params.ref_audio_tokens = ref.ref_codes
                params.ref_T = ref.ref_T
                ref_text_b = ref_text.encode("utf-8") if ref_text else None
                if ref_text_b is not None:
                    params.ref_text = ref_text_b

                out = OvAudio()
                t_start = time.time()
                rc = lib.ov_synthesize(ctx, C.byref(params), C.byref(out))
                if rc != OV_STATUS_OK:
                    raise RuntimeError(f"ov_synthesize failed ({rc}): {_last_error()}")
                try:
                    if not out.samples or out.n_samples <= 0:
                        raise ValueError(
                            "OmniVoice generated empty audio "
                            f"(text_len={len(text)}, voice={Path(voice_path).name})"
                        )
                    output_sample_rate = int(out.sample_rate)
                    output_channels = int(out.channels)
                    if output_sample_rate != OUTPUT_SAMPLE_RATE:
                        raise ValueError(
                            "OmniVoice returned an inconsistent sample rate "
                            f"({output_sample_rate} Hz; expected {OUTPUT_SAMPLE_RATE} Hz with the upscaler)"
                        )
                    if output_channels != 1:
                        raise ValueError(
                            f"OmniVoice returned {output_channels} channels; expected mono audio"
                        )
                    samples = np.ctypeslib.as_array(out.samples, shape=(out.n_samples,)).copy()
                finally:
                    lib.ov_audio_free(C.byref(out))

                # float PCM -> int16 little-endian bytes
                pcm_bytes = (samples * 32767.0).clip(-32768, 32767).astype("<i2").tobytes()
                if not pcm_bytes:
                    raise ValueError(
                        "OmniVoice produced no PCM bytes "
                        f"(text_len={len(text)}, voice={Path(voice_path).name})"
                    )

                elapsed = (time.time() - t_start) * 1000
                duration = len(pcm_bytes) / (2 * output_sample_rate)
                print(f"[OmniVoiceCpp] Synthesized {duration:.2f}s in {elapsed:.0f}ms")

                response_queue.put({
                    "type": "done",
                    "audio": pcm_bytes,
                    "sample_rate": output_sample_rate,
                })

            except Exception as exc:
                traceback.print_exc()
                response_queue.put({"type": "error", "error": str(exc)})
            finally:
                if not voice_encoder_batch_active:
                    lib.ov_release_voice_encoder(ctx)

        # ---- pretokenize (warm the RVQ code cache for a voice) ---------
        elif msg_type == "pretokenize":
            try:
                _get_voice_ref(
                    msg["voice_path"],
                    ref_text=msg.get("ref_text"),
                    require_persist=True,
                )
                response_queue.put({"type": "pretokenize_done", "success": True})
            except Exception as exc:
                traceback.print_exc()
                response_queue.put({"type": "pretokenize_done", "success": False, "error": str(exc)})
            finally:
                if not voice_encoder_batch_active:
                    lib.ov_release_voice_encoder(ctx)

        # ---- voice encoder batch lifecycle ---------------------------
        elif msg_type == "voice_encoder_batch_start":
            voice_encoder_batch_active = True
            response_queue.put({"type": "voice_encoder_batch_started"})

        elif msg_type == "voice_encoder_batch_end":
            voice_encoder_batch_active = False
            lib.ov_release_voice_encoder(ctx)
            response_queue.put({
                "type": "voice_encoder_batch_ended",
                "encoder_bytes": int(lib.ov_voice_encoder_bytes(ctx)),
            })

        # ---- clear_voice_prompt ----------------------------------------
        elif msg_type == "clear_voice_prompt":
            voice_path = msg.get("voice_path", "")
            cache_key = os.path.normcase(str(Path(voice_path).resolve()))
            removed_record = _voice_ref_cache.pop(cache_key, None)
            if removed_record is not None:
                _free_voice_ref(removed_record[1], removed_record[2])
                print(f"[OmniVoiceCpp] Cleared voice reference cache for {Path(voice_path).name}")
            response_queue.put({"type": "clear_done"})

        # ---- warmup ----------------------------------------------------
        elif msg_type == "warmup":
            try:
                voice_path = msg.get("voice_path")
                if voice_path:
                    _get_voice_ref(voice_path)
                response_queue.put({"type": "warmup_done"})
            except Exception as exc:
                response_queue.put({"type": "warmup_done", "error": str(exc)})
            finally:
                if not voice_encoder_batch_active:
                    lib.ov_release_voice_encoder(ctx)

        else:
            print(f"[OmniVoiceCpp] Unknown message type: {msg_type}")

    # Clean up on exit
    for _, cached_ref, cached_owner in _voice_ref_cache.values():
        _free_voice_ref(cached_ref, cached_owner)
    _voice_ref_cache.clear()
    lib.ov_free(ctx)
    print("[OmniVoiceCpp] Worker shutting down.")


# ============================================================================
# Process Manager
# ============================================================================

class OmniVoiceCppProcessManager:
    """Manages the omnivoice.cpp worker subprocess from the main process."""

    def __init__(self):
        self._process: Optional[mp.Process] = None
        self._request_queue: Optional[mp.Queue] = None
        self._response_queue: Optional[mp.Queue] = None
        self._lock = threading.Lock()
        self._synthesis_lock = threading.RLock()
        self._ready = False
        self._closed = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def ensure_started(self) -> bool:
        """Start the worker process if not already running. Returns True when ready."""
        with self._lock:
            # shutdown() permanently retires this manager generation. Without
            # this guard, a caller that captured the old singleton just before
            # unload() could start an orphan worker after it had been removed
            # from the module-level singleton.
            if self._closed:
                return False
            if self._process is not None and self._process.is_alive():
                return self._ready

            # Clean up dead process
            if self._process is not None:
                self._cleanup()

            if not is_available():
                print(f"[OmniVoiceCpp] Backend not available "
                      f"(dll_present={dll_present()}, models_present={models_present()})")
                return False

            print("[OmniVoiceCpp] Starting worker process...")

            ctx = mp.get_context("spawn")
            self._request_queue = ctx.Queue()
            self._response_queue = ctx.Queue()

            config = _get_omnivoice_cpp_config()

            self._process = ctx.Process(
                target=_omnivoice_cpp_worker_main,
                args=(self._request_queue, self._response_queue, config),
                daemon=True,
            )
            self._process.start()

            # Wait for ready signal (GGUF load is local, but can be slow on
            # cold disk). Poll in short intervals so a dead worker is caught
            # quickly instead of blocking for the full timeout.
            deadline = time.monotonic() + 180.0
            try:
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise _queue_mod.Empty()
                    if not self._process.is_alive():
                        exitcode = self._process.exitcode
                        print(f"[OmniVoiceCpp] Worker process exited unexpectedly (code {exitcode})")
                        try:
                            resp = self._response_queue.get_nowait()
                            if resp.get("type") == "error":
                                print(f"[OmniVoiceCpp] Worker error: {resp.get('error')}")
                        except _queue_mod.Empty:
                            pass
                        self._cleanup()
                        return False
                    try:
                        resp = self._response_queue.get(timeout=min(5.0, remaining))
                    except _queue_mod.Empty:
                        continue  # check is_alive again
                    msg_type = resp.get("type")
                    if msg_type == "ready":
                        self._ready = True
                        print("[OmniVoiceCpp] Worker process ready")
                        return True
                    elif msg_type == "loading":
                        print(f"[OmniVoiceCpp] Worker: {resp.get('message', msg_type)}")
                        continue  # keep waiting
                    elif msg_type == "error":
                        print(f"[OmniVoiceCpp] Worker startup error: {resp.get('error')}")
                        self._cleanup()
                        return False
                    else:
                        print(f"[OmniVoiceCpp] Unexpected startup message: {resp}")
                        self._cleanup()
                        return False
            except _queue_mod.Empty:
                print("[OmniVoiceCpp] Worker failed to start: timed out after 180s")
                self._cleanup()
                return False
            except Exception as e:
                print(f"[OmniVoiceCpp] Worker failed to start: {e}")
                self._cleanup()
                return False

    def _cleanup(self, force: bool = False):
        """Stop the current worker and discard all process/queue handles."""
        process = self._process
        request_queue = self._request_queue
        response_queue = self._response_queue
        self._ready = False

        if process is not None:
            try:
                if process.is_alive():
                    if force:
                        process.terminate()
                    else:
                        try:
                            request_queue.put_nowait(None)
                        except Exception:
                            process.terminate()
                process.join(timeout=10.0)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=10.0)
                if process.is_alive() and hasattr(process, "kill"):
                    process.kill()
                    process.join(timeout=10.0)
            except Exception as exc:
                print(f"[OmniVoiceCpp] Worker cleanup error: {exc}")
            finally:
                try:
                    process.close()
                except Exception:
                    pass

        for worker_queue in (request_queue, response_queue):
            if worker_queue is None:
                continue
            try:
                worker_queue.cancel_join_thread()
            except Exception:
                pass
            try:
                worker_queue.close()
            except Exception:
                pass

        self._process = None
        self._request_queue = None
        self._response_queue = None

    def _submit_request(self, request: Dict[str, Any]) -> Optional[Tuple[Any, Any]]:
        """Submit to one worker generation and return its immutable wait handles."""
        with self._lock:
            process = self._process
            request_queue = self._request_queue
            response_queue = self._response_queue
            if (
                not self._ready
                or process is None
                or request_queue is None
                or response_queue is None
                or not process.is_alive()
            ):
                return None
            try:
                request_queue.put_nowait(request)
            except Exception as exc:
                print(f"[OmniVoiceCpp] Failed to submit worker request: {exc}")
                self._cleanup(force=True)
                return None
            return process, response_queue

    def _invalidate_worker(self, process, response_queue, reason: str) -> None:
        """Force-discard one failed worker without touching a newer generation."""
        with self._lock:
            if self._process is not process or self._response_queue is not response_queue:
                return
            print(f"[OmniVoiceCpp] Invalidating worker: {reason}")
            self._cleanup(force=True)

    def _await_response(
        self,
        process,
        response_queue,
        timeout: float,
    ) -> Optional[Dict[str, Any]]:
        """Wait on one worker generation; invalidate only that generation."""
        if process is None or response_queue is None:
            self._invalidate_worker(process, response_queue, "worker handles are unavailable")
            return None

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._invalidate_worker(
                    process,
                    response_queue,
                    f"response timed out after {timeout:.1f}s",
                )
                return None
            if not process.is_alive():
                # Multiprocessing queue feeder threads can publish the final
                # response just after the process exit becomes observable.
                try:
                    return response_queue.get(timeout=min(0.1, remaining))
                except _queue_mod.Empty:
                    self._invalidate_worker(
                        process,
                        response_queue,
                        "worker died without a final response",
                    )
                    return None
                except Exception as exc:
                    self._invalidate_worker(
                        process,
                        response_queue,
                        f"response queue failed after worker exit: {exc}",
                    )
                    return None
            try:
                return response_queue.get(timeout=min(2.0, remaining))
            except _queue_mod.Empty:
                continue
            except Exception as exc:
                self._invalidate_worker(
                    process,
                    response_queue,
                    f"response queue failed: {exc}",
                )
                return None

    # ------------------------------------------------------------------
    # Synthesis
    # ------------------------------------------------------------------

    @_serialized_worker_io
    def synthesize_sentence(
        self,
        text: str,
        voice_path: str,
        on_chunk: Callable[[bytes, Optional[Dict]], None],
        num_steps: Optional[int] = None,
        guidance_scale: Optional[float] = None,
        seed: Optional[int] = None,
    ) -> Tuple[bool, int]:
        """
        Synthesize a single sentence.

        Args:
            text: Pre-processed text to synthesize.
            voice_path: Resolved voice reference file path.
            on_chunk: Callback(pcm_bytes, alignment_or_none) for audio delivery.
                Word timing is always None — the main process generates
                amplitude visemes.
            num_steps: Override MaskGIT steps (default from settings).
            guidance_scale: Override CFG scale (default from settings).
            seed: Override sampler seed (default from settings).

        Returns:
            (success, bytes_produced)
        """
        if not self.ensure_started():
            print("[OmniVoiceCpp] Worker process not available")
            return (False, 0)

        # Same audio-tag handling as the torch worker (omnivoice_engine.py):
        # keeps supported tags like [laughter], strips the rest so they are
        # not spoken as text. Pure-stdlib module, fine in the main process.
        text = omni_preprocess_text(text)
        if not text or not text.strip():
            return (True, 0)

        request: Dict[str, Any] = {
            "type": "synthesize",
            "text": text,
            "voice_path": voice_path,
        }
        ref_text = ensure_voice_reference_transcript(voice_path)
        if ref_text:
            request["ref_text"] = ref_text
        if num_steps is not None:
            request["num_steps"] = num_steps
        if guidance_scale is not None:
            request["guidance_scale"] = guidance_scale
        if seed is not None:
            request["seed"] = seed

        handles = self._submit_request(request)
        if handles is None:
            print("[OmniVoiceCpp] Worker process became unavailable before synthesis")
            return (False, 0)

        # Generous timeout — CPU-backend synthesis can be slow.
        resp = self._await_response(*handles, timeout=300.0)
        if resp is None:
            print("[OmniVoiceCpp] Timeout waiting for synthesis response")
            return (False, 0)

        resp_type = resp.get("type")

        if resp_type == "done":
            pcm_bytes = resp["audio"]
            if not pcm_bytes:
                print("[OmniVoiceCpp] Synthesis returned empty audio")
                return (False, 0)
            response_sample_rate = int(resp.get("sample_rate", 0))
            if response_sample_rate != OUTPUT_SAMPLE_RATE:
                print("[OmniVoiceCpp] Synthesis returned an inconsistent sample rate "
                      f"({response_sample_rate} Hz; expected {OUTPUT_SAMPLE_RATE} Hz)")
                return (False, 0)
            on_chunk(pcm_bytes, None)
            return (True, len(pcm_bytes))

        elif resp_type == "error":
            print(f"[OmniVoiceCpp] Synthesis error: {resp.get('error')}")
            return (False, 0)

        print(f"[OmniVoiceCpp] Unexpected response type: {resp_type}")
        return (False, 0)

    # ------------------------------------------------------------------
    # Pretokenize / cache management
    # ------------------------------------------------------------------

    @_serialized_worker_io
    def begin_voice_encoder_batch(self) -> bool:
        """Keep the lazily loaded encoder resident across preprocessing calls."""
        if not self.ensure_started():
            return False
        handles = self._submit_request({"type": "voice_encoder_batch_start"})
        if handles is None:
            return False
        response = self._await_response(*handles, timeout=10.0)
        return bool(
            response
            and response.get("type") == "voice_encoder_batch_started"
        )

    @_serialized_worker_io
    def end_voice_encoder_batch(self) -> bool:
        """Release encoder memory without starting a worker that has stopped."""
        if not self.is_ready():
            return False
        handles = self._submit_request({"type": "voice_encoder_batch_end"})
        if handles is None:
            return False
        response = self._await_response(*handles, timeout=10.0)
        if not response or response.get("type") != "voice_encoder_batch_ended":
            return False
        encoder_bytes = int(response.get("encoder_bytes", 0))
        if encoder_bytes:
            print(
                "[OmniVoiceCpp] Voice encoder release left "
                f"{encoder_bytes:,} bytes resident"
            )
        return encoder_bytes == 0

    @_serialized_worker_io
    def pretokenize_voice(self, voice_path: str, ref_text: Optional[str] = None) -> bool:
        """Encode a reference and persist its shared ``.tokens.pt`` sidecar."""
        if not str(ref_text or "").strip():
            print(f"[OmniVoiceCpp] Cannot pretokenize {Path(voice_path).name}: no transcript")
            return False
        if not self.ensure_started():
            return False

        handles = self._submit_request({
            "type": "pretokenize",
            "voice_path": voice_path,
            "ref_text": str(ref_text).strip(),
        })
        if handles is None:
            return False

        resp = self._await_response(*handles, timeout=120.0)
        if resp is None:
            print("[OmniVoiceCpp] Timeout waiting for pretokenize response")
            return False
        return resp.get("success", False)

    @_serialized_worker_io
    def clear_voice_prompt(self, voice_path: str):
        """Clear cached reference codes in the worker (call when the reference changes)."""
        if not self.ensure_started():
            return

        handles = self._submit_request({
            "type": "clear_voice_prompt",
            "voice_path": voice_path,
        })
        if handles is not None:
            self._await_response(*handles, timeout=5.0)

    # ------------------------------------------------------------------
    # Warmup
    # ------------------------------------------------------------------

    @_serialized_worker_io
    def warm_up(self, voice_path: Optional[str] = None) -> bool:
        """Warm the worker and report whether it completed successfully."""
        if not self.ensure_started():
            return False

        handles = self._submit_request({
            "type": "warmup",
            "voice_path": voice_path,
        })
        if handles is None:
            return False
        response = self._await_response(*handles, timeout=120.0)
        if response is None:
            return False
        if response.get("type") != "warmup_done":
            print(f"[OmniVoiceCpp] Unexpected warm-up response: {response}")
            return False
        error = response.get("error")
        if error:
            print(f"[OmniVoiceCpp] Warm-up failed: {error}")
            return False
        return True

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def shutdown(self):
        """Shut down the worker process."""
        with self._synthesis_lock:
            with self._lock:
                self._closed = True
                self._cleanup()
                print("[OmniVoiceCpp] Worker process shut down")

    def is_ready(self) -> bool:
        """Return a non-blocking snapshot of live worker readiness."""
        # Do not take _lock here: ensure_started() intentionally holds it while
        # models load, and status polling must not stall for up to three
        # minutes. Attribute snapshots are sufficient; cleanup races are
        # handled by the guarded is_alive() call.
        process = self._process
        if self._closed or not self._ready or process is None:
            return False
        try:
            return process.is_alive()
        except (OSError, ValueError):
            return False


# ============================================================================
# Configuration
# ============================================================================

def _get_omnivoice_cpp_config() -> Dict[str, Any]:
    """Build config dict from settings.json for the worker process."""
    try:
        omni = load_settings().get("tts", {}).get("omnivoice_cpp", {})
    except Exception:
        omni = {}

    return {
        "device": str(omni.get("device", "auto")),
        "num_steps": int(omni.get("num_steps", 32)),
        "guidance_scale": float(omni.get("guidance_scale", 2.0)),
        "seed": int(omni.get("seed", 42)),
        "bin_dir": str(BIN_DIR),
        "model_path": str(MODEL_DIR / MODEL_FILENAME),
        "codec_path": str(MODEL_DIR / TOKENIZER_FILENAME),
        "upscaler_path": str(MODEL_DIR / UPSCALER_FILENAME),
    }


# ============================================================================
# Module-level singleton
# ============================================================================

_process_manager: Optional[OmniVoiceCppProcessManager] = None
_manager_lock = threading.Lock()


def _get_manager() -> OmniVoiceCppProcessManager:
    """Get or create the singleton process manager."""
    global _process_manager
    if _process_manager is None:
        with _manager_lock:
            if _process_manager is None:
                _process_manager = OmniVoiceCppProcessManager()
    return _process_manager


# ============================================================================
# Public API
# ============================================================================

def warm_up(voice_name: Optional[str] = None) -> bool:
    """Warm up the worker and report whether it completed successfully."""
    mgr = _get_manager()
    voice_path = _resolve_voice(voice_name) if voice_name else None
    return mgr.warm_up(voice_path)


def unload():
    """Shutdown worker process and free resources."""
    global _process_manager
    with _manager_lock:
        if _process_manager is not None:
            _process_manager.shutdown()
            _process_manager = None
    gc.collect()
    print("[OmniVoiceCpp] Unloaded")


def is_loaded() -> bool:
    """Check if worker process is running and ready."""
    manager = _process_manager
    return manager.is_ready() if manager is not None else False


def clear_voice_prompt(voice_path: str):
    """Clear cached reference codes for a file (call when the reference changes)."""
    with _manager_lock:
        manager = _process_manager
        if manager is not None and manager.is_ready():
            manager.clear_voice_prompt(voice_path)
