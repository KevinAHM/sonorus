"""
Config API endpoints for Sonorus settings management.

Handles settings CRUD, character import/export, system events.
"""

import os
import json
import shutil

from flask import Blueprint, request, jsonify, send_file, Response

from utils.settings import (
    SONORUS_DIR,
    CONFIG_HTML,
    DEFAULT_SETTINGS,
    load_settings,
    save_settings,
    deep_merge,
)

import llm
import event_logger

config_bp = Blueprint('config', __name__)

# These will be set by server.py to provide dependencies
_lua_socket = None


def set_lua_socket(socket):
    """Set the lua socket instance for tracking settings sync."""
    global _lua_socket
    _lua_socket = socket


# ============================================
# Config Page & Static Files
# ============================================

@config_bp.route('/')
def config_page():
    """Serve the main config page."""
    if os.path.exists(CONFIG_HTML):
        return send_file(CONFIG_HTML)
    return "Config page not found", 404


@config_bp.route('/js/<path:filename>')
def serve_js(filename):
    """Serve static JS files from sonorus/js/ folder."""
    js_dir = os.path.join(SONORUS_DIR, "js")
    js_file = os.path.join(js_dir, filename)
    if os.path.exists(js_file):
        return send_file(js_file, mimetype='application/javascript')
    return "File not found", 404


@config_bp.route('/css/<path:filename>')
def serve_css(filename):
    """Serve static CSS files from sonorus/css/ folder."""
    css_dir = os.path.join(SONORUS_DIR, "css")
    css_file = os.path.join(css_dir, filename)
    if os.path.exists(css_file):
        return send_file(css_file, mimetype='text/css')
    return "File not found", 404


@config_bp.route('/data/<path:filename>')
def serve_data(filename):
    """Serve static data files from sonorus/data/ folder."""
    data_dir = os.path.join(SONORUS_DIR, "data")
    data_file = os.path.join(data_dir, filename)
    if os.path.exists(data_file):
        return send_file(data_file, mimetype='application/json')
    return "File not found", 404


# ============================================
# Config API Endpoints
# ============================================

@config_bp.route('/api/config', methods=['GET'])
def get_config():
    """Get current settings with sensitive values masked."""
    settings = load_settings()
    masked = json.loads(json.dumps(settings))
    if masked.get('llm', {}).get('api_key'):
        masked['llm']['api_key'] = '********'
    llm_providers_with_keys = ['gemini', 'openrouter', 'openai']
    for provider in llm_providers_with_keys:
        if masked.get('llm', {}).get(provider, {}).get('api_key'):
            masked['llm'][provider]['api_key'] = '********'
    tts_providers_with_keys = ['inworld', 'elevenlabs', 'openai']
    for provider in tts_providers_with_keys:
        if masked.get('tts', {}).get(provider, {}).get('api_key'):
            masked['tts'][provider]['api_key'] = '********'
    return jsonify(masked)


@config_bp.route('/api/model-capabilities', methods=['GET'])
def get_model_capabilities():
    """Return model capabilities keyed by base model name for frontend reasoning toggle."""
    return jsonify(llm.get_model_capabilities_for_frontend())


