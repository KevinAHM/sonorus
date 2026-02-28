"""
Audio analysis module using Deepgram or Parakeet (local ONNX).

Provides transcription with word timestamps, sentiment analysis,
speech density calculation, and quality scoring for voice sample selection.

Supports two analyzers:
- deepgram: Cloud-based, provides word timestamps + sentiment. Requires API key.
- parakeet: Local ONNX model, transcript only (no timestamps/sentiment). No API key needed.
"""

import wave
from pathlib import Path
from typing import List

# Add parent for imports
import sys
VOICE_MANAGER_DIR = Path(__file__).parent
SONORUS_DIR = VOICE_MANAGER_DIR.parent
if str(SONORUS_DIR) not in sys.path:
    sys.path.insert(0, str(SONORUS_DIR))


def get_wav_duration(wav_path: Path) -> float:
    """Get duration of a WAV file in seconds."""
    try:
        with wave.open(str(wav_path), 'rb') as w:
            frames = w.getnframes()
            rate = w.getframerate()
            return frames / float(rate)
    except Exception:
        return 0.0


def analyze_sample(wav_path: Path, language: str = "en") -> dict:
    """
    Analyze a single WAV sample using Deepgram.

    Returns:
        {
            "success": bool,
            "duration": float,
            "transcript": str,
            "words": [{"word": str, "start": float, "end": float}],
            "speechDensity": float,  # 0-1
            "sentiment": str,        # "positive", "neutral", "negative"
            "sentimentScore": float, # -1 to 1
            "error": str or None
        }
    """
    try:
        from deepgram import DeepgramClient
        from utils.settings import load_settings

        # Get duration first
        duration = get_wav_duration(wav_path)
        if duration <= 0:
            return {"success": False, "error": "Invalid audio file"}

        # Load settings for Deepgram API key
        settings = load_settings()
        stt_settings = settings.get('stt', {}).get('deepgram', {})
        api_key = stt_settings.get('api_key')

        if not api_key:
            return {"success": False, "error": "Deepgram API key not configured"}

        # Read WAV file
        with open(wav_path, 'rb') as f:
            audio_data = f.read()

        # Create Deepgram client (v5 API)
        client = DeepgramClient(api_key=api_key)

        # Transcribe with sentiment and word timestamps
        # v5 API: parameters passed directly
        print(f"[Analyzer] Analyzing {wav_path.name} with language={language}")
        response = client.listen.v1.media.transcribe_file(
            request=audio_data,
            model="nova-3",
            language=language,  # Pass language parameter
            smart_format=True,
            utterances=True,  # Word-level timestamps
            sentiment=True,   # Sentiment analysis
        )

        # Extract results
        result = response.results
        channels = result.channels if hasattr(result, 'channels') else []

        print(f"[Analyzer] {wav_path.name}: Response has {len(channels)} channels")

        if not channels:
            # LOG: This is the problem - empty response from Deepgram
            print(f"[Analyzer] WARNING: {wav_path.name} - No channels in Deepgram response (language={language})")
            print(f"[Analyzer] Response structure: {dir(result)}")
            if hasattr(result, 'error'):
                print(f"[Analyzer] API Error: {result.error}")
            return {
                "success": False,  # Changed from True to False - empty transcript means failure
                "duration": duration,
                "transcript": "",
                "words": [],
                "speechDensity": 0.0,
                "sentiment": "neutral",
                "sentimentScore": 0.0,
                "error": "No transcription channels in Deepgram response"
            }

        # Get transcript and words
        alternative = channels[0].alternatives[0] if channels[0].alternatives else None
        transcript = alternative.transcript if alternative else ""
        words_data = alternative.words if alternative and hasattr(alternative, 'words') else []

        if not transcript:
            print(f"[Analyzer] WARNING: {wav_path.name} - Empty transcript despite having channels (language={language})")
            print(f"[Analyzer] Alternatives: {len(channels[0].alternatives) if channels[0] else 0}")

        words = []
        for w in words_data:
            words.append({
                "word": w.word if hasattr(w, 'word') else str(w.get('word', '')),
                "start": w.start if hasattr(w, 'start') else float(w.get('start', 0)),
                "end": w.end if hasattr(w, 'end') else float(w.get('end', 0))
            })

        # Calculate speech density
        speech_density = calculate_speech_density(duration, words)

        # Extract sentiment - try multiple access methods since SDK may not expose all fields
        sentiment = "neutral"
        sentiment_score = 0.0

        # Method 1: Try result.sentiments (SDK attribute)
        sentiments_data = None
        if hasattr(result, 'sentiments') and result.sentiments:
            sentiments_data = result.sentiments
        # Method 2: Try accessing via to_dict() or raw JSON
        elif hasattr(result, 'to_dict'):
            result_dict = result.to_dict()
            sentiments_data = result_dict.get('sentiments')
        # Method 3: Try __dict__ access
        elif hasattr(result, '__dict__'):
            sentiments_data = result.__dict__.get('sentiments')

        if sentiments_data:
            # Handle both SDK object and dict formats
            if hasattr(sentiments_data, 'average'):
                avg_sentiment = sentiments_data.average
                sentiment = getattr(avg_sentiment, 'sentiment', 'neutral')
                sentiment_score = getattr(avg_sentiment, 'sentiment_score', 0.0)
            elif isinstance(sentiments_data, dict) and 'average' in sentiments_data:
                avg_sentiment = sentiments_data['average']
                sentiment = avg_sentiment.get('sentiment', 'neutral')
                sentiment_score = avg_sentiment.get('sentiment_score', 0.0)
            print(f"[Analyzer] {wav_path.name}: Sentiment extracted - {sentiment} ({sentiment_score:.3f})")
        else:
            print(f"[Analyzer] WARNING: {wav_path.name} - No sentiment data in response (language={language})")

        print(f"[Analyzer] {wav_path.name}: SUCCESS - transcript_len={len(transcript)}, words={len(words)}, sentiment={sentiment}")

        return {
            "success": True,
            "duration": duration,
            "transcript": transcript,
            "words": words,
            "speechDensity": speech_density,
            "sentiment": sentiment,
            "sentimentScore": sentiment_score,
            "error": None
        }

    except ImportError:
        print(f"[Analyzer] ERROR: Deepgram SDK not installed")
        return {"success": False, "error": "Deepgram SDK not installed"}
    except Exception as e:
        print(f"[Analyzer] ERROR analyzing {wav_path.name}: {e}")
        import traceback
        traceback.print_exc()
        return {"success": False, "error": str(e)}


