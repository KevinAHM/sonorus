"""
OmniVoice TTS Engine

Runs OmniVoice inference in a subprocess to isolate torch/CUDA from the main
Flask server.  Follows the same process-manager pattern as pocket_tts_onnx.py
with VRAM-management routines ported from omni_ref/api_server.py.

Heavy dependencies (torch, transformers, omnivoice, …) are imported only
inside the worker function so they never pollute the main process.
"""
import gc
import os
import queue as _queue_mod
import time
import threading
import multiprocessing as mp
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from services.tts.voice_utils import find_voice_reference
from utils.settings import load_settings

# ============================================================================
# Constants
# ============================================================================

HF_REPO_ID = "k2-fsa/OmniVoice"
UPSCALER_HF_REPO = "openbmb/VoxCPM2"
SAMPLE_RATE = 24_000          # OmniVoice native sample rate
UPSCALE_SAMPLE_RATE = 48_000  # AudioVAE output sample rate

VOICE_DIR = Path(__file__).resolve().parent.parent / "voice_references"
_AUDIO_EXTS = {".wav", ".mp3", ".flac", ".m4a", ".ogg", ".opus"}


def _serialized_worker_io(func):
    def wrapper(self, *args, **kwargs):
        with self._synthesis_lock:
            return func(self, *args, **kwargs)
    return wrapper


def _build_generation_config_kwargs(
    num_steps: int,
    guidance_scale: float,
    t_shift: float,
    prefix_kv_cache_enabled: bool = True,
) -> Dict[str, Any]:
    """Build config values with the caller-selected step count authoritative."""
    return {
        "num_step": num_steps,
        "guidance_scale": guidance_scale,
        "t_shift": t_shift,
        "blockwise": prefix_kv_cache_enabled,
        "block_size": 9999,
        # Blockwise generation uses first_block_steps whenever the target fits
        # in one block. Our deliberately large block makes that every normal
        # sentence, so it must match the per-sentence step count from Sonorus.
        "first_block_steps": num_steps,
    }


# ============================================================================
# Voice resolution (main process — lightweight)
# ============================================================================

def _resolve_voice(voice_id: str) -> Optional[str]:
    """Resolve a voice name to a file path using the shared voice-utils lookup."""
    path = find_voice_reference(voice_id)
    if path:
        return str(path)

    # If voice_id is a hashed clone name (e.g. "PlayerMale_DE_DE_cafb73f0"),
    # parse it back to the original character name + language and retry.
    from services.tts.voice_utils import parse_hashed_voice_name
    original_name, lang, _ = parse_hashed_voice_name(voice_id)
    if original_name != voice_id:
        path = find_voice_reference(original_name, language=lang or "EN_US")
        if path:
            return str(path)

    # Fallback: try bare name as file in voice_references/
    if VOICE_DIR.exists():
        for ext in _AUDIO_EXTS:
            candidate = VOICE_DIR / f"{voice_id}{ext}"
            if candidate.is_file():
                return str(candidate)
    return None


def ensure_voice_reference_transcript(voice_path: str) -> Optional[str]:
    """Compatibility wrapper around the shared reference transcript service."""
    try:
        from services.tts.reference_preparation import ensure_reference_transcript

        return ensure_reference_transcript(voice_path)
    except Exception as exc:
        print(f"[OmniVoice] Reference transcription failed: {exc}")
        return None


# ============================================================================
# Worker Process
# ============================================================================

