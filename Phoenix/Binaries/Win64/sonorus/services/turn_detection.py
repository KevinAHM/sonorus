"""
Turn Detection service using Smart-Turn v3.2 model.

Determines when a user has finished their conversational turn,
distinguishing natural pauses ("um...") from actual turn completion.

Uses ONNX runtime for CPU-efficient inference.
Based on: https://github.com/pipecat-ai/smart-turn
"""
import hashlib
import os
import threading
from typing import Any, Dict, Optional

import numpy as np
from huggingface_hub import hf_hub_download

# Model configuration
MODEL_REPO_ID = "pipecat-ai/smart-turn-v3"
MODEL_FILENAME = "smart-turn-v3.2-cpu.onnx"
MODEL_SIZE_BYTES = 8_679_182
MODEL_SHA256 = "2bb026316b14a660486a75b1733cd3fbab8c2fd0314dc9af7be49f8cca967e4f"
SAMPLE_RATE = 16000
MAX_DURATION_SECONDS = 8

# Lazy-loaded components
_session = None
_feature_extractor = None
_model_lock = threading.Lock()

# Model directory (in sonorus/models/)
_SONORUS_DIR = os.path.dirname(os.path.dirname(__file__))
_MODELS_DIR = os.path.join(_SONORUS_DIR, 'models')
_MODEL_PATH = os.path.join(_MODELS_DIR, MODEL_FILENAME)


def _get_feature_extractor():
    """Get or create the WhisperFeatureExtractor instance."""
    global _feature_extractor

    if _feature_extractor is not None:
        return _feature_extractor

    from utils.feature_extraction_whisper import WhisperFeatureExtractor
    _feature_extractor = WhisperFeatureExtractor(
        feature_size=80,
        sampling_rate=SAMPLE_RATE,
        hop_length=160,
        chunk_length=MAX_DURATION_SECONDS,
        n_fft=400,
        padding_value=0.0,
    )
    print("[TurnDetection] WhisperFeatureExtractor initialized")
    return _feature_extractor