def calculate_speech_density(total_duration: float, words: List[dict]) -> float:
    """
    Calculate what percentage of audio contains speech.
    Uses word timestamps to find speech regions.

    Args:
        total_duration: Total audio duration in seconds
        words: List of word dicts with 'start' and 'end' keys

    Returns:
        Float between 0 and 1 representing speech density
    """
    if not words or total_duration <= 0:
        return 0.0

    # Merge overlapping word regions with small gaps (300ms tolerance)
    gap_tolerance = 0.3
    speech_regions = []
    current_start = words[0].get('start', 0)
    current_end = words[0].get('end', 0)

    for word in words[1:]:
        word_start = word.get('start', 0)
        word_end = word.get('end', 0)

        if word_start - current_end < gap_tolerance:
            # Extend current region
            current_end = max(current_end, word_end)
        else:
            # Save current region and start new one
            speech_regions.append((current_start, current_end))
            current_start = word_start
            current_end = word_end

    # Don't forget the last region
    speech_regions.append((current_start, current_end))

    # Calculate total speech duration
    speech_duration = sum(end - start for start, end in speech_regions)

    return min(1.0, speech_duration / total_duration)


def calculate_quality_score(sample: dict) -> float:
    """
    Calculate composite quality score for auto-selection ranking.

    Scoring criteria:
    - Duration (30%): Peak at 6-10s, decay outside
    - Speech density (40%): Higher is better
    - Sentiment (30%): Neutral is best, HEAVILY penalize negative

    Returns:
        Float between 0 and 1
    """
    duration = sample.get('duration', 0)
    speech_density = sample.get('speechDensity', 0.5)
    sentiment_score = sample.get('sentimentScore', 0)

    # Duration score: Peak at 6-10 seconds
    if 6 <= duration <= 10:
        duration_score = 1.0
    elif duration < 6:
        duration_score = duration / 6.0
    else:
        # Decay for longer samples (still useful but less ideal)
        duration_score = max(0.3, 1.0 - (duration - 10) / 20.0)

    # Speech density score: Linear (higher is better)
    density_score = speech_density

    # Sentiment score: Neutral is best, HEAVILY penalize negative
    # sentiment_score ranges from -1 (negative) to 1 (positive)
    # We want neutral (0) to score highest
    if sentiment_score < -0.3:
        # Strong negative sentiment - apply severe penalty
        neutrality_score = 0.1 + (sentiment_score + 1) * 0.2  # Range: 0.1 to 0.24
    elif sentiment_score < 0:
        # Mild negative - moderate penalty
        neutrality_score = 0.7 + sentiment_score * 0.6  # Range: 0.52 to 0.7
    else:
        # Positive or neutral - normal scoring
        neutrality_score = 1.0 - abs(sentiment_score)

    # Composite score with weights
    quality_score = (
        duration_score * 0.3 +
        density_score * 0.4 +
        neutrality_score * 0.3
    )

    return round(quality_score, 3)


