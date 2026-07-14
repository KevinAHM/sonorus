"""
Voice Manager API routes.

Provides endpoints for:
- Language and character listing
- Voice sample extraction
- Audio analysis (transcription, sentiment, speech density)
- Sample selection and auto-selection
- Manifest export
- Session persistence
"""

import os
import json
import uuid
import threading
from pathlib import Path
from typing import Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Blueprint, request, jsonify, send_file, Response

# Path setup
VOICE_MANAGER_DIR = Path(__file__).parent
SONORUS_DIR = VOICE_MANAGER_DIR.parent
DATA_DIR = SONORUS_DIR / "data"
STATIC_DIR = VOICE_MANAGER_DIR / "static"
SESSIONS_DIR = VOICE_MANAGER_DIR / "sessions"
EXTRACTED_AUDIO_DIR = SONORUS_DIR / "extracted_audio"

# Ensure directories exist
SESSIONS_DIR.mkdir(exist_ok=True)

# Language configuration
LANGUAGES = {
    "EN_US": {"path": "en-us", "name": "English", "deepgram": "en"},
    "DE_DE": {"path": "de-de", "name": "German", "deepgram": "de"},
    "ES_ES": {"path": "es-es", "name": "Spanish (Spain)", "deepgram": "es"},
    "ES_MX": {"path": "es-mx", "name": "Spanish (Latin America)", "deepgram": "es-419"},
    "FR_FR": {"path": "fr-fr", "name": "French", "deepgram": "fr"},
    "IT_IT": {"path": "it-it", "name": "Italian", "deepgram": "it"},
    "PT_BR": {"path": "pt-br", "name": "Portuguese", "deepgram": "pt-BR"},
    "JA_JP": {"path": "ja-jp", "name": "Japanese", "deepgram": "ja"},
    "PL_PL": {"path": "pl-pl", "name": "Polish", "deepgram": "pl"},
    "RU_RU": {"path": "ru-ru", "name": "Russian", "deepgram": "ru"},
    "KO_KR": {"path": "ko-kr", "name": "Korean", "deepgram": "ko"},
    "ZH_CN": {"path": "zh-cn", "name": "Chinese (Simplified)", "deepgram": "zh-CN"},
    "ZH_TW": {"path": "zh-tw", "name": "Chinese (Traditional)", "deepgram": "zh-TW"},
    "AR_AE": {"path": "ar-ae", "name": "Arabic", "deepgram": "ar"}
}

# Languages not supported by Parakeet (no CJK or Arabic coverage)
PARAKEET_UNSUPPORTED_PREFIXES = ['JA', 'KO', 'ZH', 'AR']

# Background task tracking
_active_tasks: Dict[str, dict] = {}
_task_lock = threading.Lock()

# Create blueprint
voice_manager_bp = Blueprint('voice_manager', __name__, url_prefix='/voice-manager')


# ============================================
# Page & Static Files
# ============================================

@voice_manager_bp.route('/')
def index():
    """Serve the voice builder HTML page."""
    html_path = STATIC_DIR / "voice-builder.html"
    if html_path.exists():
        return send_file(html_path)
    return "Voice Builder page not found", 404


@voice_manager_bp.route('/static/<path:filename>')
def serve_static(filename):
    """Serve static files (JS, CSS)."""
    file_path = STATIC_DIR / filename
    if file_path.exists():
        mimetype = 'application/javascript' if filename.endswith('.js') else \
                   'text/css' if filename.endswith('.css') else \
                   'application/octet-stream'
        return send_file(file_path, mimetype=mimetype)
    return "File not found", 404


# ============================================
# Languages & Characters
# ============================================

@voice_manager_bp.route('/api/config-status')
def get_config_status():
    """Check if required API keys/models are configured."""
    import sys
    if str(SONORUS_DIR) not in sys.path:
        sys.path.insert(0, str(SONORUS_DIR))

    from utils.settings import load_settings

    settings = load_settings()
    stt_settings = settings.get('stt', {}).get('deepgram', {})
    deepgram_key = stt_settings.get('api_key')

    # Check Parakeet availability for the requested language
    language = request.args.get('lang', 'EN_US')
    lang_prefix = language.split('_')[0]
    parakeet_supported = lang_prefix not in PARAKEET_UNSUPPORTED_PREFIXES

    return jsonify({
        "deepgramConfigured": bool(deepgram_key),
        "parakeetSupported": parakeet_supported,
        "issues": [] if deepgram_key else ["Deepgram API key not configured - required for audio analysis"]
    })


@voice_manager_bp.route('/api/analyzer/warmup', methods=['POST'])
def warmup_analyzer():
    """Warm up the Parakeet worker process (downloads model on first use)."""
    from .analyzer import ensure_parakeet_ready

    def _warmup():
        success = ensure_parakeet_ready()
        if success:
            print("[VoiceManager] Parakeet worker ready")
        else:
            print("[VoiceManager] Parakeet worker failed to start")

    # Run in background thread so we don't block the HTTP response
    thread = threading.Thread(target=_warmup, daemon=True)
    thread.start()

    return jsonify({"status": "warming_up"})


@voice_manager_bp.route('/api/languages')
def get_languages():
    """Get available languages with manifest status."""
    result = []
    for code, info in LANGUAGES.items():
        manifest_file = DATA_DIR / f"voice_manifest_{info['path'].replace('-', '_')}.json"
        has_manifest = manifest_file.exists()

        # Check if it's the default English manifest
        if code == "EN_US":
            has_manifest = (DATA_DIR / "voice_manifest.json").exists()

        result.append({
            "code": code,
            "name": info["name"],
            "path": info["path"],
            "hasManifest": has_manifest
        })

    return jsonify(result)


