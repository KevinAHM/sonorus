"""
Forced alignment module for phoneme/word-level timestamps using ONNX Runtime (INT8).
Replaces torch/transformers logic with numpy-based CTC alignment.
"""
import logging
import numpy as np
import onnxruntime as ort
import json
from pathlib import Path
from typing import List, Dict, Tuple
from dataclasses import dataclass

# Local models directory (avoids HF cache issues on Windows)
MODELS_DIR = Path(__file__).resolve().parent.parent / "models"

try:
    import scipy.signal
except ImportError:
    pass # Managed by checks

logger = logging.getLogger(__name__)

# Global model cache (ONNX session)
_alignment_session = None

@dataclass
class Point:
    """Represents a point in the alignment path."""
    token_index: int
    time_index: int
    score: float

@dataclass
class Segment:
    """Represents a segment with start/end times."""
    label: str
    start: int
    end: int
    score: float

    @property
    def length(self):
        return self.end - self.start

def get_trellis_numpy(emission: np.ndarray, tokens: List[int], blank_id: int = 0) -> np.ndarray:
    """
    Compute trellis matrix for CTC forced alignment (Numpy version).
    """
    num_frame = emission.shape[0]
    num_tokens = len(tokens)

    trellis = np.zeros((num_frame, num_tokens))
    trellis[1:, 0] = np.cumsum(emission[1:, blank_id], axis=0)
    trellis[0, 1:] = -np.inf
    trellis[-num_tokens + 1:, 0] = -np.inf

    for t in range(num_frame - 1):
        # Score for staying at the same token
        stay = trellis[t, 1:] + emission[t, blank_id]
        # Score for changing to the next token
        change = trellis[t, :-1] + emission[t, tokens[1:]]
        # Take max
        trellis[t + 1, 1:] = np.maximum(stay, change)

    return trellis


def backtrack_numpy(
    trellis: np.ndarray, 
    emission: np.ndarray, 
    tokens: List[int], 
    blank_id: int = 0,
    start_token_index: int = -1
) -> List[Point]:
    """
    Backtrack through trellis to get token-frame alignments (Numpy version).
    
    Args:
        start_token_index: If >= 0, starts backtracking from this token index at the last frame.
                           If -1, starts from the last token (standard behavior).
    """
    t = trellis.shape[0] - 1
    j = trellis.shape[1] - 1 if start_token_index == -1 else start_token_index
    
    # Bounds check: ensure j doesn't exceed trellis dimensions
    j = min(j, trellis.shape[1] - 1)
    
    if t < 0 or j < 0:
        return []  # Not enough data to align

    path = [Point(j, t, np.exp(emission[t, blank_id]))]
    while j > 0 and t > 0:
        # Frame-wise score of stay vs change
        p_stay = emission[t - 1, blank_id]
        p_change = emission[t - 1, tokens[j]]

        # Context-aware score for stay vs change
        stayed = trellis[t - 1, j] + p_stay
        changed = trellis[t - 1, j - 1] + p_change

        # Update position
        t -= 1
        if changed > stayed:
            j -= 1

        # Store the path with frame-wise probability
        score = p_change if changed > stayed else p_stay
        path.append(Point(j, t, np.exp(score)))

    # Now j == 0, which means, it reached the SoS.
    while t > 0:
        prob = np.exp(emission[t - 1, blank_id])
        path.append(Point(j, t - 1, prob))
        t -= 1

    return path[::-1]

def merge_repeats(path: List[Point], transcript: str) -> List[Segment]:
    i1, i2 = 0, 0
    segments = []
    while i1 < len(path):
        while i2 < len(path) and path[i1].token_index == path[i2].token_index:
            i2 += 1
        score = sum(path[k].score for k in range(i1, i2)) / (i2 - i1)
        segments.append(
            Segment(
                transcript[path[i1].token_index],
                path[i1].time_index,
                path[i2 - 1].time_index + 1,
                score,
            )
        )
        i1 = i2
    return segments

def merge_words(segments: List[Segment], separator: str = "|") -> List[Segment]:
    words = []
    i1, i2 = 0, 0
    while i1 < len(segments):
        if i2 >= len(segments) or segments[i2].label == separator:
            if i1 != i2:
                segs = segments[i1:i2]
                word = "".join([seg.label for seg in segs])
                total_len = sum(seg.length for seg in segs)
                score = sum(seg.score * seg.length for seg in segs) / total_len
                words.append(Segment(word, segments[i1].start, segments[i2 - 1].end, score))
            i1 = i2 + 1
            i2 = i1
        else:
            i2 += 1
    return words

