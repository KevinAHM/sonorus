"""
Pocket TTS ONNX Component

Runs ONNX inference in a separate process to avoid GIL/CPU contention.
Models and tokenizer are downloaded from HuggingFace: KevinAHM/pocket-tts-onnx

Dependencies:
    - onnxruntime (or onnxruntime-gpu for CUDA)
    - numpy
    - soundfile
    - sentencepiece
    - scipy (for resampling)
    - huggingface_hub
"""
import os
import gc
import time
import threading
import multiprocessing as mp
from pathlib import Path
from typing import Any, Callable, Dict, Generator, List, Optional, Tuple, Union

import numpy as np

from utils.text_utils import preprocess_text, remove_brackets, normalize_for_tts, chunk_text_for_tts
import random
from utils.localization import get_lowercase_map
from services.tts.voice_utils import compute_reference_hash

# ============================================================================
# Configuration
# ============================================================================

SAMPLE_RATE = 24_000  # Pocket TTS native sample rate
SAMPLES_PER_FRAME = 1920
FRAME_DURATION = SAMPLES_PER_FRAME / SAMPLE_RATE  # 0.08s per frame

# HuggingFace repo for ONNX models
HF_REPO_ID = "KevinAHM/pocket-tts-onnx"

# Local models directory (avoids HF cache issues on Windows)
MODELS_DIR = Path(__file__).resolve().parent.parent / "models" / "pocket"

# Voice reference directory
VOICE_DIR = Path(__file__).resolve().parent.parent / "voice_references"
_AUDIO_EXTS = {".wav", ".mp3", ".flac", ".m4a", ".ogg", ".opus"}


# ============================================================================
# Settings
# ============================================================================

def _get_pocket_config() -> Dict:
    """Get Pocket TTS ONNX configuration from settings."""
    try:
        from utils.settings import load_settings
        settings = load_settings()
        tts_settings = settings.get('tts', {})
        pocket_settings = tts_settings.get('pocket', {})

        return {
            "device": pocket_settings.get('device', 'cpu'),
            "temperature": float(pocket_settings.get('temperature', 0.7)),
            "lsd_steps": int(pocket_settings.get('lsd_steps', 10)),
            "eos_threshold": float(pocket_settings.get('eos_threshold', -4.0)),
            "cache_size": int(pocket_settings.get('cache_size', 50)),
            "speed": float(tts_settings.get('speed', 1.0)),
            "precision": pocket_settings.get('precision', 'int8'),
            "streaming": pocket_settings.get('streaming', True),
        }
    except Exception:
        return {
            "device": "cpu",
            "temperature": 0.7,
            "lsd_steps": 10,
            "eos_threshold": -4.0,
            "cache_size": 50,
            "speed": 1.0,
            "precision": "int8",
            "streaming": True,
        }


# ============================================================================
# Voice File Resolution (needed in main process)
# ============================================================================

def find_voice_file(name: str) -> Optional[Path]:
    """Find a voice file by name in voice_references directory."""
    if not name or len(name) > 255:
        return None

    if not VOICE_DIR.exists():
        return None

    name_path = Path(name)

    # Support full filename with extension
    if name_path.suffix and name_path.suffix.lower() in _AUDIO_EXTS:
        direct_path = VOICE_DIR / name
        if direct_path.is_file():
            return direct_path

    # Try uppercase variant using lowercase_map
    lowercase_map = get_lowercase_map()
    name_no_spaces = name.replace(" ", "").lower()

    for upper_name, lower_name in lowercase_map.items():
        if lower_name == name_no_spaces:
            for path in VOICE_DIR.iterdir():
                if not path.is_file():
                    continue
                if path.suffix.lower() not in _AUDIO_EXTS:
                    continue
                if path.stem.startswith(upper_name):
                    return path

    # Look for matching stem
    for path in VOICE_DIR.iterdir():
        if not path.is_file():
            continue
        if path.suffix.lower() not in _AUDIO_EXTS:
            continue
        if path.stem == name:
            return path

    return None


def _resolve_voice(voice_id: str) -> Optional[str]:
    """Resolve voice_id to file path string."""
    voice_path = find_voice_file(voice_id)
    if voice_path:
        return str(voice_path)

    # Try with _reference_15s suffix
    ref_name = f"{voice_id}_reference_15s"
    voice_path = find_voice_file(ref_name)
    if voice_path:
        return str(voice_path)

    # If voice_id is a hashed clone name (e.g. "PlayerMale_DE_DE_cafb73f0"),
    # parse it back to the original character name + language and retry.
    from services.tts.voice_utils import parse_hashed_voice_name, find_voice_reference
    original_name, lang, _ = parse_hashed_voice_name(voice_id)
    if original_name != voice_id:
        path = find_voice_reference(original_name, language=lang or "EN_US")
        if path:
            return str(path)

    return None


# ============================================================================
# ONNX Worker Process
# ============================================================================