@voice_manager_bp.route('/api/characters')
def get_characters():
    """Get list of significant NPCs for a language."""
    language = request.args.get('lang', 'EN_US')

    # Load base manifest to get voice list
    manifest_path = DATA_DIR / "voice_manifest.json"
    if not manifest_path.exists():
        return jsonify({"error": "Base voice manifest not found"}), 404

    with open(manifest_path, 'r', encoding='utf-8') as f:
        manifest = json.load(f)

    voices = manifest.get("voices", {})

    from utils.text_utils import voice_name_to_display_name, INSIGNIFICANT_PREFIXES

    characters = []
    lang_path = LANGUAGES.get(language, {}).get("path", "en-us")

    # Load session once, not per-character
    session_data = _load_session_for_language(language)

    for voice_name in voices.keys():
        # Filter out insignificant NPCs (generic students, enemies, etc.)
        if any(voice_name.lower().startswith(prefix) for prefix in INSIGNIFICANT_PREFIXES):
            continue
        # Check extraction status for this language
        voice_dir = EXTRACTED_AUDIO_DIR / lang_path / voice_name / "wav"
        sample_count = len(list(voice_dir.glob("*.wav"))) if voice_dir.exists() else 0

        # Check if analyzed (has metadata)
        analysis_cache = session_data.get("analysisCache", {}).get(voice_name, {})
        analyzed_count = len(analysis_cache)

        # Check selection status
        selections = session_data.get("selections", {}).get(voice_name, [])
        selected_count = len(selections)

        # Determine status
        if selected_count > 0:
            status = "complete"
        elif analyzed_count > 0:
            status = "analyzed"
        elif sample_count > 0:
            status = "extracted"
        else:
            status = "pending"

        characters.append({
            "voiceName": voice_name,
            "displayName": voice_name_to_display_name(voice_name),
            "status": status,
            "sampleCount": sample_count,
            "analyzedCount": analyzed_count,
            "selectedCount": selected_count
        })

    # Sort by display name
    characters.sort(key=lambda x: x["displayName"])

    return jsonify(characters)


# ============================================
# Extraction
# ============================================

@voice_manager_bp.route('/api/extract', methods=['POST'])
def start_extraction():
    """Start extraction for character(s) in specified language."""
    data = request.get_json() or {}
    language = data.get("language", "EN_US")
    voice_names = data.get("voiceNames", [])

    if not voice_names:
        return jsonify({"error": "No voice names provided"}), 400

    task_id = str(uuid.uuid4())

    with _task_lock:
        _active_tasks[task_id] = {
            "type": "extraction",
            "status": "running",
            "progress": 0,
            "current": None,
            "extracted": 0,
            "total": len(voice_names),
            "errors": [],
            "language": language
        }

    # Start background thread
    thread = threading.Thread(
        target=_run_extraction,
        args=(task_id, language, voice_names),
        daemon=True
    )
    thread.start()

    return jsonify({"taskId": task_id, "status": "started"})


@voice_manager_bp.route('/api/extract/status')
def get_extraction_status():
    """Check extraction progress."""
    task_id = request.args.get('taskId')

    with _task_lock:
        task = _active_tasks.get(task_id)
        if not task:
            return jsonify({"error": "Task not found"}), 404
        return jsonify(task)


def _run_extraction(task_id: str, language: str, voice_names: List[str]):
    """Background extraction worker."""
    from .extractor import extract_voice_for_language

    lang_path = LANGUAGES.get(language, {}).get("path", "en-us")
    print(f"[VoiceManager] Starting extraction for {len(voice_names)} voice(s), lang={lang_path}")

    for voice_idx, voice_name in enumerate(voice_names):
        with _task_lock:
            _active_tasks[task_id]["current"] = voice_name

        # Progress callback for WEM-level updates
        def progress_callback(current_wem, total_wems, message):
            # For single voice: use WEM progress directly
            # For multiple voices: combine voice progress + WEM progress
            if len(voice_names) == 1:
                # Single voice - show WEM progress
                progress = int((current_wem / total_wems) * 100) if total_wems > 0 else 0
            else:
                # Multiple voices - weight by voice + wem progress
                voice_progress = voice_idx / len(voice_names)
                wem_progress = (current_wem / total_wems) / len(voice_names) if total_wems > 0 else 0
                progress = int((voice_progress + wem_progress) * 100)

            with _task_lock:
                _active_tasks[task_id]["progress"] = progress
                _active_tasks[task_id]["message"] = message

        try:
            result = extract_voice_for_language(voice_name, lang_path, progress_callback)
            if result.get("success"):
                with _task_lock:
                    _active_tasks[task_id]["extracted"] += 1
            else:
                print(f"[VoiceManager] Extraction failed for {voice_name}: {result.get('error')}")
                with _task_lock:
                    _active_tasks[task_id]["errors"].append({
                        "voice": voice_name,
                        "error": result.get("error", "Unknown error")
                    })
        except Exception as e:
            print(f"[VoiceManager] Exception extracting {voice_name}: {e}")
            import traceback
            traceback.print_exc()
            with _task_lock:
                _active_tasks[task_id]["errors"].append({
                    "voice": voice_name,
                    "error": str(e)
                })

    with _task_lock:
        errors = _active_tasks[task_id]["errors"]
        extracted = _active_tasks[task_id]["extracted"]
        _active_tasks[task_id]["status"] = "complete"
        _active_tasks[task_id]["progress"] = 100
        _active_tasks[task_id]["current"] = None

    print(f"[VoiceManager] Extraction complete. Extracted: {extracted}, Errors: {len(errors)}")


# ============================================
# Samples & Analysis
# ============================================

