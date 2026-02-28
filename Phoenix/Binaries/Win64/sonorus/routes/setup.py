"""
Setup API endpoints for Sonorus setup wizard.

Handles localization extraction, voice extraction, TTS/LLM testing.
"""

import os
import sys
import json
import time
import subprocess
import threading

from flask import Blueprint, request, jsonify

from utils.settings import (
    SONORUS_DIR,
    DATA_DIR,
    load_settings,
    save_settings,
)

setup_bp = Blueprint('setup', __name__)

# ============================================
# Setup State (module-level)
# ============================================
_setup_running = None  # Track which setup command is running
_setup_lock = threading.Lock()
_setup_error = None  # Store last error message
_lua_socket = None  # Set by server.py for tracking_settings sync


def set_lua_socket(lua_socket):
    """Set lua_socket reference for syncing settings to Lua after extraction."""
    global _lua_socket
    _lua_socket = lua_socket


def _run_setup_command(command, args=None):
    """Run a setup command in background thread."""
    global _setup_running, _setup_error

    _setup_error = None

    try:
        if command == "extract_localization":
            language = args.get("language", "EN_US") if args else "EN_US"
            script_path = os.path.join(SONORUS_DIR, "setup", "extract_localization.py")

            if not os.path.exists(script_path):
                raise FileNotFoundError(f"Setup script not found: {script_path}")

            print(f"[Setup] Running: extract_localization.py --both --language {language}")

            result = subprocess.run(
                [sys.executable, script_path, "--both", "--language", language],
                capture_output=True,
                text=True,
                cwd=SONORUS_DIR,
                timeout=600  # 10 minute timeout
            )

            if result.returncode != 0:
                error_msg = result.stderr or result.stdout or "Unknown error"
                # Make error human-readable
                if "repak.exe" in error_msg.lower() or "not found" in error_msg.lower():
                    error_msg = "Required tool 'repak.exe' is missing. Ensure the bin/ folder contains all required tools."
                elif "pak file" in error_msg.lower() or "pakchunk" in error_msg.lower():
                    error_msg = "Game files not found. Verify Hogwarts Legacy is installed correctly."
                elif "permission" in error_msg.lower():
                    error_msg = "Cannot write files. Try running as administrator or check folder permissions."
                raise Exception(error_msg)

            print(f"[Setup] extract_localization complete")

            # Save language to settings
            settings = load_settings()
            if 'setup' not in settings:
                settings['setup'] = {}
            settings['setup']['language'] = language
            save_settings(settings)

            # Sync tracking settings to Lua (includes new language)
            if _lua_socket:
                try:
                    _lua_socket.send_tracking_settings()
                    print(f"[Setup] Synced language '{language}' to Lua")
                except Exception as e:
                    print(f"[Setup] Warning: Failed to sync to Lua: {e}")

        elif command == "extract_voices":
            language = args.get("language", "EN_US") if args else "EN_US"
            script_path = os.path.join(SONORUS_DIR, "setup", "extract_voices.py")

            if not os.path.exists(script_path):
                raise FileNotFoundError(f"Setup script not found: {script_path}")

            # Check if language-specific voice manifest exists
            if language == "EN_US":
                manifest_path = os.path.join(DATA_DIR, "voice_manifest.json")
            else:
                lang_suffix = language.lower()
                manifest_path = os.path.join(DATA_DIR, f"voice_manifest_{lang_suffix}.json")

            if not os.path.exists(manifest_path):
                manifest_name = os.path.basename(manifest_path)
                if language == "EN_US":
                    raise FileNotFoundError(f"Voice manifest not found. Ensure {manifest_name} exists in the data folder.")
                else:
                    # For non-English, suggest using voice manager to build manifest
                    raise FileNotFoundError(
                        f"Voice manifest not found for {language}. "
                        f"You need to build the voice manifest for this language first. "
                        f"Visit the Voice Manager at http://localhost:5000/voice-manager/ to extract and build {manifest_name}."
                    )

            print(f"[Setup] Running: extract_voices.py --from-manifest --language {language}")

            result = subprocess.run(
                [sys.executable, script_path, "--from-manifest", "--language", language],
                capture_output=True,
                text=True,
                cwd=SONORUS_DIR,
                timeout=3600  # 60 minute timeout for voice extraction
            )

            if result.returncode != 0:
                error_msg = result.stderr or result.stdout or "Unknown error"
                # Make error human-readable
                if "vgmstream" in error_msg.lower():
                    error_msg = "Required tool 'vgmstream-cli.exe' is missing. Download from https://github.com/vgmstream/vgmstream/releases"
                elif "wwiser" in error_msg.lower():
                    error_msg = "Required tool 'wwiser.pyz' is missing. Ensure the bin/ folder contains all required tools."
                elif "permission" in error_msg.lower():
                    error_msg = "Cannot write files. Try running as administrator or check folder permissions."
                raise Exception(error_msg)

            print(f"[Setup] extract_voices complete")

            # Invalidate voice reference cache and re-sync significant NPCs to Lua
            try:
                from services.tts.voice_utils import invalidate_voice_reference_cache
                invalidate_voice_reference_cache()
                print("[Setup] Invalidated voice reference cache")
                if _lua_socket:
                    _lua_socket.send_significant_npcs()
                    print("[Setup] Re-synced significant NPCs to Lua")
            except Exception as e:
                print(f"[Setup] Warning: Failed to sync significant NPCs: {e}")

        else:
            raise ValueError(f"Unknown setup command: {command}")

    except subprocess.TimeoutExpired:
        _setup_error = "Operation timed out. The game files may be too large or the system is busy."
    except FileNotFoundError as e:
        _setup_error = str(e)
    except Exception as e:
        _setup_error = str(e)
    finally:
        with _setup_lock:
            _setup_running = None