@config_bp.route('/api/config', methods=['POST'])
def save_config():
    """Save settings with hot-reload for certain changes."""
    new_settings = request.get_json() or {}
    existing = load_settings()

    if new_settings.get('llm', {}).get('api_key') == '********':
        if 'llm' not in new_settings:
            new_settings['llm'] = {}
        new_settings['llm']['api_key'] = existing.get('llm', {}).get('api_key', '')
    llm_providers_with_keys = ['gemini', 'openrouter', 'openai']
    for provider in llm_providers_with_keys:
        new_key = new_settings.get('llm', {}).get(provider, {}).get('api_key', '')
        existing_key = existing.get('llm', {}).get(provider, {}).get('api_key', '')
        if new_key == '********':
            if 'llm' not in new_settings:
                new_settings['llm'] = {}
            if provider not in new_settings['llm']:
                new_settings['llm'][provider] = {}
            new_settings['llm'][provider]['api_key'] = existing_key

    # Check if TTS provider itself changed
    new_tts_provider = new_settings.get('tts', {}).get('provider', '')
    existing_tts_provider = existing.get('tts', {}).get('provider', '')
    tts_provider_switched = new_tts_provider and new_tts_provider != existing_tts_provider

    # Check if language changed
    new_language = new_settings.get('setup', {}).get('language', 'EN_US')
    existing_language = existing.get('setup', {}).get('language', 'EN_US')
    language_changed = new_language != existing_language

    # Track which TTS providers had API key or workspace changes
    tts_providers_changed = []
    tts_providers_with_keys = ['inworld', 'elevenlabs', 'openai']
    for provider in tts_providers_with_keys:
        new_key = new_settings.get('tts', {}).get(provider, {}).get('api_key', '')
        existing_key = existing.get('tts', {}).get(provider, {}).get('api_key', '')

        if new_key == '********':
            # Masked value - preserve existing key
            if 'tts' not in new_settings:
                new_settings['tts'] = {}
            if provider not in new_settings['tts']:
                new_settings['tts'][provider] = {}
            new_settings['tts'][provider]['api_key'] = existing_key
        elif new_key and new_key != existing_key:
            # API key changed - mark for cache refresh
            tts_providers_changed.append(provider)
            print(f"[Settings] API key changed for TTS provider: {provider}")

    # Also check if Inworld workspace_id changed
    new_workspace = new_settings.get('tts', {}).get('inworld', {}).get('workspace_id', '')
    existing_workspace = existing.get('tts', {}).get('inworld', {}).get('workspace_id', '')
    if new_workspace and new_workspace != existing_workspace and 'inworld' not in tts_providers_changed:
        tts_providers_changed.append('inworld')
        print("[Settings] Workspace ID changed for TTS provider: inworld")

    # Preserve setup test flags that are set by test endpoints, not the frontend.
    # The frontend config object doesn't get updated when tests pass (they save directly
    # to settings.json), so a config save would wipe the flags without this preservation.
    existing_setup = existing.get('setup', {})
    new_setup = new_settings.get('setup', {})
    for key in ('tts_tested', 'tts_test_language', 'llm_tested'):
        if key in existing_setup and key not in new_setup:
            new_setup[key] = existing_setup[key]
    if new_setup:
        new_settings['setup'] = new_setup

    merged = deep_merge(DEFAULT_SETTINGS.copy(), new_settings)

    # Strip prompt values that match defaults so code updates take effect.
    # Only persist prompts users have actually customized.
    merged_prompts = merged.get('prompts', {})
    default_prompts = DEFAULT_SETTINGS.get('prompts', {})
    for key in ('default', 'scene_continuation', 'interjection_prompt_mode'):
        if key in merged_prompts and merged_prompts[key] == default_prompts.get(key):
            del merged_prompts[key]

    if save_settings(merged):
        print("[Settings] Configuration saved")

        # Handle TTS provider switch
        if tts_provider_switched:
            print(f"[Settings] TTS provider changed: {existing_tts_provider} -> {new_tts_provider}")
            try:
                from services import tts

                # Unload old provider's models
                if existing_tts_provider == 'pocket':
                    try:
                        from services.pocket_tts_onnx import unload as unload_pocket
                        unload_pocket()
                        print("[Settings] Unloaded Pocket TTS models")
                    except Exception as e:
                        print(f"[Settings] Error unloading Pocket TTS: {e}")

                # Disconnect Inworld WebSocket before clearing its cache
                if existing_tts_provider == 'inworld':
                    try:
                        provider_instance = tts.get_provider()
                        if hasattr(provider_instance, 'disconnect_websocket'):
                            provider_instance.disconnect_websocket()
                            print("[Settings] Disconnected Inworld WebSocket")
                    except Exception as e:
                        print(f"[Settings] Error disconnecting Inworld WebSocket: {e}")

                # Clear old provider cache
                if existing_tts_provider:
                    tts.clear_provider_cache(existing_tts_provider)

                # Pre-load new provider's voices
                print(f"[Settings] Loading voices for {new_tts_provider}...")
                tts.clear_provider_cache(new_tts_provider)  # Ensure fresh instance
                voice_list = tts.list_voices()
                print(f"[Settings] Loaded {len(voice_list) if voice_list else 0} voices from {new_tts_provider}")

                # Preload new provider's models (if Pocket TTS)
                if new_tts_provider == 'pocket':
                    try:
                        from services.pocket_tts_onnx import warm_up as warmup_pocket
                        import threading
                        def preload_pocket():
                            try:
                                warmup_pocket()
                                print("[Settings] Pocket TTS models preloaded")
                            except Exception as e:
                                print(f"[Settings] Pocket TTS preload failed: {e}")
                        threading.Thread(target=preload_pocket, daemon=True).start()
                    except Exception as e:
                        print(f"[Settings] Error starting Pocket TTS preload: {e}")

                # Connect Inworld WebSocket (if switching to Inworld)
                elif new_tts_provider == 'inworld':
                    try:
                        provider_instance = tts.get_provider()
                        if hasattr(provider_instance, 'connect_websocket'):
                            if provider_instance.connect_websocket():
                                print("[Settings] Inworld WebSocket connected")
                            else:
                                print("[Settings] Inworld WebSocket unavailable (falling back to HTTP)")
                    except Exception as e:
                        print(f"[Settings] Error connecting Inworld WebSocket: {e}")

            except Exception as e:
                print(f"[Settings] Error switching TTS provider: {e}")

        # Refresh voice cache for providers with changed API keys
        elif tts_providers_changed:
            try:
                from services import tts
                for provider in tts_providers_changed:
                    # Clear the cached provider so it re-initializes with new key
                    tts.clear_provider_cache(provider)
            except Exception as e:
                print(f"[Settings] Error refreshing TTS cache: {e}")

        # Handle language change - clear ALL provider caches
        if language_changed:
            print(f"[Settings] Language changed: {existing_language} -> {new_language}")
            try:
                from services import tts
                # Clear all provider caches (voices are language-specific)
                tts.clear_provider_cache()
                print("[Settings] Cleared all TTS provider caches for language change")
            except Exception as e:
                print(f"[Settings] Error clearing TTS caches for language change: {e}")

        # Hot-reload STT settings
        new_stt = new_settings.get('stt', {})
        existing_stt = existing.get('stt', {})
        stt_provider_changed = new_stt.get('provider') != existing_stt.get('provider')
        stt_hotkey_changed = new_stt.get('hotkey') != existing_stt.get('hotkey')
        stt_api_key_changed = (
            new_stt.get('deepgram', {}).get('api_key') != existing_stt.get('deepgram', {}).get('api_key') or
            new_stt.get('whisper', {}).get('api_key') != existing_stt.get('whisper', {}).get('api_key')
        )

        if stt_provider_changed or stt_api_key_changed:
            # Unload old local STT provider if switching away
            if stt_provider_changed:
                old_stt_provider = existing_stt.get('provider')
                if old_stt_provider == 'parakeet':
                    try:
                        from services.parakeet_stt import unload as unload_stt
                        unload_stt()
                        print("[Settings] Unloaded Parakeet STT worker")
                    except Exception as e:
                        print(f"[Settings] Error unloading Parakeet STT: {e}")
                elif old_stt_provider == 'canary':
                    try:
                        from services.canary_stt import unload as unload_stt
                        unload_stt()
                        print("[Settings] Unloaded Canary STT worker")
                    except Exception as e:
                        print(f"[Settings] Error unloading Canary STT: {e}")
                elif old_stt_provider == 'moonshine':
                    try:
                        from services.moonshine_stt import unload as unload_stt
                        unload_stt()
                        print("[Settings] Unloaded Moonshine STT worker")
                    except Exception as e:
                        print(f"[Settings] Error unloading Moonshine STT: {e}")

            # Preload new local STT provider if switching to one
            if stt_provider_changed:
                new_stt_provider = new_stt.get('provider')
                if new_stt_provider == 'parakeet':
                    try:
                        from services.parakeet_stt import warm_up as warmup_fn
                        import threading
                        def preload_parakeet():
                            try:
                                warmup_fn()
                                print("[Settings] Parakeet STT worker preloaded")
                            except Exception as e:
                                print(f"[Settings] Parakeet STT preload failed: {e}")
                        threading.Thread(target=preload_parakeet, daemon=True).start()
                    except Exception as e:
                        print(f"[Settings] Error starting Parakeet STT preload: {e}")
                elif new_stt_provider == 'canary':
                    try:
                        from services.canary_stt import warm_up as warmup_fn
                        import threading
                        def preload_canary():
                            try:
                                warmup_fn()
                                print("[Settings] Canary STT worker preloaded")
                            except Exception as e:
                                print(f"[Settings] Canary STT preload failed: {e}")
                        threading.Thread(target=preload_canary, daemon=True).start()
                    except Exception as e:
                        print(f"[Settings] Error starting Canary STT preload: {e}")
                elif new_stt_provider == 'moonshine':
                    try:
                        from services.moonshine_stt import warm_up as warmup_fn
                        import threading
                        def preload_moonshine():
                            try:
                                warmup_fn()
                                print("[Settings] Moonshine STT worker preloaded")
                            except Exception as e:
                                print(f"[Settings] Moonshine STT preload failed: {e}")
                        threading.Thread(target=preload_moonshine, daemon=True).start()
                    except Exception as e:
                        print(f"[Settings] Error starting Moonshine STT preload: {e}")

            # Provider or API key changed - restart capture with new settings
            try:
                from input import voice as stt_capture_module
                stt_capture_module.restart_capture()
            except Exception as e:
                print(f"[Settings] Error restarting STT: {e}")
        elif stt_hotkey_changed:
            # Just hotkey changed - update on running instance
            try:
                from input import voice as stt_capture_module
                stt_capture_module.set_capture_hotkey(new_stt.get('hotkey', 'middle_mouse'))
            except Exception as e:
                print(f"[Settings] Error updating STT hotkey: {e}")

        # Hot-reload open_mic settings - load/unload VAD and turn detection models
        new_open_mic = new_settings.get('open_mic', {})
        existing_open_mic = existing.get('open_mic', {})
        open_mic_enabled_changed = new_open_mic.get('enabled') != existing_open_mic.get('enabled')

        if open_mic_enabled_changed:
            new_enabled = new_open_mic.get('enabled', False)
            if new_enabled:
                # Open mic enabled - switch mode and preload models
                print("[Settings] Open mic enabled - switching to open_mic mode...")
                try:
                    from input.voice import set_capture_mode
                    set_capture_mode('open_mic')
                except Exception as e:
                    print(f"[Settings] Failed to set open_mic mode: {e}")
                import threading
                def preload_speech_models():
                    try:
                        from services.vad import VADProcessor
                        _ = VADProcessor(threshold=0.5, sample_rate=16000)
                        print("[Settings] VAD model preloaded")
                    except Exception as e:
                        print(f"[Settings] VAD preload failed: {e}")
                    try:
                        from services.turn_detection import TurnDetector
                        import numpy as np
                        detector = TurnDetector()
                        # Warm up with dummy prediction
                        dummy_audio = np.zeros(16000, dtype=np.float32)
                        detector.predict(dummy_audio)
                        print("[Settings] Turn detection model preloaded")
                    except Exception as e:
                        print(f"[Settings] Turn detection preload failed: {e}")
                threading.Thread(target=preload_speech_models, daemon=True).start()
            else:
                # Open mic disabled - switch to PTT mode and unload models
                print("[Settings] Open mic disabled - switching to PTT mode...")
                try:
                    from input.voice import set_capture_mode
                    set_capture_mode('ptt')
                except Exception as e:
                    print(f"[Settings] Failed to set ptt mode: {e}")
                try:
                    from services.vad import unload as unload_vad
                    unload_vad()
                except Exception as e:
                    print(f"[Settings] VAD unload failed: {e}")
                try:
                    from services.turn_detection import unload as unload_turn
                    unload_turn()
                except Exception as e:
                    print(f"[Settings] Turn detection unload failed: {e}")

        # Hot-reload spell detection models when voice_spells toggled
        voice_spells_changed = new_stt.get('voice_spells') != existing_stt.get('voice_spells')
        if voice_spells_changed:
            if new_stt.get('voice_spells', True):
                # Voice spells enabled - preload spell detection models
                import threading
                def preload_spell_models():
                    try:
                        from services.spell_detector import warm_up as warmup_spells
                        warmup_spells()
                        print("[Settings] Spell detection models preloaded")
                    except Exception as e:
                        print(f"[Settings] Spell detection preload failed: {e}")
                threading.Thread(target=preload_spell_models, daemon=True).start()
            else:
                # Voice spells disabled - unload models to free memory
                try:
                    from services.spell_detector import unload as unload_spells
                    unload_spells()
                except Exception as e:
                    print(f"[Settings] Spell detection unload failed: {e}")

        # Hot-reload chat hotkey
        new_input = new_settings.get('input', {})
        existing_input = existing.get('input', {})
        if new_input.get('chat_hotkey') != existing_input.get('chat_hotkey'):
            try:
                from input import text as chat_capture_module
                chat_capture_module.set_capture_hotkey(new_input.get('chat_hotkey', 'enter'))
                print(f"[Settings] Chat hotkey updated: {new_input.get('chat_hotkey')}")
            except Exception as e:
                print(f"[Settings] Error updating chat hotkey: {e}")

        # Hot-reload stop conversation hotkey
        if new_input.get('stop_hotkey') != existing_input.get('stop_hotkey'):
            try:
                from input import hotkeys as stop_capture_module
                stop_capture_module.set_hotkey(new_input.get('stop_hotkey', 'delete'))
                print(f"[Settings] Stop hotkey updated: {new_input.get('stop_hotkey')}")
            except Exception as e:
                print(f"[Settings] Error updating stop hotkey: {e}")

        # Hot-reload conversation mode hotkey
        if new_input.get('mode_hotkey') != existing_input.get('mode_hotkey'):
            try:
                from input import mode_hotkey as mode_hotkey_module
                mode_hotkey_module.set_hotkey(new_input.get('mode_hotkey', 'home'))
                print(f"[Settings] Mode hotkey updated: {new_input.get('mode_hotkey')}")
            except Exception as e:
                print(f"[Settings] Error updating mode hotkey: {e}")

        # Hot-reload memory settings
        new_memory = new_settings.get('memory', {})
        existing_memory = existing.get('memory', {})
        new_llm = new_settings.get('llm', {})
        existing_llm = existing.get('llm', {})

        # Check if any memory-related settings changed
        memory_connection_changed = (
            # Memory enabled/disabled
            new_memory.get('enabled') != existing_memory.get('enabled') or
            # LLM provider changed (affects embedder type)
            new_llm.get('provider') != existing_llm.get('provider') or
            # Memory models changed
            new_memory.get('graphiti_model') != existing_memory.get('graphiti_model') or
            new_memory.get('graphiti_small_model') != existing_memory.get('graphiti_small_model') or
            new_memory.get('reranker_model') != existing_memory.get('reranker_model') or
            new_memory.get('max_concurrency') != existing_memory.get('max_concurrency') or
            # API keys changed (affects embedder initialization)
            new_llm.get('gemini', {}).get('api_key') != existing_llm.get('gemini', {}).get('api_key') or
            new_llm.get('openrouter', {}).get('api_key') != existing_llm.get('openrouter', {}).get('api_key') or
            new_llm.get('openai', {}).get('api_key') != existing_llm.get('openai', {}).get('api_key') or
            # Reasoning toggles changed (affects thinking_config in memory LLM client)
            new_llm.get('gemini', {}).get('reasoning_enabled') != existing_llm.get('gemini', {}).get('reasoning_enabled') or
            new_llm.get('openrouter', {}).get('reasoning_enabled') != existing_llm.get('openrouter', {}).get('reasoning_enabled') or
            new_llm.get('openai', {}).get('reasoning_enabled') != existing_llm.get('openai', {}).get('reasoning_enabled') or
            new_memory.get('graphiti_model_reasoning') != existing_memory.get('graphiti_model_reasoning') or
            new_memory.get('graphiti_small_model_reasoning') != existing_memory.get('graphiti_small_model_reasoning') or
            new_memory.get('chapter_model_reasoning') != existing_memory.get('chapter_model_reasoning') or
            new_memory.get('prose_model_reasoning') != existing_memory.get('prose_model_reasoning')
        )
        if memory_connection_changed:
            try:
                from utils.memory import reset_memory_connection, init_memory
                print("[Settings] Memory settings changed - reinitializing...")
                reset_memory_connection()
                if new_memory.get('enabled', True):
                    init_memory()
            except ImportError:
                pass
            except Exception as e:
                print(f"[Settings] Error reinitializing memory: {e}")

        # Hot-reload LLM client cache (connection pooling)
        llm_client_changed = (
            new_llm.get('provider') != existing_llm.get('provider') or
            new_llm.get('openrouter', {}).get('api_key') != existing_llm.get('openrouter', {}).get('api_key') or
            new_llm.get('openai', {}).get('api_key') != existing_llm.get('openai', {}).get('api_key') or
            new_llm.get('openai', {}).get('api_url') != existing_llm.get('openai', {}).get('api_url')
        )
        if llm_client_changed:
            try:
                llm.invalidate_client()
                llm.prewarm_client()
            except Exception as e:
                print(f"[Settings] Error invalidating LLM client: {e}")

        # Sync tracking settings to Lua if relevant settings changed
        new_server = new_settings.get('server', {})
        existing_server = existing.get('server', {})
        new_history = new_settings.get('history', {})
        existing_history = existing.get('history', {})
        new_conversation = new_settings.get('conversation', {})
        existing_conversation = existing.get('conversation', {})
        new_dev = new_settings.get('dev', {})
        existing_dev = existing.get('dev', {})
        new_input = new_settings.get('input', {})
        existing_input = existing.get('input', {})
        new_time_dilation = new_settings.get('time_dilation', {})
        existing_time_dilation = existing.get('time_dilation', {})
        if (new_server.get('enabled') != existing_server.get('enabled') or
            new_history.get('track_ambient') != existing_history.get('track_ambient') or
            new_history.get('track_cutscene') != existing_history.get('track_cutscene') or
            new_conversation.get('companion_callout_block_minutes') != existing_conversation.get('companion_callout_block_minutes') or
            new_dev.get('enabled') != existing_dev.get('enabled') or
            new_input.get('preview_lock') != existing_input.get('preview_lock') or
            new_time_dilation != existing_time_dilation or
            new_conversation.get('companion_follow_distance_m') != existing_conversation.get('companion_follow_distance_m')):
            if _lua_socket:
                _lua_socket.send_tracking_settings()

        return jsonify({"status": "ok"})
    return jsonify({"error": "Failed to save"}), 500