@voice_manager_bp.route('/api/samples')
def get_samples():
    """Get samples for a character with metadata."""
    language = request.args.get('lang', 'EN_US')
    voice_name = request.args.get('voice')

    if not voice_name:
        return jsonify({"error": "Voice name required"}), 400

    lang_path = LANGUAGES.get(language, {}).get("path", "en-us")
    wav_dir = EXTRACTED_AUDIO_DIR / lang_path / voice_name / "wav"

    if not wav_dir.exists():
        return jsonify({"samples": [], "message": "No samples extracted yet"})

    # Load session for cached analysis
    session_data = _load_session_for_language(language)
    analysis_cache = session_data.get("analysisCache", {}).get(voice_name, {})
    selections = session_data.get("selections", {}).get(voice_name, [])

    samples = []
    for wav_file in sorted(wav_dir.glob("*.wav")):
        wem_id = wav_file.stem

        # Get cached analysis or basic info
        analysis = analysis_cache.get(wem_id, {})

        # Get duration if not in cache
        if "duration" not in analysis:
            import wave
            try:
                with wave.open(str(wav_file), 'rb') as w:
                    frames = w.getnframes()
                    rate = w.getframerate()
                    analysis["duration"] = frames / float(rate)
            except:
                analysis["duration"] = 0

        samples.append({
            "wemId": wem_id,
            "duration": analysis.get("duration", 0),
            "transcript": analysis.get("transcript", ""),
            "speechDensity": analysis.get("speechDensity"),
            "sentiment": analysis.get("sentiment"),
            "sentimentScore": analysis.get("sentimentScore"),
            "qualityScore": analysis.get("qualityScore"),
            "selected": wem_id in selections,
            "analyzed": bool(analysis.get("transcript"))
        })

    # Filter out samples shorter than 3 seconds
    samples = [s for s in samples if s["duration"] >= 3.0]

    # Sort by quality score (highest first), then by duration if no quality score
    samples.sort(key=lambda x: (x.get("qualityScore") or 0, x["duration"]), reverse=True)

    return jsonify({"samples": samples})


@voice_manager_bp.route('/api/analyze', methods=['POST'])
def start_analysis():
    """Analyze samples for a character (transcription + metrics)."""
    data = request.get_json() or {}
    language = data.get("language", "EN_US")
    voice_names = data.get("voiceNames", [])
    selected_wem_ids = data.get("selectedWemIds", {})  # {voiceName: [wemId1, wemId2]}
    analyzer = data.get("analyzer", "deepgram")

    if not voice_names:
        return jsonify({"error": "No voice names provided"}), 400

    # Validate Parakeet language support
    if analyzer == "parakeet":
        lang_prefix = language.split('_')[0]
        if lang_prefix in PARAKEET_UNSUPPORTED_PREFIXES:
            return jsonify({"error": f"Parakeet does not support {language}"}), 400

    task_id = str(uuid.uuid4())

    with _task_lock:
        _active_tasks[task_id] = {
            "type": "analysis",
            "status": "running",
            "progress": 0,
            "current": None,
            "analyzed": 0,
            "total": 0,  # Will be updated when we count samples
            "errors": [],
            "language": language,
            "analyzer": analyzer
        }

    thread = threading.Thread(
        target=_run_analysis,
        args=(task_id, language, voice_names, selected_wem_ids, analyzer),
        daemon=True
    )
    thread.start()

    return jsonify({"taskId": task_id, "status": "started"})


@voice_manager_bp.route('/api/analyze/status')
def get_analysis_status():
    """Check analysis progress."""
    task_id = request.args.get('taskId')

    with _task_lock:
        task = _active_tasks.get(task_id)
        if not task:
            return jsonify({"error": "Task not found"}), 404
        return jsonify(task)


def _analyze_one_sample(wav_file: Path, deepgram_lang: str, wem_id: str, voice_name: str, existing_cache: dict, analyzer: str = "deepgram"):
    """Analyze a single sample (worker function for ThreadPoolExecutor)."""
    from .analyzer import analyze, calculate_quality_score

    # Skip if already analyzed
    if wem_id in existing_cache and existing_cache[wem_id].get("transcript"):
        return {"cached": True, "wem_id": wem_id, "voice_name": voice_name}

    try:
        result = analyze(wav_file, language=deepgram_lang, analyzer=analyzer)
        if result.get("success"):
            result["qualityScore"] = calculate_quality_score(result)
            return {"success": True, "wem_id": wem_id, "voice_name": voice_name, "data": result}
        else:
            return {"success": False, "wem_id": wem_id, "voice_name": voice_name, "error": result.get("error", "Analysis failed")}
    except Exception as e:
        return {"success": False, "wem_id": wem_id, "voice_name": voice_name, "error": str(e)}