def _model_file_is_valid(path: str) -> bool:
    """Return whether the smart-turn model matches the published artifact."""
    try:
        if os.path.getsize(path) != MODEL_SIZE_BYTES:
            return False
        digest = hashlib.sha256()
        with open(path, "rb") as model_file:
            for chunk in iter(lambda: model_file.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest() == MODEL_SHA256
    except OSError:
        return False


def _download_model():
    """Validate and, when necessary, download the smart-turn ONNX model."""
    if _model_file_is_valid(_MODEL_PATH):
        return _MODEL_PATH

    os.makedirs(_MODELS_DIR, exist_ok=True)

    replace_invalid = os.path.exists(_MODEL_PATH)
    if replace_invalid:
        print("[TurnDetection] Existing smart-turn model is incomplete or corrupt; replacing it")
        try:
            os.remove(_MODEL_PATH)
        except OSError as exc:
            raise RuntimeError(f"Could not remove invalid smart-turn model: {exc}") from exc

    print("[TurnDetection] Downloading smart-turn model through huggingface_hub...")

    try:
        downloaded_path = hf_hub_download(
            repo_id=MODEL_REPO_ID,
            filename=MODEL_FILENAME,
            local_dir=_MODELS_DIR,
            force_download=replace_invalid,
        )
        if os.path.abspath(downloaded_path) != os.path.abspath(_MODEL_PATH):
            raise RuntimeError(f"Model downloaded to unexpected path: {downloaded_path}")
        if not _model_file_is_valid(_MODEL_PATH):
            raise RuntimeError("Downloaded smart-turn model failed size/SHA-256 validation")
        print(f"[TurnDetection] Model downloaded to {_MODEL_PATH}")
        return _MODEL_PATH
    except Exception as e:
        if os.path.exists(_MODEL_PATH) and not _model_file_is_valid(_MODEL_PATH):
            try:
                os.remove(_MODEL_PATH)
            except OSError:
                pass
        print(f"[TurnDetection] Failed to download model: {e}")
        raise


def _load_model():
    """Load the ONNX model."""
    global _session

    if _session is not None:
        return _session

    with _model_lock:
        if _session is not None:
            return _session

        # Ensure model is downloaded
        model_path = _download_model()

        try:
            import onnxruntime as ort

            # Configure session for CPU
            so = ort.SessionOptions()
            so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            so.inter_op_num_threads = 1
            so.intra_op_num_threads = 2  # Use 2 threads for inference
            so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

            _session = ort.InferenceSession(
                model_path,
                sess_options=so,
                providers=['CPUExecutionProvider']
            )
            print("[TurnDetection] ONNX session created")

        except ImportError:
            print("[TurnDetection] onnxruntime not installed. Run: pip install onnxruntime")
            raise

        return _session


def _truncate_or_pad_audio(audio_array: np.ndarray, n_seconds: int = MAX_DURATION_SECONDS) -> np.ndarray:
    """Truncate audio to last n seconds or pad with zeros at the beginning."""
    max_samples = n_seconds * SAMPLE_RATE

    if len(audio_array) > max_samples:
        # Keep the end (most recent audio)
        return audio_array[-max_samples:]
    elif len(audio_array) < max_samples:
        # Pad with zeros at the beginning
        padding = max_samples - len(audio_array)
        return np.pad(audio_array, (padding, 0), mode='constant', constant_values=0)

    return audio_array


class TurnDetector:
    """
    Conversational turn detection using Smart-Turn v3.2 model.

    Analyzes audio to determine if a speaker has completed their turn
    or is just pausing mid-thought.

    Usage:
        detector = TurnDetector()

        # After VAD detects silence:
        result = detector.predict(audio_array)
        if result['complete']:
            # Turn is complete, send to STT
        else:
            # Still speaking, continue listening
    """

    def __init__(self, confidence_threshold: float = 0.5):
        """
        Args:
            confidence_threshold: Probability threshold for turn completion (0.0-1.0).
                Higher = more confident turn is complete before marking it so.
        """
        self._threshold = confidence_threshold
        self._session = None

    def _ensure_initialized(self):
        """Lazy initialization of model."""
        if self._session is not None:
            return

        self._session = _load_model()

    def predict(self, audio_array: np.ndarray) -> Dict[str, Any]:
        """
        Predict whether a conversational turn is complete.

        Args:
            audio_array: Audio samples as numpy array. Should be:
                - float32 or int16
                - 16kHz sample rate
                - Mono (1D array)
                - Up to 8 seconds (will be truncated/padded)

        Returns:
            Dictionary with:
                - complete: bool - True if turn is complete
                - probability: float - Confidence score (0.0-1.0)
        """
        self._ensure_initialized()

        # Convert int16 to float32 if needed
        if audio_array.dtype == np.int16:
            audio_array = audio_array.astype(np.float32) / 32768.0

        # Ensure float32
        if audio_array.dtype != np.float32:
            audio_array = audio_array.astype(np.float32)

        # Flatten if needed
        if audio_array.ndim > 1:
            audio_array = audio_array.flatten()

        # Truncate to last 8 seconds (keep the end)
        audio_array = _truncate_or_pad_audio(audio_array)

        # Extract features using WhisperFeatureExtractor (matching reference implementation)
        feature_extractor = _get_feature_extractor()
        inputs = feature_extractor(
            audio_array,
            sampling_rate=SAMPLE_RATE,
            return_tensors="np",
            padding="max_length",
            max_length=MAX_DURATION_SECONDS * SAMPLE_RATE,
            truncation=True,
            do_normalize=True,
        )

        input_features = inputs["input_features"].astype(np.float32)

        # Run inference
        outputs = self._session.run(None, {"input_features": input_features})

        # Extract probability
        probability = float(outputs[0][0].item())

        return {
            "complete": probability > self._threshold,
            "probability": probability
        }

    @property
    def threshold(self) -> float:
        """Current confidence threshold."""
        return self._threshold

    @threshold.setter
    def threshold(self, value: float):
        """Update threshold."""
        self._threshold = max(0.0, min(1.0, value))


# Module-level instance for convenience
_detector_instance: Optional[TurnDetector] = None


def get_detector() -> TurnDetector:
    """Get or create the shared TurnDetector instance."""
    global _detector_instance
    if _detector_instance is None:
        _detector_instance = TurnDetector()
    return _detector_instance


def predict_turn_complete(audio_array: np.ndarray) -> Dict[str, Any]:
    """
    Convenience function to predict turn completion.

    Args:
        audio_array: Audio samples (see TurnDetector.predict for details).

    Returns:
        Dictionary with 'complete' (bool) and 'probability' (float).
    """
    return get_detector().predict(audio_array)


def is_available() -> bool:
    """Check if turn detection service is available (dependencies installed)."""
    try:
        import onnxruntime
        return True
    except ImportError:
        return False


def unload():
    """
    Unload turn detection model and clear instances to free memory.

    Call this when disabling open mic or speech features.
    """
    global _session, _feature_extractor, _detector_instance

    with _model_lock:
        if _session is not None:
            print("[TurnDetection] Unloading ONNX session...")
            del _session
            _session = None

        if _feature_extractor is not None:
            _feature_extractor = None

    _detector_instance = None

    # Force garbage collection
    import gc
    gc.collect()

    print("[TurnDetection] Model unloaded")


def is_loaded() -> bool:
    """Check if turn detection model is currently loaded."""
    return _session is not None


if __name__ == "__main__":
    _download_model()