def play_audio_system(audio_data, sample_rate=44100):
    """Play audio through system default device using sounddevice.

    Uses explicit OutputStream instead of sd.play() to avoid race condition
    with finished_callback on ASIO and other backends that can cause
    AttributeError: '_CallbackContext' object has no attribute 'out'
    """
    import sounddevice as sd
    import numpy as np
    import threading

    # Convert bytes to numpy array (assuming 16-bit PCM)
    audio_array = np.frombuffer(audio_data, dtype=np.int16)
    audio_float = audio_array.astype(np.float32) / 32768.0

    # Add 150ms silence at end to prevent clipping
    silence = np.zeros(int(sample_rate * 0.15), dtype=np.float32)
    audio_float = np.concatenate([audio_float, silence])

    # Use explicit OutputStream to avoid finished_callback race condition
    # that occurs with sd.play() on some backends (ASIO, WASAPI exclusive)
    done_event = threading.Event()
    position = [0]  # Use list for mutable access in callback

    def callback(outdata, frames, time_info, status):
        if status:
            print(f"[Setup] Audio playback status: {status}")

        start = position[0]
        end = start + frames

        if end <= len(audio_float):
            outdata[:, 0] = audio_float[start:end]
            position[0] = end
        else:
            # Reached end of audio
            remaining = len(audio_float) - start
            if remaining > 0:
                outdata[:remaining, 0] = audio_float[start:]
                outdata[remaining:, 0] = 0
            else:
                outdata[:, 0] = 0
            raise sd.CallbackStop()

    try:
        stream = sd.OutputStream(
            samplerate=sample_rate,
            channels=1,
            dtype='float32',
            callback=callback,
            finished_callback=done_event.set
        )
        with stream:
            done_event.wait()
    except Exception as e:
        # Fallback: try blocking write if callback mode fails
        print(f"[Setup] Callback playback failed ({e}), trying blocking mode")
        try:
            sd.play(audio_float, sample_rate)
            sd.wait()
        except Exception:
            pass  # Silently ignore if all playback fails


# ============================================
# Setup Status (single source of truth)
# ============================================