def _run_analysis(task_id: str, language: str, voice_names: List[str], selected_wem_ids: Dict[str, List[str]] = None, analyzer: str = "deepgram"):
    """Background analysis worker with parallel processing."""
    from .analyzer import analyze, calculate_quality_score

    lang_path = LANGUAGES.get(language, {}).get("path", "en-us")
    deepgram_lang = LANGUAGES.get(language, {}).get("deepgram", "en")
    selected_wem_ids = selected_wem_ids or {}

    # Warm up Parakeet worker if needed
    if analyzer == "parakeet":
        from .analyzer import ensure_parakeet_ready
        if not ensure_parakeet_ready():
            with _task_lock:
                _active_tasks[task_id]["status"] = "error"
                _active_tasks[task_id]["errors"].append({"voice": "", "error": "Failed to start Parakeet worker"})
            return

    # Count total samples and prepare job list
    total_samples = 0
    jobs = []  # List of (wav_file, wem_id, voice_name)

    for voice_name in voice_names:
        wav_dir = EXTRACTED_AUDIO_DIR / lang_path / voice_name / "wav"
        if wav_dir.exists():
            # Get selected WEM IDs for this voice (if any)
            selected_ids = selected_wem_ids.get(voice_name, [])

            wav_files = list(wav_dir.glob("*.wav"))

            # Filter by selected IDs if any exist
            if selected_ids:
                wav_files = [f for f in wav_files if f.stem in selected_ids]

            for wav_file in wav_files:
                jobs.append((wav_file, wav_file.stem, voice_name))
                total_samples += 1

    with _task_lock:
        _active_tasks[task_id]["total"] = total_samples

    if total_samples == 0:
        with _task_lock:
            _active_tasks[task_id]["status"] = "complete"
            _active_tasks[task_id]["progress"] = 100
        return

    # Load existing session
    session_data = _load_session_for_language(language)
    if "analysisCache" not in session_data:
        session_data["analysisCache"] = {}

    # Ensure all voice caches exist
    for voice_name in voice_names:
        if voice_name not in session_data["analysisCache"]:
            session_data["analysisCache"][voice_name] = {}

    # Process samples: parallel for Deepgram (API calls), serial for Parakeet (single worker process)
    max_workers = 1 if analyzer == "parakeet" else 4
    analyzed = 0
    progress_lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submit all jobs
        futures = []
        for wav_file, wem_id, voice_name in jobs:
            existing_cache = session_data["analysisCache"].get(voice_name, {})
            future = executor.submit(
                _analyze_one_sample,
                wav_file,
                deepgram_lang,
                wem_id,
                voice_name,
                existing_cache,
                analyzer
            )
            futures.append(future)

        # Process results as they complete
        for future in as_completed(futures):
            result = future.result()

            # Thread-safe updates
            with progress_lock:
                voice_name = result["voice_name"]
                wem_id = result["wem_id"]

                # Update cache if successful (not cached)
                if result.get("success") and not result.get("cached"):
                    session_data["analysisCache"][voice_name][wem_id] = result["data"]
                    analyzed += 1
                elif result.get("cached"):
                    analyzed += 1

                # Track errors
                if result.get("success") is False:
                    with _task_lock:
                        _active_tasks[task_id]["errors"].append({
                            "voice": voice_name,
                            "wemId": wem_id,
                            "error": result.get("error", "Unknown error")
                        })

                # Update progress
                with _task_lock:
                    _active_tasks[task_id]["analyzed"] = analyzed
                    _active_tasks[task_id]["progress"] = int((analyzed / total_samples) * 100)
                    _active_tasks[task_id]["current"] = voice_name

    # Save session with analysis cache
    _save_session_for_language(language, session_data)

    with _task_lock:
        _active_tasks[task_id]["status"] = "complete"
        _active_tasks[task_id]["progress"] = 100
        _active_tasks[task_id]["current"] = None


def _check_targets_met(selected_samples: List[dict], strict_mode: bool = True) -> dict:
    """
    Check if 10s and 15s targets are met.

    Args:
        selected_samples: List of selected sample dicts with duration
        strict_mode: If True, 10s target requires SINGLE sample 8-12s.
                     If False, accepts combinations as fallback.

    Returns:
        {"target_10s": bool, "target_15s": bool}
    """
    if not selected_samples:
        return {"target_10s": False, "target_15s": False}

    # Sort by duration (descending) for greedy selection
    sorted_samples = sorted(selected_samples, key=lambda x: x.get("duration", 0), reverse=True)

    # 10s target: Prefer single sample, fall back to combinations if not strict
    target_10s = False
    # First, try to find a SINGLE sample in 8-12s range
    for sample in sorted_samples:
        dur = sample.get("duration", 0)
        if 8.0 <= dur <= 12.0:
            target_10s = True
            break

    # If strict mode and no single sample found, 10s target not met
    # If lenient mode and no single sample, try combinations
    if not target_10s and not strict_mode:
        for start_idx in range(len(sorted_samples)):
            total = 0
            for i in range(start_idx, len(sorted_samples)):
                total += sorted_samples[i].get("duration", 0)
                if 8.0 <= total <= 12.0:
                    target_10s = True
                    break
                if total > 12.0:
                    break
            if target_10s:
                break

    # 15s target: Always accept combinations
    target_15s = False
    for start_idx in range(len(sorted_samples)):
        total = 0
        for i in range(start_idx, len(sorted_samples)):
            total += sorted_samples[i].get("duration", 0)
            if 13.0 <= total <= 17.0:
                target_15s = True
                break
            if total > 17.0:
                break
        if target_15s:
            break

    return {"target_10s": target_10s, "target_15s": target_15s}


