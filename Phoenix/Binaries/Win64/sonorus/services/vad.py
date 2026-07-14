"""
Voice Activity Detection (VAD) service using FireRedVAD.

Provides streaming VAD for open mic mode - detects speech start/end
in real-time by processing audio chunks.

Uses ONNX runtime for CPU-efficient inference with kaldi_native_fbank
for feature extraction.

Two API levels:
1. VADAnalyzer - Low-level, returns probabilities per chunk
2. VADProcessor - High-level, speech start/end events with state machine
"""
import time
import numpy as np
from typing import Optional, Literal, Callable

# Constants
SAMPLE_RATE = 16000
# FireRedVAD processes arbitrary chunk sizes; 512 samples (32ms) is a good
# minimum for streaming that yields ~1-2 fbank frames per call.
CHUNK_SIZE = 512
MODEL_RESET_INTERVAL = 5.0


def _create_model():
    """Create a new FireRedVAD model instance (own caches, shared ONNX session)."""
    from services.firered_vad import FireRedVADModel
    return FireRedVADModel()


class VADAnalyzer:
    """
    Low-level VAD analyzer using FireRedVAD.

    Returns speech probability for each audio chunk. FireRedVAD produces
    per-frame (10ms) probabilities; this returns the max probability across
    all frames in the chunk for compatibility with threshold-based detection.

    Usage:
        analyzer = VADAnalyzer(threshold=0.5)
        prob = analyzer.analyze(audio_chunk)
        is_speech = prob > analyzer.threshold
    """

    def __init__(self, threshold: float = 0.5, sample_rate: int = SAMPLE_RATE):
        if sample_rate != 16000:
            raise ValueError(f"FireRedVAD only supports 16000 sample rate, got {sample_rate}")

        self._threshold = threshold
        self._sample_rate = sample_rate
        self._chunk_size = CHUNK_SIZE

        self._model = None
        self._last_reset_time = time.time()

    def _ensure_initialized(self):
        if self._model is None:
            self._model = _create_model()

    def _maybe_reset(self):
        if (time.time() - self._last_reset_time) >= MODEL_RESET_INTERVAL:
            if self._model is not None:
                self._model.reset_states()
            self._last_reset_time = time.time()

    def _prepare_chunk(self, audio_chunk: np.ndarray) -> np.ndarray:
        """Ensure audio is int16 1D."""
        if audio_chunk.dtype == np.float32 or audio_chunk.dtype == np.float64:
            audio_chunk = (audio_chunk * 32768.0).clip(-32768, 32767).astype(np.int16)
        if audio_chunk.ndim > 1:
            audio_chunk = audio_chunk.flatten()
        return audio_chunk

    def analyze(self, audio_chunk: np.ndarray) -> float:
        """
        Get speech probability for an audio chunk.

        Args:
            audio_chunk: Audio samples (int16 or float32, mono).

        Returns:
            Max speech probability across frames (0.0-1.0).
        """
        self._ensure_initialized()
        self._maybe_reset()

        chunk = self._prepare_chunk(audio_chunk)
        probs = self._model(chunk, self._sample_rate)

        if not probs:
            return 0.0
        return float(max(probs))

    def is_speech(self, audio_chunk: np.ndarray) -> bool:
        return self.analyze(audio_chunk) > self._threshold

    def reset(self):
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

    FireRedVAD outputs per-frame (10ms) probabilities. This processor
    applies smoothing and a state machine to determine speech boundaries.

    Usage:
        processor = VADProcessor(threshold=0.5)
        event = processor.process_chunk(audio_chunk)
        if event == 'start':
            # Speech started
        elif event == 'end':
            # Speech ended
    """

    def __init__(
        self,
        threshold: float = 0.5,
        sample_rate: int = 16000,
        min_silence_ms: int = 100,
        speech_pad_ms: int = 30
    ):
        if sample_rate != 16000:
            raise ValueError(f"FireRedVAD only supports 16000 sample rate, got {sample_rate}")

        self._threshold = threshold
        self._sample_rate = sample_rate
        self._min_silence_ms = min_silence_ms
        self._speech_pad_ms = speech_pad_ms

        self._model = None
        self._is_speaking = False

        # State machine (frame-level, 10ms per frame)
        self._min_silence_frames = int(min_silence_ms / 10)
        self._min_speech_frames = 8  # 80ms minimum speech
        self._speech_frame_count = 0
        self._silence_frame_count = 0
        self._triggered = False
        self._temp_end = 0
        self._current_frame = 0

        # Smoothing window (5-frame moving average like FireRedVAD default)
        self._smooth_window_size = 5
        self._smooth_buffer = []
        self._smooth_sum = 0.0

        self._chunk_size = CHUNK_SIZE

    def _ensure_initialized(self):
        if self._model is not None:
            return
        self._model = _create_model()

    def _smooth_prob(self, prob):
        """Moving average smoothing."""
        self._smooth_buffer.append(prob)
        self._smooth_sum += prob
        if len(self._smooth_buffer) > self._smooth_window_size:
            self._smooth_sum -= self._smooth_buffer.pop(0)
        return self._smooth_sum / len(self._smooth_buffer)

    def process_chunk(self, audio_chunk: np.ndarray) -> Optional[Literal['start', 'end']]:
        """
        Process an audio chunk and detect speech events.

        Args:
            audio_chunk: Audio samples as numpy array (int16 or float32, mono).

        Returns:
            'start' if speech just started
            'end' if speech just ended
            None if no state change
        """
        self._ensure_initialized()

        # FireRedVAD expects int16
        if audio_chunk.dtype == np.float32 or audio_chunk.dtype == np.float64:
            audio_chunk = (audio_chunk * 32768.0).clip(-32768, 32767).astype(np.int16)
        if audio_chunk.ndim > 1:
            audio_chunk = audio_chunk.flatten()

        # Get per-frame probabilities from model
        frame_probs = self._model(audio_chunk, self._sample_rate)
        if not frame_probs:
            return None

        # Process each frame through state machine
        result = None
        for prob in frame_probs:
            self._current_frame += 1
            smoothed = self._smooth_prob(prob)
            is_speech = smoothed >= self._threshold

            event = self._frame_state_machine(is_speech)
            if event is not None:
                result = event  # Return last event from this chunk

        return result

    def _frame_state_machine(self, is_speech: bool) -> Optional[Literal['start', 'end']]:
        """Per-frame state machine for speech boundary detection."""
        if is_speech:
            self._silence_frame_count = 0
            self._speech_frame_count += 1
            self._temp_end = 0

            if not self._triggered and self._speech_frame_count >= self._min_speech_frames:
                self._triggered = True
                if not self._is_speaking:
                    self._is_speaking = True
                    return 'start'
        else:
            self._speech_frame_count = 0

            if self._triggered:
                if self._temp_end == 0:
                    self._temp_end = self._current_frame

                self._silence_frame_count += 1
                if self._silence_frame_count >= self._min_silence_frames:
                    self._triggered = False
                    self._temp_end = 0
                    self._silence_frame_count = 0
                    if self._is_speaking:
                        self._is_speaking = False
                        return 'end'
            else:
                self._silence_frame_count += 1

        return None

    def reset(self):
        """Reset VAD state."""
        if self._model:
            self._model.reset_states()
        self._is_speaking = False
        self._triggered = False
        self._temp_end = 0
        self._current_frame = 0
        self._speech_frame_count = 0
        self._silence_frame_count = 0
        self._smooth_buffer.clear()
        self._smooth_sum = 0.0

    @property
    def is_speaking(self) -> bool:
        return self._is_speaking

    @property
    def threshold(self) -> float:
        return self._threshold

    @threshold.setter
    def threshold(self, value: float):
        self._threshold = max(0.0, min(1.0, value))

    @property
    def chunk_size(self) -> int:
        return self._chunk_size


class VADEventHandler:
    """
    Higher-level VAD event handler with callbacks.
    Manages the VAD processor and fires callbacks on speech events.
    """

    def __init__(
        self,
        on_speech_start: Optional[Callable[[], None]] = None,
        on_speech_end: Optional[Callable[[], None]] = None,
        check_guards: Optional[Callable[[], bool]] = None,
        threshold: float = 0.5,
        sample_rate: int = 16000
    ):
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
        if not self._enabled:
            return

        event = self._processor.process_chunk(audio_chunk)

        if event == 'start':
            if self._check_guards and self._check_guards():
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
    """Check if VAD service is available (FireRedVAD dependencies installed)."""
    try:
        import kaldi_native_fbank
        return True
    except ImportError:
        return False


# Module-level shared instances
_analyzer_instance: Optional[VADAnalyzer] = None
_processor_instance: Optional[VADProcessor] = None


def get_analyzer(threshold: float = 0.5) -> VADAnalyzer:
    """Get or create shared VADAnalyzer instance."""
    global _analyzer_instance
    if _analyzer_instance is None:
        _analyzer_instance = VADAnalyzer(threshold=threshold)
    return _analyzer_instance


def get_processor(threshold: float = 0.5, min_silence_ms: int = 200) -> VADProcessor:
    """Get or create shared VADProcessor instance."""
    global _processor_instance
    if _processor_instance is None:
        _processor_instance = VADProcessor(threshold=threshold, min_silence_ms=min_silence_ms)
    return _processor_instance


def analyze_chunk(audio_chunk: np.ndarray, threshold: float = 0.5) -> float:
    """Convenience function to get speech probability for an audio chunk."""
    return get_analyzer(threshold).analyze(audio_chunk)


def unload():
    """Unload VAD model and clear instances to free memory."""
    global _analyzer_instance, _processor_instance

    print("[VAD] Unloading FireRedVAD...")
    _analyzer_instance = None
    _processor_instance = None

    from services.firered_vad import unload_session
    unload_session()

    import gc
    gc.collect()
    print("[VAD] Model unloaded")


def is_loaded() -> bool:
    """Check if VAD ONNX session is currently loaded."""
    from services.firered_vad import _session
    return _session is not None