@config_bp.route('/api/sync-tracking-settings', methods=['POST'])
def sync_tracking_settings():
    """Manually trigger tracking settings sync to Lua (used by config page for live updates)."""
    if _lua_socket:
        _lua_socket.send_tracking_settings()
    return jsonify({"status": "ok"})


@config_bp.route('/api/config/reset', methods=['POST'])
def reset_config():
    """Reset settings to defaults."""
    if save_settings(DEFAULT_SETTINGS.copy()):
        print("[Settings] Reset to defaults")
        return jsonify({"status": "ok"})
    return jsonify({"error": "Failed to reset"}), 500


@config_bp.route('/api/config/defaults/prompt', methods=['GET'])
def get_default_prompt():
    """Get the default character prompt."""
    return jsonify({"prompt": DEFAULT_SETTINGS['prompts']['default']})


@config_bp.route('/api/config/defaults/scene-continuation-prompt', methods=['GET'])
def get_default_scene_continuation_prompt():
    """Get the default scene continuation prompt."""
    return jsonify({"prompt": DEFAULT_SETTINGS['prompts']['scene_continuation']})


@config_bp.route('/api/config/defaults/interjection-prompt-mode', methods=['GET'])
def get_default_interjection_prompt_mode():
    """Get the default interjection prompt for director mode."""
    return jsonify({"prompt": DEFAULT_SETTINGS['prompts']['interjection_prompt_mode']})