def _omnivoice_worker_main(
    request_queue: mp.Queue,
    response_queue: mp.Queue,
    config: Dict[str, Any],
) -> None:
    """
    Subprocess entry-point.  All torch / CUDA work happens here.

    *config* keys:
        model_repo, model_revision, device, dtype, num_steps,
        prefix_kv_cache_enabled,
        guidance_scale, t_shift, upscale, voice_dir
    """
    # ------------------------------------------------------------------
    # Heavy imports (subprocess only)
    # ------------------------------------------------------------------
    try:
        import gc as _gc
        import ctypes
        import numpy as np
        # Spawned subprocess has a clean sys.path — add services/ directory
        # so we can import omnivoice_text and omnivoice_vae
        import sys
        _services_dir = os.path.dirname(os.path.abspath(__file__))
        if _services_dir not in sys.path:
            sys.path.insert(0, _services_dir)

        import torch
        import torchaudio
        from huggingface_hub import hf_hub_download, snapshot_download
        from safetensors import safe_open
        from transformers import AutoConfig, BitsAndBytesConfig, HiggsAudioV2TokenizerModel
        from omnivoice.models.omnivoice import (
            OmniVoice,
            OmniVoiceGenerationConfig,
            VoiceClonePrompt,
        )
        from omnivoice.utils.audio import trim_leading_silence
        from omnivoice.utils.text import add_punctuation
        from omnivoice_vae import AudioVAE, AudioVAEConfig
        from omnivoice_text import preprocess_text as omni_preprocess_text
    except Exception as exc:
        import traceback
        traceback.print_exc()
        response_queue.put({"type": "error", "error": f"Import failed: {exc}"})
        return

    # ------------------------------------------------------------------
    # Config unpacking
    # ------------------------------------------------------------------
    model_repo: str      = config.get("model_repo", HF_REPO_ID)
    model_revision       = config.get("model_revision", None)
    requested_device: str = config.get("device", "auto")
    device: str          = "cuda"
    dtype_name: str      = config.get("dtype", "float16")
    default_num_steps    = int(config.get("num_steps", 32))
    default_prefix_kv_cache = bool(config.get("prefix_kv_cache_enabled", True))
    default_guidance     = float(config.get("guidance_scale", 2.0))
    default_t_shift      = float(config.get("t_shift", 0.1))
    upscale_enabled: bool = config.get("upscale", True)
    voice_dir            = Path(config.get("voice_dir", str(VOICE_DIR)))
    language: str        = config.get("language", "English")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _dtype_from_name(name: str) -> Optional[torch.dtype]:
        name = (name or "").lower()
        mapping = {
            "float32": torch.float32, "fp32": torch.float32,
            "float16": torch.float16, "fp16": torch.float16,
            "bfloat16": torch.bfloat16, "bf16": torch.bfloat16,
        }
        if name in {"none", ""}:
            return None
        if name not in mapping:
            raise ValueError(f"Invalid dtype '{name}'. Choose from: {', '.join(sorted(mapping))} or 'none'.")
        return mapping[name]

    def _trim_system_ram() -> None:
        """Return free heap pages to the OS (Linux glibc)."""
        _gc.collect()
        try:
            ctypes.CDLL("libc.so.6").malloc_trim(0)
        except OSError:
            pass

    def _choose_auto_cuda_device() -> str:
        """Pick the visible CUDA device with the most currently free VRAM."""
        if not torch.cuda.is_available():
            return "cuda"
        try:
            best_index = 0
            best_free = -1
            for index in range(torch.cuda.device_count()):
                with torch.cuda.device(index):
                    free_bytes, _total_bytes = torch.cuda.mem_get_info()
                if free_bytes > best_free:
                    best_index = index
                    best_free = free_bytes
            torch.cuda.set_device(best_index)
            return f"cuda:{best_index}"
        except Exception as exc:
            print(f"[OmniVoice] Auto GPU selection failed ({exc}); using default CUDA device")
            return "cuda"

    def _resolve_cuda_device(value: str) -> str:
        """Resolve saved config to a usable torch device, falling back to default CUDA."""
        requested = str(value or "auto").strip().lower()
        if requested in ("", "auto"):
            resolved = _choose_auto_cuda_device()
            print(f"[OmniVoice] Auto-selected device: {resolved}")
            return resolved

        if requested.startswith("cuda:"):
            try:
                index = int(requested.split(":", 1)[1])
                if torch.cuda.is_available() and 0 <= index < torch.cuda.device_count():
                    torch.cuda.set_device(index)
                    print(f"[OmniVoice] Using configured device: cuda:{index}")
                    return f"cuda:{index}"
                print(f"[OmniVoice] Configured device {requested} is unavailable; using default CUDA device")
                return "cuda"
            except Exception as exc:
                print(f"[OmniVoice] Invalid configured device {requested!r} ({exc}); using default CUDA device")
                return "cuda"

        if requested == "cuda":
            return "cuda"

        print(f"[OmniVoice] Using configured device: {requested}")
        return requested

    def _device_map_for_load(value: str):
        if value.startswith("cuda:"):
            return {"": value}
        return value

    device = _resolve_cuda_device(requested_device)

    # ------------------------------------------------------------------
    # Model loading (skip_encoder=True to save ~600 MB VRAM)
    # ------------------------------------------------------------------
    _model: Optional[OmniVoice] = None

    # Local model storage (same pattern as pocket_tts_onnx)
    MODELS_DIR = Path(__file__).resolve().parent.parent / "models" / "omnivoice"

    _DOWNLOAD_COMPLETE_MARKER = MODELS_DIR / ".download_complete"

    def _ensure_model_downloaded() -> str:
        """Download model repo to sonorus/models/omnivoice/ if not already present.
        Returns the local path."""
        if _DOWNLOAD_COMPLETE_MARKER.exists():
            return str(MODELS_DIR)

        print(f"[OmniVoice] Downloading model from {model_repo}...")
        response_queue.put({"type": "downloading", "message": "Downloading OmniVoice model..."})
        MODELS_DIR.mkdir(parents=True, exist_ok=True)

        # Run download in a thread so we can send heartbeats to prevent
        # the process manager's startup timeout from firing.
        _dl_error: List[Optional[Exception]] = [None]

        def _do_download():
            try:
                snapshot_download(
                    repo_id=model_repo,
                    local_dir=str(MODELS_DIR),
                    revision=model_revision,
                )
            except Exception as exc:
                _dl_error[0] = exc

        dl_thread = threading.Thread(target=_do_download, daemon=True)
        dl_thread.start()

        while dl_thread.is_alive():
            dl_thread.join(timeout=30.0)
            if dl_thread.is_alive():
                response_queue.put({"type": "downloading", "message": "Still downloading OmniVoice model..."})

        if _dl_error[0] is not None:
            raise _dl_error[0]

        # Write a dedicated marker only after the full download succeeds.
        _DOWNLOAD_COMPLETE_MARKER.write_text("ok")
        print(f"[OmniVoice] Model downloaded to {MODELS_DIR}")
        return str(MODELS_DIR)

    def _load_model() -> OmniVoice:
        nonlocal _model, device
        if _model is not None:
            return _model

        local_path = _ensure_model_downloaded()

        print(f"[OmniVoice] Loading model ({dtype_name})...")
        response_queue.put({"type": "loading", "message": f"Loading OmniVoice model ({dtype_name})..."})

        load_kwargs: Dict[str, Any] = dict(device_map=_device_map_for_load(device))
        dl = dtype_name.lower()
        if dl in ("int8", "8bit"):
            load_kwargs["quantization_config"] = BitsAndBytesConfig(load_in_8bit=True)
        elif dl in ("int4", "4bit"):
            load_kwargs["quantization_config"] = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_compute_dtype=torch.float16,
                bnb_4bit_quant_type="nf4",
            )
        else:
            load_kwargs["torch_dtype"] = _dtype_from_name(dtype_name)

        # Always skip encoder — _ensure_audio_encoder() loads it on demand
        load_kwargs["skip_encoder"] = True

        try:
            _model = OmniVoice.from_pretrained(local_path, **load_kwargs)
        except Exception as exc:
            if device.startswith("cuda:"):
                print(f"[OmniVoice] Failed to load on {device}: {exc}")
                print("[OmniVoice] Falling back to default CUDA device")
                _model = None
                _gc.collect()
                torch.cuda.empty_cache()
                device = "cuda"
                load_kwargs["device_map"] = _device_map_for_load(device)
                _model = OmniVoice.from_pretrained(local_path, **load_kwargs)
            else:
                raise
        print(f"[OmniVoice] Model loaded.")
        _trim_system_ram()
        return _model

    # ------------------------------------------------------------------
    # Audio encoder management (ported from api_server.py)
    # ------------------------------------------------------------------
    _ENCODER_MODULES = (
        "semantic_model",    # HuBERT – 360 MB
        "acoustic_encoder",  # DAC encoder – 196 MB
        "encoder_semantic",  # semantic encoder – 56 MB
        "fc",                # encoder projection – 4 MB
        "fc1",               # unused projection – 3 MB
        # NOTE: fc2 + acoustic_decoder + decoder_semantic + quantizer are needed for decode
    )

    def _resolve_audio_tokenizer_path() -> str:
        """Resolve the audio tokenizer safetensors path (local models dir)."""
        local_path = _ensure_model_downloaded()
        return os.path.join(local_path, "audio_tokenizer")

    def _unload_audio_encoder() -> None:
        """Delete encoder-only modules from the audio tokenizer to free VRAM."""
        model = _load_model()
        tok = model.audio_tokenizer
        if tok is None:
            return
        # Clear LRU cache that holds references to encoder modules
        if hasattr(tok._get_conv1d_layers, "cache_clear"):
            tok._get_conv1d_layers.cache_clear()
        freed = 0
        for name in _ENCODER_MODULES:
            mod = getattr(tok, name, None)
            if mod is None:
                continue
            freed += sum(p.numel() * p.element_size() for p in mod.parameters())
            mod.cpu()
            setattr(tok, name, None)
            del mod
        _gc.collect()
        torch.cuda.empty_cache()
        if freed > 0:
            print(f"[OmniVoice] Unloaded audio encoder ({freed / 1024 / 1024:.0f} MB freed)")
        _trim_system_ram()

    def _ensure_audio_encoder() -> None:
        """Load encoder weights from safetensors directly into the existing tokenizer."""
        model = _load_model()
        tok = model.audio_tokenizer
        if tok is None or getattr(tok, "semantic_model", None) is not None:
            return  # already loaded
        print("[OmniVoice] Loading audio encoder weights...")
        at_path = _resolve_audio_tokenizer_path()
        st_path = os.path.join(at_path, "model.safetensors")
        _encoder_prefixes = tuple(p + "." for p in _ENCODER_MODULES)
        # Create a fresh instance on CPU to get the encoder module structure
        _cfg = AutoConfig.from_pretrained(at_path)
        _fresh = HiggsAudioV2TokenizerModel(config=_cfg)
        # Load only encoder weights from safetensors
        encoder_sd: Dict[str, Any] = {}
        with safe_open(st_path, framework="pt", device="cpu") as sf:
            for key in sf.keys():
                if key.startswith(_encoder_prefixes):
                    encoder_sd[key] = sf.get_tensor(key)
        _fresh.load_state_dict(encoder_sd, strict=False)
        del encoder_sd
        # Move encoder modules to GPU and attach to the existing tokenizer
        for name in _ENCODER_MODULES:
            mod = getattr(_fresh, name, None)
            if mod is not None:
                setattr(tok, name, mod.to(tok.device))
        del _fresh
        print("[OmniVoice] Audio encoder loaded.")
        _trim_system_ram()

    # ------------------------------------------------------------------
    # Pretokenization
    # ------------------------------------------------------------------
    def _is_tokenizable_voice(path: Path) -> bool:
        """Check if an audio file should be tokenized.

        Only process *_reference_15s files and files with no _reference_Ns
        pattern (e.g. narrator.wav). Skip _5s, _60s, etc.
        """
        import re
        match = re.search(r'_reference_(\d+)s$', path.stem)
        if match:
            return match.group(1) == '15'
        return True

    # ------------------------------------------------------------------
    # Upscaler (VoxCPM2 AudioVAE)
    # ------------------------------------------------------------------
    _upscaler = None

    def _load_upscaler():
        nonlocal _upscaler
        if _upscaler is not None:
            return _upscaler
        if not upscale_enabled:
            return None

        print("[OmniVoice] Loading audio upscaler (VoxCPM2 AudioVAE)...")
        weights_path = hf_hub_download(repo_id=UPSCALER_HF_REPO, filename="audiovae.pth")
        vae_config = AudioVAEConfig(
            encoder_dim=128, encoder_rates=[2, 5, 8, 8],
            latent_dim=64, decoder_dim=2048,
            decoder_rates=[8, 6, 5, 2, 2, 2],
            sr_bin_boundaries=[20000, 30000, 40000],
            sample_rate=16000, out_sample_rate=48000,
        )
        vae = AudioVAE(config=vae_config)
        sd = torch.load(weights_path, map_location="cpu", weights_only=True)
        vae.load_state_dict(sd.get("state_dict", sd))
        del sd
        vae = vae.to(device).to(torch.bfloat16).eval()
        for module in vae.modules():
            if hasattr(module, "weight_g"):
                torch.nn.utils.remove_weight_norm(module)
        _upscaler = vae
        print("[OmniVoice] Upscaler ready.")
        _trim_system_ram()
        return _upscaler

    @torch.inference_mode()
    def _upscale_audio(pcm_bytes: bytes, input_sr: int = SAMPLE_RATE) -> bytes:
        """Upscale PCM16 audio from input_sr to 48 kHz via the VAE."""
        if not pcm_bytes:
            return pcm_bytes

        vae = _load_upscaler()
        if vae is None:
            return pcm_bytes

        # PCM16 bytes -> float tensor
        audio_int16 = torch.frombuffer(pcm_bytes, dtype=torch.int16).float() / 32767.0

        # Resample to 16 kHz (VAE input requirement)
        if input_sr != 16000:
            audio_16k = torchaudio.functional.resample(audio_int16.unsqueeze(0), input_sr, 16000).squeeze(0)
        else:
            audio_16k = audio_int16

        # VAE encode + decode at 48 kHz
        audio_in = audio_16k.to(dtype=torch.bfloat16, device=device).unsqueeze(0).unsqueeze(0)
        z = vae.encode(audio_in, sample_rate=16000)
        sr_cond = torch.tensor([48000], device=device, dtype=torch.int32)
        audio_48k = vae.decode(z, sr_cond=sr_cond)

        # Back to PCM16 bytes
        audio_np = audio_48k.squeeze().cpu().float().numpy()
        audio_int16_out = (audio_np * 32767).clip(-32768, 32767).astype("int16")
        return audio_int16_out.tobytes()

    # ------------------------------------------------------------------
    # Voice prompt cache (3-tier: memory -> .tokens.pt -> encoder fallback)
    # ------------------------------------------------------------------
    _voice_prompt_cache: Dict[str, VoiceClonePrompt] = {}

    def _get_voice_prompt(voice_path: str, ref_text: Optional[str] = None) -> VoiceClonePrompt:
        """Resolve a voice file path to a VoiceClonePrompt, with caching."""
        model = _load_model()
        t_start = time.time()

        p = Path(voice_path)
        cache_key = f"file:{p.resolve()}"

        # 1) Memory cache
        if cache_key in _voice_prompt_cache:
            return _voice_prompt_cache[cache_key]

        # Look for a .txt transcript
        txt_path = p.with_suffix(".txt")
        if ref_text is None and txt_path.is_file():
            ref_text = txt_path.read_text(encoding="utf-8").strip()

        # 2) Pre-tokenized .tokens.pt
        token_path = p.with_suffix(".tokens.pt")
        if token_path.is_file():
            data = torch.load(token_path, map_location="cpu", weights_only=True)
            token_ref_text = data.get("ref_text")
            if ref_text is not None and str(ref_text).strip():
                # A current sidecar is authoritative; cached audio codes do not
                # depend on transcript text and remain reusable.
                effective_ref_text = str(ref_text).strip()
            elif token_ref_text is not None and str(token_ref_text).strip():
                effective_ref_text = str(token_ref_text).strip()
            else:
                effective_ref_text = token_ref_text

            if effective_ref_text is not None:
                vcp = VoiceClonePrompt(
                    ref_audio_tokens=data["audio_codes"].to(device),
                    ref_text=add_punctuation(effective_ref_text),
                    ref_rms=float(data["ref_rms"]),
                )
                _voice_prompt_cache[cache_key] = vcp
                elapsed = (time.time() - t_start) * 1000
                print(
                    f"[OmniVoice] Loaded pretokenized {token_path.name} "
                    f"(ref_text_len={len(vcp.ref_text)}, {elapsed:.1f}ms)"
                )
                return vcp

        # 3) Fallback: encode via audio tokenizer (lazy-reload encoder)
        _ensure_audio_encoder()
        print(f"[OmniVoice] Creating voice prompt from {p.name}...")
        if ref_text:
            print(f"[OmniVoice] Using lazy transcript for {p.name} (ref_text_len={len(ref_text)})")
        else:
            print(f"[OmniVoice] No transcript for {p.name}; voice prompt will use empty ref_text")
        vcp = model.create_voice_clone_prompt(ref_audio=voice_path, ref_text=ref_text)

        # Cache the tokens to disk for next time
        try:
            torch.save(
                {"audio_codes": vcp.ref_audio_tokens.cpu(), "ref_rms": vcp.ref_rms, "ref_text": vcp.ref_text},
                token_path,
            )
        except Exception as exc:
            print(f"[OmniVoice] Failed to cache tokens for {p.name}: {exc}")

        _voice_prompt_cache[cache_key] = vcp
        # Unload encoder after on-demand encoding
        _unload_audio_encoder()
        elapsed = (time.time() - t_start) * 1000
        print(f"[OmniVoice] Voice prompt ready ({elapsed:.1f}ms)")
        return vcp

    def _generation_config_with_postprocess(
        gen_config: OmniVoiceGenerationConfig,
        postprocess_output: bool,
    ) -> OmniVoiceGenerationConfig:
        """Clone a generation config while overriding output post-processing."""
        cfg = gen_config.__dict__.copy()
        cfg["postprocess_output"] = postprocess_output
        return OmniVoiceGenerationConfig(**cfg)

    def _has_audio_samples(audio_tensor) -> bool:
        return audio_tensor is not None and audio_tensor.numel() > 0 and audio_tensor.shape[-1] > 0

    @torch.inference_mode()
    def _generate_audio_tensor(text: str, vcp: VoiceClonePrompt, gen_config: OmniVoiceGenerationConfig):
        audios = _load_model().generate(
            text=text,
            voice_clone_prompt=vcp,
            language=language,
            generation_config=gen_config,
        )
        audio_tensor = audios[0]  # (1, T)
        if _has_audio_samples(audio_tensor):
            return audio_tensor

        print(
            "[OmniVoice] Empty audio after post-processing; retrying once "
            f"without silence removal (language={language}, text_len={len(text)})"
        )
        retry_config = _generation_config_with_postprocess(gen_config, False)
        audios = _load_model().generate(
            text=text,
            voice_clone_prompt=vcp,
            language=language,
            generation_config=retry_config,
        )
        return audios[0]

    # ------------------------------------------------------------------
    # Startup sequence
    # ------------------------------------------------------------------
    try:
        _load_model()
        # Upscaler loaded lazily on first synthesis — keeps peak VRAM low during pretokenization
        response_queue.put({"type": "ready"})
    except Exception as exc:
        import traceback
        traceback.print_exc()
        response_queue.put({"type": "error", "error": str(exc)})
        return

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    _in_batch = False
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
                text = omni_preprocess_text(text)
                if not text:
                    raise ValueError("OmniVoice synthesis text is empty after preprocessing")

                voice_path: str = msg["voice_path"]
                num_steps: int = msg.get("num_steps", default_num_steps)
                prefix_kv_cache_enabled: bool = bool(
                    msg.get("prefix_kv_cache_enabled", default_prefix_kv_cache)
                )
                guidance: float = msg.get("guidance_scale", default_guidance)

                vcp = _get_voice_prompt(voice_path, ref_text=msg.get("ref_text"))

                gen_config = OmniVoiceGenerationConfig(
                    **_build_generation_config_kwargs(
                        num_steps=num_steps,
                        guidance_scale=guidance,
                        t_shift=default_t_shift,
                        prefix_kv_cache_enabled=prefix_kv_cache_enabled,
                    )
                )

                t_start = time.time()
                with torch.inference_mode():
                    audio_tensor = _generate_audio_tensor(
                        text=text,
                        vcp=vcp,
                        gen_config=gen_config,
                    )

                if not _has_audio_samples(audio_tensor):
                    raise ValueError(
                        "OmniVoice generated empty audio "
                        f"(language={language}, text_len={len(text)}, voice={Path(voice_path).name})"
                    )

                # Trim leading silence
                audio_tensor = trim_leading_silence(audio_tensor.clone(), SAMPLE_RATE)
                if not _has_audio_samples(audio_tensor):
                    raise ValueError(
                        "OmniVoice audio became empty after leading-silence trim "
                        f"(language={language}, text_len={len(text)}, voice={Path(voice_path).name})"
                    )

                # Convert to int16 PCM
                audio_np = audio_tensor.squeeze().cpu().float().numpy()
                pcm_int16 = (audio_np * 32767).clip(-32768, 32767).astype("int16")
                pcm_bytes = pcm_int16.tobytes()
                if not pcm_bytes:
                    raise ValueError(
                        "OmniVoice produced no PCM bytes "
                        f"(language={language}, text_len={len(text)}, voice={Path(voice_path).name})"
                    )

                # Optionally upscale
                if upscale_enabled:
                    pcm_bytes = _upscale_audio(pcm_bytes)

                elapsed = (time.time() - t_start) * 1000
                duration = len(pcm_bytes) / (2 * (UPSCALE_SAMPLE_RATE if upscale_enabled else SAMPLE_RATE))
                print(f"[OmniVoice] Synthesized {duration:.2f}s in {elapsed:.0f}ms")

                response_queue.put({
                    "type": "done",
                    "audio": pcm_bytes,
                    "sample_rate": UPSCALE_SAMPLE_RATE if upscale_enabled else SAMPLE_RATE,
                })

            except Exception as exc:
                import traceback
                traceback.print_exc()
                response_queue.put({"type": "error", "error": str(exc)})

        # ---- batch_start (load encoder, hold it for multiple pretokenize msgs) --
        elif msg_type == "batch_start":
            _ensure_audio_encoder()
            _in_batch = True
            print("[OmniVoice] Batch started — encoder loaded")
            response_queue.put({"type": "batch_ready"})

        # ---- batch_end (unload encoder after batch) -----------------------
        elif msg_type == "batch_end":
            _unload_audio_encoder()
            _in_batch = False
            print("[OmniVoice] Batch ended — encoder unloaded")
            response_queue.put({"type": "batch_done"})

        # ---- pretokenize (single file) ------------------------------------
        elif msg_type == "pretokenize":
            try:
                voice_path = msg["voice_path"]
                ref_text = msg.get("ref_text")

                p = Path(voice_path)
                token_path = p.with_suffix(".tokens.pt")
                if token_path.is_file():
                    response_queue.put({"type": "pretokenize_done", "success": True, "cached": True})
                    continue

                # If not in batch mode, load/unload encoder per file (on-the-fly fallback)
                if not _in_batch:
                    _ensure_audio_encoder()
                model = _load_model()
                vcp = model.create_voice_clone_prompt(ref_audio=voice_path, ref_text=ref_text)
                torch.save(
                    {"audio_codes": vcp.ref_audio_tokens.cpu(), "ref_rms": vcp.ref_rms, "ref_text": vcp.ref_text},
                    token_path,
                )
                if not _in_batch:
                    _unload_audio_encoder()
                response_queue.put({"type": "pretokenize_done", "success": True, "cached": False})
            except Exception as exc:
                import traceback
                traceback.print_exc()
                response_queue.put({"type": "pretokenize_done", "success": False, "error": str(exc)})

        # ---- clear_voice_prompt ----------------------------------------
        elif msg_type == "clear_voice_prompt":
            voice_path = msg.get("voice_path", "")
            p = Path(voice_path)
            cache_key = f"file:{p.resolve()}"
            removed = _voice_prompt_cache.pop(cache_key, None)
            if removed is not None:
                print(f"[OmniVoice] Cleared voice prompt cache for {p.name}")
            response_queue.put({"type": "clear_done"})

        # ---- warmup ----------------------------------------------------
        elif msg_type == "warmup":
            try:
                voice_path = msg.get("voice_path")
                if voice_path:
                    _ = _get_voice_prompt(voice_path)
                response_queue.put({"type": "warmup_done"})
            except Exception as exc:
                response_queue.put({"type": "warmup_done", "error": str(exc)})

        else:
            print(f"[OmniVoice] Unknown message type: {msg_type}")

    # Clean up on exit
    print("[OmniVoice] Worker shutting down.")


