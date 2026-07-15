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

from utils.game_settings import get_game_settings_warnings
from utils.settings import (
    SONORUS_DIR,
    DATA_DIR,
    GEMINI_CHAT_DEFAULT_OR,
    load_settings,
    save_settings,
    is_llm_provider_feature_disabled,
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


def _get_current_tts_provider(settings):
    """Return the currently selected TTS provider."""
    return settings.get('tts', {}).get('provider', 'inworld')


def _get_current_llm_provider(settings):
    """Return the currently selected LLM provider."""
    return settings.get('llm', {}).get('provider', 'gemini')


def _humanize_llm_test_error(error_msg, llm_provider):
    """Translate setup LLM test errors into clearer user-facing messages."""
    if not error_msg:
        return 'No response received from model'

    lower_error = error_msg.lower()

    if llm_provider == 'openrouter':
        if (
            ('requires more credits' in lower_error and 'max_tokens' in lower_error)
            or 'insufficient credits' in lower_error
        ):
            return 'OpenRouter balance too low, deposit $5 (minimum) into OpenRouter to continue'

    if 'api_key' in lower_error or 'unauthorized' in lower_error or '401' in error_msg:
        return "Invalid API key. Check your OpenRouter/OpenAI/Ollama/llama.cpp API key."
    if 'not found' in lower_error or '404' in error_msg:
        return "Model not available. Verify the model ID."
    if 'insufficient' in lower_error or 'credits' in lower_error:
        return "API account has insufficient credits."
    if 'timeout' in lower_error:
        return "Request timed out. Try again."

    return error_msg


def _get_default_embedding_model(llm_provider):
    """Return the memory embedding default for the active LLM provider."""
    if llm_provider == 'openrouter':
        return 'openai/text-embedding-3-small'
    if llm_provider == 'gemini':
        return 'gemini-embedding-2'
    return 'text-embedding-3-small'


def _safe_positive_int(value):
    try:
        parsed = int(float(value))
        return parsed if parsed > 0 else None
    except (TypeError, ValueError):
        return None


def _reasoning_request_is_off(reasoning_request):
    """Return True when the request did not enable OpenRouter reasoning."""
    if reasoning_request is None:
        return True
    if not isinstance(reasoning_request, dict):
        return True
    if reasoning_request.get('enabled') is False:
        return True
    if reasoning_request.get('max_tokens') == 0:
        return True
    if str(reasoning_request.get('effort', '')).lower() in ('minimal', 'none', 'off', 'disabled'):
        return True
    return False


def _openrouter_reasoning_warning(llm_module):
    """Build a warning if OpenRouter reports reasoning tokens despite reasoning being off."""
    metadata = llm_module.get_last_response_metadata() if hasattr(llm_module, 'get_last_response_metadata') else {}
    if metadata.get('warning'):
        return metadata['warning']
    if metadata.get('provider') != 'openrouter':
        return None

    reasoning_tokens = _safe_positive_int((metadata.get('usage') or {}).get('reasoning_tokens'))
    if not reasoning_tokens:
        return None

    if not _reasoning_request_is_off(metadata.get('reasoning_requested')):
        return None

    return (
        f"OpenRouter reported {reasoning_tokens} reasoning tokens even though Sonorus did not enable reasoning for this test. "
        "This provider may ignore the reasoning toggle."
    )


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

            # Undubbed languages use EN_US voice manifest and references
            from constants import get_voice_language
            voice_language = get_voice_language(language)

            # Check if voice manifest exists (using voice language, not game language)
            manifest_path, _ = _get_voice_paths(voice_language)

            if not os.path.exists(manifest_path):
                manifest_name = os.path.basename(manifest_path)
                if voice_language == "EN_US":
                    raise FileNotFoundError(f"Voice manifest not found. Ensure {manifest_name} exists in the data folder.")
                else:
                    # For non-English dubbed languages, suggest using voice manager
                    server_port = os.getenv("SONORUS_SERVER_PORT", "5400")
                    raise FileNotFoundError(
                        f"Voice manifest not found for {voice_language}. "
                        f"You need to build the voice manifest for this language first. "
                        f"Visit the Voice Manager at http://localhost:{server_port}/voice-manager/ "
                        f"to extract and build {manifest_name}."
                    )

            # Extract using voice language (undubbed languages extract EN_US audio)
            print(f"[Setup] Running: extract_voices.py --from-manifest --language {voice_language}")

            result = subprocess.run(
                [sys.executable, script_path, "--from-manifest", "--language", voice_language],
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
    """Get voice manifest and references directory for the given language.

    Undubbed languages automatically fall back to EN_US paths.
    """
    from constants import get_voice_language
    language = get_voice_language(language)

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


@setup_bp.route('/api/setup/open-voice-references', methods=['POST'])
def setup_open_voice_references():
    """Open the active voice references folder in Explorer."""
    try:
        data = request.get_json(silent=True) or {}
        settings = load_settings()
        language = str(data.get("language") or settings.get('setup', {}).get('language', 'EN_US'))
        _, voice_refs_dir = _get_voice_paths(language)
        voice_refs_dir = os.path.abspath(voice_refs_dir)

        voice_root = os.path.abspath(os.path.join(SONORUS_DIR, "voice_references"))
        if os.path.commonpath([voice_root, voice_refs_dir]) != voice_root:
            return jsonify({"error": "Invalid voice references path"}), 400

        os.makedirs(voice_refs_dir, exist_ok=True)
        subprocess.Popen(["explorer", voice_refs_dir])
        return jsonify({"ok": True, "path": voice_refs_dir})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
    current_tts_provider = _get_current_tts_provider(settings)
    tts_test_provider = settings.get('setup', {}).get('tts_test_provider')
    tts_tested = settings.get('setup', {}).get('tts_tested', False) and tts_test_provider == current_tts_provider
    tts_test_language = settings.get('setup', {}).get('tts_test_language')
    # TTS is valid if tested AND (voices complete OR tested for this specific language)
    tts_valid = tts_tested and (voices_complete or tts_test_language == language)

    # LLM test
    current_llm_provider = _get_current_llm_provider(settings)
    llm_test_provider = settings.get('setup', {}).get('llm_test_provider')
    llm_tested = settings.get('setup', {}).get('llm_tested', False) and llm_test_provider == current_llm_provider

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
        if settings.get('setup', {}).get('tts_tested', False) and tts_test_provider and tts_test_provider != current_tts_provider:
            missing.append(f"TTS test (tested for {tts_test_provider}, need {current_tts_provider})")
        elif tts_tested and tts_test_language != language:
            missing.append(f"TTS test (tested for {tts_test_language}, need {language})")
        else:
            missing.append("TTS test")
    if not llm_tested:
        if settings.get('setup', {}).get('llm_tested', False) and llm_test_provider and llm_test_provider != current_llm_provider:
            missing.append(f"LLM test (tested for {llm_test_provider}, need {current_llm_provider})")
        else:
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
        'tts_test_provider': tts_test_provider,
        'current_tts_provider': current_tts_provider,
        'llm_tested': llm_tested,
        'llm_test_provider': llm_test_provider,
        'current_llm_provider': current_llm_provider,
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
    return get_game_settings_warnings()


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
    current_llm_provider = status['current_llm_provider']
    models = {
        'chat': conv_settings.get('chat_model', GEMINI_CHAT_DEFAULT_OR),
        'target': conv_settings.get('target_selection_model', 'meta-llama/llama-4-scout:nitro'),
        'interject': conv_settings.get('interjection_model', 'google/gemini-3.1-flash-lite')
    }
    if vision_config.get('enabled', True) and not is_llm_provider_feature_disabled('vision', settings):
        models['vision'] = vision_settings.get('model', 'google/gemini-2.5-flash-lite:nitro')
    # Include input correction model if enabled
    if (conv_settings.get('input_correction_enabled') and conv_settings.get('input_correction_model')
            and not is_llm_provider_feature_disabled('input_correction', settings)):
        models['input_correction'] = conv_settings['input_correction_model']
    # Include memory models only when the active provider allows memory.
    if memory_settings.get('enabled') and not is_llm_provider_feature_disabled('memory', settings):
        models['embedding'] = memory_settings.get('embedding_model') or _get_default_embedding_model(current_llm_provider)
        for key, setting_key in [('chapter', 'chapter_model'), ('prose', 'prose_model'),
                                  ('graphiti', 'graphiti_model'), ('graphiti_small', 'graphiti_small_model'),
                                  ('reranker', 'reranker_model')]:
            model_id = memory_settings.get(setting_key)
            if model_id:
                models[key] = model_id
    # Include owl post models if enabled
    owl_settings = settings.get('owl_post', {})
    if owl_settings.get('enabled', True) and not is_llm_provider_feature_disabled('owl_post', settings):
        for key, setting_key in [('owl_classifier', 'orchestrator_model'), ('owl_mail', 'mail_model'),
                                  ('owl_board', 'board_model'), ('owl_summarize', 'summarize_model')]:
            model_id = owl_settings.get(setting_key)
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
                "tested": status['tts_tested'],
                "tested_provider": status['tts_test_provider'],
                "current_provider": status['current_tts_provider'],
            },
            "llm": {
                "status": llm_status,
                "tested": status['llm_tested'],
                "tested_provider": status['llm_test_provider'],
                "current_provider": status['current_llm_provider'],
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
            elif provider == 'inworld':
                inworld = tts_settings.get('inworld', {})
                missing = []
                if not inworld.get('api_key'):
                    missing.append('API key')
                if missing:
                    missing_text = ' and '.join(missing)
                    raise Exception(f"TTS not configured. Please add your Inworld {missing_text} in the TTS settings.")
                raise Exception("TTS not configured. Check your Inworld settings.")
            elif provider == 'elevenlabs':
                raise Exception("TTS not configured. Please add your ElevenLabs API key in the TTS settings.")
            elif provider == 'omnivoice_api':
                omni_api = tts_settings.get('omnivoice_api', {})
                if not omni_api.get('api_url'):
                    raise Exception("TTS not configured. Please add your OmniVoice API URL in the TTS settings.")
                raise Exception("TTS not configured. Check your OmniVoice API settings.")
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
        settings['setup']['tts_test_provider'] = provider
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

        settings = load_settings()
        if 'setup' not in settings:
            settings['setup'] = {}
        settings['setup']['tts_tested'] = False
        settings['setup']['tts_test_provider'] = provider
        save_settings(settings)

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
        llm_provider = _get_current_llm_provider(settings)
        conv_settings = settings.get('conversation', {})
        vision_config = settings.get('agents', {}).get('vision', {})
        vision_settings = vision_config.get('llm', {})
        memory_settings = settings.get('memory', {})

        # Collect models and their uses - build properly to handle duplicates
        # Include max_tokens for each use case to test reasoning properly
        model_uses = {}
        models_list = [
            # Core models (always tested)
            (conv_settings.get('chat_model', GEMINI_CHAT_DEFAULT_OR), 'chat',
             conv_settings.get('max_tokens', 8192)),
            (conv_settings.get('target_selection_model', 'gemini-2.5-flash-lite'), 'target',
             conv_settings.get('speaker_selection_max_tokens', 512)),
            (conv_settings.get('interjection_model', 'gemini-2.5-flash-lite'), 'interject',
             conv_settings.get('speaker_selection_max_tokens', 512)),
        ]

        # Vision model (only if vision is enabled)
        if vision_config.get('enabled', True) and not is_llm_provider_feature_disabled('vision', settings):
            models_list.append((
                vision_settings.get('model', 'google/gemini-2.5-flash-lite:nitro'), 'vision',
                vision_settings.get('max_tokens', 8192),
            ))

        # Input correction model (if enabled and configured)
        if (conv_settings.get('input_correction_enabled') and conv_settings.get('input_correction_model')
                and not is_llm_provider_feature_disabled('input_correction', settings)):
            models_list.append(
                (conv_settings['input_correction_model'], 'input_correction', 1024)
            )

        # Memory models (only if memory is enabled and provider allows memory)
        embedding_model = None
        if memory_settings.get('enabled') and not is_llm_provider_feature_disabled('memory', settings):
            embedding_model = memory_settings.get('embedding_model') or _get_default_embedding_model(llm_provider)
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

        # Owl Post models (only if owl post is enabled)
        owl_settings = settings.get('owl_post', {})
        if owl_settings.get('enabled', True) and not is_llm_provider_feature_disabled('owl_post', settings):
            owl_models = [
                (owl_settings.get('orchestrator_model'), 'owl_classifier'),
                (owl_settings.get('mail_model'), 'owl_mail'),
                (owl_settings.get('board_model'), 'owl_board'),
                (owl_settings.get('summarize_model'), 'owl_summarize'),
            ]
            for model_id, use in owl_models:
                if model_id:
                    models_list.append((model_id, use, 8192))

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
                    temperature=0.2,
                    max_tokens=max_tokens,
                    context="setup_test"
                )
                duration_ms = (time.time() - start_time) * 1000

                if response:
                    result = {
                        'success': True,
                        'used_for': uses,
                        'response_excerpt': response[:50],
                        'duration_ms': round(duration_ms)
                    }
                    warning = _openrouter_reasoning_warning(llm)
                    if warning:
                        result['warning'] = warning
                    results[model_id] = result
                else:
                    all_success = False
                    # Get the actual error from llm module
                    error_msg = _humanize_llm_test_error(
                        llm.get_last_error() or 'No response received from model',
                        llm_provider
                    )
                    results[model_id] = {
                        'success': False,
                        'used_for': uses,
                        'error': error_msg
                    }
            except Exception as e:
                all_success = False
                error_msg = _humanize_llm_test_error(str(e), llm_provider)
                results[model_id] = {
                    'success': False,
                    'used_for': uses,
                    'error': error_msg
                }

        if embedding_model:
            result_key = f"{embedding_model} [embedding]"
            try:
                start_time = time.time()
                embedding_result = llm.test_embedding(
                    model=embedding_model,
                    text="Sonorus setup embedding test"
                )
                duration_ms = (time.time() - start_time) * 1000

                if embedding_result:
                    dimensions = embedding_result.get('dimensions')
                    results[result_key] = {
                        'success': True,
                        'used_for': ['embedding'],
                        'response_excerpt': f"{dimensions} dimensions" if dimensions else "Embedding returned",
                        'duration_ms': round(duration_ms)
                    }
                else:
                    all_success = False
                    error_msg = _humanize_llm_test_error(
                        llm.get_last_error() or 'No embedding returned from model',
                        llm_provider
                    )
                    results[result_key] = {
                        'success': False,
                        'used_for': ['embedding'],
                        'error': error_msg
                    }
            except Exception as e:
                all_success = False
                error_msg = _humanize_llm_test_error(str(e), llm_provider)
                results[result_key] = {
                    'success': False,
                    'used_for': ['embedding'],
                    'error': error_msg
                }

        # Mark LLM test as complete if all passed
        if all_success:
            settings = load_settings()
            if 'setup' not in settings:
                settings['setup'] = {}
            settings['setup']['llm_tested'] = True
            settings['setup']['llm_test_provider'] = llm_provider
            save_settings(settings)
        else:
            settings = load_settings()
            if 'setup' not in settings:
                settings['setup'] = {}
            settings['setup']['llm_tested'] = False
            settings['setup']['llm_test_provider'] = llm_provider
            save_settings(settings)

        failed_count = sum(1 for r in results.values() if not r['success'])
        total_count = len(results)
        unique_errors = {
            r.get('error')
            for r in results.values()
            if not r.get('success') and r.get('error')
        }
        summary_error = next(iter(unique_errors)) if len(unique_errors) == 1 else f'{failed_count} of {total_count} models failed'

        if not all_success:
            _setup_error = summary_error

        return jsonify({
            'success': all_success,
            'results': results,
            'error': summary_error if not all_success else None
        })

    except Exception as e:
        settings = load_settings()
        if 'setup' not in settings:
            settings['setup'] = {}
        settings['setup']['llm_tested'] = False
        settings['setup']['llm_test_provider'] = _get_current_llm_provider(settings)
        save_settings(settings)

        _setup_error = str(e)
        return jsonify({
            'success': False,
            'results': {},
            'error': str(e)
        })

    finally:
        with _setup_lock:
            _setup_running = None