def _get_localization_paths(language):
    """Get localization file paths for the given language.

    Returns (main_loc_path, subtitles_path) with language suffix for non-English.
    """
    if language == "EN_US":
        return (
            os.path.join(DATA_DIR, "main_localization.json"),
            os.path.join(DATA_DIR, "subtitles.json")
        )
    else:
        suffix = f"_{language.lower()}"
        return (
            os.path.join(DATA_DIR, f"main_localization{suffix}.json"),
            os.path.join(DATA_DIR, f"subtitles{suffix}.json")
        )


def _get_voice_paths(language):
    """Get voice manifest and references directory for the given language."""
    if language == "EN_US":
        return (
            os.path.join(DATA_DIR, "voice_manifest.json"),
            os.path.join(SONORUS_DIR, "voice_references")
        )
    else:
        lang_suffix = language.lower()
        return (
            os.path.join(DATA_DIR, f"voice_manifest_{lang_suffix}.json"),
            os.path.join(SONORUS_DIR, "voice_references", lang_suffix)
        )


def _compute_setup_status(language=None, include_progress=False):
    """
    Compute setup status for a language. Single source of truth for all setup checks.

    Args:
        language: Language to check (defaults to saved language from settings)
        include_progress: If True, include detailed progress info (voices_extracted count)

    Returns dict with:
        - complete: bool - overall setup completion
        - language: str - language being checked
        - main_loc: bool - main_localization.json exists
        - subtitles: bool - subtitles.json exists
        - voices_complete: bool - all voice references extracted
        - voices_total: int - total voices in manifest
        - voices_referenced: int - voices with reference files
        - voices_extracted: int - (only if include_progress) voices with extracted WAV files
        - tts_tested: bool - TTS test passed (raw setting)
        - tts_valid: bool - TTS test valid for this language
        - tts_test_language: str|None - language TTS was tested for
        - llm_tested: bool - LLM test passed
        - missing: list[str] - human-readable list of incomplete items
    """
    settings = load_settings()
    if language is None:
        language = settings.get('setup', {}).get('language', 'EN_US')

    # Localization files
    main_loc_path, subtitles_path = _get_localization_paths(language)
    main_loc = os.path.exists(main_loc_path)
    subtitles = os.path.exists(subtitles_path)

    # Voice extraction
    manifest_path, voice_refs_dir = _get_voice_paths(language)
    extracted_audio_dir = os.path.join(SONORUS_DIR, "extracted_audio")

    voices_total = 0
    voices_extracted = 0
    voices_referenced = 0

    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, 'r', encoding='utf-8') as f:
                manifest = json.load(f)
            voices_total = len(manifest.get("voices", {}))

            for voice_name in manifest.get("voices", {}).keys():
                # Check for final reference file
                ref_file = os.path.join(voice_refs_dir, f"{voice_name}_reference_60s.wav")
                has_reference = os.path.exists(ref_file)
                if has_reference:
                    voices_referenced += 1

                # Check for extracted WAV files (only if progress requested)
                if include_progress:
                    voice_wav_dir = os.path.join(extracted_audio_dir, voice_name, "wav")
                    has_extracted = os.path.exists(voice_wav_dir) and any(
                        f.endswith('.wav') for f in os.listdir(voice_wav_dir)
                    )
                    if has_extracted or has_reference:
                        voices_extracted += 1
        except Exception:
            pass

    voices_complete = voices_total > 0 and voices_referenced >= voices_total

    # TTS test (language-aware)
    tts_tested = settings.get('setup', {}).get('tts_tested', False)
    tts_test_language = settings.get('setup', {}).get('tts_test_language')
    # TTS is valid if tested AND (voices complete OR tested for this specific language)
    tts_valid = tts_tested and (voices_complete or tts_test_language == language)

    # LLM test
    llm_tested = settings.get('setup', {}).get('llm_tested', False)

    # Overall completion
    complete = (main_loc and subtitles) and voices_complete and tts_valid and llm_tested

    # Build missing items list
    missing = []
    if not main_loc:
        missing.append(f"main_localization for {language}")
    if not subtitles:
        missing.append(f"subtitles for {language}")
    if not voices_complete:
        missing.append(f"voices for {language} ({voices_referenced}/{voices_total} extracted)")
    if not tts_valid:
        if tts_tested and tts_test_language != language:
            missing.append(f"TTS test (tested for {tts_test_language}, need {language})")
        else:
            missing.append("TTS test")
    if not llm_tested:
        missing.append("LLM test")

    result = {
        'complete': complete,
        'language': language,
        'main_loc': main_loc,
        'subtitles': subtitles,
        'voices_complete': voices_complete,
        'voices_total': voices_total,
        'voices_referenced': voices_referenced,
        'tts_tested': tts_tested,
        'tts_valid': tts_valid,
        'tts_test_language': tts_test_language,
        'llm_tested': llm_tested,
        'missing': missing,
    }

    if include_progress:
        result['voices_extracted'] = voices_extracted

    return result