def _onnx_worker_main(
    request_queue,
    response_queue,
    config: Dict
):
    """
    Worker process main loop. Runs ONNX inference isolated from main process.

    This function runs in a separate process - all ONNX operations happen here.
    """
    import numpy as np

    # Import heavy deps only in worker process
    import onnxruntime as ort
    import sentencepiece as spm
    import soundfile as sf
    import scipy.signal
    from huggingface_hub import hf_hub_download

    print(f"[PocketONNX Worker] Starting (PID: {os.getpid()})...")

    # ========== Engine Setup ==========

    precision = config.get("precision", "int8")
    device = config.get("device", "cpu")
    temperature = config.get("temperature", 0.7)
    lsd_steps = config.get("lsd_steps", 10)
    eos_threshold = config.get("eos_threshold", -4.0)

    # Get providers
    if device == "cpu":
        providers = ["CPUExecutionProvider"]
    elif device == "cuda":
        providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
    else:
        available = ort.get_available_providers()
        if "CUDAExecutionProvider" in available:
            providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
        else:
            providers = ["CPUExecutionProvider"]

    # Session options - cap intra-op threads to avoid over-subscription
    # overhead on the small sequential matmuls in the autoregressive loop.
    sess_opts = ort.SessionOptions()
    sess_opts.intra_op_num_threads = min(os.cpu_count() or 4, 4)
    sess_opts.inter_op_num_threads = 1

    # Download and load models (local cache in sonorus/models/pocket/)
    def get_model(filename: str) -> str:
        """Get model path, downloading from HF if not cached locally."""
        local_path = MODELS_DIR / filename

        # Check if already cached locally
        if local_path.exists():
            return str(local_path)

        # Download from HuggingFace directly into local models dir
        print(f"[PocketONNX Worker] Downloading {filename}...")
        response_queue.put({"type": "downloading", "message": f"Downloading {filename}..."})
        MODELS_DIR.mkdir(parents=True, exist_ok=True)
        hf_hub_download(repo_id=HF_REPO_ID, filename=filename, local_dir=str(MODELS_DIR))
        print(f"[PocketONNX Worker] Downloaded {filename}")

        return str(local_path)

    suffix = "_int8" if precision == "int8" else ""

    print(f"[PocketONNX Worker] Loading models (precision={precision}, device={device})...")

    tokenizer_path = get_model("tokenizer.model")
    tokenizer = spm.SentencePieceProcessor()
    tokenizer.Load(tokenizer_path)

    mimi_encoder = ort.InferenceSession(
        get_model("onnx/mimi_encoder.onnx"),
        sess_options=sess_opts, providers=providers
    )
    text_conditioner = ort.InferenceSession(
        get_model("onnx/text_conditioner.onnx"),
        sess_options=sess_opts, providers=providers
    )
    flow_lm_main = ort.InferenceSession(
        get_model(f"onnx/flow_lm_main{suffix}.onnx"),
        sess_options=sess_opts, providers=providers
    )

    flow_lm_flow = ort.InferenceSession(
        get_model(f"onnx/flow_lm_flow{suffix}.onnx"),
        sess_options=sess_opts, providers=providers
    )

    mimi_decoder = ort.InferenceSession(
        get_model(f"onnx/mimi_decoder{suffix}.onnx"),
        sess_options=sess_opts, providers=providers
    )

    # Pre-compute flow buffers for configured lsd_steps
    dt = 1.0 / lsd_steps
    st_buffers = []
    for j in range(lsd_steps):
        s = j / lsd_steps
        t = s + dt
        st_buffers.append((
            np.array([[s]], dtype=np.float32),
            np.array([[t]], dtype=np.float32)
        ))

    # Pre-compute fallback buffers for LSD=1 (used when running below real-time)
    dt_fallback = 1.0
    st_buffers_fallback = [(
        np.array([[0.0]], dtype=np.float32),
        np.array([[1.0]], dtype=np.float32)
    )]

    # Voice embedding cache (in worker process)
    voice_cache: Dict[str, np.ndarray] = {}

    print(f"[PocketONNX Worker] Ready")
    response_queue.put({"type": "ready"})

    # ========== Helper Functions ==========

    def init_state(session) -> dict:
        state = {}
        type_map = {
            "tensor(float)": np.float32,
            "tensor(int64)": np.int64,
            "tensor(bool)": np.bool_,
        }
        for inp in session.get_inputs():
            if inp.name.startswith("state_"):
                shape = [s if isinstance(s, int) else 0 for s in inp.shape]
                dtype = type_map.get(inp.type, np.float32)
                state[inp.name] = np.zeros(shape, dtype=dtype)
        return state

    def update_state(state: dict, result: list, session):
        for i in range(2, len(session.get_outputs())):
            name = session.get_outputs()[i].name
            if name.startswith("out_state_"):
                idx = int(name.replace("out_state_", ""))
                state[f"state_{idx}"] = result[i]

    def load_audio(path: str) -> np.ndarray:
        audio, sr = sf.read(path)
        if len(audio.shape) > 1:
            audio = audio.mean(axis=1)
        if sr != SAMPLE_RATE:
            num_samples = int(len(audio) * SAMPLE_RATE / sr)
            audio = scipy.signal.resample(audio, num_samples)
        audio = audio.astype(np.float32)
        if np.abs(audio).max() > 1.0:
            audio = audio / np.abs(audio).max()
        return audio.reshape(1, 1, -1)

    def get_voice_embeddings(voice_path: str, voice_hash: Optional[str] = None) -> np.ndarray:
        # Build cache key using hash for invalidation
        cache_key = f"{voice_path}:{voice_hash}" if voice_hash else voice_path
        npy_path = f"{voice_path}.{voice_hash}.pocket.npy" if voice_hash else f"{voice_path}.pocket.npy"

        # 1. Check memory cache
        if cache_key in voice_cache:
            return voice_cache[cache_key]

        # 2. Check disk cache (.pocket.npy with hash)
        if os.path.exists(npy_path):
            try:
                embeddings = np.load(npy_path)
                voice_cache[cache_key] = embeddings
                print(f"[PocketONNX Worker] Loaded cached embedding: {npy_path}")
                return embeddings
            except Exception as e:
                print(f"[PocketONNX Worker] Failed to load cached embedding, recomputing: {e}")

        # 3. Compute from audio
        audio = load_audio(voice_path)
        embeddings = mimi_encoder.run(None, {"audio": audio})[0]
        while embeddings.ndim > 3:
            embeddings = embeddings.squeeze(0)
        if embeddings.ndim < 3:
            embeddings = embeddings[None]

        # 4. Save to disk cache
        try:
            np.save(npy_path, embeddings)
            print(f"[PocketONNX Worker] Saved embedding cache: {npy_path}")
        except Exception as e:
            print(f"[PocketONNX Worker] Failed to save embedding cache: {e}")

        # 5. Store in memory cache
        voice_cache[cache_key] = embeddings
        return embeddings

    def tokenize_text(text: str) -> np.ndarray:
        text = text.strip()
        if text and text[-1].isalnum():
            text = text + "."
        if text and not text[0].isupper():
            text = text[0].upper() + text[1:]
        token_ids = tokenizer.Encode(text)
        return np.array(token_ids, dtype=np.int64).reshape(1, -1)

    # ========== Warmup Synthesis ==========
    # Run a quick synthesis to warm up the inference pipeline
    print("[PocketONNX Worker] Running warmup synthesis...")
    warmup_start = time.time()
    try:
        # Short text for minimal warmup time
        warmup_text = "Hello."
        warmup_tokens = tokenize_text(warmup_text)
        warmup_text_emb = text_conditioner.run(None, {"token_ids": warmup_tokens})[0]
        if warmup_text_emb.ndim == 2:
            warmup_text_emb = warmup_text_emb[None]

        # Initialize warmup state
        warmup_state = init_state(flow_lm_main)

        # Text conditioning pass (no voice - just warming up the model)
        empty_seq = np.zeros((1, 0, 32), dtype=np.float32)
        res_warmup = flow_lm_main.run(None, {
            "sequence": empty_seq,
            "text_embeddings": warmup_text_emb,
            **warmup_state
        })
        update_state(warmup_state, res_warmup, flow_lm_main)

        # Generate just a few frames to warm up flow and decoder
        curr = np.full((1, 1, 32), np.nan, dtype=np.float32)
        empty_text = np.zeros((1, 0, 1024), dtype=np.float32)
        warmup_mimi_state = init_state(mimi_decoder)

        for _ in range(3):  # Just 3 frames
            res_step = flow_lm_main.run(None, {
                "sequence": curr,
                "text_embeddings": empty_text,
                **warmup_state
            })
            conditioning = res_step[0]
            update_state(warmup_state, res_step, flow_lm_main)

            # Flow step
            x = np.zeros((1, 32), dtype=np.float32)
            s_arr, t_arr = st_buffers[0]
            flow_out = flow_lm_flow.run(None, {"c": conditioning, "s": s_arr, "t": t_arr, "x": x})
            x = x + flow_out[0] * dt

            curr = x.reshape(1, 1, 32)

            # Decoder step
            mimi_decoder.run(None, {"latent": curr, **warmup_mimi_state})

        warmup_elapsed = (time.time() - warmup_start) * 1000
        print(f"[PocketONNX Worker] Warmup complete in {warmup_elapsed:.0f}ms")
    except Exception as e:
        print(f"[PocketONNX Worker] Warmup failed (non-fatal): {e}")

    # ========== Main Loop ==========

    while True:
        try:
            req = request_queue.get(timeout=1.0)
        except:
            continue

        if req is None:
            print("[PocketONNX Worker] Shutdown signal received")
            break

        req_type = req.get("type")

        if req_type == "synthesize":
            text = req["text"]
            voice_path = req["voice_path"]
            voice_hash = req.get("voice_hash")
            temp_override = req.get("temperature")
            do_align = False  # Alignment handled by amplitude visemes in main process
            do_stream = req.get("streaming", True)

            try:
                # Get voice embeddings
                voice_emb = get_voice_embeddings(voice_path, voice_hash)
                text_ids = tokenize_text(text)

                # Use override or default temperature
                curr_temp = temp_override if temp_override is not None else temperature

                # Text conditioning
                text_emb = text_conditioner.run(None, {"token_ids": text_ids})[0]
                if text_emb.ndim == 2:
                    text_emb = text_emb[None]

                # Initialize state
                state = init_state(flow_lm_main)
                mimi_state = init_state(mimi_decoder)

                empty_seq = np.zeros((1, 0, 32), dtype=np.float32)
                empty_text = np.zeros((1, 0, 1024), dtype=np.float32)

                # Voice conditioning pass
                res_voice = flow_lm_main.run(None, {
                    "sequence": empty_seq,
                    "text_embeddings": voice_emb,
                    **state
                })
                update_state(state, res_voice, flow_lm_main)

                # Text conditioning pass
                res_text = flow_lm_main.run(None, {
                    "sequence": empty_seq,
                    "text_embeddings": text_emb,
                    **state
                })
                update_state(state, res_text, flow_lm_main)

                # Streaming mode: alignment state for incremental processing
                if do_stream and do_align:
                    from services.alignment import align_audio_to_words
                    words_original = text.split()
                    word_cursor = 0
                    audio_buffer_16k = np.array([], dtype=np.float32)
                    buffer_offset_time = 0.0
                    last_alignment_check = time.time()
                    alignment_interval = 0.3
                    first_word_committed = False
                    synthesis_start_time = time.time()

                # Autoregressive generation
                curr = np.full((1, 1, 32), np.nan, dtype=np.float32)
                latent_buffer = []
                decoded_frames = 0
                first_chunk_frames = 2
                max_chunk_frames = 15
                eos_step = None  # Track when EOS first detected
                frames_after_eos = 3  # Continue generating this many frames after EOS

                # Dynamic LSD adjustment state
                use_fallback_lsd = do_stream  # Always LSD=1 when streaming
                frame_duration = SAMPLES_PER_FRAME / SAMPLE_RATE  # 0.08s per frame
                generation_start_time = time.time()
                target_rtf = 1.2  # Throttle to 120% real-time when streaming
                playback_buffer_frames = 13  # ~1.0s buffer before throttling engages

                for frame_idx in range(500):  # max_frames
                    # Run main model
                    res_step = flow_lm_main.run(None, {
                        "sequence": curr,
                        "text_embeddings": empty_text,
                        **state
                    })

                    conditioning = res_step[0]
                    eos_logit = res_step[1]
                    update_state(state, res_step, flow_lm_main)

                    # Check EOS - record when EOS is first detected
                    if eos_logit[0][0] > eos_threshold and eos_step is None:
                        eos_step = frame_idx

                    # Stop only after frames_after_eos additional frames
                    if eos_step is not None and frame_idx >= eos_step + frames_after_eos:
                        break

                    # Flow matching - use fallback LSD=1 if running below real-time
                    std = np.sqrt(curr_temp) if curr_temp > 0 else 0.0
                    x = np.random.normal(0, std, (1, 32)).astype(np.float32) if std > 0 else np.zeros((1, 32), dtype=np.float32)

                    if use_fallback_lsd:
                        # LSD=1: single step
                        s_arr, t_arr = st_buffers_fallback[0]
                        flow_out = flow_lm_flow.run(None, {
                            "c": conditioning,
                            "s": s_arr,
                            "t": t_arr,
                            "x": x
                        })
                        x = x + flow_out[0] * dt_fallback
                    else:
                        # Normal LSD steps
                        for j in range(lsd_steps):
                            s_arr, t_arr = st_buffers[j]
                            flow_out = flow_lm_flow.run(None, {
                                "c": conditioning,
                                "s": s_arr,
                                "t": t_arr,
                                "x": x
                            })
                            x = x + flow_out[0] * dt

                    latent = x.reshape(1, 1, 32)
                    latent_buffer.append(latent)
                    curr = latent

                    # Streaming mode: decode and send during generation
                    if do_stream:
                        pending = len(latent_buffer) - decoded_frames
                        chunk_size = 0

                        if decoded_frames == 0 and pending >= first_chunk_frames:
                            chunk_size = first_chunk_frames
                        elif pending >= max_chunk_frames:
                            chunk_size = max_chunk_frames

                        if chunk_size > 0:
                            latents_chunk = np.concatenate(
                                latent_buffer[decoded_frames:decoded_frames + chunk_size],
                                axis=1
                            )
                            res = mimi_decoder.run(None, {"latent": latents_chunk, **mimi_state})
                            audio_chunk = res[0].squeeze()

                            for k, val in enumerate(res[1:]):
                                mimi_state[f"state_{k}"] = val

                            decoded_frames += chunk_size

                            # Streaming alignment processing
                            chunk_alignments = []
                            if do_align and word_cursor < len(words_original):
                                num_samples_16k = int(len(audio_chunk) * 16000 / SAMPLE_RATE)
                                chunk_16k = scipy.signal.resample(audio_chunk, num_samples_16k).astype(np.float32)
                                audio_buffer_16k = np.concatenate([audio_buffer_16k, chunk_16k])

                                if (time.time() - last_alignment_check) > alignment_interval:
                                    buffer_duration = len(audio_buffer_16k) / 16000

                                    # Limit text to ~3 words per second of audio (with minimum of 3)
                                    # This prevents trying to align tiny audio against huge text
                                    max_words_to_align = max(3, int(buffer_duration * 3) + 2)
                                    remaining_words = words_original[word_cursor:]
                                    words_to_align = remaining_words[:max_words_to_align]
                                    remaining_text = " ".join(words_to_align)

                                    try:
                                        res_align = align_audio_to_words(
                                            audio_buffer_16k,
                                            remaining_text,
                                            audio_sample_rate=16000,
                                            partial=True
                                        )
                                        detected_words = res_align["words"]
                                        detected_starts = res_align["wordStartTimeSeconds"]
                                        detected_ends = res_align["wordEndTimeSeconds"]

                                        # DEBUG: Log alignment results
                                        print(f"[PocketONNX Worker] ALIGN: buf={buffer_duration:.2f}s, trying={len(words_to_align)}/{len(remaining_words)} words, detected={len(detected_words)}: {detected_words[:5]}{'...' if len(detected_words) > 5 else ''}")

                                        words_to_commit = 0

                                        for i, det_word in enumerate(detected_words):
                                            if word_cursor + i >= len(words_original):
                                                break
                                            target_word = words_original[word_cursor + i]
                                            match = det_word.lower().strip(",.?!") == target_word.lower().strip(",.?!")
                                            if not match:
                                                print(f"[PocketONNX Worker] ALIGN: Word {i} mismatch: detected='{det_word}' vs target='{target_word}'")
                                                break

                                            is_last_detected = (i == len(detected_words) - 1)
                                            is_last_total = (word_cursor + i == len(words_original) - 1)
                                            force_commit = (buffer_duration > 2.0 and is_last_detected)

                                            if is_last_detected and not is_last_total and not force_commit:
                                                print(f"[PocketONNX Worker] ALIGN: Skipping last detected word '{det_word}' (buf={buffer_duration:.2f}s, force={force_commit})")
                                                continue

                                            start_abs = detected_starts[i] + buffer_offset_time
                                            end_abs = detected_ends[i] + buffer_offset_time

                                            chunk_alignments.append({
                                                "word": target_word,
                                                "start": round(start_abs, 3),
                                                "end": round(end_abs, 3)
                                            })
                                            words_to_commit += 1

                                        # Log commit result
                                        print(f"[PocketONNX Worker] ALIGN: Committing {words_to_commit} words")

                                        if words_to_commit > 0:
                                            # Profile first word commit
                                            if not first_word_committed:
                                                first_word_committed = True
                                                elapsed = (time.time() - synthesis_start_time) * 1000
                                                print(f"[PocketONNX Worker] PROFILE: First word committed at {elapsed:.0f}ms (buf={buffer_duration:.2f}s)")

                                            word_cursor += words_to_commit
                                            last_commit_end_rel = detected_ends[words_to_commit - 1]
                                            cut_samples = int(last_commit_end_rel * 16000)

                                            if cut_samples < len(audio_buffer_16k):
                                                audio_buffer_16k = audio_buffer_16k[cut_samples:]
                                                buffer_offset_time += last_commit_end_rel
                                            else:
                                                audio_buffer_16k = np.array([], dtype=np.float32)
                                                buffer_offset_time += last_commit_end_rel

                                        last_alignment_check = time.time()
                                    except Exception:
                                        pass

                            response_queue.put({
                                "type": "chunk",
                                "audio": audio_chunk.astype(np.float32).tobytes(),
                                "samples": len(audio_chunk),
                                "alignments": chunk_alignments if do_align else None
                            })

                            # Check real RTF after decode+alignment and switch to LSD=1 if needed
                            if not use_fallback_lsd:
                                elapsed = time.time() - generation_start_time
                                audio_decoded_sec = decoded_frames * frame_duration
                                rtf = audio_decoded_sec / elapsed if elapsed > 0 else 999
                                if rtf < 1.0:
                                    use_fallback_lsd = True
                                    print(f"[PocketONNX Worker] RTF={rtf:.2f}x (after decode+align), switching to LSD=1")


                    # Throttle: yield CPU if generating faster than target_rtf
                    # Skip throttling until playback buffer is filled (~1.0s)
                    if do_stream and frame_idx >= playback_buffer_frames:
                        elapsed = time.time() - generation_start_time
                        audio_generated_sec = (frame_idx + 1) * frame_duration
                        target_elapsed = audio_generated_sec / target_rtf
                        if elapsed < target_elapsed:
                            time.sleep(target_elapsed - elapsed)

                # ===== Post-generation decoding =====

                if do_stream:
                    # Streaming: decode remaining latents frame-by-frame to avoid clipping
                    remaining_count = len(latent_buffer) - decoded_frames
                    print(f"[PocketONNX Worker] Post-EOS: {remaining_count} frames to decode frame-by-frame")

                    final_audio_chunks = []
                    while decoded_frames < len(latent_buffer):
                        single_latent = latent_buffer[decoded_frames]  # Already (1, 1, 32)
                        res = mimi_decoder.run(None, {"latent": single_latent, **mimi_state})
                        audio_chunk = res[0].squeeze()

                        for k, val in enumerate(res[1:]):
                            mimi_state[f"state_{k}"] = val

                        decoded_frames += 1
                        final_audio_chunks.append(audio_chunk)

                    if final_audio_chunks:
                        print(f"[PocketONNX Worker] Decoded {len(final_audio_chunks)} final frames, total samples: {sum(len(c) for c in final_audio_chunks)}")
                        combined_audio = np.concatenate(final_audio_chunks)

                        chunk_alignments = []
                        if do_align and word_cursor < len(words_original):
                            num_samples_16k = int(len(combined_audio) * 16000 / SAMPLE_RATE)
                            chunk_16k = scipy.signal.resample(combined_audio, num_samples_16k).astype(np.float32)
                            audio_buffer_16k = np.concatenate([audio_buffer_16k, chunk_16k])

                            remaining_text = " ".join(words_original[word_cursor:])
                            try:
                                res_align = align_audio_to_words(
                                    audio_buffer_16k,
                                    remaining_text,
                                    audio_sample_rate=16000,
                                    partial=False
                                )
                                for i, det_word in enumerate(res_align["words"]):
                                    if word_cursor + i >= len(words_original):
                                        break
                                    chunk_alignments.append({
                                        "word": words_original[word_cursor + i],
                                        "start": round(res_align["wordStartTimeSeconds"][i] + buffer_offset_time, 3),
                                        "end": round(res_align["wordEndTimeSeconds"][i] + buffer_offset_time, 3)
                                    })
                            except Exception:
                                pass

                        response_queue.put({
                            "type": "chunk",
                            "audio": combined_audio.astype(np.float32).tobytes(),
                            "samples": len(combined_audio),
                            "alignments": chunk_alignments if do_align else None
                        })
                else:
                    # Non-streaming: decode all latents in chunks of 15, then align once
                    all_audio_chunks = []

                    while decoded_frames < len(latent_buffer):
                        chunk_size = min(max_chunk_frames, len(latent_buffer) - decoded_frames)
                        latents_chunk = np.concatenate(
                            latent_buffer[decoded_frames:decoded_frames + chunk_size],
                            axis=1
                        )
                        res = mimi_decoder.run(None, {"latent": latents_chunk, **mimi_state})
                        audio_chunk = res[0].squeeze()

                        for k, val in enumerate(res[1:]):
                            mimi_state[f"state_{k}"] = val

                        decoded_frames += chunk_size
                        all_audio_chunks.append(audio_chunk)

                    # Concatenate all audio
                    if all_audio_chunks:
                        full_audio = np.concatenate(all_audio_chunks)
                    else:
                        full_audio = np.array([], dtype=np.float32)

                    # Run alignment on complete audio
                    final_alignments = None
                    if do_align and len(full_audio) > 0:
                        from services.alignment import align_audio_to_words
                        try:
                            num_samples_16k = int(len(full_audio) * 16000 / SAMPLE_RATE)
                            audio_16k = scipy.signal.resample(full_audio, num_samples_16k).astype(np.float32)

                            res_align = align_audio_to_words(
                                audio_16k,
                                text,
                                audio_sample_rate=16000,
                                partial=False
                            )
                            final_alignments = [
                                {"word": w, "start": round(s, 3), "end": round(e, 3)}
                                for w, s, e in zip(
                                    res_align["words"],
                                    res_align["wordStartTimeSeconds"],
                                    res_align["wordEndTimeSeconds"]
                                )
                            ]
                        except Exception:
                            pass

                    # Send complete audio as single chunk
                    if len(full_audio) > 0:
                        response_queue.put({
                            "type": "chunk",
                            "audio": full_audio.astype(np.float32).tobytes(),
                            "samples": len(full_audio),
                            "alignments": final_alignments
                        })

                # Send trailing silence to prevent 3D audio cutoff (200ms)
                silence_samples = int(SAMPLE_RATE * 0.2)
                silence = np.zeros(silence_samples, dtype=np.float32)
                response_queue.put({
                    "type": "chunk",
                    "audio": silence.tobytes(),
                    "samples": silence_samples,
                    "alignments": None
                })

                response_queue.put({"type": "done"})

            except Exception as e:
                import traceback
                traceback.print_exc()
                response_queue.put({"type": "error", "error": str(e)})

        elif req_type == "warmup":
            voice_path = req.get("voice_path")
            voice_hash = req.get("voice_hash")
            if voice_path:
                try:
                    get_voice_embeddings(voice_path, voice_hash)
                    print(f"[PocketONNX Worker] Warmed up voice: {voice_path}")
                except Exception as e:
                    print(f"[PocketONNX Worker] Warmup failed: {e}")
            response_queue.put({"type": "warmup_done"})

        elif req_type == "clear_embedding":
            voice_path = req.get("voice_path")
            if voice_path:
                # Clear memory cache entries for this voice path (any hash)
                # Match exact path or path with hash suffix (path:hash)
                keys_to_delete = [k for k in voice_cache if k == voice_path or k.startswith(f"{voice_path}:")]
                for k in keys_to_delete:
                    del voice_cache[k]
                if keys_to_delete:
                    print(f"[PocketONNX Worker] Cleared {len(keys_to_delete)} memory cache entries for: {voice_path}")
                # Clear disk cache files (all hashes for this voice)
                import glob
                pattern = glob.escape(voice_path) + ".*.pocket.npy"
                for npy_file in glob.glob(pattern):
                    try:
                        os.remove(npy_file)
                        print(f"[PocketONNX Worker] Deleted disk cache: {npy_file}")
                    except Exception as e:
                        print(f"[PocketONNX Worker] Failed to delete disk cache: {e}")
            response_queue.put({"type": "clear_embedding_done"})

    print("[PocketONNX Worker] Exiting")


