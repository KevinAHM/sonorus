"""
Config API endpoints for Sonorus settings management.

Handles settings CRUD, character import/export, system events.
"""

import os
import json
import shutil
import subprocess
from pathlib import Path

from flask import Blueprint, request, jsonify, send_file, Response

from utils.settings import (
    SONORUS_DIR,
    DATA_DIR,
    SETTINGS_FILE,
    CONFIG_HTML,
    DEFAULT_SETTINGS,
    load_settings,
    save_settings,
    deep_merge,
)
from utils import player_context, player_profile_db
from utils.localization import get_display_name

import llm
import event_logger
from constants import SONORUS_MOD_PACKAGE_ID, VR_BRIDGE_DLL_URL, VR_BRIDGE_DLL_SHA256
from utils.package_id_check import read_package_id

config_bp = Blueprint('config', __name__)

# These will be set by server.py to provide dependencies
_lua_socket = None


def set_lua_socket(socket):
    """Set the lua socket instance for tracking settings sync."""
    global _lua_socket
    _lua_socket = socket


def _load_saved_settings_file():
    try:
        if not os.path.exists(SETTINGS_FILE):
            return {}
        with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        print(f"[Config] Failed to read saved settings file: {exc}")
        return {}


def _preserve_ignored_player_bio_settings(settings, saved_settings):
    """Keep legacy Player bio keys unchanged while ignoring them at runtime."""
    prompts = settings.setdefault('prompts', {})
    saved_prompts = saved_settings.get('prompts', {}) if isinstance(saved_settings, dict) else {}
    if not isinstance(saved_prompts, dict):
        saved_prompts = {}

    static_bios = prompts.setdefault('static_bios', {})
    saved_static_bios = saved_prompts.get('static_bios', {})
    if not isinstance(saved_static_bios, dict):
        saved_static_bios = {}
    if isinstance(static_bios, dict):
        if 'Player' in saved_static_bios:
            static_bios['Player'] = saved_static_bios.get('Player')
        else:
            static_bios.pop('Player', None)

    editor_guidance = prompts.setdefault('editor_guidance', {})
    saved_editor_guidance = saved_prompts.get('editor_guidance', {})
    if not isinstance(saved_editor_guidance, dict):
        saved_editor_guidance = {}
    if isinstance(editor_guidance, dict):
        if 'Player' in saved_editor_guidance:
            editor_guidance['Player'] = saved_editor_guidance.get('Player')
        else:
            editor_guidance.pop('Player', None)

    if 'player_static_bios' in saved_prompts:
        prompts['player_static_bios'] = saved_prompts.get('player_static_bios')
    else:
        prompts.pop('player_static_bios', None)


def _preserve_saved_migrations(settings, saved_settings):
    saved_migrations = saved_settings.get('migrations', {}) if isinstance(saved_settings, dict) else {}
    if not isinstance(saved_migrations, dict) or not saved_migrations:
        return
    migrations = settings.setdefault('migrations', {})
    if not isinstance(migrations, dict):
        migrations = {}
        settings['migrations'] = migrations
    migrations.update(saved_migrations)


def _hide_legacy_player_bio_settings(settings):
    prompts = settings.setdefault('prompts', {})
    prompts.pop('player_static_bios', None)
    static_bios = prompts.setdefault('static_bios', {})
    if isinstance(static_bios, dict):
        static_bios.pop('Player', None)
    editor_guidance = prompts.setdefault('editor_guidance', {})
    if isinstance(editor_guidance, dict):
        editor_guidance.pop('Player', None)


def _set_player_bio_for_owner(player_owner, player_bio):
    if not player_owner:
        return False
    player_owner = player_context.normalize_player_name(player_owner)
    if not player_owner:
        return False
    current_player = player_context.get_context().current_player_name
    if current_player and player_owner == current_player:
        return player_profile_db.set_player_static_bio(player_bio)
    return player_profile_db.set_player_static_bio_for_player(player_owner, player_bio)


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


@config_bp.route('/images/<path:filename>')
def serve_images(filename):
    """Serve static image files from sonorus/images/ folder."""
    images_dir = os.path.join(SONORUS_DIR, "images")
    img_file = os.path.join(images_dir, filename)
    if os.path.exists(img_file):
        return send_file(img_file)
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
    settings = load_settings(raw=True)
    masked = json.loads(json.dumps(settings))
    try:
        ctx = player_context.get_context()
        current_player = ctx.current_player_name
        current_player_display = ctx.current_player_display_name
        player_ready = ctx.is_ready()
    except Exception:
        current_player = None
        current_player_display = None
        player_ready = False

    _hide_legacy_player_bio_settings(masked)
    prompts = masked.setdefault('prompts', {})
    static_bios = prompts.setdefault('static_bios', {})
    if isinstance(static_bios, dict) and player_ready and current_player:
        static_bios['Player'] = player_profile_db.get_player_static_bio()
    if masked.get('llm', {}).get('api_key'):
        masked['llm']['api_key'] = '********'
    llm_providers_with_keys = ['gemini', 'openrouter', 'openai', 'ollama', 'llamacpp']
    for provider in llm_providers_with_keys:
        if masked.get('llm', {}).get(provider, {}).get('api_key'):
            masked['llm'][provider]['api_key'] = '********'
    tts_providers_with_keys = ['inworld', 'elevenlabs', 'openai', 'omnivoice_api']
    for provider in tts_providers_with_keys:
        if masked.get('tts', {}).get(provider, {}).get('api_key'):
            masked['tts'][provider]['api_key'] = '********'
    masked['player_context'] = {
        "ready": player_ready,
        "player_name": current_player_display or current_player or "",
        "normalized_name": current_player or "",
    }
    return jsonify(masked)


@config_bp.route('/api/player-context', methods=['GET'])
def get_player_context():
    """Return the active player scope used for per-player data."""
    try:
        ctx = player_context.get_context()
        normalized_name = ctx.current_player_name or ""
        player_name = ctx.current_player_display_name or normalized_name
        player_data_dir = ctx.player_data_dir or ""
        ready = ctx.is_ready()
    except Exception as exc:
        print(f"[Config] Failed to read player context: {exc}")
        normalized_name = ""
        player_name = ""
        player_data_dir = ""
        ready = False

    game_player_name = ""
    game_normalized_name = ""
    mismatch = False
    if _lua_socket:
        try:
            game_context = _lua_socket.get_game_context() or {}
            game_player_name = str(game_context.get('playerName') or '').strip()
            if game_player_name:
                game_normalized_name = player_context.normalize_player_name(game_player_name)
                mismatch = bool(normalized_name and game_normalized_name and normalized_name != game_normalized_name)
        except Exception as exc:
            print(f"[Config] Failed to compare game player context: {exc}")

    return jsonify({
        "ready": ready,
        "player_name": player_name,
        "normalized_name": normalized_name,
        "player_data_dir": player_data_dir,
        "game_player_name": game_player_name,
        "game_normalized_name": game_normalized_name,
        "mismatch": mismatch,
    })


