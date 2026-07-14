"""
Moonshine STT Provider (Local ONNX) - Worker Process Architecture

Uses Moonshine Base ONNX model for local speech recognition.
Models downloaded from HuggingFace: onnx-community/moonshine-base-ONNX

Runs inference in a separate process to avoid blocking the main server.
Architecture mirrors parakeet_stt.py worker process pattern.

Dependencies (all already in requirements.txt):
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
HF_REPO_ID = "onnx-community/moonshine-base-ONNX"

# Local models directory
MODELS_DIR = Path(__file__).resolve().parent.parent / "models" / "moonshine"

# Files needed for int8 inference
MODEL_FILES = [
    "config.json",
    "tokenizer.json",
    "onnx/encoder_model_int8.onnx",
    "onnx/decoder_model_merged_int8.onnx",
]


# ============================================================================
# Lightweight Tokenizer (no transformers dependency)
# ============================================================================

class MoonshineTokenizer:
    """Decode-only BPE tokenizer for Moonshine, loaded from tokenizer.json.

    Replaces transformers.AutoTokenizer — only needs to decode token IDs to text.
    Handles byte-level BPE (GPT-2 style) by reversing the byte-to-unicode mapping.
    """

    def __init__(self, path):
        import json as _json
        with open(path, 'r', encoding='utf-8') as f:
            data = _json.load(f)

        # Build id -> token mapping from vocab
        vocab = data.get('model', {}).get('vocab', {})
        self.id_to_token = {v: k for k, v in vocab.items()}

        # Overlay added tokens (special tokens take precedence)
        self.special_ids = set()
        for tok in data.get('added_tokens', []):
            self.id_to_token[tok['id']] = tok['content']
            if tok.get('special', False):
                self.special_ids.add(tok['id'])

        # Check decoder type for post-processing
        decoder_cfg = data.get('decoder', {})
        self._is_byte_level = self._check_byte_level(decoder_cfg)

        if self._is_byte_level:
            self._byte_decoder = self._build_byte_decoder()

    def _check_byte_level(self, decoder_cfg):
        """Recursively check if any decoder stage is ByteLevel."""
        if decoder_cfg.get('type') == 'ByteLevel':
            return True
        for sub in decoder_cfg.get('decoders', []):
            if self._check_byte_level(sub):
                return True
        return False

    @staticmethod
    def _build_byte_decoder():
        """Build unicode char -> byte mapping (inverse of GPT-2 bytes_to_unicode)."""
        bs = (
            list(range(ord("!"), ord("~") + 1))
            + list(range(ord("\xa1"), ord("\xac") + 1))
            + list(range(ord("\xae"), ord("\xff") + 1))
        )
        cs = bs[:]
        n = 0
        for b in range(256):
            if b not in bs:
                bs.append(b)
                cs.append(256 + n)
                n += 1
        return {chr(c): b for b, c in zip(bs, cs)}

    def decode(self, token_ids, skip_special_tokens=True):
        """Decode a sequence of token IDs to a text string."""
        tokens = []
        for tid in token_ids:
            tid = int(tid)
            if skip_special_tokens and tid in self.special_ids:
                continue
            tok = self.id_to_token.get(tid, '')
            tokens.append(tok)

        text = ''.join(tokens)

        if self._is_byte_level:
            byte_list = [self._byte_decoder.get(c, ord(c)) for c in text]
            text = bytes(byte_list).decode('utf-8', errors='replace')

        # SentencePiece uses ▁ (U+2581) as word boundary marker → replace with space
        text = text.replace('\u2581', ' ').strip()

        return text

    def batch_decode(self, batch_ids, skip_special_tokens=True):
        """Decode a batch of token ID sequences."""
        return [self.decode(ids, skip_special_tokens) for ids in batch_ids]


# ============================================================================
# Worker Process
# ============================================================================

def _moonshine_worker_main(request_queue, response_queue):
    """
    Worker process main loop. Runs Moonshine ONNX inference isolated from main process.

    This function runs in a separate process - all model operations happen here.
    """
    import json
    import numpy as np

    print(f"[STT/Moonshine Worker] Starting (PID: {os.getpid()})...")

    # Download models if needed
    from huggingface_hub import hf_hub_download

    MODELS_DIR.mkdir(parents=True, exist_ok=True)

    for filename in MODEL_FILES:
        local_path = MODELS_DIR / filename
        if local_path.exists():
            continue
        print(f"[STT/Moonshine Worker] Downloading {filename}...")
        response_queue.put({"type": "downloading", "message": f"Downloading {filename}..."})
        hf_hub_download(repo_id=HF_REPO_ID, filename=filename, local_dir=str(MODELS_DIR))
        print(f"[STT/Moonshine Worker] Downloaded {filename}")

    # Load config
    with open(MODELS_DIR / "config.json", 'r') as f:
        config = json.load(f)

    eos_token_id = config['eos_token_id']
    decoder_start_token_id = config['decoder_start_token_id']
    num_kv_heads = config['decoder_num_key_value_heads']
    dim_kv = config['hidden_size'] // config['decoder_num_attention_heads']
    num_layers = config['decoder_num_hidden_layers']
    max_pos = config['max_position_embeddings']

    # Load tokenizer (lightweight, no transformers needed)
    tokenizer = MoonshineTokenizer(str(MODELS_DIR / "tokenizer.json"))

    # Load ONNX sessions
    print("[STT/Moonshine Worker] Loading ONNX models...")
    response_queue.put({"type": "loading", "message": "Loading Moonshine STT models..."})
    load_start = time.time()

    import onnxruntime as ort
    sess_opts = ort.SessionOptions()
    sess_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

    encoder_session = ort.InferenceSession(
        str(MODELS_DIR / "onnx" / "encoder_model_int8.onnx"),
        sess_options=sess_opts,
    )
    decoder_session = ort.InferenceSession(
        str(MODELS_DIR / "onnx" / "decoder_model_merged_int8.onnx"),
        sess_options=sess_opts,
    )

    elapsed = (time.time() - load_start) * 1000
    print(f"[STT/Moonshine Worker] Models loaded in {elapsed:.0f}ms")

    # Signal ready
    response_queue.put({"type": "ready"})

    # Main loop
    while True:
        try:
            req = request_queue.get(timeout=1.0)
        except:
            continue

        if req is None:
            print("[STT/Moonshine Worker] Shutdown signal received")
            break

        req_type = req.get("type")

        if req_type == "transcribe":
            audio_data = req["audio_data"]
            sample_rate = req["sample_rate"]

            try:
                # Convert PCM int16 bytes to float32 numpy array [-1.0, 1.0]
                audio = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0
                audio = audio[np.newaxis, :]  # [1, samples] — batch dim

                start = time.time()

                # Encode audio
                encoder_outputs = encoder_session.run(None, {"input_values": audio})[0]

                # Prepare decoder inputs
                batch_size = encoder_outputs.shape[0]
                input_ids = np.array([[decoder_start_token_id]] * batch_size, dtype=np.int64)

                past_key_values = {
                    f'past_key_values.{layer}.{module}.{kv}': np.zeros(
                        [batch_size, num_kv_heads, 0, dim_kv], dtype=np.float32
                    )
                    for layer in range(num_layers)
                    for module in ('decoder', 'encoder')
                    for kv in ('key', 'value')
                }

                # Max tokens: ~6 per second of audio, minimum 10, capped at model max
                seconds = audio.shape[-1] / sample_rate
                max_len = min(max(int(seconds * 6), 10), max_pos)

                # Autoregressive decoding
                generated = input_ids
                for i in range(max_len):
                    use_cache = i > 0
                    logits, *present_kv = decoder_session.run(None, {
                        "input_ids": generated[:, -1:],
                        "encoder_hidden_states": encoder_outputs,
                        "use_cache_branch": np.array([use_cache]),
                        **past_key_values,
                    })

                    next_tokens = logits[:, -1].argmax(-1, keepdims=True)

                    # Update KV cache — encoder KV only changes on first iter
                    for j, key in enumerate(past_key_values):
                        if not use_cache or 'decoder' in key:
                            past_key_values[key] = present_kv[j]

                    generated = np.concatenate([generated, next_tokens], axis=-1)

                    if (next_tokens == eos_token_id).all():
                        break

                # Decode tokens to text
                results = tokenizer.batch_decode(generated, skip_special_tokens=True)
                text = results[0].strip() if results else ""

                elapsed_ms = (time.time() - start) * 1000
                print(f'[STT/Moonshine Worker] Transcribed: "{text}" ({elapsed_ms:.0f}ms)')

                response_queue.put({
                    "type": "result",
                    "success": True,
                    "text": text,
                    "confidence": 1.0,
                    "error": None,
                })

            except Exception as e:
                print(f"[STT/Moonshine Worker] Error: {e}")
                import traceback
                traceback.print_exc()
                response_queue.put({
                    "type": "result",
                    "success": False,
                    "text": "",
                    "confidence": 0.0,
                    "error": str(e),
                })

        elif req_type == "warmup":
            response_queue.put({"type": "warmup_done"})

    print("[STT/Moonshine Worker] Exiting")


# ============================================================================
# Process Manager (Main Process Side)
# ============================================================================

class MoonshineProcessManager:
    """Manages the Moonshine STT worker process from the main process."""

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

            print("[STT/Moonshine] Starting worker process...")

            # Use spawn to ensure clean process (no inherited state)
            ctx = mp.get_context('spawn')
            self._request_queue = ctx.Queue()
            self._response_queue = ctx.Queue()

            self._process = ctx.Process(
                target=_moonshine_worker_main,
                args=(self._request_queue, self._response_queue),
                daemon=True,
            )
            self._process.start()

            # Wait for ready signal, extending timeout on progress messages
            try:
                while True:
                    resp = self._response_queue.get(timeout=120.0)
                    msg_type = resp.get("type")
                    if msg_type == "ready":
                        self._ready = True
                        print("[STT/Moonshine] Worker process ready")
                        return True
                    elif msg_type in ("downloading", "loading"):
                        print(f"[STT/Moonshine] Worker: {resp.get('message', msg_type)}")
                        # Keep waiting — reset timeout
                        continue
                    else:
                        print(f"[STT/Moonshine] Unexpected worker message: {resp}")
                        self._cleanup()
                        return False
            except Exception as e:
                print(f"[STT/Moonshine] Worker failed to start: {e}")
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
            "sample_rate": sample_rate,
        })

        try:
            resp = self._response_queue.get(timeout=30.0)
            if resp.get("type") == "result":
                return {
                    "success": resp["success"],
                    "text": resp["text"],
                    "confidence": resp["confidence"],
                    "error": resp["error"],
                }
            return {
                "success": False,
                "text": "",
                "confidence": 0.0,
                "error": f"Unexpected response type: {resp.get('type')}"
            }
        except Exception as e:
            print("[STT/Moonshine] Timeout waiting for transcription result")
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
            print("[STT/Moonshine] Worker process shut down")


# ============================================================================
# Module-Level Singleton
# ============================================================================

_process_manager = None
_manager_lock = threading.Lock()


def _get_manager() -> MoonshineProcessManager:
    """Get or create the process manager singleton."""
    global _process_manager
    if _process_manager is None:
        with _manager_lock:
            if _process_manager is None:
                _process_manager = MoonshineProcessManager()
    return _process_manager


# ============================================================================
# Public API (same interface as parakeet_stt)
# ============================================================================

def transcribe(audio_data: bytes, sample_rate: int = 16000) -> dict:
    """
    Transcribe audio using Moonshine ONNX model.

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
    print("[STT/Moonshine] Unloaded")


def is_loaded() -> bool:
    """Check if worker process is running and ready."""
    if _process_manager is None:
        return False
    return _process_manager._ready and _process_manager._process is not None and _process_manager._process.is_alive()
