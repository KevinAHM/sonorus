"""
Voice Activity Detection (VAD) service using Silero VAD.

Provides streaming VAD for open mic mode - detects speech start/end
in real-time by processing audio chunks.

Uses ONNX runtime for CPU-efficient inference.

Two API levels:
1. SileroVADAnalyzer - Low-level, Pipecat-compatible, returns probabilities
2. VADProcessor - High-level, uses VADIterator for speech start/end events
"""
import time
import numpy as np
# torch removed - using numpy-only silero_vad
from typing import Optional, Literal, Callable
import threading

# Lazy-loaded model
_vad_model = None
_model_lock = threading.Lock()

# Constants matching Pipecat/smart-turn expectations
SAMPLE_RATE = 16000
CHUNK_SIZE = 512  # Silero expects 512 samples at 16kHz
MODEL_RESET_INTERVAL = 5.0  # Reset internal state every N seconds


def _load_model():
    """Load Silero VAD model (ONNX)."""
    global _vad_model
    if _vad_model is not None:
        return _vad_model

    with _model_lock:
        if _vad_model is not None:
            return _vad_model

        try:
            from services.silero_vad import load_silero_vad
            _vad_model = load_silero_vad(onnx=True)
            return _vad_model
        except ImportError:
            print("[VAD] silero-vad not installed. Run: pip install silero-vad")
            raise
        except Exception as e:
            print(f"[VAD] Failed to load model: {e}")
            raise


class SileroVADAnalyzer:
    """
    Low-level Silero VAD analyzer - Pipecat compatible.

    Returns speech probability for each chunk, letting you handle
    the state machine externally. Compatible with smart-turn's pattern.

    Usage:
        analyzer = SileroVADAnalyzer(threshold=0.5)

        # In audio callback:
        prob = analyzer.analyze(audio_chunk)
        is_speech = prob > analyzer.threshold

        # Or use the convenience method:
        is_speech = analyzer.is_speech(audio_chunk)
    """

    def __init__(self, threshold: float = 0.5, sample_rate: int = SAMPLE_RATE):
        """
        Args:
            threshold: Speech probability threshold (0.0-1.0).
            sample_rate: Audio sample rate (must be 8000 or 16000).
        """
        if sample_rate not in (8000, 16000):
            raise ValueError(f"Sample rate must be 8000 or 16000, got {sample_rate}")

        self._threshold = threshold
        self._sample_rate = sample_rate
        self._chunk_size = 512 if sample_rate == 16000 else 256

        self._model = None
        self._last_reset_time = time.time()

    def _ensure_initialized(self):
        """Lazy initialization of model."""
        if self._model is None:
            self._model = _load_model()

    def _maybe_reset(self):
        """Periodically reset model state to prevent drift in long sessions."""
        if (time.time() - self._last_reset_time) >= MODEL_RESET_INTERVAL:
            if self._model is not None:
                self._model.reset_states()
            self._last_reset_time = time.time()

    def _prepare_chunk(self, audio_chunk: np.ndarray) -> np.ndarray:
        """Prepare audio chunk for VAD processing."""
        # Convert int16 to float32 if needed
        if audio_chunk.dtype == np.int16:
            audio_chunk = audio_chunk.astype(np.float32) / 32768.0

        # Ensure 1D
        if audio_chunk.ndim > 1:
            audio_chunk = audio_chunk.flatten()

        # Pad or truncate to expected chunk size
        if len(audio_chunk) < self._chunk_size:
            audio_chunk = np.pad(audio_chunk, (0, self._chunk_size - len(audio_chunk)))
        elif len(audio_chunk) > self._chunk_size:
            audio_chunk = audio_chunk[:self._chunk_size]

        return audio_chunk

    def analyze(self, audio_chunk: np.ndarray) -> float:
        """
        Get speech probability for an audio chunk.

        Args:
            audio_chunk: Audio samples (int16 or float32, mono, 512 samples at 16kHz).

        Returns:
            Speech probability (0.0-1.0).
        """
        self._ensure_initialized()
        self._maybe_reset()

        chunk = self._prepare_chunk(audio_chunk)

        # silero-vad model (numpy version) accepts numpy arrays
        prob = self._model(chunk, self._sample_rate).item()

        return float(prob)

    def is_speech(self, audio_chunk: np.ndarray) -> bool:
        """
        Check if audio chunk contains speech.

        Args:
            audio_chunk: Audio samples (int16 or float32, mono).

        Returns:
            True if speech probability exceeds threshold.
        """
        return self.analyze(audio_chunk) > self._threshold

    def reset(self):
        """Reset model state."""
        if self._model is not None:
            self._model.reset_states()
        self._last_reset_time = time.time()

    @property
    def threshold(self) -> float:
        return self._threshold

    @threshold.setter
    def threshold(self, value: float):
        self._threshold = max(0.0, min(1.0, value))

    @property
    def chunk_size(self) -> int:
        return self._chunk_size

    @property
    def sample_rate(self) -> int:
        return self._sample_rate


