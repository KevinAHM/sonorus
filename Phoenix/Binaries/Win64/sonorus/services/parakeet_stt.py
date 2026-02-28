"""
Parakeet STT Provider (Local ONNX) - Worker Process Architecture

Uses onnx-asr with NeMo Parakeet TDT 0.6B v3 for local speech recognition.
Models are downloaded from HuggingFace: istupakov/parakeet-tdt-0.6b-v3-onnx

Runs inference in a separate process to avoid blocking the main server.
Architecture mirrors pocket_tts_onnx.py worker process pattern.

Dependencies:
    - onnx-asr
    - onnxruntime
    - numpy
    - huggingface_hub
"""
import os
import sys
import gc
import time
import threading
import multiprocessing as mp
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# HuggingFace repo for ONNX models
HF_REPO_ID = "istupakov/parakeet-tdt-0.6b-v3-onnx"

# Local models directory (matches pocket TTS pattern)
MODELS_DIR = Path(__file__).resolve().parent.parent / "models" / "parakeet"

# Files needed for int8 inference
INT8_MODEL_FILES = [
    "config.json",
    "decoder_joint-model.int8.onnx",
    "encoder-model.int8.onnx",
    "nemo128.onnx",
    "vocab.txt",
]


# ============================================================================
# Worker Process
# ============================================================================

def _parakeet_worker_main(request_queue, response_queue):
    """
    Worker process main loop. Runs ONNX STT inference isolated from main process.

    This function runs in a separate process - all model operations happen here.
    """
    import io
    import wave
    import tempfile

    print(f"[STT/Parakeet Worker] Starting (PID: {os.getpid()})...")

    # Download models if needed
    from huggingface_hub import hf_hub_download

    MODELS_DIR.mkdir(parents=True, exist_ok=True)

    for filename in INT8_MODEL_FILES:
        local_path = MODELS_DIR / filename
        if local_path.exists():
            continue
        print(f"[STT/Parakeet Worker] Downloading {filename}...")
        response_queue.put({"type": "downloading", "message": f"Downloading {filename}..."})
        hf_hub_download(repo_id=HF_REPO_ID, filename=filename, local_dir=str(MODELS_DIR))
        print(f"[STT/Parakeet Worker] Downloaded {filename}")

    # Load model
    print("[STT/Parakeet Worker] Loading model...")
    response_queue.put({"type": "loading", "message": "Loading STT model..."})
    load_start = time.time()

    import onnx_asr

    model = onnx_asr.load_model(
        "nemo-parakeet-tdt-0.6b-v3",
        path=str(MODELS_DIR),
        quantization="int8",
        providers=["CPUExecutionProvider"],
    )

    elapsed = (time.time() - load_start) * 1000
    print(f"[STT/Parakeet Worker] Model loaded in {elapsed:.0f}ms")

    # Signal ready
    response_queue.put({"type": "ready"})

    # Main loop
    while True:
        try:
            req = request_queue.get(timeout=1.0)
        except:
            continue

        if req is None:
            print("[STT/Parakeet Worker] Shutdown signal received")
            break

        req_type = req.get("type")

        if req_type == "transcribe":
            audio_data = req["audio_data"]
            sample_rate = req["sample_rate"]
            tmp_path = None

            try:
                def _write_wav_to_temp(pcm_data, rate):
                    """Write PCM bytes to a temp WAV file, return path."""
                    buf = io.BytesIO()
                    with wave.open(buf, 'wb') as wf:
                        wf.setnchannels(1)
                        wf.setsampwidth(2)  # 16-bit
                        wf.setframerate(rate)
                        wf.writeframes(pcm_data)
                    t = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
                    t.write(buf.getvalue())
                    t.close()
                    return t.name

                tmp_path = _write_wav_to_temp(audio_data, sample_rate)

                start = time.time()
                result = model.recognize(tmp_path)
                elapsed = (time.time() - start) * 1000

                text = result.strip() if isinstance(result, str) else str(result).strip()
                print(f"[STT/Parakeet Worker] Transcribed: \"{text}\" ({elapsed:.0f}ms)")

                # Retry with silence padding if no speech detected
                if not text:
                    print("[STT/Parakeet Worker] No speech detected, retrying with 3s silence padding...")
                    # Clean up first attempt
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                    tmp_path = None

                    # Pad with 3s silence (16-bit mono = 2 bytes per sample)
                    silence = b'\x00' * (sample_rate * 3 * 2)
                    padded_audio = silence + audio_data + silence

                    tmp_path = _write_wav_to_temp(padded_audio, sample_rate)

                    start = time.time()
                    result = model.recognize(tmp_path)
                    elapsed = (time.time() - start) * 1000

                    text = result.strip() if isinstance(result, str) else str(result).strip()
                    print(f"[STT/Parakeet Worker] Padded retry: \"{text}\" ({elapsed:.0f}ms)")

                response_queue.put({
                    "type": "result",
                    "success": True,
                    "text": text,
                    "confidence": 1.0,
                    "error": None
                })

            except Exception as e:
                print(f"[STT/Parakeet Worker] Error: {e}")
                import traceback
                traceback.print_exc()
                response_queue.put({
                    "type": "result",
                    "success": False,
                    "text": "",
                    "confidence": 0.0,
                    "error": str(e)
                })
            finally:
                if tmp_path is not None:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass

        elif req_type == "warmup":
            response_queue.put({"type": "warmup_done"})

    print("[STT/Parakeet Worker] Exiting")