def is_setup_complete():
    """Check if setup is complete for the configured language."""
    status = _compute_setup_status()
    if not status['complete'] and status['missing']:
        print(f"[Setup] Incomplete: {', '.join(status['missing'])}")
    return status['complete']


def check_game_settings_warnings():
    """Check game settings for issues. Returns list of warning strings."""
    warnings = []
    try:
        ini_path = os.path.join(
            os.environ.get("LOCALAPPDATA", ""),
            "Hogwarts Legacy", "Saved", "Config",
            "WindowsNoEditor", "GameUserSettings.ini"
        )
        if os.path.isfile(ini_path):
            with open(ini_path, "r", encoding="utf-8") as f:
                for line in f:
                    stripped = line.strip()
                    if stripped.lower().startswith("subtitlesenabled="):
                        value = stripped.split("=", 1)[1].strip().lower()
                        if value == "false":
                            warnings.append(
                                "Subtitles are currently DISABLED in Hogwarts Legacy. "
                                "Sonorus requires subtitles to be ON to function correctly. "
                                "Please enable subtitles in the game's Audio settings."
                            )
                        break
    except Exception:
        pass  # Don't break setup if we can't read game settings
    return warnings


@setup_bp.route('/api/setup/status', methods=['GET'])
def get_setup_status():
    """Check setup completion status."""
    global _setup_running, _setup_error

    # Get saved and selected language
    settings = load_settings()
    saved_language = settings.get('setup', {}).get('language', 'EN_US')
    selected_language = request.args.get('language', saved_language)

    # Get computed status from single source of truth
    status = _compute_setup_status(language=selected_language, include_progress=True)

    # Determine UI status strings based on computed values and running state
    # Localization status
    if status['main_loc'] and status['subtitles']:
        loc_status = "complete"
    elif selected_language != saved_language:
        # Check if old language files exist (language mismatch)
        old_main_path, old_sub_path = _get_localization_paths(saved_language)
        if os.path.exists(old_main_path) and os.path.exists(old_sub_path):
            loc_status = "language_mismatch"
        else:
            loc_status = "not_started"
    else:
        loc_status = "not_started"

    if _setup_running == "extract_localization":
        loc_status = "running"
    elif _setup_error and loc_status == "not_started":
        loc_status = "error"

    # Voices status
    if status['voices_complete']:
        voices_status = "complete"
    elif status['voices_referenced'] > 0 or status.get('voices_extracted', 0) > 0:
        voices_status = "partial"
    else:
        voices_status = "not_started"

    if _setup_running == "extract_voices":
        voices_status = "running"
    elif _setup_error and not status['voices_complete']:
        voices_status = "error"

    # TTS status
    if status['tts_valid']:
        tts_status = "complete"
    elif status['tts_tested'] and status['tts_test_language'] and status['tts_test_language'] != selected_language:
        tts_status = "language_mismatch"
    else:
        tts_status = "not_started"
    if _setup_running == "test_tts":
        tts_status = "running"

    # LLM status
    llm_status = "complete" if status['llm_tested'] else "not_started"
    if _setup_running == "test_llm":
        llm_status = "running"

    # Get configured models for display (all unique models that will be tested)
    conv_settings = settings.get('conversation', {})
    vision_config = settings.get('agents', {}).get('vision', {})
    vision_settings = vision_config.get('llm', {})
    memory_settings = settings.get('memory', {})
    models = {
        'chat': conv_settings.get('chat_model', 'google/gemini-3-flash-preview:nitro'),
        'target': conv_settings.get('target_selection_model', 'meta-llama/llama-4-scout:nitro'),
        'interject': conv_settings.get('interjection_model', 'x-ai/grok-4.1-fast')
    }
    if vision_config.get('enabled', True):
        models['vision'] = vision_settings.get('model', 'google/gemini-2.5-flash-lite:nitro')
    # Include input correction model if enabled
    if conv_settings.get('input_correction_enabled') and conv_settings.get('input_correction_model'):
        models['input_correction'] = conv_settings['input_correction_model']
    # Include memory models if memory is enabled
    if memory_settings.get('enabled'):
        for key, setting_key in [('chapter', 'chapter_model'), ('prose', 'prose_model'),
                                  ('graphiti', 'graphiti_model'), ('graphiti_small', 'graphiti_small_model'),
                                  ('reranker', 'reranker_model')]:
            model_id = memory_settings.get(setting_key)
            if model_id:
                models[key] = model_id

    # Check for game settings warnings
    warnings = check_game_settings_warnings()

    return jsonify({
        "complete": status['complete'],
        "language": saved_language,
        "selected_language": selected_language,
        "warnings": warnings,
        "steps": {
            "localization": {
                "status": loc_status,
                "files": {
                    "main_localization": status['main_loc'],
                    "subtitles": status['subtitles']
                }
            },
            "voices": {
                "status": voices_status,
                "total": status['voices_total'],
                "extracted": status.get('voices_extracted', 0),
                "referenced": status['voices_referenced']
            },
            "tts": {
                "status": tts_status,
                "tested": status['tts_tested']
            },
            "llm": {
                "status": llm_status,
                "tested": status['llm_tested'],
                "models": models
            }
        },
        "running_command": _setup_running,
        "last_error": _setup_error
    })