# ============================================================================
# Process Manager (Main Process Side)
# ============================================================================

class PocketONNXProcessManager:
    """Manages the ONNX worker process from the main process."""

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

            print("[PocketONNX] Starting worker process...")

            # Use spawn to ensure clean process (no inherited state)
            ctx = mp.get_context('spawn')
            self._request_queue = ctx.Queue()
            self._response_queue = ctx.Queue()

            config = _get_pocket_config()

            self._process = ctx.Process(
                target=_onnx_worker_main,
                args=(self._request_queue, self._response_queue, config),
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
                        print("[PocketONNX] Worker process ready")
                        return True
                    elif msg_type in ("downloading", "loading"):
                        print(f"[PocketONNX] Worker: {resp.get('message', msg_type)}")
                        # Keep waiting — reset timeout
                        continue
                    else:
                        print(f"[PocketONNX] Unexpected worker message: {resp}")
                        self._cleanup()
                        return False
            except Exception as e:
                print(f"[PocketONNX] Worker failed to start: {e}")
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

    def synthesize(
        self,
        text: str,
        voice_id: str,
        on_chunk: Callable[[bytes, Optional[Dict]], None],
        temperature: Optional[float] = None,
        align: bool = True
    ) -> bool:
        """
        Synthesize text to speech via worker process.

        Args:
            text: Text to synthesize
            voice_id: Voice name or path
            on_chunk: Callback(pcm_bytes, alignment) for each chunk
            temperature: Optional temperature override
            align: Enable word-level alignment (default True)

        Returns:
            True on success, False on error
        """
        # Preprocess in main process (lightweight)
        text = preprocess_text(text)
        text = remove_brackets(text)
        text = normalize_for_tts(text)
        if not text:
            print("[PocketONNX] Text is empty after normalization")
            return False

        # Chunk text for natural sentence breaks (targets ~50 tokens per chunk)
        text_chunks = chunk_text_for_tts(text, target_tokens=50)
        if not text_chunks:
            print("[PocketONNX] No text chunks after splitting")
            return False

        total_tokens = len(text.split())
        if len(text_chunks) > 1:
            print(f"[PocketONNX] Split into {len(text_chunks)} chunks ({total_tokens} tokens): {[len(c.split()) for c in text_chunks]} words each")
        else:
            print(f"[PocketONNX] Single chunk ({total_tokens} tokens, under 50 limit or single sentence)")

        # Resolve voice path and compute hash
        voice_path = _resolve_voice(voice_id)
        if not voice_path:
            print(f"[PocketONNX] Voice not found: {voice_id}")
            return False

        voice_hash = compute_reference_hash(voice_path)
        if not voice_hash:
            print(f"[PocketONNX] Failed to compute voice hash: {voice_path}")
            return False

        # Ensure worker is running
        if not self.ensure_started():
            print("[PocketONNX] Worker process not available")
            return False

        # Get streaming setting from config
        config = _get_pocket_config()
        streaming = config.get("streaming", True)

        start_time = time.time()
        total_bytes = 0
        chunk_count = 0
        cumulative_time = 0.0  # Track time offset for alignment across chunks

        try:
            for chunk_idx, text_chunk in enumerate(text_chunks):
                # Add silence gap between chunks (0.25-1.0s random)
                if chunk_idx > 0:
                    silence_duration = random.uniform(0.25, 1.0)
                    silence_samples = int(silence_duration * SAMPLE_RATE)
                    silence_pcm = np.zeros(silence_samples, dtype=np.int16).tobytes()
                    on_chunk(silence_pcm, None)
                    total_bytes += len(silence_pcm)
                    cumulative_time += silence_duration
                    print(f"[PocketONNX] Added {silence_duration:.2f}s silence gap")

                # Send request to worker for this chunk
                self._request_queue.put({
                    "type": "synthesize",
                    "text": text_chunk,
                    "voice_path": voice_path,
                    "voice_hash": voice_hash,
                    "temperature": temperature,
                    "align": align,
                    "streaming": streaming
                })

                # Receive audio chunks for this text chunk
                chunk_timeout = 60.0 if not streaming else 30.0
                is_first_audio_chunk = (chunk_count == 0)

                while True:
                    try:
                        resp = self._response_queue.get(timeout=chunk_timeout)
                    except:
                        print("[PocketONNX] Timeout waiting for worker response")
                        return False

                    resp_type = resp.get("type")

                    if resp_type == "chunk":
                        # Convert float32 bytes back to numpy, then to int16 PCM
                        audio_bytes = resp["audio"]
                        num_samples = resp["samples"]
                        chunk_alignments = resp.get("alignments")
                        audio_chunk = np.frombuffer(audio_bytes, dtype=np.float32).copy()

                        # Apply fade-in on very first chunk only
                        if is_first_audio_chunk:
                            fade_samples = int(SAMPLE_RATE * 0.002)
                            if len(audio_chunk) > fade_samples:
                                fade_curve = np.linspace(0, 1, fade_samples)
                                audio_chunk[:fade_samples] *= fade_curve
                            is_first_audio_chunk = False

                        # Convert to int16 PCM
                        audio_np = np.clip(audio_chunk, -1.0, 1.0)
                        pcm_int16 = (audio_np * 32767).astype(np.int16)
                        pcm_bytes = pcm_int16.tobytes()

                        total_bytes += len(pcm_bytes)
                        chunk_count += 1

                        # Convert alignment list to dict format, adjusting times for cumulative offset
                        word_timing = None
                        if chunk_alignments:
                            word_timing = {
                                "words": [a["word"] for a in chunk_alignments],
                                "wordStartTimeSeconds": [a["start"] + cumulative_time for a in chunk_alignments],
                                "wordEndTimeSeconds": [a["end"] + cumulative_time for a in chunk_alignments]
                            }

                        on_chunk(pcm_bytes, word_timing)

                    elif resp_type == "done":
                        # Actually, simpler: track from total_bytes
                        cumulative_time = total_bytes / (SAMPLE_RATE * 2)
                        break

                    elif resp_type == "error":
                        print(f"[PocketONNX] Worker error: {resp.get('error')}")
                        return False

            proc_time = time.time() - start_time
            duration = total_bytes / (SAMPLE_RATE * 2)
            rtf = duration / proc_time if proc_time > 0 else 0

            print(f"[PocketONNX] Synthesis complete: {chunk_count} chunks, {total_bytes} bytes, {duration:.2f}s audio")
            print(f"[PocketONNX] Stats: {proc_time*1000:.1f}ms | RTF: {rtf:.2f}x")

            return total_bytes > 0

        except Exception as e:
            print(f"[PocketONNX] Synthesis failed: {e}")
            import traceback
            traceback.print_exc()
            return False

    def synthesize_sentence(self, text: str, voice_path: str, voice_hash: str,
                             on_chunk: Callable, temperature: Optional[float] = None,
                             cumulative_time: float = 0.0, align: bool = True,
                             is_first_audio_chunk: bool = False) -> Tuple[bool, int, float]:
        """
        Synthesize a single pre-processed sentence via the worker process.

        This is the inner loop extracted from synthesize() for reuse by
        sentence-streaming. The text should already be preprocessed/normalized.

        Args:
            text: Pre-processed text to synthesize (already normalized)
            voice_path: Resolved voice file path
            voice_hash: Hash of voice reference file
            on_chunk: Callback(pcm_bytes, alignment) for each audio chunk
            temperature: Optional temperature override
            cumulative_time: Time offset in seconds for alignment (from previous sentences)
            align: Enable word-level alignment
            is_first_audio_chunk: Whether to apply fade-in on very first chunk

        Returns:
            (success, bytes_produced, updated_cumulative_time) tuple
        """
        config = _get_pocket_config()
        streaming = config.get("streaming", True)

        bytes_produced = 0

        # Sub-chunk if sentence is long (>50 tokens)
        text_chunks = chunk_text_for_tts(text, target_tokens=50)
        if not text_chunks:
            return (True, 0, cumulative_time)

        for chunk_idx, text_chunk in enumerate(text_chunks):
            # Add silence gap between sub-chunks within a sentence
            if chunk_idx > 0:
                silence_duration = random.uniform(0.15, 0.4)
                silence_samples = int(silence_duration * SAMPLE_RATE)
                silence_pcm = np.zeros(silence_samples, dtype=np.int16).tobytes()
                on_chunk(silence_pcm, None)
                bytes_produced += len(silence_pcm)
                cumulative_time += silence_duration

            # Send request to worker
            self._request_queue.put({
                "type": "synthesize",
                "text": text_chunk,
                "voice_path": voice_path,
                "voice_hash": voice_hash,
                "temperature": temperature,
                "align": align,
                "streaming": streaming
            })

            # Receive audio chunks
            chunk_timeout = 60.0 if not streaming else 30.0
            chunk_bytes_in_this_request = 0

            while True:
                try:
                    resp = self._response_queue.get(timeout=chunk_timeout)
                except:
                    print("[PocketONNX] Timeout waiting for worker response")
                    return (False, bytes_produced, cumulative_time)

                resp_type = resp.get("type")

                if resp_type == "chunk":
                    audio_bytes = resp["audio"]
                    chunk_alignments = resp.get("alignments")
                    audio_chunk = np.frombuffer(audio_bytes, dtype=np.float32).copy()

                    # Apply fade-in on very first chunk only
                    if is_first_audio_chunk:
                        fade_samples = int(SAMPLE_RATE * 0.002)
                        if len(audio_chunk) > fade_samples:
                            fade_curve = np.linspace(0, 1, fade_samples)
                            audio_chunk[:fade_samples] *= fade_curve
                        is_first_audio_chunk = False

                    # Convert to int16 PCM
                    audio_np = np.clip(audio_chunk, -1.0, 1.0)
                    pcm_int16 = (audio_np * 32767).astype(np.int16)
                    pcm_bytes = pcm_int16.tobytes()

                    bytes_produced += len(pcm_bytes)
                    chunk_bytes_in_this_request += len(pcm_bytes)

                    # Convert alignment to dict format with cumulative offset
                    word_timing = None
                    if chunk_alignments:
                        word_timing = {
                            "words": [a["word"] for a in chunk_alignments],
                            "wordStartTimeSeconds": [a["start"] + cumulative_time for a in chunk_alignments],
                            "wordEndTimeSeconds": [a["end"] + cumulative_time for a in chunk_alignments]
                        }

                    on_chunk(pcm_bytes, word_timing)

                elif resp_type == "done":
                    # Update cumulative time from bytes produced in this worker request
                    cumulative_time += chunk_bytes_in_this_request / (SAMPLE_RATE * 2)
                    break

                elif resp_type == "error":
                    print(f"[PocketONNX] Worker error: {resp.get('error')}")
                    return (False, bytes_produced, cumulative_time)

        return (True, bytes_produced, cumulative_time)

    def warm_up(self, voice_name: Optional[str] = None):
        """Warm up worker process and optionally pre-cache a voice."""
        if not self.ensure_started():
            return

        if voice_name:
            voice_path = _resolve_voice(voice_name)
            if voice_path:
                voice_hash = compute_reference_hash(voice_path)
                self._request_queue.put({
                    "type": "warmup",
                    "voice_path": voice_path,
                    "voice_hash": voice_hash
                })
                try:
                    self._response_queue.get(timeout=30.0)
                except:
                    pass

    def shutdown(self):
        """Shutdown worker process."""
        with self._lock:
            self._cleanup()
            print("[PocketONNX] Worker process shut down")

    def clear_embedding(self, voice_path: str):
        """Clear cached embedding for a voice file in the worker process."""
        if not self.ensure_started():
            return

        self._request_queue.put({
            "type": "clear_embedding",
            "voice_path": voice_path
        })
        try:
            self._response_queue.get(timeout=5.0)
        except:
            pass


# ============================================================================
# Module-Level Singleton
# ============================================================================

_process_manager: Optional[PocketONNXProcessManager] = None
_manager_lock = threading.Lock()


def _get_manager() -> PocketONNXProcessManager:
    """Get or create the process manager singleton."""
    global _process_manager
    if _process_manager is None:
        with _manager_lock:
            if _process_manager is None:
                _process_manager = PocketONNXProcessManager()
    return _process_manager


# ============================================================================
# Public API (unchanged interface)
# ============================================================================

def synthesize(
    text: str,
    voice_id: str,
    on_chunk: Callable[[bytes, Optional[Dict]], None],
    temperature: Optional[float] = None,
    align: bool = True
) -> bool:
    """Synthesize text to speech with streaming chunks and optional alignment."""
    return _get_manager().synthesize(text, voice_id, on_chunk, temperature, align)


def warm_up(voice_name: Optional[str] = None):
    """Warm up models and optionally pre-cache a voice."""
    _get_manager().warm_up(voice_name)


def unload():
    """Shutdown worker process and free resources."""
    global _process_manager
    if _process_manager is not None:
        _process_manager.shutdown()
        _process_manager = None
    gc.collect()
    print("[PocketONNX] Unloaded")


def is_loaded() -> bool:
    """Check if worker process is running."""
    if _process_manager is None:
        return False
    return _process_manager._ready and _process_manager._process is not None and _process_manager._process.is_alive()


def clear_voice_embedding(voice_path: str):
    """
    Clear cached embedding for a voice file.

    Call this when a voice reference file has changed to force
    recomputation of embeddings on next synthesis.

    Args:
        voice_path: Path to the voice reference file
    """
    _get_manager().clear_embedding(voice_path)


# Backwards compatibility
class PocketTTSOnnxSynthesizer:
    """Wrapper for backwards compatibility."""

    def synthesize(self, text: str, voice_id: str, on_chunk: Callable, temperature: Optional[float] = None, align: bool = True) -> bool:
        return synthesize(text, voice_id, on_chunk, temperature, align)

    def warm_up(self, voice_name: Optional[str] = None):
        warm_up(voice_name)


def get_synthesizer() -> PocketTTSOnnxSynthesizer:
    """Get synthesizer instance (backwards compatible)."""
    return PocketTTSOnnxSynthesizer()