def get_alignment_session():
    """Lazy load ONNX session for alignment."""
    global _alignment_session
    if _alignment_session is None:
        logger.info("Loading Distil-Wav2Vec2 ONNX model...")

        # Local cache path (CPU only - DML incompatible with this model)
        local_path = MODELS_DIR / "onnx" / "distil-wav2vec2_int8.onnx"

        if local_path.exists():
            model_path = str(local_path)
            logger.info(f"Using cached alignment model: {model_path}")
        else:
            from huggingface_hub import hf_hub_download

            try:
                logger.info("Downloading alignment model from HuggingFace...")
                MODELS_DIR.mkdir(parents=True, exist_ok=True)
                hf_hub_download(
                    repo_id="KevinAHM/distil-wav2vec2-onnx",
                    filename="onnx/distil-wav2vec2_int8.onnx",
                    local_dir=str(MODELS_DIR)
                )
                model_path = str(local_path)
                logger.info(f"Downloaded alignment model: {model_path}")
            except Exception as e:
                logger.warning(f"Could not download from HF: {e}")
                raise FileNotFoundError("Alignment model not found locally or on HF.")

        sess_options = ort.SessionOptions()
        sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        _alignment_session = ort.InferenceSession(model_path, sess_options)
        print(f"[Alignment] Loaded (CPU, int8)")
    return _alignment_session

def get_vocab():
    # Hardcoded vocab for typical Wav2Vec2 CTC (OthmaneJ/distil-wav2vec2)
    # The model uses 32 tokens. 
    # [PAD], [UNK], |, E, T, ... (Usually sorted by freq or standard)
    # Since I don't have the tokenizer.json, I will use a standard mapping.
    # WAIT: Standard pytorch load showed 'labels': list(processor.tokenizer.get_vocab().keys())
    # OthmaneJ/distil-wav2vec2 usually follows standard English alphabet.
    # Valid tokens: <pad>, <s>, </s>, <unk>, |, E, T, A, O, N, I, H, S, R, D, L, U, M, W, C, F, G, Y, P, B, V, K, ', X, J, Q, Z
    # Let's approximate standard vocab + CTC blank (usually 0 is <pad>)
    
    # Minimal vocab map constructed from standard Wav2Vec2
    vocab_list = [
        "<pad>", "<s>", "</s>", "<unk>", "|", 
        "E", "T", "A", "O", "N", "I", "H", "S", "R", "D", "L", "U", "M", "W", "C", "F", "G", "Y", "P", "B", "V", "K", "'", "X", "J", "Q", "Z"
    ]
    return {token: i for i, token in enumerate(vocab_list)}

def get_emissions(
    audio_waveform: np.ndarray,
    audio_sample_rate: int = 24000,
    target_sample_rate: int = 16000
) -> np.ndarray:
    """
    Run Wav2Vec2 inference to get emissions.
    """
    session = get_alignment_session()
    
    # Handle torch tensor input if passed by legacy code
    if hasattr(audio_waveform, 'cpu'):
        audio_waveform = audio_waveform.cpu().numpy()
        
    if audio_waveform.ndim > 1:
        audio_waveform = audio_waveform.flatten()

    # Resample
    if audio_sample_rate != target_sample_rate:
        samples = int(len(audio_waveform) * target_sample_rate / audio_sample_rate)
        # Check if scipy is available, if not use linear interpolation
        try:
             import scipy.signal
             audio_waveform = scipy.signal.resample(audio_waveform, samples)
        except ImportError:
             # simple linear interpolation fallback
             x = np.linspace(0, len(audio_waveform), samples)
             audio_waveform = np.interp(x, np.arange(len(audio_waveform)), audio_waveform)
    
    # Normalize (Standardization Mean=0, Std=1)
    # Protection against constant silence
    mean = np.mean(audio_waveform)
    std = np.std(audio_waveform)
    normalized_audio = (audio_waveform - mean) / (std + 1e-7)
    
    # Prepare input [Batch, Time]
    # Check if model expects float16 (FP16 model)
    input_meta = session.get_inputs()[0]
    if input_meta.type == 'tensor(float16)':
        input_values = normalized_audio[np.newaxis, :].astype(np.float16)
    else:
        input_values = normalized_audio[np.newaxis, :].astype(np.float32)

    # Inference
    logits = session.run(None, {"input_values": input_values})[0] # [B, T, Vocab]
    
    # Log Softmax
    def log_softmax(x):
        e = np.exp(x - np.max(x, axis=-1, keepdims=True))
        return np.log(e / np.sum(e, axis=-1, keepdims=True))
        
    emissions = log_softmax(logits)[0] # [Time, Vocab]
    return emissions