def analyze_sample_parakeet(wav_path: Path, language: str = "en") -> dict:
    """
    Analyze a single WAV sample using Parakeet (local ONNX).

    Returns same structure as analyze_sample() but with estimated metrics
    (no word timestamps or sentiment from Parakeet).
    """
    try:
        from services.parakeet_stt import transcribe

        # Get duration first
        duration = get_wav_duration(wav_path)
        if duration <= 0:
            return {"success": False, "error": "Invalid audio file"}

        # Read WAV file and extract PCM
        with wave.open(str(wav_path), 'rb') as w:
            pcm_data = w.readframes(w.getnframes())
            sample_rate = w.getframerate()

        # Transcribe via Parakeet worker process
        print(f"[Analyzer] Analyzing {wav_path.name} with Parakeet (language={language})")
        result = transcribe(pcm_data, sample_rate)

        if not result.get("success"):
            return {
                "success": False,
                "error": result.get("error", "Parakeet transcription failed")
            }

        text = result.get("text", "").strip()

        if not text:
            print(f"[Analyzer] WARNING: {wav_path.name} - No speech detected by Parakeet")
            return {
                "success": True,
                "duration": duration,
                "transcript": "",
                "words": [],
                "speechDensity": 0.0,
                "sentiment": "neutral",
                "sentimentScore": 0.0,
                "error": None
            }

        # Estimate speech density from word count and typical speaking rate (~2.5 words/sec)
        word_count = len(text.split())
        estimated_speech_time = word_count / 2.5
        speech_density = min(1.0, estimated_speech_time / duration)

        print(f"[Analyzer] {wav_path.name}: SUCCESS (Parakeet) - transcript_len={len(text)}, words={word_count}")

        return {
            "success": True,
            "duration": duration,
            "transcript": text,
            "words": [],  # No word timestamps from Parakeet
            "speechDensity": speech_density,
            "sentiment": "neutral",  # No sentiment from Parakeet
            "sentimentScore": 0.0,
            "error": None
        }

    except ImportError:
        print(f"[Analyzer] ERROR: Parakeet STT not available")
        return {"success": False, "error": "Parakeet STT not available (onnx-asr not installed)"}
    except Exception as e:
        print(f"[Analyzer] ERROR analyzing {wav_path.name} with Parakeet: {e}")
        import traceback
        traceback.print_exc()
        return {"success": False, "error": str(e)}


def ensure_parakeet_ready():
    """Warm up the Parakeet worker process. Call before analysis."""
    try:
        from services.parakeet_stt import warm_up
        warm_up()
        return True
    except ImportError:
        print("[Analyzer] Parakeet STT not available (onnx-asr not installed)")
        return False
    except Exception as e:
        print(f"[Analyzer] Failed to warm up Parakeet: {e}")
        return False


def analyze(wav_path: Path, language: str = "en", analyzer: str = "deepgram") -> dict:
    """
    Dispatch to the appropriate analyzer.

    Args:
        wav_path: Path to WAV file
        language: Language code (e.g., "en", "de", "fr")
        analyzer: "deepgram" or "parakeet"

    Returns:
        Analysis result dict
    """
    if analyzer == "parakeet":
        return analyze_sample_parakeet(wav_path, language)
    else:
        return analyze_sample(wav_path, language)


def auto_select_samples(samples: List[dict], target_duration: float = 15.0) -> List[str]:
    """
    Select optimal samples for voice cloning.

    Algorithm:
    1. Score each sample with quality_score
    2. Sort by score (highest first)
    3. Greedy select until target_duration reached (10% tolerance)

    Args:
        samples: List of sample dicts with analysis data
        target_duration: Target total duration in seconds

    Returns:
        List of selected wemIds
    """
    if not samples:
        return []

    # Score samples that don't already have a quality score
    scored_samples = []
    for sample in samples:
        if 'qualityScore' not in sample or sample['qualityScore'] is None:
            sample['qualityScore'] = calculate_quality_score(sample)
        scored_samples.append(sample)

    # Sort by quality score (highest first)
    scored_samples.sort(key=lambda x: x.get('qualityScore', 0), reverse=True)

    # Greedy selection
    selected = []
    total_duration = 0.0
    max_duration = target_duration * 1.1  # 10% tolerance

    for sample in scored_samples:
        duration = sample.get('duration', 0)
        wem_id = sample.get('wemId')

        if not wem_id:
            continue

        # Skip very short samples (less than 2 seconds)
        if duration < 2.0:
            continue

        # Check if adding this sample would exceed max
        if total_duration + duration <= max_duration:
            selected.append(wem_id)
            total_duration += duration

        # Stop if we've reached target
        if total_duration >= target_duration:
            break

    return selected