# ============================================================================
# Process Manager
# ============================================================================

class OmniVoiceProcessManager:
    """Manages the OmniVoice worker subprocess from the main process."""

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

            print("[OmniVoice] Starting worker process...")

            ctx = mp.get_context("spawn")
            self._request_queue = ctx.Queue()
            self._response_queue = ctx.Queue()

            config = _get_omnivoice_config()

            self._process = ctx.Process(
                target=_omnivoice_worker_main,
                args=(self._request_queue, self._response_queue, config),
                daemon=True,
            )
            self._process.start()

            # Wait for ready signal (300 s — first run may download the model)
            # Poll in short intervals so we detect a dead worker quickly
            # instead of blocking for the full timeout.
            deadline = time.monotonic() + 300.0
            try:
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise _queue_mod.Empty()
                    # Check if the worker process died
                    if not self._process.is_alive():
                        exitcode = self._process.exitcode
                        print(f"[OmniVoice] Worker process exited unexpectedly (code {exitcode})")
                        # Drain any final error message it managed to send
                        try:
                            resp = self._response_queue.get_nowait()
                            if resp.get("type") == "error":
                                print(f"[OmniVoice] Worker error: {resp.get('error')}")
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
                        print("[OmniVoice] Worker process ready")
                        return True
                    elif msg_type in ("downloading", "loading"):
                        print(f"[OmniVoice] Worker: {resp.get('message', msg_type)}")
                        continue  # keep waiting
                    elif msg_type == "error":
                        print(f"[OmniVoice] Worker startup error: {resp.get('error')}")
                        self._cleanup()
                        return False
                    else:
                        print(f"[OmniVoice] Unexpected startup message: {resp}")
                        self._cleanup()
                        return False
            except _queue_mod.Empty:
                print("[OmniVoice] Worker failed to start: timed out after 300s")
                self._cleanup()
                return False
            except Exception as e:
                print(f"[OmniVoice] Worker failed to start: {e}")
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
        prefix_kv_cache_enabled: Optional[bool] = None,
    ) -> Tuple[bool, int]:
        """
        Synthesize a single sentence.

        Args:
            text: Pre-processed text to synthesize.
            voice_path: Resolved voice reference file path.
            on_chunk: Callback(pcm_bytes, alignment_or_none) for audio delivery.
            num_steps: Override diffusion steps (default from settings).
            guidance_scale: Override CFG scale (default from settings).
            prefix_kv_cache_enabled: Reuse the frozen conditioning-prefix KV cache.

        Returns:
            (success, bytes_produced)
        """
        if not self.ensure_started():
            print("[OmniVoice] Worker process not available")
            return (False, 0)

        request: Dict[str, Any] = {
            "type": "synthesize",
            "text": text,
            "voice_path": voice_path,
        }
        ref_text = ensure_voice_reference_transcript(voice_path)
        if not ref_text:
            print("[OmniVoice] A reference transcript is required for synthesis")
            return (False, 0)
        request["ref_text"] = ref_text
        if num_steps is not None:
            request["num_steps"] = num_steps
        if guidance_scale is not None:
            request["guidance_scale"] = guidance_scale
        if prefix_kv_cache_enabled is not None:
            request["prefix_kv_cache_enabled"] = prefix_kv_cache_enabled

        with self._synthesis_lock:
            # The worker has one shared response queue, so only one synthesis
            # request can own the queue at a time.
            self._request_queue.put(request)

            try:
                resp = self._response_queue.get(timeout=120.0)
            except Exception:
                print("[OmniVoice] Timeout waiting for synthesis response")
                return (False, 0)

            resp_type = resp.get("type")

            if resp_type == "done":
                pcm_bytes = resp["audio"]
                if not pcm_bytes:
                    print("[OmniVoice] Synthesis returned empty audio")
                    return (False, 0)
                on_chunk(pcm_bytes, None)
                return (True, len(pcm_bytes))

            elif resp_type == "error":
                print(f"[OmniVoice] Synthesis error: {resp.get('error')}")
                return (False, 0)

            print(f"[OmniVoice] Unexpected response type: {resp_type}")
            return (False, 0)

    # ------------------------------------------------------------------
    # Pretokenize
    # ------------------------------------------------------------------

    @_serialized_worker_io
    def pretokenize_voice(self, voice_path: str, ref_text: Optional[str] = None) -> bool:
        """Pretokenize a voice reference (creates .tokens.pt). Returns True on success."""
        if not self.ensure_started():
            return False

        self._request_queue.put({
            "type": "pretokenize",
            "voice_path": voice_path,
            "ref_text": ref_text,
        })

        try:
            resp = self._response_queue.get(timeout=120.0)
            return resp.get("success", False)
        except Exception:
            print("[OmniVoice] Timeout waiting for pretokenize response")
            return False

    @_serialized_worker_io
    def pretokenize_batch(self, voices: list, on_progress: Callable = None,
                          transcribe_fn: Callable = None) -> list:
        """Batch pretokenize — encoder loaded once, transcribe+tokenize interleaved.

        Worker loads encoder at batch_start, keeps it for all files, unloads at batch_end.
        For each file: main process transcribes (STT), sends transcript to worker, worker tokenizes.

        Args:
            voices: list of {"path": str}
            on_progress: optional callback(completed, total)
            transcribe_fn: callable(audio_path) -> str|None for STT transcription

        Returns:
            list of {"path", "success", "error"?} results
        """
        if not self.ensure_started():
            return [{"path": v["path"], "success": False, "error": "Worker not available"} for v in voices]

        total = len(voices)

        # Tell worker to load encoder and hold it
        self._request_queue.put({"type": "batch_start"})
        try:
            resp = self._response_queue.get(timeout=300.0)
            if resp.get("type") != "batch_ready":
                return [{"path": v["path"], "success": False, "error": "Worker failed to start batch"} for v in voices]
        except Exception:
            return [{"path": v["path"], "success": False, "error": "Timeout starting batch"} for v in voices]

        results = []
        created_pts = []  # track .pt files we create so we can wipe on abort
        stt_failures = 0
        stt_attempts = 0
        for i, entry in enumerate(voices):
            voice_path = entry["path"]

            # Transcribe in main process (has STT)
            ref_text = entry.get("ref_text")
            if not ref_text and transcribe_fn:
                stt_attempts += 1
                ref_text = transcribe_fn(voice_path)

            if not ref_text:
                stt_failures += 1
                # Abort if STT is systematically failing (not just the odd file)
                # Allow up to 3 failures in the first 5 attempts, then require >50% success
                if stt_attempts >= 5 and stt_failures / stt_attempts > 0.5:
                    print(f"[OmniVoice] Aborting batch: STT failing too often "
                          f"({stt_failures}/{stt_attempts}). Check STT service/credits.")
                    # Wipe any .pt files we created during this run
                    for pt_path in created_pts:
                        try:
                            os.remove(pt_path)
                        except OSError:
                            pass
                    if created_pts:
                        print(f"[OmniVoice] Cleaned up {len(created_pts)} .tokens.pt files from aborted batch")
                    # Tell worker to clean up
                    self._request_queue.put({"type": "batch_end"})
                    try:
                        self._response_queue.get(timeout=30.0)
                    except Exception:
                        pass
                    return [{"path": voice_path, "success": False,
                             "error": f"Batch aborted: STT failing ({stt_failures}/{stt_attempts})"}]

                # An isolated STT failure is retained for a later retry.
                print(f"[OmniVoice] Skipping {Path(voice_path).name}: no transcript")
                results.append({
                    "path": voice_path,
                    "success": False,
                    "error": "Reference transcript unavailable",
                })
                if on_progress:
                    on_progress(i + 1, total)
                continue

            # Send to worker for tokenization (encoder already loaded)
            self._request_queue.put({
                "type": "pretokenize",
                "voice_path": voice_path,
                "ref_text": ref_text,
            })

            try:
                resp = self._response_queue.get(timeout=120.0)
                success = resp.get("success", False)
                results.append({"path": voice_path, "success": success})
                if success and not resp.get("cached", False):
                    created_pts.append(str(Path(voice_path).with_suffix(".tokens.pt")))
            except Exception:
                results.append({"path": voice_path, "success": False, "error": "Timeout"})

            if on_progress:
                on_progress(i + 1, total)

        # Tell worker to unload encoder
        self._request_queue.put({"type": "batch_end"})
        try:
            self._response_queue.get(timeout=30.0)
        except Exception:
            pass

        return results

    # ------------------------------------------------------------------
    # Cache management
    # ------------------------------------------------------------------

    @_serialized_worker_io
    def clear_voice_prompt(self, voice_path: str):
        """Clear cached voice prompt in the worker process."""
        if not self.ensure_started():
            return

        self._request_queue.put({
            "type": "clear_voice_prompt",
            "voice_path": voice_path,
        })
        try:
            self._response_queue.get(timeout=5.0)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Warmup
    # ------------------------------------------------------------------

    @_serialized_worker_io
    def warm_up(self, voice_path: Optional[str] = None):
        """Warm up the worker and optionally pre-cache a voice prompt."""
        if not self.ensure_started():
            return

        self._request_queue.put({
            "type": "warmup",
            "voice_path": voice_path,
        })
        try:
            self._response_queue.get(timeout=60.0)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def shutdown(self):
        """Shut down the worker process."""
        with self._lock:
            self._cleanup()
            print("[OmniVoice] Worker process shut down")