# ============================================
# Character Import/Export
# ============================================

@config_bp.route('/api/characters/export', methods=['GET'])
def export_characters():
    """Export character settings (editor guidance + viseme scales)."""
    settings = load_settings()
    char_data = {
        "editor_guidance": settings.get('prompts', {}).get('editor_guidance', {}),
        "viseme_scales": settings.get('lipsync', {}).get('npc_scales', {})
    }
    response = Response(
        json.dumps(char_data, indent=2),
        mimetype='application/json',
        headers={'Content-Disposition': 'attachment;filename=character_settings.json'}
    )
    return response


@config_bp.route('/api/characters/import', methods=['POST'])
def import_characters():
    """Import character settings, merging with existing."""
    try:
        data = request.get_json()
        if not isinstance(data, dict):
            return jsonify({"error": "Invalid format - expected object"}), 400

        settings = load_settings()

        # Merge editor guidance (also accept legacy 'bios' key)
        guidance_data = data.get('editor_guidance') or data.get('bios') or {}
        if guidance_data and isinstance(guidance_data, dict):
            if 'prompts' not in settings:
                settings['prompts'] = {}
            if 'editor_guidance' not in settings['prompts']:
                settings['prompts']['editor_guidance'] = {}
            settings['prompts']['editor_guidance'].update(guidance_data)

        # Merge viseme scales
        if 'viseme_scales' in data and isinstance(data['viseme_scales'], dict):
            if 'lipsync' not in settings:
                settings['lipsync'] = {}
            if 'npc_scales' not in settings['lipsync']:
                settings['lipsync']['npc_scales'] = {}
            settings['lipsync']['npc_scales'].update(data['viseme_scales'])

        if save_settings(settings):
            guidance_count = len(guidance_data)
            scale_count = len(data.get('viseme_scales', {}))
            print(f"[Settings] Imported {guidance_count} character guidance, {scale_count} viseme scales")
            return jsonify({"status": "ok", "editor_guidance": guidance_count, "viseme_scales": scale_count})
        return jsonify({"error": "Failed to save"}), 500
    except Exception as e:
        print(f"[Settings] Character import error: {e}")
        return jsonify({"error": str(e)}), 400