class VADProcessor:
    """
    Streaming Voice Activity Detection processor.

    Processes audio chunks and detects speech start/end events.
    Designed for real-time open mic mode.

    Usage:
        processor = VADProcessor(threshold=0.5)

        # In audio callback:
        event = processor.process_chunk(audio_chunk)
        if event == 'start':
            # Speech started
        elif event == 'end':
            # Speech ended (silence detected)
    """

    def __init__(
        self,
        threshold: float = 0.5,
        sample_rate: int = 16000,
        min_silence_ms: int = 100,
        speech_pad_ms: int = 30
    ):
        """
        Args:
            threshold: Speech probability threshold (0.0-1.0). Higher = less sensitive.
            sample_rate: Audio sample rate (must be 8000 or 16000).
            min_silence_ms: Minimum silence duration before marking speech end.
            speech_pad_ms: Padding around speech segments.
        """
        if sample_rate not in (8000, 16000):
            raise ValueError(f"Sample rate must be 8000 or 16000, got {sample_rate}")

        self._threshold = threshold
        self._sample_rate = sample_rate
        self._min_silence_ms = min_silence_ms
        self._speech_pad_ms = speech_pad_ms

        # State
        self._model = None
        self._iterator = None
        self._is_speaking = False

        # Expected chunk size: 512 samples for 16kHz, 256 for 8kHz
        self._chunk_size = 512 if sample_rate == 16000 else 256

    def _ensure_initialized(self):
        """Lazy initialization of model and iterator."""
        if self._iterator is not None:
            return

        self._model = _load_model()

        # Import VADIterator from silero_vad
        from services.silero_vad import VADIterator

        self._iterator = VADIterator(
            model=self._model,
            threshold=self._threshold,
            sampling_rate=self._sample_rate,
            min_silence_duration_ms=self._min_silence_ms,
            speech_pad_ms=self._speech_pad_ms
        )

    def process_chunk(self, audio_chunk: np.ndarray) -> Optional[Literal['start', 'end']]:
        """
        Process an audio chunk and detect speech events.

        Args:
            audio_chunk: Audio samples as numpy array. Should be:
                - int16 or float32
                - Mono (1D array)
                - 512 samples for 16kHz, 256 for 8kHz

        Returns:
            'start' if speech just started
            'end' if speech just ended (silence detected)
            None if no state change
        """
        self._ensure_initialized()

        # Convert int16 to float32 if needed (silero expects float)
        if audio_chunk.dtype == np.int16:
            audio_chunk = audio_chunk.astype(np.float32) / 32768.0

        # Ensure correct shape
        if audio_chunk.ndim > 1:
            audio_chunk = audio_chunk.flatten()

        # Pad or truncate to expected chunk size
        if len(audio_chunk) < self._chunk_size:
            audio_chunk = np.pad(audio_chunk, (0, self._chunk_size - len(audio_chunk)))
        elif len(audio_chunk) > self._chunk_size:
            audio_chunk = audio_chunk[:self._chunk_size]

        # Process through VADIterator (numpy version accepts numpy arrays)
        result = self._iterator(audio_chunk, return_seconds=True)

        if result is None:
            return None

        if 'start' in result:
            if not self._is_speaking:
                self._is_speaking = True
                return 'start'
        elif 'end' in result:
            if self._is_speaking:
                self._is_speaking = False
                return 'end'

        return None

    def reset(self):
        """Reset VAD state. Call when starting a new session."""
        if self._iterator:
            self._iterator.reset_states()
        self._is_speaking = False

    @property
    def is_speaking(self) -> bool:
        """Whether speech is currently detected."""
        return self._is_speaking

    @property
    def threshold(self) -> float:
        """Current threshold value."""
        return self._threshold

    @threshold.setter
    def threshold(self, value: float):
        """Update threshold (takes effect on next chunk)."""
        self._threshold = max(0.0, min(1.0, value))
        if self._iterator:
            self._iterator.threshold = self._threshold

    @property
    def chunk_size(self) -> int:
        """Expected chunk size in samples."""
        return self._chunk_size