def _run_auto_build(task_id: str, language: str, analyzer: str = "deepgram"):
    """
    Auto-build worker: Extract, analyze, and select samples NPC by NPC until targets met.

    Strategy:
    - Only process NPCs that don't have both 10s and 15s targets
    - Extract samples if not already extracted
    - Analyze samples between 5-15s duration only
    - Select samples with quality > 0.90
    - Stop when both targets are met for an NPC
    """
    from .analyzer import analyze, calculate_quality_score
    from utils.text_utils import voice_name_to_display_name, INSIGNIFICANT_PREFIXES
    import wave

    lang_path = LANGUAGES.get(language, {}).get("path", "en-us")
    deepgram_lang = LANGUAGES.get(language, {}).get("deepgram", "en")

    # Warm up Parakeet worker if needed
    if analyzer == "parakeet":
        from .analyzer import ensure_parakeet_ready
        if not ensure_parakeet_ready():
            with _task_lock:
                _active_tasks[task_id]["status"] = "error"
                _active_tasks[task_id]["errors"].append({"voice": "", "error": "Failed to start Parakeet worker"})
            return

    # Load base manifest to get voice list
    manifest_path = DATA_DIR / "voice_manifest.json"
    if not manifest_path.exists():
        with _task_lock:
            _active_tasks[task_id]["status"] = "error"
            _active_tasks[task_id]["error"] = "Base manifest not found"
        return

    with open(manifest_path, 'r', encoding='utf-8') as f:
        manifest = json.load(f)

    voices = manifest.get("voices", {})

    # Filter to significant NPCs
    voice_names = [
        v for v in voices.keys()
        if not any(v.lower().startswith(prefix) for prefix in INSIGNIFICANT_PREFIXES)
    ]

    # Load session
    session_data = _load_session_for_language(language)
    if "analysisCache" not in session_data:
        session_data["analysisCache"] = {}
    if "selections" not in session_data:
        session_data["selections"] = {}

    # Filter to NPCs that need work
    npcs_to_process = []
    for voice_name in voice_names:
        # Get current selections
        selected_ids = session_data["selections"].get(voice_name, [])
        analysis_cache = session_data["analysisCache"].get(voice_name, {})

        # Build selected samples list
        selected_samples = []
        wav_dir = EXTRACTED_AUDIO_DIR / lang_path / voice_name / "wav"
        for wem_id in selected_ids:
            if wem_id in analysis_cache:
                selected_samples.append({"wemId": wem_id, **analysis_cache[wem_id]})
            else:
                # Sample not in cache - load duration from file if it exists
                wav_file = wav_dir / f"{wem_id}.wav"
                if wav_file.exists():
                    import wave
                    try:
                        with wave.open(str(wav_file), 'rb') as w:
                            frames = w.getnframes()
                            rate = w.getframerate()
                            duration = frames / float(rate)
                        selected_samples.append({"wemId": wem_id, "duration": duration})
                    except:
                        pass  # Skip if can't read file

        # Check if targets already met (use lenient mode for existing NPCs)
        targets = _check_targets_met(selected_samples, strict_mode=False)
        if not (targets["target_10s"] and targets["target_15s"]):
            npcs_to_process.append(voice_name)

    with _task_lock:
        _active_tasks[task_id]["total"] = len(npcs_to_process)

    if len(npcs_to_process) == 0:
        with _task_lock:
            _active_tasks[task_id]["status"] = "complete"
            _active_tasks[task_id]["progress"] = 100
        return

    print(f"[AutoBuild] Processing {len(npcs_to_process)} NPCs for {language}")

    completed = 0
    for voice_name in npcs_to_process:
        # Check if cancelled
        with _task_lock:
            if _active_tasks[task_id].get("cancelled"):
                print(f"[AutoBuild] Cancelled by user")
                return

        display_name = voice_name_to_display_name(voice_name)
        with _task_lock:
            _active_tasks[task_id]["current"] = display_name
            _active_tasks[task_id]["stage"] = "Initializing"

        # Initialize cache for this voice
        if voice_name not in session_data["analysisCache"]:
            session_data["analysisCache"][voice_name] = {}
        if voice_name not in session_data["selections"]:
            session_data["selections"][voice_name] = []

        analysis_cache = session_data["analysisCache"][voice_name]
        selected_ids = session_data["selections"][voice_name]
        targets_met = False

        # FIRST: Try to select from already-analyzed samples before analyzing new ones
        if analysis_cache and not selected_ids:
            print(f"[AutoBuild] {voice_name}: Found {len(analysis_cache)} pre-analyzed samples, selecting best...")
            with _task_lock:
                _active_tasks[task_id]["stage"] = "Selecting from analyzed samples"

            # Sort by quality and select best samples that meet targets
            analyzed_samples = [{"wemId": wid, **data} for wid, data in analysis_cache.items()
                               if data.get("qualityScore") and data.get("duration", 0) >= 3.0]
            analyzed_samples.sort(key=lambda x: x.get("qualityScore", 0), reverse=True)

            for sample in analyzed_samples:
                if sample["qualityScore"] > 0.90:
                    selected_ids.append(sample["wemId"])
                    print(f"[AutoBuild] {voice_name}: Pre-selected {sample['wemId']} (quality={sample['qualityScore']:.3f})")

                    # Check targets after each add
                    selected_samples = [{"wemId": sid, **analysis_cache[sid]} for sid in selected_ids if sid in analysis_cache]
                    targets = _check_targets_met(selected_samples)

                    if targets["target_10s"] and targets["target_15s"]:
                        print(f"[AutoBuild] {voice_name}: Targets met from pre-analyzed with {len(selected_ids)} samples!")
                        targets_met = True
                        break

            if targets_met:
                # Save and skip to next NPC
                _save_session_for_language(language, session_data)
                completed += 1
                with _task_lock:
                    _active_tasks[task_id]["completed"] = completed
                    _active_tasks[task_id]["progress"] = int((completed / len(npcs_to_process)) * 100)
                    _active_tasks[task_id]["stage"] = f"Targets met ✓ ({len(selected_ids)} samples)"
                continue

        # Incremental extraction with early stopping
        from .extractor import extract_voice_batch

        wav_dir = EXTRACTED_AUDIO_DIR / lang_path / voice_name / "wav"
        wav_dir.mkdir(parents=True, exist_ok=True)

        BATCH_SIZE = 200
        MAX_BATCHES = 3
        offset = len(list(wav_dir.glob("*.wav"))) if wav_dir.exists() else 0

        print(f"[AutoBuild] {voice_name}: Starting with {offset} existing samples")

        # Extract and analyze in batches until targets met
        for batch_num in range(MAX_BATCHES):
            extracted_this_batch = False

            # Extract batch if needed
            if batch_num > 0 or offset == 0:
                with _task_lock:
                    _active_tasks[task_id]["stage"] = f"Extracting batch {batch_num + 1}/{MAX_BATCHES}"
                print(f"[AutoBuild] {voice_name}: Extracting batch {batch_num + 1}/{MAX_BATCHES} (offset={offset})...")
                try:
                    result = extract_voice_batch(voice_name, lang_path, offset, BATCH_SIZE, progress_callback=None)
                    if not result.get("success"):
                        print(f"[AutoBuild] {voice_name}: Batch extraction failed - {result.get('error')}")
                        break
                    print(f"[AutoBuild] {voice_name}: Batch {batch_num + 1} extracted {result.get('sample_count', 0)} samples")
                    extracted_this_batch = True

                    # No more samples available
                    if not result.get("has_more") and result.get("sample_count", 0) == 0:
                        print(f"[AutoBuild] {voice_name}: No more samples available")
                        break
                except Exception as e:
                    print(f"[AutoBuild] {voice_name}: Batch extraction error - {e}")
                    break

            # Collect unanalyzed samples in 5-15s range
            samples_to_analyze = []
            for wav_file in wav_dir.glob("*.wav"):
                wem_id = wav_file.stem

                # Skip if already analyzed
                if wem_id in analysis_cache and analysis_cache[wem_id].get("transcript"):
                    continue

                # Get duration
                duration = 0
                try:
                    with wave.open(str(wav_file), 'rb') as w:
                        frames = w.getnframes()
                        rate = w.getframerate()
                        duration = frames / float(rate)
                except:
                    continue

                # Only process 5-15s samples
                if 5.0 <= duration <= 15.0:
                    samples_to_analyze.append((wav_file, wem_id, duration))

            if not samples_to_analyze:
                print(f"[AutoBuild] {voice_name}: No new samples to analyze in batch {batch_num + 1}")
                with _task_lock:
                    _active_tasks[task_id]["stage"] = "Checking existing selections"
                # Check if targets already met with existing selections
                selected_samples = []
                for sid in selected_ids:
                    if sid in analysis_cache:
                        selected_samples.append({"wemId": sid, **analysis_cache[sid]})
                # During extraction, use strict mode (single sample for 10s)
                targets = _check_targets_met(selected_samples, strict_mode=True)
                if targets["target_10s"] and targets["target_15s"]:
                    targets_met = True
                    with _task_lock:
                        _active_tasks[task_id]["stage"] = f"Targets met ✓ ({len(selected_ids)} samples)"
                    break
                # Try next batch if available
                offset += BATCH_SIZE
                continue

            print(f"[AutoBuild] {voice_name}: Analyzing {len(samples_to_analyze)} samples from batch {batch_num + 1}")

            with _task_lock:
                _active_tasks[task_id]["stage"] = f"Analyzing {len(samples_to_analyze)} samples"

            # Analyze in small micro-batches and check targets after each
            # Parakeet uses single worker process, so 1 concurrent; Deepgram can do 4
            MICRO_BATCH_SIZE = 1 if analyzer == "parakeet" else 4
            micro_workers = 1 if analyzer == "parakeet" else 4
            analyzed_in_batch = 0

            for micro_start in range(0, len(samples_to_analyze), MICRO_BATCH_SIZE):
                # Check if targets already met before starting micro-batch
                if targets_met:
                    break

                micro_batch = samples_to_analyze[micro_start:micro_start + MICRO_BATCH_SIZE]

                with ThreadPoolExecutor(max_workers=micro_workers) as executor:
                    futures = {}
                    for wav_file, wem_id, duration in micro_batch:
                        future = executor.submit(analyze, wav_file, deepgram_lang, analyzer)
                        futures[future] = (wem_id, duration)

                    # Process this micro-batch
                    for future in as_completed(futures):
                        if targets_met:
                            break

                        wem_id, duration = futures[future]

                        try:
                            result = future.result()
                            analyzed_in_batch += 1

                            if result.get("success"):
                                result["qualityScore"] = calculate_quality_score(result)
                                analysis_cache[wem_id] = result

                                # Only add to selection if quality > 0.90 AND targets not yet met
                                if result["qualityScore"] > 0.90 and not targets_met:
                                    if wem_id not in selected_ids:
                                        selected_ids.append(wem_id)
                                        print(f"[AutoBuild] {voice_name}: Added {wem_id} (quality={result['qualityScore']:.3f})")

                                    # Check if targets met AFTER adding
                                    selected_samples = [{"wemId": sid, **analysis_cache[sid]} for sid in selected_ids if sid in analysis_cache]
                                    targets = _check_targets_met(selected_samples)

                                    if targets["target_10s"] and targets["target_15s"]:
                                        print(f"[AutoBuild] {voice_name}: Both targets met with {len(selected_ids)} samples! Stopping.")
                                        targets_met = True
                                        break

                        except Exception as e:
                            print(f"[AutoBuild] Error analyzing {wem_id}: {e}")

                # Update progress after each micro-batch
                with _task_lock:
                    _active_tasks[task_id]["stage"] = f"Analyzed {analyzed_in_batch}/{len(samples_to_analyze)}"

            # Check if targets met after this batch
            if targets_met:
                print(f"[AutoBuild] {voice_name}: Targets met after batch {batch_num + 1}")
                with _task_lock:
                    _active_tasks[task_id]["stage"] = f"Targets met ✓ ({len(selected_ids)} samples)"
                break

            # Prepare for next batch - only increment offset if we extracted
            if extracted_this_batch:
                offset += BATCH_SIZE

        # Desperate pass: If targets not met, analyze remaining samples > 3s but ONLY select what's needed
        if not targets_met:
            print(f"[AutoBuild] {voice_name}: Targets not met after batches. Running desperate pass...")
            with _task_lock:
                _active_tasks[task_id]["stage"] = "Desperate pass (analyzing > 3s)"

            # Collect unanalyzed samples > 3s (not just 5-15s)
            desperate_samples = []
            for wav_file in wav_dir.glob("*.wav"):
                wem_id = wav_file.stem

                # Skip if already analyzed
                if wem_id in analysis_cache and analysis_cache[wem_id].get("transcript"):
                    continue

                # Get duration
                duration = 0
                try:
                    with wave.open(str(wav_file), 'rb') as w:
                        frames = w.getnframes()
                        rate = w.getframerate()
                        duration = frames / float(rate)
                except:
                    continue

                # Accept anything > 3s
                if duration > 3.0:
                    desperate_samples.append((wav_file, wem_id, duration))

            if desperate_samples:
                print(f"[AutoBuild] {voice_name}: Desperate pass analyzing up to {len(desperate_samples)} samples")

                # Analyze in micro-batches, stopping when targets met
                MICRO_BATCH_SIZE = 1 if analyzer == "parakeet" else 4
                micro_workers = 1 if analyzer == "parakeet" else 4
                for micro_start in range(0, len(desperate_samples), MICRO_BATCH_SIZE):
                    if targets_met:
                        break

                    micro_batch = desperate_samples[micro_start:micro_start + MICRO_BATCH_SIZE]

                    with ThreadPoolExecutor(max_workers=micro_workers) as executor:
                        futures = {executor.submit(analyze, wf, deepgram_lang, analyzer): (wid, dur)
                                   for wf, wid, dur in micro_batch}

                        for future in as_completed(futures):
                            if targets_met:
                                break

                            wem_id, duration = futures[future]
                            try:
                                result = future.result()
                                if result.get("success"):
                                    result["qualityScore"] = calculate_quality_score(result)
                                    analysis_cache[wem_id] = result

                                    # In desperate pass, lower threshold to 0.70 but still be selective
                                    if result["qualityScore"] > 0.70 and wem_id not in selected_ids:
                                        selected_ids.append(wem_id)
                                        print(f"[AutoBuild] {voice_name}: Desperate add {wem_id} (quality={result['qualityScore']:.3f})")

                                        # Check targets after each add
                                        selected_samples = [{"wemId": sid, **analysis_cache[sid]} for sid in selected_ids if sid in analysis_cache]
                                        targets = _check_targets_met(selected_samples, strict_mode=False)

                                        if targets["target_10s"] and targets["target_15s"]:
                                            print(f"[AutoBuild] {voice_name}: Targets met in desperate pass with {len(selected_ids)} samples!")
                                            targets_met = True
                                            break

                            except Exception as e:
                                print(f"[AutoBuild] Error in desperate pass for {wem_id}: {e}")

                with _task_lock:
                    _active_tasks[task_id]["stage"] = f"Desperate pass done ({len(selected_ids)} samples)"
            else:
                print(f"[AutoBuild] {voice_name}: No samples available for desperate pass")

        # Save progress
        analyzed_count = len(session_data["analysisCache"].get(voice_name, {}))
        selected_count = len(session_data["selections"].get(voice_name, []))
        print(f"[AutoBuild] {voice_name}: Saving session - {analyzed_count} analyzed, {selected_count} selected")
        _save_session_for_language(language, session_data)

        completed += 1
        with _task_lock:
            _active_tasks[task_id]["completed"] = completed
            _active_tasks[task_id]["progress"] = int((completed / len(npcs_to_process)) * 100)

    with _task_lock:
        _active_tasks[task_id]["status"] = "complete"
        _active_tasks[task_id]["progress"] = 100
        _active_tasks[task_id]["current"] = None

    print(f"[AutoBuild] Complete! Processed {completed} NPCs")