# ============================================
# System Events
# ============================================

@config_bp.route('/api/system-events', methods=['GET'])
def get_system_events():
    """Get recent system events."""
    limit = request.args.get('limit', 100, type=int)
    events = event_logger.get_recent_events(limit=limit)
    return jsonify(events)


@config_bp.route('/api/system-events', methods=['DELETE'])
def clear_system_events():
    """Clear all system events."""
    event_logger.clear_events()
    return jsonify({"status": "cleared"})


# ============================================
# TTS Status Endpoints
# ============================================

@config_bp.route('/api/logs/server', methods=['GET'])
def get_server_logs():
    """
    Read latest server session log for troubleshooting support.
    Logs are in logs/server_YYYY-MM-DD_N.log format.
    """
    import glob

    logs_dir = os.path.join(SONORUS_DIR, "logs")
    if not os.path.exists(logs_dir):
        return jsonify({"error": "logs folder not found", "content": ""}), 404

    # Find all server logs and get the most recent by modification time
    log_files = glob.glob(os.path.join(logs_dir, "server_*.log"))
    if not log_files:
        return jsonify({"error": "No server logs found (server restart required)", "content": ""}), 404

    latest_log = max(log_files, key=os.path.getmtime)

    try:
        with open(latest_log, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()

        # Filter noisy log spam
        filtered = []
        for line in lines:
            line = line.rstrip()
            if not line:
                continue
            if line.startswith('127.0.0.1'):
                continue
            if '[Socket] Game context received:' in line:
                continue
            filtered.append(line)

        content = '\n'.join(filtered)
        return jsonify({"content": content})
    except Exception as e:
        return jsonify({"error": str(e), "content": ""}), 500


@config_bp.route('/api/logs', methods=['DELETE'])
def delete_logs():
    """
    Delete all log files in the logs folder.
    """
    import glob

    logs_dir = os.path.join(SONORUS_DIR, "logs")
    if not os.path.exists(logs_dir):
        return jsonify({"deleted": 0})

    try:
        log_files = glob.glob(os.path.join(logs_dir, "*.log")) + glob.glob(os.path.join(logs_dir, "*.txt"))
        deleted = 0
        for log_file in log_files:
            try:
                os.remove(log_file)
                deleted += 1
            except Exception:
                pass  # Skip files that can't be deleted (e.g., current session log)
        return jsonify({"deleted": deleted})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@config_bp.route('/api/logs/client', methods=['GET'])
def get_client_logs():
    """
    Read UE4SS.log file for troubleshooting support.
    The log file is at ../ue4ss/UE4SS.log relative to sonorus directory.
    Only returns logs from after the current server session started.
    """
    import glob
    import re
    from datetime import datetime

    # Path: sonorus/../ue4ss/UE4SS.log = Win64/ue4ss/UE4SS.log
    log_path = os.path.join(SONORUS_DIR, "..", "ue4ss", "UE4SS.log")
    log_path = os.path.normpath(log_path)

    if not os.path.exists(log_path):
        return jsonify({"error": "UE4SS.log not found", "content": ""}), 404

    # Get the current server session start time from the latest server log
    session_start = None
    logs_dir = os.path.join(SONORUS_DIR, "logs")
    if os.path.exists(logs_dir):
        log_files = glob.glob(os.path.join(logs_dir, "server_*.log"))
        if log_files:
            latest_log = max(log_files, key=os.path.getmtime)
            # Use file creation time (or mtime as fallback)
            try:
                session_start = datetime.fromtimestamp(os.path.getctime(latest_log))
            except:
                session_start = datetime.fromtimestamp(os.path.getmtime(latest_log))

    try:
        # Read with error handling for encoding issues
        with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()

        # Skip UE4SS initialization spam - only return content after member offsets
        marker = "##### MEMBER OFFSETS END #####"
        if marker in content:
            content = content.split(marker, 1)[1].lstrip('\n')

        # Fix missing newlines - UE4SS sometimes concatenates log lines
        content = re.sub(r'(?<!\n)(\[\d{4}-\d{2}-\d{2})', r'\n\1', content)

        # Filter to only lines after session start
        if session_start:
            lines = content.split('\n')
            filtered = []
            timestamp_pattern = re.compile(r'^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')
            for line in lines:
                match = timestamp_pattern.match(line)
                if match:
                    try:
                        line_time = datetime.strptime(match.group(1), '%Y-%m-%d %H:%M:%S')
                        if line_time >= session_start:
                            filtered.append(line)
                    except:
                        filtered.append(line)  # Keep lines with unparseable timestamps
                elif filtered:  # Keep continuation lines after we've started including
                    filtered.append(line)
            content = '\n'.join(filtered)

        return jsonify({"content": content})
    except Exception as e:
        return jsonify({"error": str(e), "content": ""}), 500


@config_bp.route('/api/tts/vram-status', methods=['GET'])
def get_vram_status():
    """
    Get VRAM status for NeuTTS GPU configuration.

    Returns:
    {
        "cuda_available": bool,
        "vram_free_gb": float | null,
        "vram_total_gb": float | null,
        "vram_used_gb": float | null,
        "model_loaded": bool,
        "model_on_gpu": bool,
        "server_device": "cpu" | "cuda"
    }
    """
    settings = load_settings()
    server_device = settings.get('tts', {}).get('neutts', {}).get('device', 'cpu')

    result = {
        "cuda_available": False,
        "vram_free_gb": None,
        "vram_total_gb": None,
        "vram_used_gb": None,
        "model_loaded": False,
        "model_on_gpu": False,
        "server_device": server_device
    }

    # Check CUDA availability and get VRAM info
    try:
        import torch
        result["cuda_available"] = torch.cuda.is_available()

        if result["cuda_available"]:
            free, total = torch.cuda.mem_get_info()
            result["vram_free_gb"] = round(free / 1e9, 2)
            result["vram_total_gb"] = round(total / 1e9, 2)
            result["vram_used_gb"] = round((total - free) / 1e9, 2)
    except ImportError:
        pass
    except Exception as e:
        print(f"[Config] Error getting CUDA info: {e}")

    # Check if NeuTTS model is loaded (REMOVED)
    # result["model_loaded"] = False

    return jsonify(result)


@config_bp.route('/api/system/ram-status', methods=['GET'])
def get_ram_status():
    """Get system RAM status for local models (Parakeet STT, Pocket TTS)."""
    import ctypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    try:
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(stat)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))

        total = stat.ullTotalPhys
        free = stat.ullAvailPhys
        used = total - free

        return jsonify({
            "ram_free_gb": round(free / 1e9, 2),
            "ram_total_gb": round(total / 1e9, 2),
            "ram_used_gb": round(used / 1e9, 2)
        })
    except Exception as e:
        print(f"[Config] Error getting RAM info: {e}")
        return jsonify({
            "ram_free_gb": 0,
            "ram_total_gb": 0,
            "ram_used_gb": 0
        })