@setup_bp.route('/api/setup/extract-localization', methods=['POST'])
def setup_extract_localization():
    """Start localization extraction."""
    global _setup_running, _setup_error

    with _setup_lock:
        if _setup_running:
            return jsonify({"error": f"Setup already running: {_setup_running}"}), 400
        _setup_running = "extract_localization"
        _setup_error = None

    data = request.get_json() or {}
    language = data.get("language", "EN_US")

    # Start extraction in background thread
    thread = threading.Thread(
        target=_run_setup_command,
        args=("extract_localization", {"language": language}),
        daemon=True
    )
    thread.start()

    return jsonify({
        "status": "started",
        "message": "Extracting localization files..."
    })


@setup_bp.route('/api/setup/extract-voices', methods=['POST'])
def setup_extract_voices():
    """Start voice reference extraction."""
    global _setup_running, _setup_error

    with _setup_lock:
        if _setup_running:
            return jsonify({"error": f"Setup already running: {_setup_running}"}), 400
        _setup_running = "extract_voices"
        _setup_error = None

    data = request.get_json() or {}
    language = data.get("language", "EN_US")

    # Start extraction in background thread
    thread = threading.Thread(
        target=_run_setup_command,
        args=("extract_voices", {"language": language}),
        daemon=True
    )
    thread.start()

    return jsonify({
        "status": "started",
        "message": "Extracting voice references... This may take several minutes."
    })