class VADEventHandler:
    """
    Higher-level VAD event handler with callbacks.

    Manages the VAD processor and fires callbacks on speech events.
    Includes debouncing and guard checks.
    """

    def __init__(
        self,
        on_speech_start: Optional[Callable[[], None]] = None,
        on_speech_end: Optional[Callable[[], None]] = None,
        check_guards: Optional[Callable[[], bool]] = None,
        threshold: float = 0.5,
        sample_rate: int = 16000
    ):
        """
        Args:
            on_speech_start: Called when speech starts (after guards pass).
            on_speech_end: Called when speech ends.
            check_guards: Optional callable returning True if VAD should be blocked.
            threshold: VAD threshold (0.0-1.0).
            sample_rate: Audio sample rate.
        """
        self._processor = VADProcessor(
            threshold=threshold,
            sample_rate=sample_rate
        )

        self._on_speech_start = on_speech_start
        self._on_speech_end = on_speech_end
        self._check_guards = check_guards

        self._enabled = True
        self._speech_started_fired = False

    def process_chunk(self, audio_chunk: np.ndarray):
        """
        Process audio chunk and fire callbacks.

        Args:
            audio_chunk: Audio samples (int16 or float32, mono).
        """
        if not self._enabled:
            return

        event = self._processor.process_chunk(audio_chunk)

        if event == 'start':
            # Check guards before firing speech start
            if self._check_guards and self._check_guards():
                # Guards blocked - reset and ignore
                self._processor.reset()
                return

            self._speech_started_fired = True
            if self._on_speech_start:
                self._on_speech_start()

        elif event == 'end':
            if self._speech_started_fired:
                self._speech_started_fired = False
                if self._on_speech_end:
                    self._on_speech_end()

    def reset(self):
        """Reset state."""
        self._processor.reset()
        self._speech_started_fired = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool):
        self._enabled = value
        if not value:
            self.reset()

    @property
    def threshold(self) -> float:
        return self._processor.threshold

    @threshold.setter
    def threshold(self, value: float):
        self._processor.threshold = value

    @property
    def is_speaking(self) -> bool:
        return self._processor.is_speaking

    @property
    def chunk_size(self) -> int:
        return self._processor.chunk_size


def is_available() -> bool:
    """Check if VAD service is available (silero-vad installed)."""
    try:
        import silero_vad
        return True
    except ImportError:
        return False


# Module-level shared instances
_analyzer_instance: Optional[SileroVADAnalyzer] = None
_processor_instance: Optional[VADProcessor] = None


def get_analyzer(threshold: float = 0.5) -> SileroVADAnalyzer:
    """Get or create shared SileroVADAnalyzer instance (Pipecat-compatible)."""
    global _analyzer_instance
    if _analyzer_instance is None:
        _analyzer_instance = SileroVADAnalyzer(threshold=threshold)
    return _analyzer_instance


def get_processor(threshold: float = 0.5, min_silence_ms: int = 200) -> VADProcessor:
    """Get or create shared VADProcessor instance (high-level with events)."""
    global _processor_instance
    if _processor_instance is None:
        _processor_instance = VADProcessor(threshold=threshold, min_silence_ms=min_silence_ms)
    return _processor_instance


def analyze_chunk(audio_chunk: np.ndarray, threshold: float = 0.5) -> float:
    """
    Convenience function to get speech probability for an audio chunk.

    Args:
        audio_chunk: Audio samples (int16 or float32, mono, 512 samples at 16kHz).
        threshold: Speech threshold (only affects the shared instance).

    Returns:
        Speech probability (0.0-1.0).
    """
    return get_analyzer(threshold).analyze(audio_chunk)


def unload():
    """
    Unload VAD model and clear instances to free memory.

    Call this when disabling open mic or speech features.
    """
    global _vad_model, _analyzer_instance, _processor_instance

    with _model_lock:
        if _vad_model is not None:
            print("[VAD] Unloading Silero VAD model...")
            # Reset states before unloading
            try:
                _vad_model.reset_states()
            except Exception:
                pass
            del _vad_model
            _vad_model = None

    _analyzer_instance = None
    _processor_instance = None

    # Force garbage collection
    import gc
    gc.collect()

    print("[VAD] Model unloaded")


def is_loaded() -> bool:
    """Check if VAD model is currently loaded."""
    return _vad_model is not None
