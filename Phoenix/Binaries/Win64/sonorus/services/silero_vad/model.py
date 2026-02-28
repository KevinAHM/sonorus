"""
Silero VAD model loader - torch-free version.
"""
import os
from .utils_vad import OnnxWrapper

# Model is stored in sonorus/models/
_SONORUS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
_MODELS_DIR = os.path.join(_SONORUS_DIR, 'models')
MODEL_PATH = os.path.join(_MODELS_DIR, 'silero_vad.onnx')


def load_silero_vad(onnx=True):
    """
    Load Silero VAD model.

    Args:
        onnx: Must be True (JIT models require torch)

    Returns:
        OnnxWrapper instance
    """
    if not onnx:
        raise ValueError("Only ONNX models supported in torch-free version. Use onnx=True.")

    if not os.path.exists(MODEL_PATH):
        raise FileNotFoundError(f"Silero VAD model not found at {MODEL_PATH}")

    model = OnnxWrapper(MODEL_PATH, force_onnx_cpu=True)
    print("[VAD] Silero VAD model loaded (ONNX, torch-free)")
    return model