# ============================================
# Game Mods Endpoints
# ============================================

@config_bp.route('/api/game-mods/status', methods=['GET'])
def get_game_mods_status():
    """
    Get status of all game mods (installation + live data).

    Returns:
    {
        "mods": {
            "house_points": {
                "installed": bool,
                "name": str,
                "icon": str,
                "description": str,
                "settings": {...},
                "live_data": {...}
            }
        }
    }
    """
    from utils import mods

    settings = load_settings()
    mod_status = mods.get_all_mod_status()

    return jsonify({
        "mods": {
            mod_id: {
                "installed": status["installed"],
                "name": status["info"]["name"],
                "icon": status["info"]["icon"],
                "description": status["info"]["description"],
                "settings": settings.get("game_mods", {}).get(mod_id, {}),
                "live_data": status["live_data"]
            }
            for mod_id, status in mod_status.items()
        }
    })


@config_bp.route('/api/game-mods/settings', methods=['POST'])
def save_game_mods_settings():
    """
    Save game mod settings.

    Body: {"house_points": {"context_enabled": true, ...}}
    """
    new_mod_settings = request.get_json() or {}
    settings = load_settings()

    # Merge with existing game_mods settings
    if "game_mods" not in settings:
        settings["game_mods"] = {}

    for mod_id, mod_settings in new_mod_settings.items():
        if mod_id not in settings["game_mods"]:
            settings["game_mods"][mod_id] = {}
        settings["game_mods"][mod_id].update(mod_settings)

    if save_settings(settings):
        print(f"[Settings] Game mod settings updated: {list(new_mod_settings.keys())}")
        return jsonify({"status": "ok"})
    return jsonify({"error": "Failed to save"}), 500