# ============================================
# Audio Serving
# ============================================

@voice_manager_bp.route('/audio/<lang>/<voice>/<wem_id>.wav')
def serve_audio(lang, voice, wem_id):
    """Serve individual WAV sample for playback."""
    lang_path = None
    for code, info in LANGUAGES.items():
        if info["path"] == lang or code == lang:
            lang_path = info["path"]
            break

    if not lang_path:
        lang_path = lang

    wav_path = EXTRACTED_AUDIO_DIR / lang_path / voice / "wav" / f"{wem_id}.wav"
    if wav_path.exists():
        return send_file(wav_path, mimetype='audio/wav')
    return "Audio not found", 404


@voice_manager_bp.route('/audio/preview/<lang>/<voice>.wav')
def serve_preview(lang, voice):
    """Serve concatenated preview audio of selected samples."""
    # TODO: Implement preview generation
    return "Preview not implemented yet", 501


# ============================================
# Selection & Export
# ============================================

@voice_manager_bp.route('/api/select', methods=['POST'])
def update_selection():
    """Update sample selections for a character."""
    data = request.get_json() or {}
    language = data.get("language", "EN_US")
    voice_name = data.get("voiceName")
    selected_ids = data.get("selectedWemIds", [])

    if not voice_name:
        return jsonify({"error": "Voice name required"}), 400

    session_data = _load_session_for_language(language)
    if "selections" not in session_data:
        session_data["selections"] = {}

    session_data["selections"][voice_name] = selected_ids
    _save_session_for_language(language, session_data)

    return jsonify({"success": True})