@config_bp.route('/api/character-display-names', methods=['GET'])
def get_character_display_names():
    """Return NPC ID -> display name mapping from voice manifest."""
    settings = load_settings()
    language = settings.get('setup', {}).get('language', 'EN_US')
    from constants import get_voice_language
    voice_language = get_voice_language(language)
    if voice_language == 'EN_US':
        manifest_path = os.path.join(DATA_DIR, "voice_manifest.json")
    else:
        manifest_path = os.path.join(DATA_DIR, f"voice_manifest_{voice_language.lower()}.json")
    try:
        with open(manifest_path, 'r', encoding='utf-8') as f:
            manifest = json.load(f)
        voices = manifest.get('voices', {})
        return jsonify({npc_id: get_display_name(npc_id) for npc_id in voices})
    except FileNotFoundError:
        return jsonify({})
    except Exception as exc:
        print(f"[Config] Failed to build character display names: {exc}")
        return jsonify({})


@config_bp.route('/api/model-capabilities', methods=['GET'])
def get_model_capabilities():
    """Return model capabilities keyed by base model name for frontend reasoning toggle."""
    return jsonify(llm.get_model_capabilities_for_frontend())


@config_bp.route('/api/openrouter-models', methods=['GET'])
def get_openrouter_models():
    """Return cached OpenRouter model IDs for frontend autocomplete."""
    return jsonify(llm.get_openrouter_model_ids_for_frontend())


@config_bp.route('/api/openrouter-embedding-models', methods=['GET'])
def get_openrouter_embedding_models():
    """Return cached OpenRouter embedding model IDs for frontend autocomplete."""
    return jsonify(llm.get_openrouter_embedding_model_ids_for_frontend())


@config_bp.route('/api/openrouter-vision-models', methods=['GET'])
def get_openrouter_vision_models():
    """Return cached OpenRouter vision-capable model IDs for frontend autocomplete."""
    return jsonify(llm.get_openrouter_vision_model_ids_for_frontend())


@config_bp.route('/api/openrouter-model-providers', methods=['GET'])
def get_openrouter_model_providers():
    """Return OpenRouter provider endpoint metadata for one model."""
    model = (request.args.get('model') or '').strip()
    force_refresh = request.args.get('refresh') in ('1', 'true', 'yes')
    if not model:
        return jsonify([])
    return jsonify(llm.get_openrouter_model_providers_for_frontend(model, force_refresh=force_refresh))


def _iter_mod_utoc_paths():
    """Yield installed cooked mod containers from known mod install locations."""
    seen = set()

    game_root = Path(SONORUS_DIR).parent.parent.parent
    paks_root = game_root / "Content" / "Paks"
    candidate_roots = [
        paks_root / "mods",
        paks_root / "~mods",
        paks_root / "LogicMods",
    ]

    mods_root = game_root / "Mods"
    if mods_root.exists():
        for path in mods_root.glob("*/Content/Paks/WindowsNoEditor"):
            candidate_roots.append(path)

    for root in candidate_roots:
        if not root.exists():
            continue
        for path in root.rglob("*.utoc"):
            resolved = str(path.resolve())
            if resolved in seen:
                continue
            seen.add(resolved)
            yield path


@config_bp.route('/api/mod-package-conflicts', methods=['GET'])
def get_mod_package_conflicts():
    """Scan installed cooked mod containers and report duplicate package IDs."""
    try:
        game_root = Path(SONORUS_DIR).parent.parent.parent
        paks_root = game_root / "Content" / "Paks"
        scan_roots = [
            paks_root / "mods",
            paks_root / "~mods",
            paks_root / "LogicMods",
        ]
        mods_root = game_root / "Mods"
        if mods_root.exists():
            scan_roots.extend(mods_root.glob("*/Content/Paks/WindowsNoEditor"))

        entries = []
        for path in _iter_mod_utoc_paths():
            try:
                entries.append(read_package_id(path))
            except Exception as exc:
                entries.append({
                    "name": path.stem,
                    "utoc_path": str(path.resolve()),
                    "ucas_path": str(path.with_suffix(".ucas").resolve()) if path.with_suffix(".ucas").exists() else None,
                    "error": str(exc),
                })

        valid_entries = [entry for entry in entries if not isinstance(entry, dict)]
        grouped = {}
        for entry in valid_entries:
            grouped.setdefault(entry.package_id, []).append(entry)

        conflicts = []
        for package_id, group in sorted(grouped.items(), key=lambda item: item[0]):
            if len(group) < 2:
                continue
            conflicts.append({
                "package_id": package_id,
                "package_id_hex": f"0x{package_id:016x}",
                "mods": [
                    {
                        "name": entry.name,
                        "utoc_path": entry.utoc_path,
                        "ucas_path": entry.ucas_path,
                        "source": entry.source,
                    }
                    for entry in sorted(group, key=lambda item: item.name.lower())
                ],
            })

        sonorus_conflicts = []
        if SONORUS_MOD_PACKAGE_ID is not None:
            sonorus_group = grouped.get(SONORUS_MOD_PACKAGE_ID, [])
            if sonorus_group:
                sonorus_conflicts = [
                    {
                        "name": entry.name,
                        "utoc_path": entry.utoc_path,
                        "ucas_path": entry.ucas_path,
                        "source": entry.source,
                    }
                    for entry in sorted(sonorus_group, key=lambda item: item.name.lower())
                ]

        return jsonify({
            "scan_roots": [str(path.resolve()) for path in scan_roots if path.exists()],
            "entries_scanned": len(valid_entries),
            "entries_with_errors": [entry for entry in entries if isinstance(entry, dict)],
            "conflicts": conflicts,
            "sonorus_package_id": SONORUS_MOD_PACKAGE_ID,
            "sonorus_package_id_hex": f"0x{SONORUS_MOD_PACKAGE_ID:016x}" if SONORUS_MOD_PACKAGE_ID is not None else None,
            "sonorus_conflicts": sonorus_conflicts,
        })
    except Exception as exc:
        print(f"[Config] Failed to scan mod package conflicts: {exc}")
        return jsonify({"error": str(exc)}), 500


