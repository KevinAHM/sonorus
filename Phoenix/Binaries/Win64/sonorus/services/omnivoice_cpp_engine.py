"""
OmniVoice.cpp TTS Engine

Runs OmniVoice inference through omnivoice.cpp (ggml/Vulkan) in a subprocess,
isolating the native DLL from the main Flask server.  Follows the same
process-manager pattern as omnivoice_engine.py / pocket_tts_onnx.py, but with
no torch anywhere: the worker talks to omnivoice.dll via ctypes, so it runs
on any Vulkan GPU (or CPU) without CUDA.

The ctypes structs below mirror omnivoice.h (OV_ABI_VERSION 3) field for
field.  Defaults are always populated via ov_init_default_params /
ov_tts_default_params rather than hand-filled.
"""
import ctypes as C
import gc
import os
import queue as _queue_mod
import time
import threading
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
SAMPLE_RATE = 24_000  # OmniVoice native sample rate (codec output)

_SONORUS_ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = _SONORUS_ROOT / "omnivoice_cpp" / "bin"
MODEL_DIR = _SONORUS_ROOT / "omnivoice_cpp" / "models"

MODEL_FILENAME = "omnivoice-base-Q8_0.gguf"
TOKENIZER_FILENAME = "omnivoice-tokenizer-F32.gguf"
_DLL_NAMES = ("omnivoice.dll", "libomnivoice.dll")


def _serialized_worker_io(func):
    def wrapper(self, *args, **kwargs):
        with self._synthesis_lock:
            return func(self, *args, **kwargs)
    return wrapper


# ============================================================================
# Availability checks (main process — lightweight, no DLL load)
# ============================================================================

def _find_dll() -> Optional[Path]:
    """Locate the omnivoice DLL in BIN_DIR (either MSVC or MinGW naming)."""
    for name in _DLL_NAMES:
        candidate = BIN_DIR / name
        if candidate.is_file():
            return candidate
    return None


def dll_present() -> bool:
    """Check if the omnivoice.cpp DLL has been installed."""
    return _find_dll() is not None


def models_present() -> bool:
    """Check if both GGUF models have been downloaded."""
    return (
        (MODEL_DIR / MODEL_FILENAME).is_file()
        and (MODEL_DIR / TOKENIZER_FILENAME).is_file()
    )


def is_available() -> bool:
    """Check if the omnivoice.cpp backend can be started (DLL + models)."""
    return dll_present() and models_present()


# ============================================================================
# Model download
# ============================================================================

def download_models(progress_cb=None):
    """
    Download the two GGUF models from HuggingFace into MODEL_DIR.

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
    filenames = [MODEL_FILENAME, TOKENIZER_FILENAME]
    total = len(filenames)

    for index, filename in enumerate(filenames):
        if progress_cb:
            progress_cb(index, total, f"Downloading {filename}...")
        if (MODEL_DIR / filename).is_file():
            print(f"[OmniVoiceCpp] Model already present: {filename}")
            continue
        print(f"[OmniVoiceCpp] Downloading {filename} from {HF_REPO_ID}...")
        hf_hub_download(repo_id=HF_REPO_ID, filename=filename, local_dir=str(MODEL_DIR))
        print(f"[OmniVoiceCpp] Downloaded {filename}")

    if progress_cb:
        progress_cb(total, total, "OmniVoice.cpp models ready")
    print(f"[OmniVoiceCpp] Models ready in {MODEL_DIR}")


# ============================================================================
# ctypes ABI (mirrors omnivoice.h, OV_ABI_VERSION 3)
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
    lib.ov_init_default_params.restype = None
    lib.ov_init_default_params.argtypes = [C.POINTER(OvInitParams)]
    lib.ov_init.restype = C.c_void_p          # opaque struct ov_context *
    lib.ov_init.argtypes = [C.POINTER(OvInitParams)]
    lib.ov_free.restype = None
    lib.ov_free.argtypes = [C.c_void_p]
    lib.ov_tts_default_params.restype = None
    lib.ov_tts_default_params.argtypes = [C.POINTER(OvTtsParams)]
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
        bin_dir, model_path, codec_path
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
        os.add_dll_directory(str(bin_dir))
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

        init_params = OvInitParams()
        lib.ov_init_default_params(C.byref(init_params))
        init_params.model_path = model_path
        init_params.codec_path = codec_path

        print(f"[OmniVoiceCpp] Loading models from {config['model_path']}...")
        response_queue.put({"type": "loading", "message": "Loading OmniVoice GGUF models..."})
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
                lib.ov_tts_default_params(C.byref(params))
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
                duration = len(pcm_bytes) / (2 * SAMPLE_RATE)
                print(f"[OmniVoiceCpp] Synthesized {duration:.2f}s in {elapsed:.0f}ms")

                response_queue.put({
                    "type": "done",
                    "audio": pcm_bytes,
                    "sample_rate": SAMPLE_RATE,
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

    def _cleanup(self):
        """Terminate and clean up the worker process."""
        if self._process is not None:
            if self._process.is_alive():
                self._request_queue.put(None)  # shutdown signal
                self._process.join(timeout=10.0)
                if self._process.is_alive():
                    self._process.terminate()
            self._process = None
        self._request_queue = None
        self._response_queue = None
        self._ready = False

    def _await_response(self, timeout: float) -> Optional[Dict[str, Any]]:
        """Wait for a worker response, bailing out early if the worker dies."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            if self._process is None or not self._process.is_alive():
                # Drain any final message the worker managed to send
                try:
                    return self._response_queue.get_nowait()
                except Exception:
                    print("[OmniVoiceCpp] Worker process died while waiting for response")
                    return None
            try:
                return self._response_queue.get(timeout=min(2.0, remaining))
            except _queue_mod.Empty:
                continue

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

        self._request_queue.put(request)

        # Generous timeout — CPU-backend synthesis can be slow.
        resp = self._await_response(timeout=300.0)
        if resp is None:
            print("[OmniVoiceCpp] Timeout waiting for synthesis response")
            return (False, 0)

        resp_type = resp.get("type")

        if resp_type == "done":
            pcm_bytes = resp["audio"]
            if not pcm_bytes:
                print("[OmniVoiceCpp] Synthesis returned empty audio")
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

        self._request_queue.put({
            "type": "pretokenize",
            "voice_path": voice_path,
        })

        resp = self._await_response(timeout=120.0)
        if resp is None:
            print("[OmniVoiceCpp] Timeout waiting for pretokenize response")
            return False
        return resp.get("success", False)

    @_serialized_worker_io
    def clear_voice_prompt(self, voice_path: str):
        """Clear cached reference codes in the worker (call when the reference changes)."""
        if not self.ensure_started():
            return

        self._request_queue.put({
            "type": "clear_voice_prompt",
            "voice_path": voice_path,
        })
        self._await_response(timeout=5.0)

    # ------------------------------------------------------------------
    # Warmup
    # ------------------------------------------------------------------

    @_serialized_worker_io
    def warm_up(self, voice_path: Optional[str] = None):
        """Warm up the worker and optionally pre-encode a voice reference."""
        if not self.ensure_started():
            return

        self._request_queue.put({
            "type": "warmup",
            "voice_path": voice_path,
        })
        self._await_response(timeout=120.0)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def shutdown(self):
        """Shut down the worker process."""
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