@voice_manager_bp.route('/api/auto-select', methods=['POST'])
def auto_select():
    """Run auto-selection algorithm for character(s)."""
    data = request.get_json() or {}
    language = data.get("language", "EN_US")
    voice_names = data.get("voiceNames", [])
    target_duration = data.get("targetDuration", 60.0)

    if not voice_names:
        return jsonify({"error": "No voice names provided"}), 400

    from .analyzer import auto_select_samples

    session_data = _load_session_for_language(language)
    if "selections" not in session_data:
        session_data["selections"] = {}

    results = {}
    for voice_name in voice_names:
        analysis_cache = session_data.get("analysisCache", {}).get(voice_name, {})

        # Build sample list with analysis data
        samples = []
        for wem_id, analysis in analysis_cache.items():
            samples.append({
                "wemId": wem_id,
                **analysis
            })

        if samples:
            selected = auto_select_samples(samples, target_duration)
            session_data["selections"][voice_name] = selected
            results[voice_name] = {
                "selectedIds": selected,
                "count": len(selected)
            }
        else:
            results[voice_name] = {
                "selectedIds": [],
                "count": 0,
                "error": "No analyzed samples"
            }

    _save_session_for_language(language, session_data)

    return jsonify({"results": results})


@voice_manager_bp.route('/api/auto-build-selections', methods=['POST'])
def auto_build_selections():
    """
    Automatic mode: Build selections NPC by NPC.

    For each NPC that doesn't have 10s and 15s targets met:
    - Analyze samples between 5-15s duration
    - Select samples with quality > 0.90
    - Stop when both targets are met
    """
    data = request.get_json() or {}
    language = data.get("language", "EN_US")
    analyzer = data.get("analyzer", "deepgram")

    # Validate Parakeet language support
    if analyzer == "parakeet":
        lang_prefix = language.split('_')[0]
        if lang_prefix in PARAKEET_UNSUPPORTED_PREFIXES:
            return jsonify({"error": f"Parakeet does not support {language}"}), 400

    task_id = str(uuid.uuid4())

    with _task_lock:
        _active_tasks[task_id] = {
            "type": "auto_build",
            "status": "running",
            "progress": 0,
            "current": None,
            "completed": 0,
            "total": 0,
            "errors": [],
            "language": language,
            "analyzer": analyzer,
            "cancelled": False
        }

    thread = threading.Thread(
        target=_run_auto_build,
        args=(task_id, language, analyzer),
        daemon=True
    )
    thread.start()

    return jsonify({"taskId": task_id, "status": "started"})