@config_bp.route('/api/config', methods=['POST'])
def save_config():
    """Save settings with hot-reload for certain changes."""
    new_settings = request.get_json() or {}
    new_settings.pop('character_display_names', None)
    submitted_player_context = new_settings.pop('player_context', {}) or {}
    submitted_player = submitted_player_context.get('normalized_name') if isinstance(submitted_player_context, dict) else None
    submitted_player = player_context.normalize_player_name(submitted_player) if submitted_player else None
    existing = load_settings(raw=True)
    saved_settings = _load_saved_settings_file()
    try:
        current_player = player_context.get_context().current_player_name
    except Exception:
        current_player = None

    prompts = new_settings.setdefault('prompts', {})
    static_bios = prompts.setdefault('static_bios', {})
    player_card_bio = ""
    player_card_present = False
    if isinstance(static_bios, dict):
        if 'Player' in static_bios:
            player_card_bio = static_bios.pop('Player', '') or ""
            player_card_present = True
    editor_guidance = prompts.get('editor_guidance', {})
    if isinstance(editor_guidance, dict):
        editor_guidance.pop('Player', None)
    prompts.pop('player_static_bios', None)

    player_bio_owner = submitted_player or current_player
    if player_card_present and player_bio_owner:
        if not _set_player_bio_for_owner(player_bio_owner, player_card_bio):
            return jsonify({"error": "Failed to save player bio"}), 500

    if new_settings.get('llm', {}).get('api_key') == '********':
        if 'llm' not in new_settings:
            new_settings['llm'] = {}
        new_settings['llm']['api_key'] = existing.get('llm', {}).get('api_key', '')
    llm_providers_with_keys = ['gemini', 'openrouter', 'openai', 'ollama', 'llamacpp']
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
    tts_providers_with_keys = ['inworld', 'elevenlabs', 'openai', 'omnivoice_api']
    submitted_tts = new_settings.get('tts', {})
    for provider in tts_providers_with_keys:
        provider_submitted = isinstance(submitted_tts, dict) and provider in submitted_tts
        new_key = submitted_tts.get(provider, {}).get('api_key', '') if provider_submitted else ''
        existing_key = existing.get('tts', {}).get(provider, {}).get('api_key', '')

        if new_key == '********':
            # Masked value - preserve existing key
            if 'tts' not in new_settings:
                new_settings['tts'] = {}
            if provider not in new_settings['tts']:
                new_settings['tts'][provider] = {}
            new_settings['tts'][provider]['api_key'] = existing_key
        elif provider_submitted and new_key != existing_key:
            # API key changed - mark for cache refresh
            tts_providers_changed.append(provider)
            print(f"[Settings] API key changed for TTS provider: {provider}")

    new_omni_api_url = new_settings.get('tts', {}).get('omnivoice_api', {}).get('api_url', '')
    existing_omni_api_url = existing.get('tts', {}).get('omnivoice_api', {}).get('api_url', '')
    if (
        new_tts_provider == existing_tts_provider == 'omnivoice_api'
        and new_omni_api_url
        and new_omni_api_url != existing_omni_api_url
        and 'omnivoice_api' not in tts_providers_changed
    ):
        tts_providers_changed.append('omnivoice_api')
        print("[Settings] API URL changed for TTS provider: omnivoice_api")

    # Inworld derives the workspace from the API key. Drop legacy workspace_id
    # values so a stale "default" entry cannot affect future config saves.
    new_settings.get('tts', {}).get('inworld', {}).pop('workspace_id', None)

    # Preserve setup test flags that are set by test endpoints, not the frontend.
    # The frontend config object doesn't get updated when tests pass (they save directly
    # to settings.json), so a config save would wipe the flags without this preservation.
    existing_setup = existing.get('setup', {})
    new_setup = new_settings.get('setup', {})
    for key in ('tts_tested', 'tts_test_language', 'tts_test_provider', 'llm_tested', 'llm_test_provider'):
        if key in existing_setup and key not in new_setup:
            new_setup[key] = existing_setup[key]
    if new_setup:
        new_settings['setup'] = new_setup

    new_llm = new_settings.get('llm', {})
    existing_llm = existing.get('llm', {})
    llm_provider_switched = new_llm.get('provider', '') and new_llm.get('provider') != existing_llm.get('provider', '')

    llm_client_changed = (
        llm_provider_switched or
        new_llm.get('openrouter', {}).get('api_key') != existing_llm.get('openrouter', {}).get('api_key') or
        new_llm.get('openai', {}).get('api_key') != existing_llm.get('openai', {}).get('api_key') or
        new_llm.get('openai', {}).get('api_url') != existing_llm.get('openai', {}).get('api_url') or
        new_llm.get('ollama', {}).get('api_key') != existing_llm.get('ollama', {}).get('api_key') or
        new_llm.get('ollama', {}).get('api_url') != existing_llm.get('ollama', {}).get('api_url') or
        new_llm.get('llamacpp', {}).get('api_key') != existing_llm.get('llamacpp', {}).get('api_key') or
        new_llm.get('llamacpp', {}).get('api_url') != existing_llm.get('llamacpp', {}).get('api_url') or
        new_llm.get('gemini', {}).get('api_key') != existing_llm.get('gemini', {}).get('api_key')
    )

    active_tts_provider = new_tts_provider or existing_tts_provider
    new_omnivoice_device = new_settings.get('tts', {}).get('omnivoice', {}).get('device', 'auto')
    existing_omnivoice_device = existing.get('tts', {}).get('omnivoice', {}).get('device', 'auto')
    omnivoice_device_changed = (
        active_tts_provider == 'omnivoice'
        and new_omnivoice_device != existing_omnivoice_device
    )
    active_tts_changed = (
        tts_provider_switched or
        active_tts_provider in tts_providers_changed or
        omnivoice_device_changed
    )

    if active_tts_changed:
        new_setup['tts_tested'] = False
        new_setup['tts_test_provider'] = active_tts_provider

    if llm_client_changed:
        new_setup['llm_tested'] = False
        new_setup['llm_test_provider'] = new_llm.get('provider', existing_llm.get('provider', 'gemini'))

    if new_setup:
        new_settings['setup'] = new_setup

    merged = deep_merge(DEFAULT_SETTINGS.copy(), new_settings)
    merged.get('tts', {}).get('inworld', {}).pop('workspace_id', None)
    _preserve_ignored_player_bio_settings(merged, saved_settings)
    _preserve_saved_migrations(merged, saved_settings)

    # Strip prompt values that match defaults so code updates take effect.
    # Only persist prompts users have actually customized.
    merged_prompts = merged.get('prompts', {})
    default_prompts = DEFAULT_SETTINGS.get('prompts', {})
    for key in ('default', 'scene_continuation', 'interjection_prompt_mode',
                 'owl_mail_classifier', 'owl_mail_letter', 'owl_board_thread', 'owl_board_reply'):
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

                if existing_tts_provider == 'omnivoice':
                    try:
                        from services.omnivoice_engine import unload as unload_omnivoice
                        unload_omnivoice()
                        print("[Settings] Unloaded OmniVoice models")
                    except Exception as e:
                        print(f"[Settings] Error unloading OmniVoice: {e}")

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

                elif new_tts_provider == 'omnivoice' and _is_torch_installed() and _count_untokenized_voices() == 0:
                    try:
                        from services.omnivoice_engine import warm_up as warmup_omnivoice
                        import threading
                        def preload_omnivoice():
                            try:
                                warmup_omnivoice()
                                print("[Settings] OmniVoice models preloaded")
                            except Exception as e:
                                print(f"[Settings] OmniVoice preload failed: {e}")
                        threading.Thread(target=preload_omnivoice, daemon=True).start()
                    except Exception as e:
                        print(f"[Settings] Error starting OmniVoice preload: {e}")

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

        if omnivoice_device_changed and not tts_provider_switched:
            print(f"[Settings] OmniVoice GPU changed: {existing_omnivoice_device} -> {new_omnivoice_device}")
            try:
                from services import tts
                from services.omnivoice_engine import unload as unload_omnivoice
                unload_omnivoice()
                tts.clear_provider_cache('omnivoice')
                print("[Settings] Unloaded OmniVoice models for GPU change")

                if _is_torch_installed() and _count_untokenized_voices() == 0:
                    import threading
                    from services.omnivoice_engine import warm_up as warmup_omnivoice
                    def preload_omnivoice_after_gpu_change():
                        try:
                            warmup_omnivoice()
                            print("[Settings] OmniVoice models preloaded after GPU change")
                        except Exception as e:
                            print(f"[Settings] OmniVoice preload after GPU change failed: {e}")
                    threading.Thread(target=preload_omnivoice_after_gpu_change, daemon=True).start()
            except Exception as e:
                print(f"[Settings] Error reloading OmniVoice after GPU change: {e}")

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
                if new_stt_provider == 'deepgram':
                    try:
                        from services.deepgram_stt import warm_up as warmup_fn
                        warmup_fn()  # Already non-blocking internally
                    except Exception as e:
                        print(f"[Settings] Error starting Deepgram STT preload: {e}")
                elif new_stt_provider == 'parakeet':
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

        # Hot-reload first-person view hotkey
        if new_input.get('fpv_hotkey') != existing_input.get('fpv_hotkey'):
            try:
                from input import fpv_hotkey as fpv_hotkey_module
                fpv_hotkey_module.set_hotkey(new_input.get('fpv_hotkey', 'insert'))
                print(f"[Settings] FPV hotkey updated: {new_input.get('fpv_hotkey')}")
            except Exception as e:
                print(f"[Settings] Error updating FPV hotkey: {e}")

        # Hot-reload owl post hotkey
        if new_input.get('owlpost_hotkey') != existing_input.get('owlpost_hotkey'):
            try:
                from input import owlpost_hotkey as owlpost_hotkey_module
                owlpost_hotkey_module.set_hotkey(new_input.get('owlpost_hotkey', 'backquote'))
                print(f"[Settings] Owl Post hotkey updated: {new_input.get('owlpost_hotkey')}")
            except Exception as e:
                print(f"[Settings] Error updating owl post hotkey: {e}")

        # Hot-reload memory settings
        new_memory = new_settings.get('memory', {})
        existing_memory = existing.get('memory', {})

        # Check if any memory-related settings changed
        memory_connection_changed = (
            # Memory enabled/disabled
            new_memory.get('enabled') != existing_memory.get('enabled') or
            # LLM provider changed (affects embedder type)
            new_llm.get('provider') != existing_llm.get('provider') or
            new_memory.get('embedding_model') != existing_memory.get('embedding_model') or
            # Memory models changed
            new_memory.get('graphiti_model') != existing_memory.get('graphiti_model') or
            new_memory.get('graphiti_small_model') != existing_memory.get('graphiti_small_model') or
            new_memory.get('reranker_model') != existing_memory.get('reranker_model') or
            new_memory.get('max_concurrency') != existing_memory.get('max_concurrency') or
            # API keys changed (affects embedder initialization)
            new_llm.get('gemini', {}).get('api_key') != existing_llm.get('gemini', {}).get('api_key') or
            new_llm.get('openrouter', {}).get('api_key') != existing_llm.get('openrouter', {}).get('api_key') or
            new_llm.get('openai', {}).get('api_key') != existing_llm.get('openai', {}).get('api_key') or
            new_llm.get('ollama', {}).get('api_key') != existing_llm.get('ollama', {}).get('api_key') or
            new_llm.get('llamacpp', {}).get('api_key') != existing_llm.get('llamacpp', {}).get('api_key') or
            new_llm.get('gemini', {}).get('disable_memory') != existing_llm.get('gemini', {}).get('disable_memory') or
            new_llm.get('openrouter', {}).get('disable_memory') != existing_llm.get('openrouter', {}).get('disable_memory') or
            new_llm.get('openai', {}).get('disable_memory') != existing_llm.get('openai', {}).get('disable_memory') or
            new_llm.get('ollama', {}).get('disable_memory') != existing_llm.get('ollama', {}).get('disable_memory') or
            new_llm.get('llamacpp', {}).get('disable_memory') != existing_llm.get('llamacpp', {}).get('disable_memory') or
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
                from utils.memory import create_memory_snapshot, reset_memory_connection, init_memory
                print("[Settings] Memory settings changed - reinitializing...")
                if existing_memory.get('enabled', True):
                    create_memory_snapshot("before_memory_reconfigure", timeout=180.0, keep=10)
                reset_ok = reset_memory_connection()
                if not reset_ok:
                    raise RuntimeError("Memory shutdown did not complete cleanly")
                if new_memory.get('enabled', True):
                    init_memory()
            except ImportError:
                pass
            except Exception as e:
                print(f"[Settings] Error reinitializing memory: {e}")

        # Hot-reload LLM client cache (connection pooling)
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
            new_conversation.get('auto_mute_ambient') != existing_conversation.get('auto_mute_ambient') or
            new_dev.get('enabled') != existing_dev.get('enabled') or
            new_input.get('preview_lock') != existing_input.get('preview_lock') or
            new_time_dilation != existing_time_dilation or
            new_conversation.get('companion_follow_distance_m') != existing_conversation.get('companion_follow_distance_m') or
            new_conversation.get('followers_enabled') != existing_conversation.get('followers_enabled') or
            new_conversation.get('conversation_fpv') != existing_conversation.get('conversation_fpv') or
            new_conversation.get('conversation_fpv_transition') != existing_conversation.get('conversation_fpv_transition') or
            new_conversation.get('conversation_look_at_speaker') != existing_conversation.get('conversation_look_at_speaker')):
            if _lua_socket:
                _lua_socket.send_tracking_settings()

        # Resend blocklist if auto_mute_ambient changed (sends empty if disabled)
        if new_conversation.get('auto_mute_ambient') != existing_conversation.get('auto_mute_ambient'):
            if _lua_socket:
                _lua_socket.send_ambient_blocklist()

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


