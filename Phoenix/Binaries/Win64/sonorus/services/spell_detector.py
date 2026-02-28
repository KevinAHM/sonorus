"""Lightweight spell detection from live microphone.

Dependencies: onnxruntime, numpy, sounddevice
No OpenWakeWord package needed — runs the ONNX pipeline directly.

Usage:
  python spell_detector.py
  python spell_detector.py --threshold 0.7 --vad 0.3
  python spell_detector.py --spells Accio Lumos Stupefy
  python spell_detector.py --list-devices
  python spell_detector.py --device 3
"""

import os
import sys
import argparse
import threading
import numpy as np
import onnxruntime as ort
from collections import deque
from pathlib import Path

MODELS_DIR = Path(__file__).parent.parent / "models"

# Module-level singleton (follows VAD/turn_detection pattern)
_detector_instance: "SpellDetector | None" = None
_detector_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Audio feature pipeline (replaces openwakeword.utils.AudioFeatures)
# ---------------------------------------------------------------------------

class AudioFeatures:
    """Streaming audio → mel spectrogram → embedding pipeline using ONNX."""

    def __init__(self, models_dir: Path):
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        providers = ["CPUExecutionProvider"]

        self.melspec_model = ort.InferenceSession(
            str(models_dir / "melspectrogram.onnx"),
            sess_options=opts, providers=providers,
        )
        self.embedding_model = ort.InferenceSession(
            str(models_dir / "embedding_model.onnx"),
            sess_options=opts, providers=providers,
        )

        self.raw_data_buffer: deque = deque(maxlen=16000 * 10)
        self.melspectrogram_buffer = np.ones((76, 32), dtype=np.float32)
        self.melspectrogram_max_len = 10 * 97
        self.accumulated_samples = 0
        self.raw_data_remainder = np.empty(0)
        self.feature_buffer_max_len = 120
        # Warm up with random audio to fill feature buffer
        self.feature_buffer = self._get_embeddings(
            np.random.randint(-1000, 1000, 16000 * 4).astype(np.int16)
        )

    def reset(self):
        self.raw_data_buffer.clear()
        self.melspectrogram_buffer = np.ones((76, 32), dtype=np.float32)
        self.accumulated_samples = 0
        self.raw_data_remainder = np.empty(0)
        self.feature_buffer = self._get_embeddings(
            np.random.randint(-1000, 1000, 16000 * 4).astype(np.int16)
        )

    def _melspec_predict(self, x):
        return self.melspec_model.run(None, {"input": x})

    def _embedding_predict(self, x):
        return self.embedding_model.run(None, {"input_1": x})[0].squeeze()

    def _get_melspectrogram(self, x):
        x = np.array(x).astype(np.int16) if isinstance(x, list) else x
        x = x[None,] if len(x.shape) < 2 else x
        x = x.astype(np.float32)
        outputs = self._melspec_predict(x)
        spec = np.squeeze(outputs[0])
        return spec / 10 + 2  # match OWW's transform

    def _get_embeddings(self, x):
        spec = self._get_melspectrogram(x)
        windows = []
        for i in range(0, spec.shape[0], 8):
            window = spec[i : i + 76]
            if window.shape[0] == 76:
                windows.append(window)
        if not windows:
            return np.zeros((0, 96), dtype=np.float32)
        batch = np.expand_dims(np.array(windows), axis=-1).astype(np.float32)
        return self._embedding_predict(batch)

    def _streaming_melspectrogram(self, n_samples):
        raw = list(self.raw_data_buffer)[-n_samples - 160 * 3 :]
        new_spec = self._get_melspectrogram(raw)
        self.melspectrogram_buffer = np.vstack((self.melspectrogram_buffer, new_spec))
        if self.melspectrogram_buffer.shape[0] > self.melspectrogram_max_len:
            self.melspectrogram_buffer = self.melspectrogram_buffer[
                -self.melspectrogram_max_len :
            ]

    def __call__(self, x):
        """Feed audio samples, returns number of processed samples."""
        processed_samples = 0

        if self.raw_data_remainder.shape[0] != 0:
            x = np.concatenate((self.raw_data_remainder, x))
            self.raw_data_remainder = np.empty(0)

        if self.accumulated_samples + x.shape[0] >= 1280:
            remainder = (self.accumulated_samples + x.shape[0]) % 1280
            if remainder != 0:
                x_even = x[:-remainder]
                self.raw_data_buffer.extend(x_even.tolist())
                self.accumulated_samples += len(x_even)
                self.raw_data_remainder = x[-remainder:]
            else:
                self.raw_data_buffer.extend(x.tolist())
                self.accumulated_samples += x.shape[0]
                self.raw_data_remainder = np.empty(0)
        else:
            self.accumulated_samples += x.shape[0]
            self.raw_data_buffer.extend(x.tolist())

        if self.accumulated_samples >= 1280 and self.accumulated_samples % 1280 == 0:
            self._streaming_melspectrogram(self.accumulated_samples)

            for i in np.arange(self.accumulated_samples // 1280 - 1, -1, -1):
                ndx = -8 * i
                ndx = ndx if ndx != 0 else len(self.melspectrogram_buffer)
                window = self.melspectrogram_buffer[-76 + ndx : ndx].astype(np.float32)
                if window.shape[0] == 76:
                    embedding = self._embedding_predict(window[None, :, :, None])
                    self.feature_buffer = np.vstack((self.feature_buffer, embedding))

            processed_samples = self.accumulated_samples
            self.accumulated_samples = 0

        if self.feature_buffer.shape[0] > self.feature_buffer_max_len:
            self.feature_buffer = self.feature_buffer[-self.feature_buffer_max_len :]

        return processed_samples if processed_samples != 0 else self.accumulated_samples

    def get_features(self, n_frames=16):
        return self.feature_buffer[-n_frames:][None,].astype(np.float32)


# ---------------------------------------------------------------------------
# VAD wrapper using sonorus's existing SileroVADAnalyzer (services/vad.py)
# ---------------------------------------------------------------------------

class VAD:
    def __init__(self):
        from services.vad import SileroVADAnalyzer
        self._analyzer = SileroVADAnalyzer(threshold=0.5)
        self.prediction_buffer: deque = deque(maxlen=125)

    def reset_states(self):
        self._analyzer.reset()

    def __call__(self, x):
        """Process audio chunk (1280 samples) in 512-sample frames."""
        chunk_size = self._analyzer.chunk_size
        preds = []
        for i in range(0, len(x), chunk_size):
            frame = x[i:i + chunk_size]
            if len(frame) < chunk_size:
                break
            preds.append(self._analyzer.analyze(frame))
        if preds:
            self.prediction_buffer.append(float(np.mean(preds)))


# ---------------------------------------------------------------------------
# Spell detector
# ---------------------------------------------------------------------------

class SpellDetector:
    """Loads all spell ONNX models and runs stacked inference."""

    def __init__(self, models_dir: Path, spell_names: list[str] | None = None,
                 vad_threshold: float = 0.5):
        self.preprocessor = AudioFeatures(models_dir)
        self.vad_threshold = vad_threshold
        self.vad = VAD() if vad_threshold > 0 else None

        # Load spell classifier models
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        providers = ["CPUExecutionProvider"]

        # Auto-discover spell models from models/spells/ subdirectory
        spells_dir = models_dir / "spells"
        if spell_names is None:
            spell_names = []
            if spells_dir.is_dir():
                for f in sorted(spells_dir.glob("*.onnx")):
                    spell_names.append(f.stem)

        self.spell_names = spell_names
        self.models = {}
        self.prediction_buffers: dict[str, deque] = {}

        for name in spell_names:
            path = spells_dir / f"{name}.onnx"
            if not path.exists():
                print(f"Warning: {path} not found, skipping")
                continue
            self.models[name] = ort.InferenceSession(
                str(path), sess_options=opts, providers=providers,
            )
            self.prediction_buffers[name] = deque(maxlen=30)

        print(f"[SpellDetect] Loaded {len(self.models)} spell models: {', '.join(self.models.keys())}")
        self._suppress_frames = 0
        self._suppress_until_silence = False

    def reset(self):
        self.preprocessor.reset()
        for buf in self.prediction_buffers.values():
            buf.clear()
        # Hold all predictions zeroed until VAD reports silence (utterance is done).
        # This prevents re-detecting the trailing window of the same spell word.
        # Once VAD drops, a 3-frame (~240ms) coast clears any residual embeddings.
        self._suppress_until_silence = True
        self._suppress_frames = 0

    def predict(self, audio: np.ndarray) -> dict[str, float]:
        """Feed audio chunk, return dict of spell → score."""
        n_prepared = self.preprocessor(audio)

        # Compute VAD first so we can use it for both suppression and gating.
        vad_active = False
        if self.vad is not None and self.vad_threshold > 0:
            self.vad(audio)
            vad_frames = list(self.vad.prediction_buffer)[-7:-4]
            vad_max = np.max(vad_frames) if len(vad_frames) > 0 else 0
            vad_active = vad_max >= self.vad_threshold

        # Post-detection suppression: hold zeroed while the triggering utterance is
        # still active, then coast 3 frames (~240ms) after VAD drops.
        if self._suppress_until_silence:
            if not vad_active:
                self._suppress_until_silence = False
                self._suppress_frames = 3
            suppressed = True
        else:
            suppressed = self._suppress_frames > 0
            if suppressed:
                self._suppress_frames -= 1

        predictions = {}
        for name, model in self.models.items():
            if n_prepared >= 1280:
                features = self.preprocessor.get_features(
                    model.get_inputs()[0].shape[1]
                )
                out = model.run(None, {model.get_inputs()[0].name: features})
                score = out[0][0][0]
            else:
                # Not enough samples yet — use last prediction
                score = self.prediction_buffers[name][-1] if self.prediction_buffers[name] else 0.0

            # Zero out during initial warmup or post-reset suppression window
            if len(self.prediction_buffers[name]) < 5 or suppressed:
                score = 0.0

            self.prediction_buffers[name].append(score)
            predictions[name] = score

        # Normal VAD gate: zero predictions when no speech detected
        if self.vad is not None and self.vad_threshold > 0 and not vad_active:
            predictions = {k: 0.0 for k in predictions}

        return predictions


# ---------------------------------------------------------------------------
# Wakeword model name → game spell ID mapping
# ---------------------------------------------------------------------------

# Wakeword model filename (stem) → game internal spell name (SPELL_TOOL_RECORDS key)
# Models without a mapping here use their stem as-is via .get(name, name) fallback.
# Models for non-castable spells (no SPELL_TOOL_RECORDS entry) are excluded so
# detection is silently ignored rather than sending an invalid cast_spell.
MODEL_TO_GAME_SPELL = {
    # Control (Yellow)
    "Arresto_Momentum": "ArrestoMomentum",
    "Glacius": "Glacius",
    "Levioso": "Levioso",

    # Force (Purple)
    "Accio": "Accio",
    "Depulso": "Depulso",
    "Descendo": "Descendo",
    "Flipendo": "Flipendo",

    # Damage (Red)
    "Bombarda": "Confringo",  # Bombarda is a talent upgrade for Confringo
    "Confringo": "Confringo",
    "Diffindo": "Diffindo",
    "Expelliarmus": "Expelliarmus",
    "Expulso": "Expulso",
    "Incendio": "Incendio",

    # Utility
    "Disillusionment": "Disillusionment",
    "Evanesco": "Vanishment",
    "Lumos": "Lumos",
    "Nox": "Lumos",  # Nox toggles Lumos off (same spell, toggle behavior)
    "Reparo": "Reparo",
    "Wingardium_Leviosa": "WingardiumLeviosa",

    # Unforgivable Curses
    "Avada_Kedavra": "AvadaKedavra",
    "Crucio": "Crucio",
    "Imperio": "Imperio",

    # Essential
    "Petrificus_Totalus": "PetrificusTotalus",
    "Protego": "Protego",
    "Revelio": "Revelio",
    "Stupefy": "Stupefy",

    # Other castable
    "Confundo": "Confundo",
    "Episkey": "Episkey",
    "Obliviate": "Obliviate",
    "Oppugno": "Oppugno",

}


def get_best_detection(scores: dict[str, float], threshold: float):
    """Return (game_spell, model_name, score) for the best detection above threshold, or None.

    Only returns detections for castable spells (those in MODEL_TO_GAME_SPELL).
    Non-castable spell models (Aguamenti, Finite, etc.) are silently ignored.
    """
    if not scores:
        return None
    # Only consider models that map to castable game spells
    castable = {k: v for k, v in scores.items() if k in MODEL_TO_GAME_SPELL}
    if not castable:
        return None
    best_name = max(castable, key=castable.get)
    best_score = castable[best_name]
    if best_score < threshold:
        return None
    game_spell = MODEL_TO_GAME_SPELL[best_name]
    return (game_spell, best_name, best_score)


# ---------------------------------------------------------------------------
# Module-level singleton API (follows VAD / turn_detection pattern)
# ---------------------------------------------------------------------------

def get_detector() -> "SpellDetector | None":
    """Get the shared SpellDetector instance. Returns None if not loaded."""
    return _detector_instance


def is_loaded() -> bool:
    """Check if spell detector models are currently loaded."""
    return _detector_instance is not None


def warm_up():
    """Load spell detector models (idempotent). Call from background thread.

    Internal VAD gates predictions to zero when no speech is detected,
    preventing garbage scores during feature buffer warmup. voice.py's
    VAD also gates the detection window (dual VAD for reliability).
    """
    global _detector_instance
    with _detector_lock:
        if _detector_instance is not None:
            return
        try:
            detector = SpellDetector(MODELS_DIR, vad_threshold=0.5)
            if not detector.models:
                print("[SpellDetect] No spell models found in models/spells/")
                return
            _detector_instance = detector
            print(f"[SpellDetect] Warmed up ({len(detector.models)} models)")
        except Exception as e:
            print(f"[SpellDetect] Warm-up failed: {e}")


def unload():
    """Unload spell detector models and free memory."""
    global _detector_instance
    with _detector_lock:
        if _detector_instance is None:
            return
        del _detector_instance
        _detector_instance = None
    import gc
    gc.collect()
    print("[SpellDetect] Models unloaded")


# ---------------------------------------------------------------------------
# Live mic demo
# ---------------------------------------------------------------------------

def run_live(args):
    import sounddevice as sd
    import queue
    import time

    detector = SpellDetector(
        MODELS_DIR,
        spell_names=args.spells if args.spells else None,
        vad_threshold=args.vad,
    )

    CHUNK = 1280
    RATE = 16000
    COOLDOWN = 1.0

    audio_queue = queue.Queue()
    last_detection_time = 0.0

    def callback(indata, frames, time_info, status):
        audio_queue.put(indata[:, 0].copy())

    device_kwargs = {"device": args.device} if args.device is not None else {}

    print(f"\nListening... (threshold={args.threshold}, vad={args.vad})")
    print("Press Ctrl+C to stop.\n")

    with sd.InputStream(samplerate=RATE, channels=1, dtype="int16",
                        blocksize=CHUNK, callback=callback, **device_kwargs):
        try:
            while True:
                audio = audio_queue.get()
                now = time.time()

                if now - last_detection_time < COOLDOWN:
                    continue

                scores = detector.predict(audio)

                # Mic level
                rms = np.sqrt(np.mean(audio.astype(np.float32) ** 2))
                level = min(int(rms / 500 * 10), 10)
                level_bar = "|" * level + " " * (10 - level)

                # Find best
                best = max(scores, key=scores.get)
                best_score = scores[best]

                scores_str = "  ".join(f"{k}={v:.3f}" for k, v in scores.items())
                indicator = f" << {best}!" if best_score >= args.threshold else ""
                print(f"\r  mic[{level_bar}] {scores_str}{indicator}    ", end="", flush=True)

                if best_score >= args.threshold:
                    print()
                    last_detection_time = now
                    detector.reset()
                    while not audio_queue.empty():
                        try:
                            audio_queue.get_nowait()
                        except queue.Empty:
                            break
        except KeyboardInterrupt:
            print("\n\nStopped.")


def list_devices():
    import sounddevice as sd
    print(sd.query_devices())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Lightweight spell detection")
    parser.add_argument("--spells", nargs="+", default=None,
                        help="Spell names to load (default: all in models/)")
    parser.add_argument("--threshold", type=float, default=0.5,
                        help="Detection threshold (default: 0.5)")
    parser.add_argument("--vad", type=float, default=0.5,
                        help="VAD threshold 0-1, 0=disabled (default: 0.5)")
    parser.add_argument("--device", type=int, default=None,
                        help="Audio input device index")
    parser.add_argument("--list-devices", action="store_true",
                        help="List audio devices and exit")
    args = parser.parse_args()

    if args.list_devices:
        list_devices()
    else:
        run_live(args)