@setup_bp.route('/api/setup/test-tts', methods=['POST'])
def setup_test_tts():
    """Test TTS by generating and playing audio through system speakers."""
    global _setup_running, _setup_error

    data = request.get_json() or {}
    text = data.get('text', 'Hello, this is a test of the voice synthesis system.')

    # Get player voice (settings > fallback to PlayerMale)
    settings = load_settings()
    conv_settings = settings.get('conversation', {})
    player_voice = conv_settings.get('player_voice_name', '') or 'PlayerMale'
    tts_settings = settings.get('tts', {})
    provider = tts_settings.get('provider', 'inworld')

    # Set running state
    with _setup_lock:
        if _setup_running:
            return jsonify({
                'success': False,
                'error': f'Another setup operation is running: {_setup_running}'
            }), 409
        _setup_running = "test_tts"
        _setup_error = None

    try:
        # Import TTS service
        from services import tts
        import soundfile as sf
        import os

        # Check if TTS is available
        if not tts.is_available():
            if provider in ('pocket', 'none'):
                raise Exception(f"TTS provider '{provider}' failed to initialize.")
            else:
                raise Exception(f"TTS not configured. Please add your {provider.title()} API key in the TTS settings.")

        print(f"[Setup] Testing TTS (blocking) with {provider} voice {player_voice}")
        start_time = time.time()
        
        # Blocking synthesis
        audio_data, sample_rate = tts.synthesize_to_bytes(text, player_voice)
        synthesis_ms = (time.time() - start_time) * 1000

        # Play through system audio
        play_audio_system(audio_data, sample_rate)

        # Mark TTS test as complete in settings (with language for validation)
        settings = load_settings()
        if 'setup' not in settings:
            settings['setup'] = {}
        settings['setup']['tts_tested'] = True
        settings['setup']['tts_test_language'] = settings.get('setup', {}).get('language', 'EN_US')
        save_settings(settings)

        return jsonify({
            'success': True,
            'voice_used': player_voice,
            'provider': provider,
            'duration_ms': synthesis_ms
        })

    except Exception as e:
        error_msg = str(e)
        # Translate common errors to human-readable messages
        if 'api_key' in error_msg.lower() or 'unauthorized' in error_msg.lower():
            error_msg = f"TTS API key not found or invalid. Configure your API key in the TTS settings section."
        elif 'connection' in error_msg.lower() or 'refused' in error_msg.lower():
            error_msg = "Cannot connect to TTS service. Check your internet connection."
        # Otherwise pass through the specific error from the TTS system

        _setup_error = error_msg
        return jsonify({
            'success': False,
            'voice_used': player_voice,
            'provider': provider,
            'error': error_msg
        })

    finally:
        with _setup_lock:
            _setup_running = None