@config_bp.route('/api/config/defaults/owl-prompt/<key>', methods=['GET'])
def get_default_owl_prompt(key):
    """Get a default owl post prompt by key."""
    valid_keys = ('owl_mail_classifier', 'owl_mail_letter', 'owl_board_thread', 'owl_board_reply')
    if key not in valid_keys:
        return jsonify({"error": "Invalid prompt key"}), 400
    return jsonify({"prompt": DEFAULT_SETTINGS['prompts'].get(key, '')})


# ============================================
# Character Import/Export
# ============================================

@config_bp.route('/api/characters/export', methods=['GET'])
def export_characters():
    """Export character settings (static bios + editor guidance + viseme scales)."""
    settings = load_settings(raw=True)
    prompts = settings.get('prompts', {})
    static_bios = dict(prompts.get('static_bios', {})) if isinstance(prompts.get('static_bios', {}), dict) else {}
    editor_guidance = dict(prompts.get('editor_guidance', {})) if isinstance(prompts.get('editor_guidance', {}), dict) else {}
    static_bios.pop('Player', None)
    editor_guidance.pop('Player', None)
    try:
        if player_context.get_context().is_ready():
            static_bios['Player'] = player_profile_db.get_player_static_bio()
    except Exception as exc:
        print(f"[Settings] Failed to include player profile bio in export: {exc}")
    char_data = {
        "static_bios": static_bios,
        "editor_guidance": editor_guidance,
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

        settings = load_settings(raw=True)
        saved_settings = _load_saved_settings_file()

        static_bio_data = data.get('static_bios') or {}
        player_static_bio_data = data.get('player_static_bios') or {}
        guidance_data = data.get('editor_guidance') or {}
        legacy_bio_data = data.get('bios') or {}

        if legacy_bio_data and isinstance(legacy_bio_data, dict) and not static_bio_data and not guidance_data:
            split_static = {}
            for npc_id, text in legacy_bio_data.items():
                if not isinstance(text, str):
                    continue
                cleaned = text.strip()
                if not cleaned:
                    continue
                if str(npc_id).strip() == 'Player':
                    split_static['Player'] = cleaned
                else:
                    split_static[npc_id] = cleaned
            static_bio_data = split_static
            guidance_data = {}

        try:
            current_player = player_context.get_context().current_player_name
        except Exception:
            current_player = None

        player_profile_bio = ""
        player_profile_bio_present = False
        if isinstance(static_bio_data, dict):
            static_bio_data = dict(static_bio_data)
            if 'Player' in static_bio_data:
                player_profile_bio = str(static_bio_data.pop('Player') or "").strip()
                player_profile_bio_present = True
        else:
            static_bio_data = {}

        if isinstance(guidance_data, dict):
            guidance_data = dict(guidance_data)
            if 'Player' in guidance_data:
                if not player_profile_bio_present:
                    player_profile_bio = str(guidance_data.get('Player') or "").strip()
                    player_profile_bio_present = True
                guidance_data.pop('Player', None)
        else:
            guidance_data = {}

        if (
            not player_profile_bio_present
            and current_player
            and isinstance(player_static_bio_data, dict)
            and current_player in player_static_bio_data
        ):
            player_profile_bio = str(player_static_bio_data.get(current_player) or "").strip()
            player_profile_bio_present = True

        player_bio_count = 0
        if player_profile_bio_present and current_player:
            if not player_profile_db.set_player_static_bio(player_profile_bio):
                return jsonify({"error": "Failed to import player bio"}), 500
            player_bio_count = 1

        if static_bio_data and isinstance(static_bio_data, dict):
            if 'prompts' not in settings:
                settings['prompts'] = {}
            if 'static_bios' not in settings['prompts']:
                settings['prompts']['static_bios'] = {}
            settings['prompts']['static_bios'].update(static_bio_data)

        # Merge editor guidance
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

        _preserve_ignored_player_bio_settings(settings, saved_settings)
        _preserve_saved_migrations(settings, saved_settings)

        if save_settings(settings):
            static_bio_count = len(static_bio_data) + player_bio_count
            guidance_count = len(guidance_data)
            scale_count = len(data.get('viseme_scales', {}))
            print(f"[Settings] Imported {static_bio_count} static bios, {guidance_count} character guidance, {scale_count} viseme scales")
            return jsonify({
                "status": "ok",
                "static_bios": static_bio_count,
                "editor_guidance": guidance_count,
                "player_static_bio": player_bio_count,
                "viseme_scales": scale_count
            })
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


@config_bp.route('/api/system-events/costs', methods=['GET'])
def get_system_event_costs():
    """Get grouped LLM cost totals by feature and module."""
    timeframe = request.args.get('timeframe', 'today', type=str)
    breakdown = event_logger.get_cost_breakdown(timeframe=timeframe)
    return jsonify(breakdown)


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
    def _parse_cuda_device_index(device):
        if not device:
            return None
        device = str(device).strip().lower()
        if device.startswith("cuda:"):
            try:
                return int(device.split(":", 1)[1])
            except (TypeError, ValueError):
                return None
        return None

    settings = load_settings(raw=True)
    provider = settings.get('tts', {}).get('provider', 'inworld')
    requested_device = request.args.get('device')
    if provider == 'omnivoice':
        configured_device = settings.get('tts', {}).get('omnivoice', {}).get('device', 'auto')
        server_device = requested_device or configured_device or 'auto'
    else:
        server_device = settings.get('tts', {}).get('neutts', {}).get('device', 'cpu')

    result = {
        "cuda_available": False,
        "vram_free_gb": None,
        "vram_total_gb": None,
        "vram_used_gb": None,
        "model_loaded": False,
        "model_on_gpu": False,
        "server_device": server_device,
        "selected_device": None,
        "selected_gpu_index": None,
        "gpu_name": None,
        "gpus": [],
    }

    # Get system-wide VRAM via nvidia-smi (sees game + all processes, not just Python)
    try:
        from utils.gpu_info import detect_gpus
        gpus = detect_gpus(force_refresh=True, log=False)
        result["gpus"] = [
            {
                "index": gpu.index,
                "device": f"cuda:{gpu.index}",
                "name": gpu.name,
                "vram_total_gb": round(gpu.vram_total_mb / 1024, 2),
                "vram_free_gb": round(gpu.vram_free_mb / 1024, 2),
                "vram_used_gb": round(gpu.vram_used_mb / 1024, 2),
                "cuda_compatible": gpu.cuda_compatible,
            }
            for gpu in gpus
        ]

        selected_index = _parse_cuda_device_index(requested_device or server_device)
        if selected_index is None and provider == 'omnivoice':
            compatible = [gpu for gpu in gpus if gpu.cuda_compatible]
            candidates = compatible or gpus
            recommended = max(candidates, key=lambda gpu: gpu.vram_free_mb) if candidates else None
            selected_index = recommended.index if recommended else None

        selected_gpu = None
        if selected_index is not None:
            selected_gpu = next((gpu for gpu in gpus if gpu.index == selected_index), None)
        if selected_gpu is None and gpus:
            selected_gpu = gpus[0]

        if selected_gpu is not None:
            result["cuda_available"] = True
            result["vram_total_gb"] = round(selected_gpu.vram_total_mb / 1024, 2)
            result["vram_free_gb"] = round(selected_gpu.vram_free_mb / 1024, 2)
            result["vram_used_gb"] = round(selected_gpu.vram_used_mb / 1024, 2)
            result["selected_gpu_index"] = selected_gpu.index
            result["selected_device"] = f"cuda:{selected_gpu.index}"
            result["gpu_name"] = selected_gpu.name
    except Exception as e:
        print(f"[Config] Error getting VRAM info: {e}")

    # Check if OmniVoice model is loaded
    if provider == 'omnivoice':
        try:
            from services.omnivoice_engine import is_loaded
            result["model_loaded"] = is_loaded()
            result["model_on_gpu"] = is_loaded()
        except Exception:
            pass

    return jsonify(result)


# OmniVoice pretokenization progress (polled by /api/tts/omnivoice/status)
_omnivoice_setup_progress = {
    "status": "idle",     # idle | transcribing | tokenizing | done | error
    "total": 0,
    "completed": 0,
    "current": "",        # name of file currently being processed
}


def _is_tokenizable_voice(path) -> bool:
    """Check if an audio file should be tokenized for OmniVoice.

    Only process:
    - *_reference_15s.wav (standard 15s references)
    - Files with no _reference_Xs pattern (e.g. narrator.wav)

    Skip _reference_5s, _reference_60s, etc.
    """
    import re
    stem = path.stem
    # Check for _reference_Ns pattern
    match = re.search(r'_reference_(\d+)s$', stem)
    if match:
        # Only keep 15s references
        return match.group(1) == '15'
    # Also keep plain _reference (no duration suffix)
    # And files with no _reference pattern at all (e.g. narrator.wav)
    return True


def _count_untokenized_voices() -> int:
    """Count voice reference files missing .tokens.pt across all subdirs."""
    from pathlib import Path
    voice_dir = Path(__file__).resolve().parent.parent / "voice_references"
    if not voice_dir.exists():
        return 0
    audio_exts = {".wav", ".mp3", ".flac", ".m4a", ".ogg", ".opus"}
    count = 0
    dirs_to_check = [voice_dir] + [d for d in voice_dir.iterdir() if d.is_dir()]
    for check_dir in dirs_to_check:
        for path in check_dir.iterdir():
            if not path.is_file():
                continue
            if path.suffix.lower() not in audio_exts:
                continue
            if not _is_tokenizable_voice(path):
                continue
            if not path.with_suffix(".tokens.pt").exists():
                count += 1
    return count


def _is_torch_installed() -> bool:
    """Check if torch is fully installed — not just mid-install.

    find_spec('torch') returns True as soon as pip creates the package
    directory, long before the install completes. We verify a late-written
    DLL exists to confirm the install actually finished.
    """
    import importlib.util
    spec = importlib.util.find_spec("torch")
    if spec is None:
        return False
    try:
        torch_dir = os.path.dirname(spec.origin)
        # torch_cpu.dll is one of the last files written during install
        return os.path.exists(os.path.join(torch_dir, "lib", "torch_cpu.dll"))
    except Exception:
        return False


@config_bp.route('/api/tts/omnivoice/status', methods=['GET'])
def get_omnivoice_status():
    """OmniVoice provider status: GPU, deps, model, voices, STT."""
    from utils.gpu_info import get_gpu_info_dict

    deps_installed = _is_torch_installed()

    model_loaded = False
    if deps_installed:
        try:
            from services.omnivoice_engine import is_loaded
            model_loaded = is_loaded()
        except Exception:
            pass

    voices_needing_setup = _count_untokenized_voices()

    settings = load_settings(raw=True)
    stt_provider = settings.get('stt', {}).get('provider', 'none')
    stt_configured = stt_provider != 'none'
    gpu_info = get_gpu_info_dict()
    configured_device = settings.get('tts', {}).get('omnivoice', {}).get('device', 'auto')
    valid_devices = {gpu.get("device") for gpu in gpu_info.get("gpus", [])}
    selected_device = configured_device
    if not selected_device or selected_device == 'auto':
        selected_device = gpu_info.get('recommended_device') or 'cuda'
    elif selected_device not in valid_devices and selected_device != 'cuda':
        selected_device = 'cuda'

    return jsonify({
        "gpu": gpu_info,
        "omnivoice_device": configured_device or 'auto',
        "selected_device": selected_device,
        "deps_installed": deps_installed,
        "model_loaded": model_loaded,
        "voices_needing_setup": voices_needing_setup,
        "stt_configured": stt_configured,
        "setup_progress": _omnivoice_setup_progress,
    })


@config_bp.route('/api/tts/omnivoice/install-deps', methods=['POST'])
def install_omnivoice_deps():
    """Launch OmniVoice dependency installer in a visible console window."""
    from utils.gpu_info import is_cuda_compatible

    if _is_torch_installed():
        return jsonify({"status": "already_installed"}), 200

    if not is_cuda_compatible():
        return jsonify({"status": "error", "error": "No compatible NVIDIA GPU. CUDA >= 12.6 driver required."}), 400

    sonorus_dir = os.path.dirname(os.path.dirname(__file__))

    # Clear stale flag from any previous failed attempt
    flag_path = os.path.join(sonorus_dir, "data", ".omnivoice_deps_installed")
    if os.path.exists(flag_path):
        os.remove(flag_path)

    bat_path = os.path.join(sonorus_dir, "install_omnivoice.bat")
    if not os.path.exists(bat_path):
        return jsonify({"status": "error", "error": "install_omnivoice.bat not found"}), 500

    # Launch in a visible console window so the user can see download progress
    subprocess.Popen(
        ["cmd", "/c", "start", "OmniVoice Installer", bat_path],
        cwd=sonorus_dir,
    )

    return jsonify({"status": "installing"}), 202


@config_bp.route('/api/tts/omnivoice/install-status', methods=['GET'])
def get_omnivoice_install_status():
    """Check if background OmniVoice install has completed."""
    deps_installed = _is_torch_installed()
    flag_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", ".omnivoice_deps_installed")
    flag_exists = os.path.exists(flag_path)
    return jsonify({
        "deps_installed": deps_installed,
        "install_complete": flag_exists,
        "restart_needed": flag_exists and not deps_installed,
    })


def _collect_untokenized_voices():
    """Return list of audio file paths missing .tokens.pt across voice_references/ and subdirs."""
    from pathlib import Path
    voice_dir = Path(__file__).resolve().parent.parent / "voice_references"
    if not voice_dir.exists():
        return []
    audio_exts = {".wav", ".mp3", ".flac", ".m4a", ".ogg", ".opus"}
    missing = []
    dirs_to_check = [voice_dir] + [d for d in voice_dir.iterdir() if d.is_dir()]
    for check_dir in dirs_to_check:
        for path in check_dir.iterdir():
            if not path.is_file():
                continue
            if path.suffix.lower() not in audio_exts:
                continue
            if not _is_tokenizable_voice(path):
                continue
            if not path.with_suffix(".tokens.pt").exists():
                missing.append(path)
    return missing


def _transcribe_audio_file(audio_path):
    """Transcribe an audio file via the configured STT provider. Returns transcript text or None."""
    import soundfile as sf
    import numpy as np
    from pathlib import Path
    audio_path = Path(audio_path)

    try:
        # Read audio file to PCM (soundfile handles wav, flac, ogg, etc.)
        data, sample_rate = sf.read(str(audio_path), dtype='float32')

        # Convert stereo to mono if needed
        if data.ndim > 1:
            data = data.mean(axis=1)

        # Resample to 16kHz (universally supported by all STT providers)
        target_sr = 16000
        if sample_rate != target_sr:
            from scipy.signal import resample_poly
            from math import gcd
            g = gcd(sample_rate, target_sr)
            data = resample_poly(data, target_sr // g, sample_rate // g).astype(np.float32)
            sample_rate = target_sr

        # Convert to int16 PCM bytes
        data = (data * 32767).clip(-32768, 32767).astype(np.int16)
        pcm_bytes = data.tobytes()

        # Use the unified STT service
        from services import stt
        provider = stt.get_provider()
        if provider is None:
            print(f"[OmniVoice Setup] No STT provider available")
            return None

        result = provider.transcribe(pcm_bytes, sample_rate)
        if result.get("success") and result.get("text"):
            return result["text"]
        else:
            print(f"[OmniVoice Setup] STT failed for {audio_path.name}: {result.get('error', 'empty result')}")
            return None
    except Exception as e:
        print(f"[OmniVoice Setup] Error reading/transcribing {audio_path.name}: {e}")
        return None


def _pretokenize_all_voices():
    """Background task: transcribe + tokenize each voice reference in one pass."""
    global _omnivoice_setup_progress

    missing = _collect_untokenized_voices()
    if not missing:
        print("[OmniVoice Setup] All voices already tokenized")
        _omnivoice_setup_progress = {"status": "done", "total": 0, "completed": 0, "current": ""}
        return

    total = len(missing)
    _omnivoice_setup_progress = {"status": "loading", "total": total, "completed": 0, "current": "Loading OmniVoice model..."}
    print(f"[OmniVoice Setup] Processing {total} voice reference(s)...")

    from services.omnivoice_engine import _get_manager

    # Ensure worker is started (may download/load model — takes a while first time)
    manager = _get_manager()
    if not manager.ensure_started():
        _omnivoice_setup_progress = {"status": "error", "total": total, "completed": 0, "current": "Failed to start OmniVoice worker"}
        print("[OmniVoice Setup] Failed to start worker")
        return

    # Send all voices to the worker in one batch.
    # Worker loads encoder once, then for each file: transcribe via STT + tokenize.
    # Encoder unloaded after the entire batch.
    _omnivoice_setup_progress = {"status": "processing", "total": total, "completed": 0, "current": ""}

    voices = [{"path": str(p)} for p in missing if not p.with_suffix(".tokens.pt").exists()]

    if voices:
        def on_progress(completed, batch_total):
            _omnivoice_setup_progress["completed"] = completed
            _omnivoice_setup_progress["total"] = batch_total
            if completed < batch_total and completed < len(voices):
                _omnivoice_setup_progress["current"] = os.path.splitext(os.path.basename(voices[completed]["path"]))[0]

        try:
            results = manager.pretokenize_batch(voices, on_progress=on_progress,
                                                 transcribe_fn=_transcribe_audio_file)
            succeeded = sum(1 for r in results if r.get("success"))
            failed = len(results) - succeeded
            print(f"[OmniVoice Setup] Done. Tokenized: {succeeded}, Failed: {failed}")
        except Exception as e:
            print(f"[OmniVoice Setup] Batch error: {e}")
            import traceback
            traceback.print_exc()

    _omnivoice_setup_progress = {"status": "done", "total": total, "completed": total, "current": ""}


@config_bp.route('/api/tts/omnivoice/pretokenize', methods=['POST'])
def pretokenize_omnivoice_voices():
    """Process voice references: transcribe via STT, then tokenize via OmniVoice encoder."""
    if not _is_torch_installed():
        return jsonify({"status": "error", "error": "Dependencies not installed"}), 400

    settings = load_settings(raw=True)
    stt_provider = settings.get('stt', {}).get('provider', 'none')
    if stt_provider == 'none':
        return jsonify({"status": "error", "error": "No STT service configured"}), 400

    import threading
    threading.Thread(target=_pretokenize_all_voices, daemon=True).start()
    return jsonify({"status": "processing"}), 202


@config_bp.route('/api/server/restart', methods=['POST'])
def restart_server():
    """Restart the server. Writes restart flag and exits; start_server.bat re-launches."""
    import sys
    import threading
    import time

    print("[Server] Restart requested...")
    # Write restart flag
    restart_flag = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", ".server_restart")
    with open(restart_flag, "w") as f:
        f.write("restart")

    def _do_exit():
        time.sleep(0.5)  # Let response send
        os._exit(42)  # Special exit code for restart

    threading.Thread(target=_do_exit, daemon=True).start()
    return jsonify({"status": "restarting"}), 200


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

        # Get this Python process's RAM usage (includes worker subprocess)
        process_ram = 0
        try:
            import psutil
            proc = psutil.Process(os.getpid())
            process_ram = proc.memory_info().rss
            # Include child processes (OmniVoice worker, etc.)
            for child in proc.children(recursive=True):
                try:
                    process_ram += child.memory_info().rss
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        except ImportError:
            # No psutil — use Windows API fallback
            try:
                class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                    _fields_ = [
                        ("cb", ctypes.c_ulong),
                        ("PageFaultCount", ctypes.c_ulong),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t),
                    ]
                pmc = PROCESS_MEMORY_COUNTERS()
                pmc.cb = ctypes.sizeof(pmc)
                handle = ctypes.windll.kernel32.GetCurrentProcess()
                if ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
                    process_ram = pmc.WorkingSetSize
            except Exception:
                pass
        except Exception:
            pass

        return jsonify({
            "ram_free_gb": round(free / 1e9, 2),
            "ram_total_gb": round(total / 1e9, 2),
            "ram_used_gb": round(used / 1e9, 2),
            "process_ram_gb": round(process_ram / 1e9, 2),
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

    settings = load_settings(raw=True)
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
    settings = load_settings(raw=True)

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
_UEVR_AUDIO_FIX_FILENAME = "hmdAudioFix.lua"


def _get_uevr_profile_folder():
    """Get the UEVR HogwartsLegacy profile folder path."""
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        return None
    return os.path.join(appdata, "UnrealVRMod", "HogwartsLegacy")


def _get_uevr_plugins_folder():
    """Get the UEVR HogwartsLegacy plugins folder path."""
    profile = _get_uevr_profile_folder()
    return os.path.join(profile, "plugins") if profile else None


def _file_hash(path):
    """Return hex SHA-256 of a file, or None if unreadable."""
    import hashlib
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


@config_bp.route('/api/vr/plugin-status', methods=['GET'])
def get_vr_plugin_status():
    """Check if the Sonorus UEVR plugin is installed and up to date."""
    target_dir = _get_uevr_plugins_folder()
    if not target_dir:
        return jsonify({
            "installed": False,
            "target_dir": None,
            "error": "Could not determine APPDATA path"
        })

    target_path = os.path.join(target_dir, _UEVR_PLUGIN_FILENAME)
    installed = os.path.isfile(target_path)

    # Compare installed DLL hash against known-good hash from constants
    dll_outdated = False
    if installed:
        dll_outdated = _file_hash(target_path) != VR_BRIDGE_DLL_SHA256

    audio_fix_outdated = False
    profile_dir = _get_uevr_profile_folder()
    if profile_dir:
        audio_src = os.path.join(SONORUS_DIR, "vr", "uevr_plugin", _UEVR_AUDIO_FIX_FILENAME)
        audio_target = os.path.join(profile_dir, "scripts", _UEVR_AUDIO_FIX_FILENAME)
        if os.path.isfile(audio_src) and os.path.isfile(audio_target):
            audio_fix_outdated = _file_hash(audio_src) != _file_hash(audio_target)

    return jsonify({
        "installed": installed,
        "target_dir": target_dir,
        "outdated": dll_outdated or audio_fix_outdated,
    })


@config_bp.route('/api/vr/install-plugin', methods=['POST'])
def install_vr_plugin():
    """Download the Sonorus UEVR plugin DLL from GitHub and install it."""
    import urllib.request
    import tempfile

    target_dir = _get_uevr_plugins_folder()
    if not target_dir:
        return jsonify({"error": "Could not determine APPDATA path"}), 500

    try:
        # Download DLL to a temp file, verify hash, then move to target
        print(f"[VR] Downloading plugin from {VR_BRIDGE_DLL_URL}...")
        with tempfile.NamedTemporaryFile(delete=False, suffix=".dll") as tmp:
            tmp_path = tmp.name
        urllib.request.urlretrieve(VR_BRIDGE_DLL_URL, tmp_path)

        dl_hash = _file_hash(tmp_path)
        if dl_hash != VR_BRIDGE_DLL_SHA256:
            os.unlink(tmp_path)
            return jsonify({"error": f"Download hash mismatch (expected {VR_BRIDGE_DLL_SHA256[:12]}..., got {dl_hash[:12] if dl_hash else 'None'}...)"}), 500

        os.makedirs(target_dir, exist_ok=True)
        target_path = os.path.join(target_dir, _UEVR_PLUGIN_FILENAME)
        shutil.move(tmp_path, target_path)
        print(f"[VR] Installed plugin: {target_path}")

        # Install/update HMD audio fix script
        # Always overwrite our own hmdAudioFix.lua if outdated.
        # Only skip if main.lua has a built-in audio fix AND we haven't installed ours yet.
        audio_fix_installed = False
        profile_dir = _get_uevr_profile_folder()
        if profile_dir:
            scripts_dir = os.path.join(profile_dir, "scripts")
            audio_src = os.path.join(SONORUS_DIR, "vr", "uevr_plugin", _UEVR_AUDIO_FIX_FILENAME)
            audio_target = os.path.join(scripts_dir, _UEVR_AUDIO_FIX_FILENAME)
            if os.path.isfile(audio_src):
                # Update if our file already exists (always keep in sync)
                # or install if no audio fix exists anywhere
                our_file_exists = os.path.isfile(audio_target)
                needs_update = our_file_exists and _file_hash(audio_src) != _file_hash(audio_target)
                if our_file_exists and needs_update:
                    shutil.copy2(audio_src, audio_target)
                    audio_fix_installed = True
                    print(f"[VR] Updated audio fix: {audio_target}")
                elif not our_file_exists:
                    # Check if main.lua has a built-in fix
                    main_lua = os.path.join(scripts_dir, "main.lua")
                    has_builtin = False
                    if os.path.isfile(main_lua):
                        try:
                            with open(main_lua, "r", encoding="utf-8", errors="ignore") as f:
                                has_builtin = "SetAudioListenerOverride" in f.read()
                        except Exception:
                            pass
                    if not has_builtin:
                        os.makedirs(scripts_dir, exist_ok=True)
                        shutil.copy2(audio_src, audio_target)
                        audio_fix_installed = True
                        print(f"[VR] Installed audio fix: {audio_target}")
                    else:
                        print("[VR] Profile has built-in audio fix in main.lua — skipping")
            else:
                print(f"[VR] Audio fix source not found: {audio_src}")

        return jsonify({"status": "ok", "path": target_path, "audio_fix_installed": audio_fix_installed})
    except Exception as e:
        print(f"[VR] Failed to install plugin: {e}")
        return jsonify({"error": str(e)}), 500
