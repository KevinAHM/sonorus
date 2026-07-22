"""
FireRedVAD ONNX streaming wrapper.

DFSMN-based Voice Activity Detection using ONNX Runtime + kaldi_native_fbank.
No torch dependency. Returns per-frame (10ms) speech probabilities.
"""
import json
import os
import numpy as np
import kaldi_native_fbank as knf
import onnxruntime as ort

_DIR = os.path.dirname(os.path.dirname(__file__))
_MODEL_DIR = os.path.join(_DIR, "models", "FireRedVAD")
_CMVN_PATH = os.path.join(_MODEL_DIR, "cmvn.json")
_ONNX_DIR = os.path.join(_MODEL_DIR, "onnx")
_ONNX_PATH = os.path.join(_ONNX_DIR, "firered_vad.onnx")

NUM_BLOCKS = 8
PROJ_DIM = 128
INITIAL_CACHE_LEN = 10
SAMPLE_RATE = 16000


def _load_cmvn(path):
    with open(path, "r") as f:
        data = json.load(f)
    return np.array(data["means"], dtype=np.float32), np.array(data["inv_stddevs"], dtype=np.float32)


def _make_fbank():
    """Create a new OnlineFbank instance with FireRedVAD settings."""
    opts = knf.FbankOptions()
    opts.frame_opts.samp_freq = 16000
    opts.frame_opts.frame_length_ms = 25
    opts.frame_opts.frame_shift_ms = 10
    opts.frame_opts.dither = 0.0
    opts.frame_opts.snip_edges = True
    opts.mel_opts.num_bins = 80
    opts.mel_opts.debug_mel = False
    return knf.OnlineFbank(opts)


# Shared ONNX session (thread-safe for inference, loaded once)
_session = None
_session_lock = __import__('threading').Lock()
_cmvn = None


def _get_session():
    """Get or create shared ONNX session (thread-safe, loaded once)."""
    global _session, _cmvn
    if _session is not None:
        return _session, _cmvn

    with _session_lock:
        if _session is not None:
            return _session, _cmvn

        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        _session = ort.InferenceSession(
            _ONNX_PATH,
            providers=["CPUExecutionProvider"],
            sess_options=opts,
        )
        _cmvn = _load_cmvn(_CMVN_PATH)
        print("[VAD] FireRedVAD ONNX session loaded")
        return _session, _cmvn


def unload_session():
    """Unload shared ONNX session to free memory."""
    global _session, _cmvn
    with _session_lock:
        _session = None
        _cmvn = None


class FireRedVADModel:
    """
    ONNX wrapper for FireRedVAD streaming inference.

    Each instance has its own streaming caches and persistent OnlineFbank,
    but shares the ONNX session. This allows multiple independent VAD
    streams (e.g. open mic + spell detector) without cache corruption,
    while only loading the model once.

    The OnlineFbank is kept alive across calls so that tail samples from
    one chunk carry over to the next (25ms window, 10ms shift means each
    512-sample chunk produces ~3 frames with no sample loss).
    """

    def __init__(self):
        session, cmvn = _get_session()
        self._session = session
        self._means, self._inv_stddevs = cmvn
        self.reset_states()

    def reset_states(self):
        """Reset streaming caches and fbank state."""
        self._caches = {
            f"cache_{i}": np.zeros((1, PROJ_DIM, INITIAL_CACHE_LEN), dtype=np.float32)
            for i in range(NUM_BLOCKS)
        }
        self._fbank = _make_fbank()
        self._frames_consumed = 0

    def __call__(self, audio_int16, sample_rate=16000):
        """
        Process raw int16 audio and return per-frame speech probabilities.

        Args:
            audio_int16: numpy int16 array of audio samples.
            sample_rate: must be 16000.

        Returns:
            List of float probabilities, one per 10ms frame.
            Empty list if no new frames are ready.
        """
        # Feed audio into persistent fbank (carries over tail samples)
        self._fbank.accept_waveform(sample_rate, audio_int16.tolist())

        # Extract only NEW frames since last call
        n_ready = self._fbank.num_frames_ready
        n_new = n_ready - self._frames_consumed
        if n_new <= 0:
            return []

        frames = [self._fbank.get_frame(i) for i in range(self._frames_consumed, n_ready)]
        self._frames_consumed = n_ready

        feat = np.vstack(frames).astype(np.float32)

        # CMVN normalize
        feat = (feat - self._means) * self._inv_stddevs
        # [num_frames, 80] -> [1, num_frames, 80]
        feat = feat[np.newaxis, :, :]

        inputs = {"feat": feat, **self._caches}
        outputs = self._session.run(None, inputs)

        probs = outputs[0]  # [1, num_frames, 1]
        for i in range(NUM_BLOCKS):
            self._caches[f"cache_{i}"] = outputs[i + 1]

        return probs[0, :, 0].tolist()