@voice_manager_bp.route('/api/auto-build-selections/status')
def get_auto_build_status():
    """Check auto-build progress."""
    task_id = request.args.get('taskId')

    with _task_lock:
        task = _active_tasks.get(task_id)
        if not task:
            return jsonify({"error": "Task not found"}), 404
        return jsonify(task)


@voice_manager_bp.route('/api/auto-build-selections/cancel', methods=['POST'])
def cancel_auto_build():
    """Cancel a running auto-build task."""
    data = request.get_json() or {}
    task_id = data.get('taskId')

    if not task_id:
        return jsonify({"error": "Task ID required"}), 400

    with _task_lock:
        task = _active_tasks.get(task_id)
        if not task:
            return jsonify({"error": "Task not found"}), 404
        task["cancelled"] = True
        task["status"] = "cancelled"

    return jsonify({"success": True})


@voice_manager_bp.route('/api/export', methods=['POST'])
def export_manifest():
    """Export manifest for a language."""
    data = request.get_json() or {}
    language = data.get("language", "EN_US")
    voice_names = data.get("voiceNames")  # None = all

    lang_info = LANGUAGES.get(language, {"path": "en-us", "name": "Unknown"})
    lang_path = lang_info["path"]

    session_data = _load_session_for_language(language)
    selections = session_data.get("selections", {})

    if voice_names:
        selections = {k: v for k, v in selections.items() if k in voice_names}

    if not selections:
        return jsonify({"error": "No selections to export"}), 400

    # Build manifest
    manifest = {
        "target_durations": [10.0, 15.0, 60.0],
        "language": language,
        "audio_path": lang_path,
        "voices": {}
    }

    # Get analysis cache for transcripts
    # analysis_cache = session_data.get("analysisCache", {})

    for voice_name, selected_ids in selections.items():
        if not selected_ids:
            continue

        wem_paths = {}
        # wem_transcripts = {}
        # voice_analysis = analysis_cache.get(voice_name, {})

        for wem_id in selected_ids:
            wem_paths[wem_id] = f"Phoenix/Content/WwiseAudio/windows/{lang_path}/{wem_id}.wem"
            # # Include transcript if available from analysis
            # sample_analysis = voice_analysis.get(wem_id, {})
            # transcript = sample_analysis.get("transcript", "")
            # if transcript:
            #     wem_transcripts[wem_id] = transcript

        manifest["voices"][voice_name] = {
            "selected_wem_ids": selected_ids,
            "wem_paths": wem_paths,
            # "wem_transcripts": wem_transcripts
        }

    # Save manifest
    manifest_filename = f"voice_manifest_{lang_path.replace('-', '_')}.json"
    manifest_path = DATA_DIR / manifest_filename

    with open(manifest_path, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, indent=2)

    return jsonify({
        "success": True,
        "path": str(manifest_path),
        "filename": manifest_filename,
        "voiceCount": len(manifest["voices"])
    })


# ============================================
# Sessions
# ============================================

def _get_session_path(language: str) -> Path:
    """Get session file path for a language."""
    lang_path = LANGUAGES.get(language, {}).get("path", "unknown")
    return SESSIONS_DIR / f"session_{lang_path}.json"


def _load_session_for_language(language: str) -> dict:
    """Load session data for a language."""
    session_path = _get_session_path(language)
    if session_path.exists():
        try:
            with open(session_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            pass
    return {"language": language, "selections": {}, "analysisCache": {}}


def _save_session_for_language(language: str, data: dict):
    """Save session data for a language."""
    import datetime
    data["lastUpdated"] = datetime.datetime.now().isoformat()
    data["language"] = language

    session_path = _get_session_path(language)
    with open(session_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)


@voice_manager_bp.route('/api/sessions')
def list_sessions():
    """List available sessions."""
    sessions = []
    for session_file in SESSIONS_DIR.glob("session_*.json"):
        try:
            with open(session_file, 'r', encoding='utf-8') as f:
                data = json.load(f)

            sessions.append({
                "id": session_file.stem,
                "language": data.get("language", "Unknown"),
                "lastUpdated": data.get("lastUpdated"),
                "voiceCount": len(data.get("selections", {})),
                "analyzedCount": len(data.get("analysisCache", {}))
            })
        except:
            pass

    return jsonify(sessions)


@voice_manager_bp.route('/api/session/save', methods=['POST'])
def save_session():
    """Save current session state."""
    data = request.get_json() or {}
    language = data.get("language", "EN_US")

    session_data = _load_session_for_language(language)

    # Merge incoming data
    if "selections" in data:
        session_data["selections"] = data["selections"]
    if "notes" in data:
        session_data["notes"] = data["notes"]

    _save_session_for_language(language, session_data)

    return jsonify({"success": True})


@voice_manager_bp.route('/api/session/load')
def load_session():
    """Load session state."""
    language = request.args.get('lang', 'EN_US')
    session_data = _load_session_for_language(language)
    return jsonify(session_data)