# ============================================
# VR Plugin Endpoints
# ============================================

_UEVR_PLUGIN_FILENAME = "sonorus_vr_bridge.dll"


def _get_uevr_plugins_folder():
    """Get the UEVR HogwartsLegacy plugins folder path."""
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        return None
    return os.path.join(appdata, "UnrealVRMod", "HogwartsLegacy", "plugins")


def _get_source_dll_path():
    """Get the path to the bundled DLL in sonorus/vr/uevr_plugin/."""
    return os.path.join(SONORUS_DIR, "vr", "uevr_plugin", _UEVR_PLUGIN_FILENAME)


@config_bp.route('/api/vr/plugin-status', methods=['GET'])
def get_vr_plugin_status():
    """Check if the Sonorus UEVR plugin is installed."""
    source_path = _get_source_dll_path()
    source_exists = os.path.isfile(source_path)

    target_dir = _get_uevr_plugins_folder()
    if not target_dir:
        return jsonify({
            "installed": False,
            "source_available": source_exists,
            "target_dir": None,
            "error": "Could not determine APPDATA path"
        })

    target_path = os.path.join(target_dir, _UEVR_PLUGIN_FILENAME)
    installed = os.path.isfile(target_path)

    return jsonify({
        "installed": installed,
        "source_available": source_exists,
        "target_dir": target_dir,
    })


@config_bp.route('/api/vr/install-plugin', methods=['POST'])
def install_vr_plugin():
    """Copy the Sonorus UEVR plugin DLL to the UEVR plugins folder."""
    source_path = _get_source_dll_path()
    if not os.path.isfile(source_path):
        return jsonify({"error": "Source DLL not found in sonorus/vr/uevr_plugin/"}), 404

    target_dir = _get_uevr_plugins_folder()
    if not target_dir:
        return jsonify({"error": "Could not determine APPDATA path"}), 500

    try:
        os.makedirs(target_dir, exist_ok=True)
        target_path = os.path.join(target_dir, _UEVR_PLUGIN_FILENAME)
        shutil.copy2(source_path, target_path)
        print(f"[VR] Installed plugin: {target_path}")
        return jsonify({"status": "ok", "path": target_path})
    except Exception as e:
        print(f"[VR] Failed to install plugin: {e}")
        return jsonify({"error": str(e)}), 500