# ============================================================================
# Process Manager (Main Process Side)
# ============================================================================

class ParakeetProcessManager:
    """Manages the Parakeet STT worker process from the main process."""

    def __init__(self):
        self._process = None
        self._request_queue = None
        self._response_queue = None
        self._lock = threading.Lock()
        self._ready = False

    def ensure_started(self) -> bool:
        """Start worker process if not already running. Returns True if ready."""
        with self._lock:
            if self._process is not None and self._process.is_alive():
                return self._ready

            # Clean up old process if dead
            if self._process is not None:
                self._cleanup()

            print("[STT/Parakeet] Starting worker process...")

            # Use spawn to ensure clean process (no inherited state)
            ctx = mp.get_context('spawn')
            self._request_queue = ctx.Queue()
            self._response_queue = ctx.Queue()

            self._process = ctx.Process(
                target=_parakeet_worker_main,
                args=(self._request_queue, self._response_queue),
                daemon=True
            )
            self._process.start()

            # Wait for ready signal, extending timeout on progress messages
            try:
                while True:
                    resp = self._response_queue.get(timeout=120.0)
                    msg_type = resp.get("type")
                    if msg_type == "ready":
                        self._ready = True
                        print("[STT/Parakeet] Worker process ready")
                        return True
                    elif msg_type in ("downloading", "loading"):
                        print(f"[STT/Parakeet] Worker: {resp.get('message', msg_type)}")
                        # Keep waiting — reset timeout
                        continue
                    else:
                        print(f"[STT/Parakeet] Unexpected worker message: {resp}")
                        self._cleanup()
                        return False
            except Exception as e:
                print(f"[STT/Parakeet] Worker failed to start: {e}")
                self._cleanup()
                return False

    def _cleanup(self):
        """Clean up worker process."""
        if self._process is not None:
            if self._process.is_alive():
                self._request_queue.put(None)  # Shutdown signal
                self._process.join(timeout=5.0)
                if self._process.is_alive():
                    self._process.terminate()
            self._process = None
        self._request_queue = None
        self._response_queue = None
        self._ready = False

    def transcribe(self, audio_data: bytes, sample_rate: int = 16000) -> dict:
        """
        Transcribe audio via worker process.

        Args:
            audio_data: Raw PCM audio bytes (16-bit mono)
            sample_rate: Audio sample rate

        Returns:
            {"success": bool, "text": str, "confidence": float, "error": str}
        """
        if not self.ensure_started():
            return {
                "success": False,
                "text": "",
                "confidence": 0.0,
                "error": "Worker process not available"
            }

        self._request_queue.put({
            "type": "transcribe",
            "audio_data": audio_data,
            "sample_rate": sample_rate
        })

        try:
            resp = self._response_queue.get(timeout=30.0)
            if resp.get("type") == "result":
                return {
                    "success": resp["success"],
                    "text": resp["text"],
                    "confidence": resp["confidence"],
                    "error": resp["error"]
                }
            return {
                "success": False,
                "text": "",
                "confidence": 0.0,
                "error": f"Unexpected response type: {resp.get('type')}"
            }
        except Exception as e:
            print(f"[STT/Parakeet] Timeout waiting for transcription result")
            return {
                "success": False,
                "text": "",
                "confidence": 0.0,
                "error": str(e)
            }

    def warm_up(self):
        """Warm up worker process (ensures model is loaded)."""
        if not self.ensure_started():
            return

        self._request_queue.put({"type": "warmup"})
        try:
            self._response_queue.get(timeout=30.0)
        except:
            pass

    def shutdown(self):
        """Shutdown worker process."""
        with self._lock:
            self._cleanup()
            print("[STT/Parakeet] Worker process shut down")


# ============================================================================
# Module-Level Singleton
# ============================================================================

_process_manager = None
_manager_lock = threading.Lock()


def _get_manager() -> ParakeetProcessManager:
    """Get or create the process manager singleton."""
    global _process_manager
    if _process_manager is None:
        with _manager_lock:
            if _process_manager is None:
                _process_manager = ParakeetProcessManager()
    return _process_manager


# ============================================================================
# Public API (same interface as before)
# ============================================================================

def transcribe(audio_data: bytes, sample_rate: int = 16000) -> dict:
    """
    Transcribe audio using Parakeet ONNX model.

    Args:
        audio_data: Raw PCM audio bytes (16-bit mono)
        sample_rate: Audio sample rate

    Returns:
        {"success": bool, "text": str, "confidence": float, "error": str}
    """
    return _get_manager().transcribe(audio_data, sample_rate)


def warm_up():
    """Warm up the worker process (download models + load)."""
    _get_manager().warm_up()


def unload():
    """Shutdown worker process and free resources."""
    global _process_manager
    if _process_manager is not None:
        _process_manager.shutdown()
        _process_manager = None
    gc.collect()
    print("[STT/Parakeet] Unloaded")


def is_loaded() -> bool:
    """Check if worker process is running and ready."""
    if _process_manager is None:
        return False
    return _process_manager._ready and _process_manager._process is not None and _process_manager._process.is_alive()
