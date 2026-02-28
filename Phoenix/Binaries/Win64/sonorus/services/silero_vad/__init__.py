"""
Silero VAD - torch-free version using numpy + onnxruntime.
"""

from .model import load_silero_vad
from .utils_vad import VADIterator, OnnxWrapper

__all__ = ['load_silero_vad', 'VADIterator', 'OnnxWrapper']