# ============================================================================
# Configuration
# ============================================================================

# Game language code -> OmniVoice language name
_GAME_LANG_TO_OMNIVOICE = {
    "EN_US": "English", "EN_GB": "English",
    "DE_DE": "German",
    "FR_FR": "French",
    "ES_ES": "Spanish", "ES_MX": "Spanish",
    "IT_IT": "Italian",
    "PT_BR": "Portuguese",
    "JA_JP": "Japanese",
    "KO_KR": "Korean",
    "ZH_CN": "Chinese", "ZH_TW": "Chinese",
    "PL_PL": "Polish",
    "RU_RU": "Russian",
    "AR_AE": "Arabic",
    "NL_NL": "Dutch",
    "TR_TR": "Turkish",
}


def _get_omnivoice_config() -> Dict[str, Any]:
    """Build config dict from settings.json for the worker process."""
    try:
        settings = load_settings()
        tts = settings.get("tts", {})
        omni = tts.get("omnivoice", {})
        game_lang = settings.get("setup", {}).get("language", "EN_US")
    except Exception:
        omni = {}
        game_lang = "EN_US"

    return {
        "model_repo": omni.get("model_repo", HF_REPO_ID),
        "model_revision": omni.get("model_revision", None),
        "device": omni.get("device", "auto"),
        "dtype": omni.get("dtype", "float16"),
        "num_steps": int(omni.get("num_steps", 32)),
        "prefix_kv_cache_enabled": bool(omni.get("prefix_kv_cache_enabled", True)),
        "guidance_scale": float(omni.get("guidance_scale", 2.0)),
        "apply_smoothing_eq": bool(omni.get("apply_smoothing_eq", True)),
        "t_shift": float(omni.get("t_shift", 0.1)),
        "upscale": True,
        "voice_dir": str(VOICE_DIR),
        "language": _GAME_LANG_TO_OMNIVOICE.get(game_lang, "English"),
    }


# ============================================================================
# Module-level singleton
# ============================================================================

_process_manager: Optional[OmniVoiceProcessManager] = None
_manager_lock = threading.Lock()


def _get_manager() -> OmniVoiceProcessManager:
    """Get or create the singleton process manager."""
    global _process_manager
    if _process_manager is None:
        with _manager_lock:
            if _process_manager is None:
                _process_manager = OmniVoiceProcessManager()
    return _process_manager


# ============================================================================
# Public API
# ============================================================================

def warm_up(voice_name: Optional[str] = None):
    """Warm up models and optionally pre-cache a voice."""
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
    print("[OmniVoice] Unloaded")


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
    """Clear cached voice prompt for a file (call when the reference changes)."""
    _get_manager().clear_voice_prompt(voice_path)