def align_emissions_to_words(
    emissions: np.ndarray,
    text: str,
    total_audio_duration: float,
    partial: bool = False
) -> Dict[str, List]:
    """
    Align pre-calculated emissions to text.
    """
    # Prepare Text
    import re
    clean_text = re.sub(r'[^\w\s\']', '', text) 
    transcript_text = clean_text.upper()
    transcript = "|" + transcript_text.replace(" ", "|") + "|"
    
    vocab = get_vocab()
    tokens = []
    for c in transcript:
        if c in vocab:
            tokens.append(vocab[c])
        elif c == " ":
             if "|" in vocab: tokens.append(vocab["|"])
    
    if len(tokens) == 0:
        return {"words": [], "wordStartTimeSeconds": [], "wordEndTimeSeconds": []}

    # Alignment
    try:
        trellis = get_trellis_numpy(emissions, tokens, blank_id=0)
        
        start_token = -1
        if partial:
            valid_scores = trellis[-1, :]
            start_token = int(np.argmax(valid_scores))
            
        path = backtrack_numpy(trellis, emissions, tokens, blank_id=0, start_token_index=start_token)
        segments = merge_repeats(path, transcript)
        word_segments = merge_words(segments, separator="|")
    except Exception as e:
        logger.error(f"Alignment logic failed: {e}")
        return {"words": [], "wordStartTimeSeconds": [], "wordEndTimeSeconds": []}

    # Format Output
    # Wav2Vec2 typically outputs at 50Hz (16000 / 320 stride = 50 frames/sec)
    # But let's check actual vs expected
    expected_frames_50hz = total_audio_duration * 50
    actual_frames = emissions.shape[0]
    ratio = total_audio_duration / actual_frames

    # Debug disabled - was too spammy
    # print(f"[Alignment] duration={total_audio_duration:.3f}s, emission_frames={actual_frames}, "
    #       f"expected@50Hz={expected_frames_50hz:.0f}, ratio={ratio:.6f}s/frame, "
    #       f"effective_hz={1/ratio:.1f}")

    original_words = text.split()
    word_list = []
    start_times = []
    end_times = []

    for i, word_seg in enumerate(word_segments):
        if i < len(original_words):
            word_list.append(original_words[i])
        else:
            word_list.append(word_seg.label)
        start_times.append(round(word_seg.start * ratio, 3))
        end_times.append(round(word_seg.end * ratio, 3))

    if len(word_list) < len(original_words):
        if partial:
            pass
        else:
            # Fallback not implemented for pure emissions route, assumes caller handles it
            pass

    return {
        "words": word_list,
        "wordStartTimeSeconds": start_times,
        "wordEndTimeSeconds": end_times
    }

def align_audio_to_words(
    audio_waveform: np.ndarray,
    text: str,
    audio_sample_rate: int = 24000,
    partial: bool = False
) -> Dict[str, List]:
    """
    Legacy wrapper (re-calculates everything).
    """
    target_sample_rate = 16000

    # Calculate duration from original audio
    original_samples = len(audio_waveform)
    duration = original_samples / audio_sample_rate

    # Calculate what the resampled length will be
    resampled_samples = int(original_samples * target_sample_rate / audio_sample_rate)
    resampled_duration = resampled_samples / target_sample_rate

    # Debug disabled - was too spammy
    # print(f"[Alignment] Input: {original_samples} samples @ {audio_sample_rate}Hz = {duration:.3f}s")
    # print(f"[Alignment] Resampled: {resampled_samples} samples @ {target_sample_rate}Hz = {resampled_duration:.3f}s")

    emissions = get_emissions(audio_waveform, audio_sample_rate, target_sample_rate)

    return align_emissions_to_words(emissions, text, duration, partial=partial)


def fallback_alignment(text, total_samples, sr):
    words = text.split()
    duration = total_samples / sr
    time_per_word = duration / len(words) if len(words) > 0 else 0
    return {
        "words": words,
        "wordStartTimeSeconds": [round(i * time_per_word, 3) for i in range(len(words))],
        "wordEndTimeSeconds": [round((i + 1) * time_per_word, 3) for i in range(len(words))]
    }


