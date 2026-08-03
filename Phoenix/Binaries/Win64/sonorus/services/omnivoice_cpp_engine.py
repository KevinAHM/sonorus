"""
OmniVoice.cpp TTS Engine

Runs OmniVoice inference through omnivoice.cpp (ggml/Vulkan) in a subprocess,
isolating the native DLL from the main Flask server.  Follows the same
process-manager pattern as omnivoice_engine.py / pocket_tts_onnx.py, but with
no torch anywhere: the worker talks to omnivoice.dll via ctypes, so it runs
on any Vulkan GPU (or CPU) without CUDA.

The ctypes structs below mirror omnivoice.h (OV_ABI_VERSION 4) field for
field.  Defaults are always populated via ov_init_default_params_v4 /
ov_tts_default_params_v4 rather than hand-filled.
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
import zipfile
import multiprocessing as mp
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from services.omnivoice_engine import (
    VOICE_DIR,
    _resolve_voice,
    ensure_voice_reference_transcript,
)
from utils.settings import load_settings

# ============================================================================
# Constants
# ============================================================================

HF_REPO_ID = "Serveurperso/OmniVoice-GGUF"
HF_REPO_REVISION = "361609388ae572a820d085185bbbe2a2aac4b30e"
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
OV_ABI_VERSION = 4

_SONORUS_ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = _SONORUS_ROOT / "omnivoice_cpp" / "bin"
MODEL_DIR = _SONORUS_ROOT / "omnivoice_cpp" / "models"
RUNTIME_MANIFEST_PATH = _SONORUS_ROOT / "omnivoice_cpp" / "runtime-manifest.json"
RUNTIME_DOWNLOAD_DIR = _SONORUS_ROOT / "omnivoice_cpp" / ".downloads"

MODEL_FILENAME = "omnivoice-base-Q8_0.gguf"
TOKENIZER_FILENAME = "omnivoice-tokenizer-F32.gguf"
UPSCALER_FILENAME = "voxcpm2-audiovae-f16.gguf"
UPSCALER_EXPECTED_BYTES = 187_868_032
UPSCALER_SHA256 = "a5fb091c0a95172bdee2ee7230335dac7d3dc318d77ca100f095d023cabd5d97"
_DLL_NAMES = ("omnivoice.dll", "libomnivoice.dll")
RUNTIME_DLL_FILENAMES = (
    "ggml.dll",
    "ggml-base.dll",
    "ggml-cpu.dll",
    "ggml-vulkan.dll",
)
_RUNTIME_MIN_BYTES = {
    "omnivoice.dll": 64 * 1024,
    "libomnivoice.dll": 64 * 1024,
    "ggml.dll": 16 * 1024,
    "ggml-base.dll": 64 * 1024,
    "ggml-cpu.dll": 128 * 1024,
    "ggml-vulkan.dll": 1024 * 1024,
}
_upscaler_validation_cache: dict[tuple[str, int, int, int], bool] = {}
_upscaler_validation_lock = threading.Lock()
_runtime_abi_cache: dict[tuple, Optional[str]] = {}
_runtime_abi_lock = threading.Lock()

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
        ("ov_init_default_params_v4", "init_abi"),
        ("ov_tts_default_params_v4", "tts_abi"),
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

def _is_valid_runtime_dll(path: Path) -> bool:
    """Reject missing, truncated, or non-PE runtime files."""
    try:
        minimum_size = _RUNTIME_MIN_BYTES.get(path.name, 16 * 1024)
        if not path.is_file() or path.stat().st_size < minimum_size:
            return False
        with path.open("rb") as dll_file:
            return dll_file.read(2) == b"MZ"
    except OSError:
        return False


def _find_dll() -> Optional[Path]:
    """Locate the omnivoice DLL in BIN_DIR (either MSVC or MinGW naming)."""
    for name in _DLL_NAMES:
        candidate = BIN_DIR / name
        if _is_valid_runtime_dll(candidate):
            return candidate
    return None


def dll_present() -> bool:
    """Check if the omnivoice.cpp DLL has been installed."""
    return _find_dll() is not None


def _runtime_dll_identity() -> tuple:
    """Return identities for every DLL that can affect ABI/load readiness."""
    identities = []
    for name in _DLL_NAMES + RUNTIME_DLL_FILENAMES:
        path = BIN_DIR / name
        try:
            file_stat = path.stat()
            identity = (str(path.resolve()), file_stat.st_size,
                        file_stat.st_ctime_ns, file_stat.st_mtime_ns)
        except OSError:
            identity = (str(path.absolute()), None, None, None)
        identities.append(identity)
    return tuple(identities)


def _probe_runtime_abi(dll_path: Path) -> Optional[str]:
    """Return None for an ABI-v4 runtime, otherwise a user-facing error."""
    with _runtime_abi_lock:
        cache_key = _runtime_dll_identity()
        if cache_key in _runtime_abi_cache:
            return _runtime_abi_cache[cache_key]

        cacheable = False
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    _ABI_PROBE_SNIPPET,
                    str(BIN_DIR),
                    str(dll_path),
                    str(OV_ABI_VERSION),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            error = "ABI readiness probe timed out"
        except Exception as exc:
            error = f"ABI readiness probe could not start: {exc}"
        else:
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

        if cacheable:
            _runtime_abi_cache.clear()
            _runtime_abi_cache[cache_key] = error
        return error


def missing_runtime_files() -> list[str]:
    """Return missing runtime DLLs or a meaningful ABI incompatibility."""
    missing = [name for name in RUNTIME_DLL_FILENAMES if not _is_valid_runtime_dll(BIN_DIR / name)]
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
    """Check that the OmniVoice library and every installed ggml DLL are ABI-ready."""
    return not missing_runtime_files()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as input_file:
        for chunk in iter(lambda: input_file.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().lower()


def _load_runtime_manifest() -> dict:
    try:
        manifest = json.loads(RUNTIME_MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeError(f"Could not read the OmniVoice runtime manifest: {exc}") from exc

    required_files = set(RUNTIME_DLL_FILENAMES) | {"omnivoice.dll"}
    files = manifest.get("files")
    archive = manifest.get("archive")
    if (
        manifest.get("schema_version") != 1
        or not isinstance(files, dict)
        or set(files) != required_files
        or not isinstance(archive, dict)
    ):
        raise RuntimeError("The OmniVoice runtime manifest has an unsupported format")

    for name, metadata in files.items():
        if (
            not isinstance(metadata, dict)
            or not isinstance(metadata.get("size"), int)
            or metadata["size"] <= 0
            or not isinstance(metadata.get("sha256"), str)
            or len(metadata["sha256"]) != 64
        ):
            raise RuntimeError(f"The OmniVoice runtime manifest entry for {name} is invalid")

    archive_filename = archive.get("filename")
    if (
        not isinstance(archive_filename, str)
        or not archive_filename
        or Path(archive_filename).name != archive_filename
        or not isinstance(archive.get("url"), str)
        or not archive["url"].startswith("https://")
        or not isinstance(archive.get("size"), int)
        or archive["size"] <= 0
        or not isinstance(archive.get("sha256"), str)
        or len(archive["sha256"]) != 64
    ):
        raise RuntimeError("The OmniVoice runtime archive entry is invalid")
    return manifest


def _file_matches_metadata(path: Path, metadata: dict) -> bool:
    try:
        return (
            path.is_file()
            and path.stat().st_size == metadata["size"]
            and _sha256_file(path) == metadata["sha256"].lower()
        )
    except OSError:
        return False


def _download_runtime_archive(url: str, destination: Path, metadata: dict) -> None:
    """Download a pinned runtime archive, retaining a resumable partial file."""
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    partial = destination.with_name(destination.name + ".incomplete")
    offset = partial.stat().st_size if partial.is_file() else 0
    headers = {"User-Agent": "Sonorus-OmniVoiceCpp-Installer"}
    if offset:
        headers["Range"] = f"bytes={offset}-"

    print("[OmniVoiceCpp] Downloading portable Windows runtime...")
    try:
        response = urlopen(Request(url, headers=headers), timeout=60)
    except HTTPError as exc:
        if exc.code == 416 and offset:
            if _file_matches_metadata(partial, metadata):
                partial.replace(destination)
                return
            partial.unlink(missing_ok=True)
            return _download_runtime_archive(url, destination, metadata)
        raise

    with response:
        status = getattr(response, "status", response.getcode())
        append = bool(offset and status == 206)
        expected_chunk = response.headers.get("Content-Length")
        expected_size = (offset if append else 0) + int(expected_chunk) if expected_chunk else None
        with partial.open("ab" if append else "wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)

    if expected_size is not None and partial.stat().st_size != expected_size:
        raise RuntimeError(
            "Incomplete OmniVoice runtime download: "
            f"received {partial.stat().st_size} of {expected_size} bytes"
        )
    if not _file_matches_metadata(partial, metadata):
        partial.unlink(missing_ok=True)
        raise RuntimeError("OmniVoice runtime archive failed its size or SHA-256 check")
    partial.replace(destination)


def download_runtime(progress_cb=None) -> None:
    """Install the pinned release runtime after archive and per-DLL verification."""
    manifest = _load_runtime_manifest()
    files = manifest["files"]
    if all(_file_matches_metadata(BIN_DIR / name, metadata)
           for name, metadata in files.items()):
        print("[OmniVoiceCpp] Portable runtime already present")
        return

    if progress_cb:
        progress_cb(0, 1, "Downloading OmniVoice portable runtime...")
    RUNTIME_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    archive_name = manifest["archive"].get("filename", "omnivoice-runtime.zip")
    archive_path = RUNTIME_DOWNLOAD_DIR / archive_name
    if not _file_matches_metadata(archive_path, manifest["archive"]):
        archive_path.unlink(missing_ok=True)
        _download_runtime_archive(
            manifest["archive"]["url"], archive_path, manifest["archive"]
        )

    expected_entries = set(files) | {
        "RUNTIME-MANIFEST.json",
        "omnivoice.cpp.LICENSE",
        "ggml.LICENSE",
    }
    with tempfile.TemporaryDirectory(prefix="sonorus-omnivoice-runtime-") as temp_dir:
        staging = Path(temp_dir)
        try:
            with zipfile.ZipFile(archive_path) as runtime_archive:
                entries = {
                    info.filename: info
                    for info in runtime_archive.infolist()
                    if not info.is_dir()
                }
                if (
                    set(entries) != expected_entries
                    or len(entries) != len(runtime_archive.infolist())
                ):
                    raise RuntimeError("OmniVoice runtime archive contains unexpected files")
                for name in files:
                    with runtime_archive.open(entries[name]) as source, \
                            (staging / name).open("wb") as destination:
                        shutil.copyfileobj(source, destination)
        except (OSError, zipfile.BadZipFile) as exc:
            archive_path.unlink(missing_ok=True)
            raise RuntimeError(f"Could not extract the OmniVoice runtime: {exc}") from exc

        invalid = [
            name for name, metadata in files.items()
            if not _file_matches_metadata(staging / name, metadata)
        ]
        if invalid:
            archive_path.unlink(missing_ok=True)
            raise RuntimeError(
                "Extracted OmniVoice runtime failed verification: " + ", ".join(invalid)
            )

        BIN_DIR.mkdir(parents=True, exist_ok=True)
        for name in files:
            (staging / name).replace(BIN_DIR / name)

    _runtime_abi_cache.clear()
    missing = missing_runtime_files()
    if missing:
        raise RuntimeError(
            "Installed OmniVoice runtime failed its ABI readiness check: "
            + ", ".join(missing)
        )
    if progress_cb:
        progress_cb(1, 1, "OmniVoice portable runtime ready")
    print(f"[OmniVoiceCpp] Portable runtime ready in {BIN_DIR}")


def _upscaler_file_identity(path: Path) -> tuple[str, int, int, int]:
    file_stat = path.stat()
    return (
        str(path.resolve()),
        file_stat.st_size,
        file_stat.st_ctime_ns,
        file_stat.st_mtime_ns,
    )


def _is_valid_upscaler_model(path: Path) -> bool:
    """Verify the separately hosted AudioVAE asset by exact size and SHA-256."""
    try:
        with _upscaler_validation_lock:
            cache_key = _upscaler_file_identity(path)
            if not path.is_file() or cache_key[1] != UPSCALER_EXPECTED_BYTES:
                return False
            cached = _upscaler_validation_cache.get(cache_key)
            if cached is not None:
                return cached

            digest = hashlib.sha256()
            with path.open("rb") as model_file:
                for chunk in iter(lambda: model_file.read(4 * 1024 * 1024), b""):
                    digest.update(chunk)

            if _upscaler_file_identity(path) != cache_key:
                return False
            valid = digest.hexdigest().lower() == UPSCALER_SHA256
            _upscaler_validation_cache.clear()
            _upscaler_validation_cache[cache_key] = valid
            return valid
    except OSError:
        return False


def model_file_ready(filename: str) -> bool:
    """Return whether one expected model file is usable by this integration."""
    path = MODEL_DIR / filename
    if filename == UPSCALER_FILENAME:
        return _is_valid_upscaler_model(path)
    return filename in (MODEL_FILENAME, TOKENIZER_FILENAME) and path.is_file()


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
# Model download
# ============================================================================

def download_models(progress_cb=None):
    """
    Download the three GGUF models from their configured sources into MODEL_DIR.

    Args:
        progress_cb: Optional callback(current, total, message) for progress.
    """
    try:
        from huggingface_hub import hf_hub_download
    except ImportError as exc:
        raise RuntimeError(
            "huggingface_hub is required to download the OmniVoice GGUF models. "
            "Install it with: pip install huggingface_hub"
        ) from exc

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    filenames = [MODEL_FILENAME, TOKENIZER_FILENAME, UPSCALER_FILENAME]
    total = len(filenames)

    for index, filename in enumerate((MODEL_FILENAME, TOKENIZER_FILENAME)):
        if progress_cb:
            progress_cb(index, total, f"Downloading {filename}...")
        if (MODEL_DIR / filename).is_file():
            print(f"[OmniVoiceCpp] Model already present: {filename}")
            continue
        print(f"[OmniVoiceCpp] Downloading {filename} from {HF_REPO_ID}...")
        hf_hub_download(
            repo_id=HF_REPO_ID,
            filename=filename,
            revision=HF_REPO_REVISION,
            local_dir=str(MODEL_DIR),
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
            try:
                hf_hub_download(
                    repo_id=UPSCALER_HF_REPO_ID,
                    filename=UPSCALER_FILENAME,
                    revision=UPSCALER_HF_REVISION,
                    local_dir=str(MODEL_DIR),
                )
            except Exception:
                if not UPSCALER_URL:
                    raise
                print("[OmniVoiceCpp] Hugging Face AudioVAE download failed; "
                      "trying the pinned GitHub release mirror")
                _download_upscaler_url(UPSCALER_URL, upscaler_path)
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
    """Download a direct release asset, retaining a resumable partial file."""
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    partial = destination.with_name(destination.name + ".incomplete")
    offset = partial.stat().st_size if partial.is_file() else 0
    headers = {"User-Agent": "Sonorus-OmniVoiceCpp-Installer"}
    if offset:
        headers["Range"] = f"bytes={offset}-"

    print(f"[OmniVoiceCpp] Downloading {destination.name} from release asset...")
    try:
        response = urlopen(Request(url, headers=headers), timeout=60)
    except HTTPError as exc:
        if exc.code == 416 and offset:
            if _is_valid_upscaler_model(partial):
                partial.replace(destination)
                print(f"[OmniVoiceCpp] Downloaded {destination.name}")
                return
            partial.unlink(missing_ok=True)
            return _download_upscaler_url(url, destination)
        raise

    with response:
        status = getattr(response, "status", response.getcode())
        append = bool(offset and status == 206)
        expected_chunk = response.headers.get("Content-Length")
        expected_size = (offset if append else 0) + int(expected_chunk) if expected_chunk else None
        with partial.open("ab" if append else "wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)

    if expected_size is not None and partial.stat().st_size != expected_size:
        raise RuntimeError(
            f"Incomplete download for {destination.name}: "
            f"received {partial.stat().st_size} of {expected_size} bytes"
        )
    partial.replace(destination)
    print(f"[OmniVoiceCpp] Downloaded {destination.name}")


# ============================================================================
# ctypes ABI (mirrors omnivoice.h, OV_ABI_VERSION 4)
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
        init_defaults_v4 = lib.ov_init_default_params_v4
    except AttributeError as exc:
        raise RuntimeError(
            "The installed omnivoice.dll does not expose ov_init_default_params_v4; "
            "install the ABI v4 runtime required for 48 kHz upscaling"
        ) from exc
    init_defaults_v4.restype = None
    init_defaults_v4.argtypes = [C.POINTER(OvInitParams)]
    try:
        tts_defaults_v4 = lib.ov_tts_default_params_v4
    except AttributeError as exc:
        raise RuntimeError(
            "The installed omnivoice.dll does not expose ov_tts_default_params_v4; "
            "install the ABI v4 runtime required for 48 kHz upscaling"
        ) from exc
    tts_defaults_v4.restype = None
    tts_defaults_v4.argtypes = [C.POINTER(OvTtsParams)]
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
        import numpy as np

        # --------------------------------------------------------------
        # Device selection — GGML_BACKEND must be set BEFORE the DLL loads
        # (backend.h reads it at backend init: "Vulkan0", "CPU", ...).
        # --------------------------------------------------------------
        device = str(config.get("device", "auto")).strip()
        if device and device.lower() != "auto":
            os.environ["GGML_BACKEND"] = device
            print(f"[OmniVoiceCpp] Forcing GGML backend: {device}")
        else:
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
        lib.ov_init_default_params_v4(C.byref(init_params))
        if init_params.abi_version != OV_ABI_VERSION:
            raise RuntimeError(
                "Incompatible omnivoice.dll ABI "
                f"({init_params.abi_version}; Sonorus requires {OV_ABI_VERSION} for 48 kHz upscaling)"
            )
        init_params.model_path = model_path
        init_params.codec_path = codec_path
        init_params.upscaler_path = upscaler_path

        print(f"[OmniVoiceCpp] Loading models from {config['model_path']}...")
        response_queue.put({"type": "loading", "message": "Loading OmniVoice and 48 kHz upscaler GGUF models..."})
        ctx = lib.ov_init(C.byref(init_params))
        if not ctx:
            raise RuntimeError(f"ov_init failed: {_last_error()}")
        print("[OmniVoiceCpp] Model loaded.")
    except Exception as exc:
        import traceback
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
        import numpy as np
        data = None
        sr = None
        if path.lower().endswith(".wav"):
            try:
                import wave
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
            import soundfile as sf  # non-wav / float-wav references
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
    _voice_ref_cache: Dict[str, OvVoiceRef] = {}

    def _get_voice_ref(voice_path: str) -> OvVoiceRef:
        """Encode (or fetch cached) RVQ reference codes for a voice file."""
        cache_key = str(Path(voice_path).resolve())
        cached = _voice_ref_cache.get(cache_key)
        if cached is not None:
            return cached

        t_start = time.time()
        pcm = _decode_ref_audio(voice_path)
        ref = OvVoiceRef()
        rc = lib.ov_extract_voice_ref(
            ctx,
            pcm.ctypes.data_as(C.POINTER(C.c_float)),
            len(pcm),
            C.byref(ref),
        )
        if rc != OV_STATUS_OK:
            raise RuntimeError(f"ov_extract_voice_ref failed ({rc}): {_last_error()}")
        _voice_ref_cache[cache_key] = ref
        elapsed = (time.time() - t_start) * 1000
        print(f"[OmniVoiceCpp] Encoded voice reference {Path(voice_path).name} "
              f"(ref_T={ref.ref_T}, {elapsed:.0f}ms)")
        return ref

    # ------------------------------------------------------------------
    # Startup complete
    # ------------------------------------------------------------------
    response_queue.put({"type": "ready"})

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    import numpy as np
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

                ref = _get_voice_ref(voice_path)

                params = OvTtsParams()
                lib.ov_tts_default_params_v4(C.byref(params))
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
                ref_text = msg.get("ref_text")
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
                import traceback
                traceback.print_exc()
                response_queue.put({"type": "error", "error": str(exc)})

        # ---- pretokenize (warm the RVQ code cache for a voice) ---------
        elif msg_type == "pretokenize":
            try:
                _get_voice_ref(msg["voice_path"])
                response_queue.put({"type": "pretokenize_done", "success": True})
            except Exception as exc:
                import traceback
                traceback.print_exc()
                response_queue.put({"type": "pretokenize_done", "success": False, "error": str(exc)})

        # ---- clear_voice_prompt ----------------------------------------
        elif msg_type == "clear_voice_prompt":
            voice_path = msg.get("voice_path", "")
            cache_key = str(Path(voice_path).resolve())
            removed = _voice_ref_cache.pop(cache_key, None)
            if removed is not None:
                lib.ov_voice_ref_free(C.byref(removed))
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

        else:
            print(f"[OmniVoiceCpp] Unknown message type: {msg_type}")

    # Clean up on exit
    for cached in _voice_ref_cache.values():
        lib.ov_voice_ref_free(C.byref(cached))
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

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def ensure_started(self) -> bool:
        """Start the worker process if not already running. Returns True when ready."""
        with self._lock:
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
        from services.omnivoice_text import preprocess_text as omni_preprocess_text
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
    def pretokenize_voice(self, voice_path: str, ref_text: Optional[str] = None) -> bool:
        """
        Warm the worker's RVQ reference-code cache for a voice.

        ref_text is accepted for API parity with the torch engine, but the
        reference encode (ov_extract_voice_ref) is audio-only — the transcript
        is resolved per-request at synthesis time instead.
        """
        if not self.ensure_started():
            return False

        handles = self._submit_request({
            "type": "pretokenize",
            "voice_path": voice_path,
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
    def warm_up(self, voice_path: Optional[str] = None):
        """Warm up the worker and optionally pre-encode a voice reference."""
        if not self.ensure_started():
            return

        handles = self._submit_request({
            "type": "warmup",
            "voice_path": voice_path,
        })
        if handles is not None:
            self._await_response(*handles, timeout=120.0)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def shutdown(self):
        """Shut down the worker process."""
        with self._synthesis_lock:
            with self._lock:
                self._cleanup()
                print("[OmniVoiceCpp] Worker process shut down")


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

def warm_up(voice_name: Optional[str] = None):
    """Warm up the worker and optionally pre-encode a voice reference."""
    mgr = _get_manager()
    voice_path = _resolve_voice(voice_name) if voice_name else None
    mgr.warm_up(voice_path)


def unload():
    """Shutdown worker process and free resources."""
    global _process_manager
    if _process_manager is not None:
        _process_manager.shutdown()
        _process_manager = None
    gc.collect()
    print("[OmniVoiceCpp] Unloaded")


def is_loaded() -> bool:
    """Check if worker process is running and ready."""
    if _process_manager is None:
        return False
    return (
        _process_manager._ready
        and _process_manager._process is not None
        and _process_manager._process.is_alive()
    )


def clear_voice_prompt(voice_path: str):
    """Clear cached reference codes for a file (call when the reference changes)."""
    _get_manager().clear_voice_prompt(voice_path)