@setup_bp.route('/api/setup/test-llm', methods=['POST'])
def setup_test_llm():
    """Test all unique LLM models configured."""
    global _setup_running, _setup_error

    # Set running state
    with _setup_lock:
        if _setup_running:
            return jsonify({
                'success': False,
                'error': f'Another setup operation is running: {_setup_running}'
            }), 409
        _setup_running = "test_llm"
        _setup_error = None

    try:
        import llm

        settings = load_settings()
        conv_settings = settings.get('conversation', {})
        vision_config = settings.get('agents', {}).get('vision', {})
        vision_settings = vision_config.get('llm', {})
        memory_settings = settings.get('memory', {})

        # Collect models and their uses - build properly to handle duplicates
        # Include max_tokens for each use case to test reasoning properly
        model_uses = {}
        models_list = [
            # Core models (always tested)
            (conv_settings.get('chat_model', 'google/gemini-3-flash-preview:nitro'), 'chat',
             conv_settings.get('max_tokens', 8192)),
            (conv_settings.get('target_selection_model', 'gemini-2.5-flash-lite'), 'target',
             conv_settings.get('speaker_selection_max_tokens', 512)),
            (conv_settings.get('interjection_model', 'gemini-2.5-flash-lite'), 'interject',
             conv_settings.get('speaker_selection_max_tokens', 512)),
        ]

        # Vision model (only if vision is enabled)
        if vision_config.get('enabled', True):
            models_list.append((
                vision_settings.get('model', 'google/gemini-2.5-flash-lite:nitro'), 'vision',
                vision_settings.get('max_tokens', 8192),
            ))

        # Input correction model (if enabled and configured)
        if conv_settings.get('input_correction_enabled') and conv_settings.get('input_correction_model'):
            models_list.append(
                (conv_settings['input_correction_model'], 'input_correction', 1024)
            )

        # Memory models (only if memory is enabled)
        if memory_settings.get('enabled'):
            memory_models = [
                (memory_settings.get('chapter_model'), 'chapter'),
                (memory_settings.get('prose_model'), 'prose'),
                (memory_settings.get('graphiti_model'), 'graphiti'),
                (memory_settings.get('graphiti_small_model'), 'graphiti_small'),
                (memory_settings.get('reranker_model'), 'reranker'),
            ]
            for model_id, use in memory_models:
                if model_id:
                    models_list.append((model_id, use, 4096))

        for model_id, use, max_tokens in models_list:
            if model_id not in model_uses:
                model_uses[model_id] = {'uses': [], 'max_tokens': max_tokens}
            model_uses[model_id]['uses'].append(use)
            # Use the highest max_tokens among uses (to properly test reasoning)
            model_uses[model_id]['max_tokens'] = max(model_uses[model_id]['max_tokens'], max_tokens)

        # Test each unique model
        test_prompt = "What is 2+2? Reply with just the number."

        results = {}
        all_success = True

        for model_id, info in model_uses.items():
            uses = info['uses']
            max_tokens = info['max_tokens']
            try:
                start_time = time.time()
                # Use the same max_tokens as production to test reasoning properly
                response = llm.chat_simple(
                    test_prompt,
                    model=model_id,
                    temperature=0.0,
                    max_tokens=max_tokens,
                    context="setup_test"
                )
                duration_ms = (time.time() - start_time) * 1000

                if response:
                    results[model_id] = {
                        'success': True,
                        'used_for': uses,
                        'response_excerpt': response[:50],
                        'duration_ms': round(duration_ms)
                    }
                else:
                    all_success = False
                    # Get the actual error from llm module
                    error_msg = llm.get_last_error() or 'No response received from model'
                    results[model_id] = {
                        'success': False,
                        'used_for': uses,
                        'error': error_msg
                    }
            except Exception as e:
                all_success = False
                error_msg = str(e)
                # Translate common errors
                if 'api_key' in error_msg.lower() or 'unauthorized' in error_msg.lower() or '401' in error_msg:
                    error_msg = "Invalid API key. Check your OpenRouter/OpenAI API key."
                elif 'not found' in error_msg.lower() or '404' in error_msg:
                    error_msg = f"Model '{model_id}' not available. Verify the model ID."
                elif 'insufficient' in error_msg.lower() or 'credits' in error_msg.lower():
                    error_msg = "API account has insufficient credits."
                elif 'timeout' in error_msg.lower():
                    error_msg = "Request timed out. Try again."

                results[model_id] = {
                    'success': False,
                    'used_for': uses,
                    'error': error_msg
                }

        # Mark LLM test as complete if all passed
        if all_success:
            settings = load_settings()
            if 'setup' not in settings:
                settings['setup'] = {}
            settings['setup']['llm_tested'] = True
            save_settings(settings)

        failed_count = sum(1 for r in results.values() if not r['success'])
        total_count = len(results)

        if not all_success:
            _setup_error = f'{failed_count} of {total_count} models failed'

        return jsonify({
            'success': all_success,
            'results': results,
            'error': f'{failed_count} of {total_count} models failed' if not all_success else None
        })

    except Exception as e:
        _setup_error = str(e)
        return jsonify({
            'success': False,
            'results': {},
            'error': str(e)
        })

    finally:
        with _setup_lock:
            _setup_running = None
