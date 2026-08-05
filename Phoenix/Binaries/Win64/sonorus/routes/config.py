"""
Config API endpoints for Sonorus settings management.

Handles settings CRUD, character import/export, system events.
"""

import importlib.metadata
import importlib.util
import base64
import json
import os
import shutil
import subprocess
import sys
import threading
import time
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
from utils.emote_embeddings import ensure_emote_index_async

import llm
import event_logger
from constants import SONORUS_MOD_PACKAGE_ID, VR_BRIDGE_DLL_URL, VR_BRIDGE_DLL_SHA256
from utils.package_id_check import read_package_id
from services.tts.universal_client import (
    UniversalAPIError,
    UniversalSpeechClient,
    language_code,
    resolve_draft_key,
)
from services.tts.reference_preparation import (
    ensure_reference_transcript,
    is_preparable_reference,
    read_reference_transcript,
)
from services.omnivoice_token_cache import load_omnivoice_token_cache
from services.speech_server.catalog import enrich_asr_capabilities
from services import omnivoice_cpp_engine
from services import stt as stt_service
from services import tts as tts_service
from utils.vulkan_gpu_info import detect_vulkan_gpus

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
    tts_providers_with_keys = ['inworld', 'elevenlabs', 'openai']
    for provider in tts_providers_with_keys:
        if masked.get('tts', {}).get(provider, {}).get('api_key'):
            masked['tts'][provider]['api_key'] = '********'
    if masked.get('speech_server', {}).get('api_key'):
        masked['speech_server']['api_key'] = '********'
    masked['player_context'] = {
        "ready": player_ready,
        "player_name": current_player_display or current_player or "",
        "normalized_name": current_player or "",
    }
    return jsonify(masked)


def _universal_client_from_draft(payload):
    payload = payload if isinstance(payload, dict) else {}
    saved = load_settings(raw=True).get('speech_server', {})
    saved = saved if isinstance(saved, dict) else {}
    api_url = payload.get('api_url', saved.get('api_url', 'http://127.0.0.1:8100'))
    api_key = resolve_draft_key(payload.get('api_key', ''), saved.get('api_key', ''))
    return UniversalSpeechClient(api_url, api_key)


def _universal_error(exc):
    return jsonify({"ok": False, "error": exc.as_dict()}), exc.status


def _universal_draft_payload():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise UniversalAPIError(
            'invalid_request',
            'The Universal speech request body must be a JSON object.',
            status=400,
        )
    return payload


def _universal_draft_bool(payload, field, default=False):
    value = payload.get(field, default)
    if not isinstance(value, bool):
        raise UniversalAPIError(
            'invalid_request', f'{field} must be a boolean.', status=400
        )
    return value


def _universal_game_language(payload):
    value = payload.get('game_language', 'EN_US')
    if not isinstance(value, str) or not value.strip() or len(value) > 32:
        raise UniversalAPIError(
            'invalid_request', 'game_language must be a language identifier.', status=400
        )
    return value.strip()


def _universal_optional_model_id(payload, field):
    value = payload.get(field)
    if value is None or value == '':
        return None
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 128:
        raise UniversalAPIError(
            'invalid_load_plan', f'{field} must be a model identifier.', status=400
        )
    return value.strip()


def _speech_install_target(client, payload):
    component = payload.get('component')
    if component not in {'model', 'upscaler', 'aligner'}:
        raise UniversalAPIError(
            'invalid_install_target', 'component must be model, upscaler, or aligner.', status=400
        )
    game_language = _universal_game_language(payload)
    capabilities = client.enriched_capabilities(game_language)
    capabilities.update(enrich_asr_capabilities(client.capabilities(), game_language))
    if capabilities.get('capabilitiesVersion', 1) < 7:
        raise UniversalAPIError(
            'installation_unavailable',
            'Model installation requires speech-server capabilities version 7.',
            status=409,
        )
    model_id = None
    if component == 'model':
        model_id = _universal_optional_model_id(payload, 'model')
        if model_id is None:
            raise UniversalAPIError(
                'invalid_install_target', 'model is required for model installation.', status=400
            )
        candidates = (
            capabilities.get('compatibleModels', [])
            + capabilities.get('compatibleASRModels', [])
        )
        target = next((item for item in candidates if item.get('id') == model_id), None)
    else:
        target = capabilities.get('upscaler' if component == 'upscaler' else 'alignment')
        if component == 'aligner' and target:
            supported = {
                value.lower().replace('_', '-').split('-', 1)[0]
                for value in target.get('languages', [])
            }
            if language_code(game_language) not in supported and '*' not in supported:
                target = None
    if not target:
        raise UniversalAPIError(
            'invalid_install_target',
            'The selected component is not compatible with the game language.',
            status=409,
        )
    if target.get('installed'):
        raise UniversalAPIError(
            'component_already_installed', 'The selected component is already installed.', status=409
        )
    if not target.get('installable'):
        raise UniversalAPIError(
            'component_not_installable', 'The selected component cannot be installed automatically.', status=409
        )
    return component, model_id, target.get('registryBundle')


def _speech_install_job_id(payload):
    value = payload.get('job_id')
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 128:
        raise UniversalAPIError(
            'invalid_install_job', 'job_id must be an installation job identifier.', status=400
        )
    return value.strip()


@config_bp.route('/api/tts/universal/connect', methods=['POST'])
def connect_universal_speech_server():
    return _connect_universal_speech_server(require_tts=True)


@config_bp.route('/api/speech-server/connect', methods=['POST'])
def connect_shared_speech_server():
    return _connect_universal_speech_server(require_tts=False)


def _connect_universal_speech_server(*, require_tts):
    try:
        payload = _universal_draft_payload()
        client = _universal_client_from_draft(payload)
        resources = None
        resource_error = None
        try:
            resources = client.resources()
        except UniversalAPIError as exc:
            resource_error = exc.as_dict()
        game_language = _universal_game_language(payload)
        capabilities = client.enriched_capabilities(
            game_language,
            resources=resources,
            force=_universal_draft_bool(payload, 'refresh'),
        )
        capabilities.update(
            enrich_asr_capabilities(
                client.capabilities(),
                game_language,
                resources,
            )
        )
        capabilities['activeInstallations'] = client.active_installations()
        response = {
            "ok": True,
            "connected": True,
            "apiUrl": client.api_url,
            "capabilities": capabilities,
            "resources": resources,
            "resourceError": resource_error,
        }
        if require_tts and not capabilities.get('compatibleModels'):
            error = UniversalAPIError(
                'no_compatible_models',
                'Connected, but the server has no voice-cloning model for the game language.',
                status=409,
                details=response,
            )
            return _universal_error(error)
        return jsonify(response)
    except UniversalAPIError as exc:
        return _universal_error(exc)


@config_bp.route('/api/tts/universal/resources', methods=['POST'])
@config_bp.route('/api/speech-server/resources', methods=['POST'])
def get_universal_resources():
    try:
        payload = _universal_draft_payload()
        client = _universal_client_from_draft(payload)
        return jsonify({"ok": True, "resources": client.resources()})
    except UniversalAPIError as exc:
        return _universal_error(exc)


@config_bp.route('/api/speech-server/install/plan', methods=['POST'])
def plan_speech_component_install():
    try:
        payload = _universal_draft_payload()
        client = _universal_client_from_draft(payload)
        component, model_id, registry_bundle = _speech_install_target(client, payload)
        plan = client.install_plan(component, model_id)
        if plan.get('registryBundle') != registry_bundle:
            raise UniversalAPIError(
                'registry_changed',
                'The component registry changed during installation planning; refresh and try again.',
                status=409,
            )
        return jsonify({'ok': True, 'plan': plan})
    except UniversalAPIError as exc:
        return _universal_error(exc)


@config_bp.route('/api/speech-server/install/start', methods=['POST'])
def start_speech_component_install():
    try:
        payload = _universal_draft_payload()
        client = _universal_client_from_draft(payload)
        component, model_id, registry_bundle = _speech_install_target(client, payload)
        job = client.start_install(
            component, model_id,
            accept_license=_universal_draft_bool(payload, 'accept_license'),
        )
        if job.get('registryBundle') != registry_bundle:
            try:
                client.cancel_installation(job['jobId'])
            except UniversalAPIError:
                pass
            raise UniversalAPIError(
                'registry_changed',
                'The component registry changed before installation started.',
                status=409,
            )
        return jsonify({'ok': True, 'job': job}), 202
    except UniversalAPIError as exc:
        return _universal_error(exc)


@config_bp.route('/api/speech-server/install/status', methods=['POST'])
def get_speech_component_install():
    try:
        payload = _universal_draft_payload()
        client = _universal_client_from_draft(payload)
        if client.capabilities().get('capabilitiesVersion', 1) < 7:
            raise UniversalAPIError(
                'installation_unavailable',
                'Model installation requires speech-server capabilities version 7.',
                status=409,
            )
        job = client.installation(_speech_install_job_id(payload))
        response = {'ok': True, 'job': job}
        if job['state'] == 'completed':
            resources = None
            try:
                resources = client.resources()
            except UniversalAPIError:
                pass
            game_language = _universal_game_language(payload)
            capabilities = client.enriched_capabilities(
                game_language, resources=resources, force=True
            )
            capabilities.update(
                enrich_asr_capabilities(client.capabilities(), game_language, resources)
            )
            capabilities['activeInstallations'] = client.active_installations()
            response.update({'capabilities': capabilities, 'resources': resources})
        return jsonify(response)
    except UniversalAPIError as exc:
        return _universal_error(exc)


@config_bp.route('/api/speech-server/install/cancel', methods=['POST'])
def cancel_speech_component_install():
    try:
        payload = _universal_draft_payload()
        client = _universal_client_from_draft(payload)
        return jsonify({
            'ok': True,
            'job': client.cancel_installation(_speech_install_job_id(payload)),
        })
    except UniversalAPIError as exc:
        return _universal_error(exc)


def _stack_payload(payload):
    result = {
        'tts_model': _universal_optional_model_id(payload, 'tts_model'),
        'asr_model': _universal_optional_model_id(payload, 'asr_model'),
        'upscale': _universal_draft_bool(payload, 'upscale'),
        'alignment': _universal_draft_bool(payload, 'alignment'),
    }
    if not result['tts_model'] and not result['asr_model']:
        raise UniversalAPIError(
            'invalid_load_plan', 'At least one remote speech model is required.', status=400
        )
    return result


@config_bp.route('/api/speech-server/plan', methods=['POST'])
def plan_universal_speech_stack():
    try:
        payload = _universal_draft_payload()
        client = _universal_client_from_draft(payload)
        capabilities = client.capabilities()
        if capabilities.get('capabilitiesVersion', 1) < 6:
            raise UniversalAPIError(
                'load_planning_unavailable',
                'Combined speech-stack planning requires capabilities version 6.',
                status=409,
            )
        plan = client.stack_plan(**_stack_payload(payload))
        return jsonify({'ok': True, 'plan': plan})
    except UniversalAPIError as exc:
        return _universal_error(exc)


@config_bp.route('/api/speech-server/warmup', methods=['POST'])
def warmup_universal_speech_stack():
    try:
        payload = _universal_draft_payload()
        client = _universal_client_from_draft(payload)
        if client.capabilities().get('capabilitiesVersion', 1) < 6:
            raise UniversalAPIError(
                'load_planning_unavailable',
                'Combined speech-stack warmup requires capabilities version 6.',
                status=409,
            )
        result = client.stack_warmup(**_stack_payload(payload))
        return jsonify({
            'ok': result.get('success') is True,
            'warmup': result,
            'resources': result.get('resources'),
        })
    except UniversalAPIError as exc:
        return _universal_error(exc)


@config_bp.route('/api/tts/universal/plan', methods=['POST'])
def plan_universal_model_load():
    try:
        payload = _universal_draft_payload()
        client = _universal_client_from_draft(payload)
        capabilities, model = _universal_selected_model(client, payload)
        if not capabilities.get('loadPlanning'):
            raise UniversalAPIError(
                'load_planning_unavailable',
                'This speech server does not expose component load planning.',
                status=409,
            )
        upscale = _universal_draft_bool(payload, 'upscale')
        adaptive_batching = _universal_draft_bool(payload, 'adaptive_batching')
        if upscale and not model.get('upscaleEligible'):
            raise UniversalAPIError(
                'upscaler_unavailable',
                'Upscaling is not available for the selected model.',
                status=409,
            )
        if adaptive_batching and not (
            model.get('segmentation') and model.get('alignmentCompatible')
        ):
            raise UniversalAPIError(
                'alignment_unavailable',
                'Adaptive batching is not available for the selected model.',
                status=409,
            )
        plan = client.load_plan(
            model['id'],
            upscale=upscale,
            adaptive_batching=adaptive_batching,
        )
        return jsonify({"ok": True, "plan": plan})
    except UniversalAPIError as exc:
        return _universal_error(exc)


@config_bp.route('/api/tts/universal/warmup', methods=['POST'])
def warmup_universal_model():
    try:
        payload = _universal_draft_payload()
        client = _universal_client_from_draft(payload)
        model_id = str(payload.get('model') or '')
        capabilities = client.enriched_capabilities(_universal_game_language(payload))
        compatible_ids = {model['id'] for model in capabilities.get('compatibleModels', [])}
        if model_id not in compatible_ids:
            raise UniversalAPIError(
                'no_compatible_models',
                'The selected model is not compatible with the game language.',
                status=409,
            )
        selected = next(
            model
            for model in capabilities.get('compatibleModels', [])
            if model['id'] == model_id
        )
        upscale = _universal_draft_bool(payload, 'upscale')
        if upscale and not selected.get('upscaleEligible', False):
            raise UniversalAPIError(
                'upscaler_unavailable',
                'Upscaling is not available for the selected model.',
                status=409,
            )
        adaptive_batching = _universal_draft_bool(payload, 'adaptive_batching')
        alignment = bool(
            adaptive_batching
            and selected.get('segmentation')
            and selected.get('alignmentCompatible')
        )
        if adaptive_batching and not alignment:
            raise UniversalAPIError(
                'alignment_unavailable',
                'Adaptive batching is not available for the selected model.',
                status=409,
            )
        result = client.warmup(
            model_id, upscale=upscale, alignment=alignment
        )
        resources = None
        try:
            resources = client.resources()
        except UniversalAPIError:
            pass
        return jsonify({"ok": True, "warmup": result, "resources": resources})
    except UniversalAPIError as exc:
        return _universal_error(exc)


_universal_voice_setup_lock = threading.RLock()
_universal_voice_setup_cancel = threading.Event()
_universal_voice_setup_progress = {
    "status": "idle",
    "model": None,
    "language": None,
    "total": 0,
    "completed": 0,
    "current": "",
    "phase": "",
    "reused": 0,
    "transcribed": 0,
    "uploaded": 0,
    "prepared": 0,
    "failures": [],
}


def _universal_selected_model(client, payload):
    capabilities = client.enriched_capabilities(_universal_game_language(payload))
    model_id = str(payload.get('model') or '')
    model = next(
        (item for item in capabilities.get('compatibleModels', []) if item['id'] == model_id),
        None,
    )
    if model is None:
        raise UniversalAPIError(
            'no_compatible_models',
            'The selected model is not compatible with the game language.',
            status=409,
        )
    return capabilities, model


def _remote_voice_for_item(remote_by_name, item, reference_hash):
    from services.tts.voice_utils import build_hashed_voice_name
    display_name = build_hashed_voice_name(
        item.character_name, item.language, reference_hash
    )
    return display_name, remote_by_name.get(display_name)


def _preparation_current(voice, model):
    policy = model.get('voiceReference') or {}
    preparation = policy.get('preparation') or {}
    if preparation.get('mode') != 'persistent':
        return True
    prepared_models = voice.get('preparedModels')
    if not isinstance(prepared_models, dict):
        return False
    marker = prepared_models.get(model['id'])
    if not isinstance(marker, dict) or marker.get('revision') != preparation.get('revision'):
        return False
    hashes = marker.get('inputHashes')
    if not isinstance(hashes, dict):
        return False
    if 'audio' in preparation.get('inputs', []) and hashes.get('audio') != voice.get('audioHash'):
        return False
    if 'transcript' in preparation.get('inputs', []) and hashes.get('transcript') != voice.get('transcriptHash'):
        return False
    return True


def _universal_voice_needs_upload(
    voice, reference_hash, transcript_policy, transcript_hash
):
    if not voice or voice.get('referenceHash') != reference_hash:
        return True
    if transcript_policy not in {'required', 'optional'} or not transcript_hash:
        return transcript_policy == 'required' and not voice.get('hasTranscript')
    if not voice.get('hasTranscript'):
        return True
    # Capability-v4 servers return transcriptHash.  A missing hash is tolerated
    # for legacy servers that can only report transcript presence.
    remote_hash = voice.get('transcriptHash')
    return remote_hash is not None and remote_hash != transcript_hash


def _universal_voice_setup_status(client, model, game_language):
    from services.tts.reference_preparation import (
        discover_voice_references,
        read_reference_transcript,
        reference_transcript_hash,
    )
    from services.tts.voice_utils import compute_reference_hash

    items = discover_voice_references(game_language)
    remote = client.voices().get('voices', [])
    remote_by_name = {
        str(voice.get('displayName') or voice.get('voiceId', '').split('__', 1)[-1]): voice
        for voice in remote
    }
    transcript_policy = (model.get('voiceReference') or {}).get('transcript', 'unused')
    transcripts_missing = uploads_missing = preparations_missing = 0
    for item in items:
        transcript = read_reference_transcript(item.path)
        if transcript_policy == 'required' and not transcript:
            transcripts_missing += 1
        reference_hash = compute_reference_hash(str(item.path))
        if not reference_hash:
            uploads_missing += 1
            continue
        _, voice = _remote_voice_for_item(remote_by_name, item, reference_hash)
        local_transcript_hash = reference_transcript_hash(transcript)
        needs_upload = _universal_voice_needs_upload(
            voice, reference_hash, transcript_policy, local_transcript_hash
        )
        if needs_upload:
            uploads_missing += 1
        if (
            (model.get('voiceReference') or {}).get('preparation', {}).get('mode')
            == 'persistent'
            and (needs_upload or not _preparation_current(voice, model))
        ):
            preparations_missing += 1
    settings = load_settings(raw=True)
    stt_configured = settings.get('stt', {}).get('provider', 'none') != 'none'
    return {
        'model': model['id'],
        'language': game_language,
        'total': len(items),
        'transcriptPolicy': transcript_policy,
        'preparationMode': (model.get('voiceReference') or {}).get('preparation', {}).get('mode', 'lazy'),
        'transcriptsMissing': transcripts_missing,
        'uploadsMissing': uploads_missing,
        'preparationsMissing': preparations_missing,
        'sttConfigured': stt_configured,
        'complete': not (transcripts_missing or uploads_missing or preparations_missing),
    }


def _run_universal_voice_setup(client, model, game_language):
    global _universal_voice_setup_progress
    from services.tts.reference_preparation import (
        discover_voice_references,
        ensure_reference_transcript,
        read_reference_transcript,
        reference_transcript_hash,
    )
    from services.tts.voice_utils import build_hashed_voice_name, compute_reference_hash

    items = discover_voice_references(game_language)
    policy = model.get('voiceReference') or {}
    transcript_policy = policy.get('transcript', 'unused')
    preparation_mode = (policy.get('preparation') or {}).get('mode', 'lazy')
    remote = client.voices().get('voices', [])
    remote_by_name = {
        str(voice.get('displayName') or voice.get('voiceId', '').split('__', 1)[-1]): voice
        for voice in remote
    }
    with _universal_voice_setup_lock:
        _universal_voice_setup_progress.update(total=len(items), status='processing')
    stt_attempts = stt_failures = 0
    for index, item in enumerate(items):
        if _universal_voice_setup_cancel.is_set():
            with _universal_voice_setup_lock:
                _universal_voice_setup_progress['status'] = 'cancelled'
            return
        with _universal_voice_setup_lock:
            _universal_voice_setup_progress.update(
                current=item.path.name, completed=index, phase='checking'
            )
        try:
            existing_transcript = read_reference_transcript(item.path)
            transcript = existing_transcript
            if transcript:
                with _universal_voice_setup_lock:
                    _universal_voice_setup_progress['reused'] += 1
            if transcript_policy == 'required' and not transcript:
                stt_attempts += 1
                with _universal_voice_setup_lock:
                    _universal_voice_setup_progress['phase'] = 'transcribing'
                try:
                    transcript = ensure_reference_transcript(item.path)
                    with _universal_voice_setup_lock:
                        _universal_voice_setup_progress['transcribed'] += 1
                except Exception:
                    stt_failures += 1
                    raise
            reference_hash = compute_reference_hash(str(item.path))
            if not reference_hash:
                raise RuntimeError('Could not hash the reference audio')
            display_name = build_hashed_voice_name(
                item.character_name, item.language, reference_hash
            )
            voice = remote_by_name.get(display_name)
            transcript_hash = reference_transcript_hash(transcript)
            needs_upload = _universal_voice_needs_upload(
                voice, reference_hash, transcript_policy, transcript_hash
            )
            if needs_upload:
                with _universal_voice_setup_lock:
                    _universal_voice_setup_progress['phase'] = 'uploading'
                audio_data = base64.b64encode(item.path.read_bytes()).decode()
                voice = client.clone_voice({
                    'displayName': display_name,
                    'langCode': item.language,
                    'audioData': audio_data,
                    'refText': transcript,
                    'referenceHash': reference_hash,
                    'tags': ['hogwarts-legacy', 'setup'],
                })
                remote_by_name[display_name] = voice
                with _universal_voice_setup_lock:
                    _universal_voice_setup_progress['uploaded'] += 1
            if preparation_mode == 'persistent' and not _preparation_current(voice, model):
                with _universal_voice_setup_lock:
                    _universal_voice_setup_progress['phase'] = 'preparing'
                result = client.prepare_voice(model['id'], voice['voiceId'])
                existing_prepared = voice.get('preparedModels')
                prepared_models = (
                    dict(existing_prepared)
                    if isinstance(existing_prepared, dict)
                    else {}
                )
                prepared_models[model['id']] = result.get('preparation', {})
                voice['preparedModels'] = prepared_models
                with _universal_voice_setup_lock:
                    _universal_voice_setup_progress['prepared'] += 1
        except Exception as exc:
            with _universal_voice_setup_lock:
                _universal_voice_setup_progress['failures'].append({
                    'voice': item.path.name, 'error': str(exc)
                })
            if stt_attempts >= 5 and stt_failures / stt_attempts > 0.5:
                with _universal_voice_setup_lock:
                    _universal_voice_setup_progress.update(
                        status='error',
                        phase='failed',
                        current='STT is failing for most reference voices.',
                    )
                return
        finally:
            with _universal_voice_setup_lock:
                _universal_voice_setup_progress['completed'] = index + 1
    with _universal_voice_setup_lock:
        _universal_voice_setup_progress.update(phase='refreshing', current='')
    try:
        # Bulk setup uses its own client, so an already-loaded provider would
        # otherwise keep an empty/stale voice list and upload the same voices
        # again on first speech.
        from services import tts
        tts.refresh_voices('universal')
    except Exception as exc:
        # The remote setup itself is complete.  A later cache miss can safely
        # reload or re-upload, so do not misreport the whole setup as failed.
        print(f"[Voice Setup] Could not refresh Universal voice cache: {exc}")
    with _universal_voice_setup_lock:
        _universal_voice_setup_progress.update(
            status='done', phase='complete', current=''
        )


def _run_universal_voice_setup_safe(client, model, game_language):
    try:
        _run_universal_voice_setup(client, model, game_language)
    except Exception as exc:
        with _universal_voice_setup_lock:
            _universal_voice_setup_progress.update(
                status='error', phase='failed', current=str(exc)
            )


@config_bp.route('/api/tts/universal/voice-setup/status', methods=['POST'])
def universal_voice_setup_status():
    try:
        payload = _universal_draft_payload()
        client = _universal_client_from_draft(payload)
        _, model = _universal_selected_model(client, payload)
        status = _universal_voice_setup_status(
            client, model, _universal_game_language(payload)
        )
        with _universal_voice_setup_lock:
            progress = dict(_universal_voice_setup_progress)
            progress['failures'] = list(progress.get('failures', []))
        return jsonify({'ok': True, 'setup': status, 'progress': progress})
    except UniversalAPIError as exc:
        return _universal_error(exc)
    except Exception as exc:
        return _universal_error(UniversalAPIError(
            'voice_setup_failed', str(exc), status=500
        ))


@config_bp.route('/api/tts/universal/voice-setup/start', methods=['POST'])
def start_universal_voice_setup():
    global _universal_voice_setup_progress
    try:
        payload = _universal_draft_payload()
        client = _universal_client_from_draft(payload)
        _, model = _universal_selected_model(client, payload)
        language = _universal_game_language(payload)
        with _universal_voice_setup_lock:
            if _universal_voice_setup_progress.get('status') == 'processing':
                raise UniversalAPIError(
                    'setup_in_progress', 'Voice reference setup is already running.', status=409
                )
            _universal_voice_setup_cancel.clear()
            _universal_voice_setup_progress = {
                'status': 'processing', 'model': model['id'], 'language': language,
                'total': 0, 'completed': 0, 'current': '', 'phase': 'starting',
                'reused': 0, 'transcribed': 0, 'uploaded': 0, 'prepared': 0,
                'failures': [],
            }
        threading.Thread(
            target=_run_universal_voice_setup_safe,
            args=(client, model, language),
            name='UniversalVoiceSetup',
            daemon=True,
        ).start()
        return jsonify({'ok': True, 'status': 'processing'}), 202
    except UniversalAPIError as exc:
        return _universal_error(exc)


@config_bp.route('/api/tts/universal/voice-setup/progress', methods=['GET'])
def universal_voice_setup_progress():
    with _universal_voice_setup_lock:
        progress = dict(_universal_voice_setup_progress)
        progress['failures'] = list(progress.get('failures', []))
    return jsonify({'ok': True, 'progress': progress})


@config_bp.route('/api/tts/universal/voice-setup/cancel', methods=['POST'])
def cancel_universal_voice_setup():
    _universal_voice_setup_cancel.set()
    return jsonify({'ok': True})


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
        sonorus_utoc_path = (paks_root / "LogicMods" / "SonorusMod.utoc").resolve()
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
            if len(group) < 2 or package_id == SONORUS_MOD_PACKAGE_ID:
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
            sonorus_group = [
                entry
                for entry in grouped.get(SONORUS_MOD_PACKAGE_ID, [])
                if Path(entry.utoc_path) != sonorus_utoc_path
            ]
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
    existing_connection = existing.get('speech_server', {})
    if not isinstance(existing_connection, dict):
        existing_connection = {}
    if 'speech_server' not in new_settings:
        # Older/cached settings pages do not know about the new root field.
        # Preserve credentials instead of treating omission as an explicit reset.
        new_settings['speech_server'] = dict(existing_connection)
    submitted_connection = new_settings['speech_server']
    if not isinstance(submitted_connection, dict):
        return jsonify({"error": "speech_server settings must be an object"}), 400
    if submitted_connection.get('api_key') == '********':
        submitted_connection['api_key'] = existing_connection.get('api_key', '')
    speech_server_changed = (
        submitted_connection.get('api_url', '') != existing_connection.get('api_url', '')
        or submitted_connection.get('api_key', '') != existing_connection.get('api_key', '')
    )

    tts_providers_changed = []
    tts_providers_with_keys = ['inworld', 'elevenlabs', 'openai']
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

    new_universal_settings = new_settings.get('tts', {}).get('universal', {})
    existing_universal_settings = existing.get('tts', {}).get('universal', {})
    universal_settings_changed = new_universal_settings != existing_universal_settings
    if (
        (speech_server_changed or universal_settings_changed)
        and 'universal' in {new_tts_provider, existing_tts_provider}
    ):
        tts_providers_changed.append('universal')
        print("[Settings] Universal Speech Server TTS configuration changed")
    if speech_server_changed:
        UniversalSpeechClient.invalidate_cache()

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
    new_omnivoice_cpp_device = new_settings.get('tts', {}).get('omnivoice_cpp', {}).get('device', 'auto')
    existing_omnivoice_cpp_device = existing.get('tts', {}).get('omnivoice_cpp', {}).get('device', 'auto')
    omnivoice_cpp_device_changed = (
        active_tts_provider == 'omnivoice_cpp'
        and new_omnivoice_cpp_device != existing_omnivoice_cpp_device
    )
    active_tts_changed = (
        tts_provider_switched or
        active_tts_provider in tts_providers_changed or
        omnivoice_device_changed or
        omnivoice_cpp_device_changed
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

                if existing_tts_provider == 'omnivoice_cpp':
                    try:
                        omnivoice_cpp_engine.unload()
                        print("[Settings] Unloaded OmniVoice (Vulkan) worker")
                    except Exception as e:
                        print(f"[Settings] Error unloading OmniVoice (Vulkan): {e}")

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
                        def preload_pocket():
                            try:
                                warmup_pocket()
                                print("[Settings] Pocket TTS models preloaded")
                            except Exception as e:
                                print(f"[Settings] Pocket TTS preload failed: {e}")
                        threading.Thread(target=preload_pocket, daemon=True).start()
                    except Exception as e:
                        print(f"[Settings] Error starting Pocket TTS preload: {e}")

                elif new_tts_provider == 'omnivoice' and _are_omnivoice_deps_installed() and _count_untokenized_voices() == 0:
                    try:
                        from services.omnivoice_engine import warm_up as warmup_omnivoice
                        def preload_omnivoice():
                            try:
                                warmup_omnivoice()
                                print("[Settings] OmniVoice models preloaded")
                            except Exception as e:
                                print(f"[Settings] OmniVoice preload failed: {e}")
                        threading.Thread(target=preload_omnivoice, daemon=True).start()
                    except Exception as e:
                        print(f"[Settings] Error starting OmniVoice preload: {e}")

                elif new_tts_provider == 'omnivoice_cpp':
                    try:
                        if omnivoice_cpp_engine.is_available():
                            def preload_omnivoice_cpp():
                                try:
                                    if omnivoice_cpp_engine.warm_up():
                                        print("[Settings] OmniVoice (Vulkan) worker preloaded")
                                    else:
                                        print("[Settings] OmniVoice (Vulkan) preload did not complete")
                                except Exception as e:
                                    print(f"[Settings] OmniVoice (Vulkan) preload failed: {e}")
                            threading.Thread(target=preload_omnivoice_cpp, daemon=True).start()
                    except Exception as e:
                        print(f"[Settings] Error activating OmniVoice (Vulkan): {e}")

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

                if _are_omnivoice_deps_installed() and _count_untokenized_voices() == 0:
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

        if omnivoice_cpp_device_changed and not tts_provider_switched:
            print(f"[Settings] OmniVoice (Vulkan) GPU changed: {existing_omnivoice_cpp_device} -> {new_omnivoice_cpp_device}")
            try:
                omnivoice_cpp_engine.unload()
                tts_service.clear_provider_cache('omnivoice_cpp')
                print("[Settings] Unloaded OmniVoice (Vulkan) worker for GPU change")

                if omnivoice_cpp_engine.is_available():
                    def preload_omnivoice_cpp_after_gpu_change():
                        try:
                            if omnivoice_cpp_engine.warm_up():
                                print("[Settings] OmniVoice (Vulkan) worker restarted after GPU change")
                            else:
                                print("[Settings] OmniVoice (Vulkan) restart after GPU change failed")
                        except Exception as e:
                            print(f"[Settings] OmniVoice (Vulkan) restart after GPU change failed: {e}")
                    threading.Thread(target=preload_omnivoice_cpp_after_gpu_change, daemon=True).start()
            except Exception as e:
                print(f"[Settings] Error restarting OmniVoice (Vulkan) after GPU change: {e}")

        # Keep an activated native provider self-healing when a saved config is
        # retried after an interrupted download. Start this after device-change
        # handling so install and worker teardown cannot race each other.
        if new_tts_provider == 'omnivoice_cpp':
            try:
                if not omnivoice_cpp_engine.is_available():
                    started = _start_omnivoice_cpp_install()
                    if started:
                        print("[Settings] OmniVoice (Vulkan) dependency install started")
            except Exception as e:
                print(f"[Settings] Error resuming OmniVoice (Vulkan) installation: {e}")

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
            new_stt.get('whisper', {}).get('api_key') != existing_stt.get('whisper', {}).get('api_key') or
            new_stt.get('universal', {}).get('model') != existing_stt.get('universal', {}).get('model') or
            (speech_server_changed and (new_stt.get('provider') or existing_stt.get('provider')) == 'universal')
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

        # Validate or generate the freeform emote index after any relevant save.
        ensure_emote_index_async()

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


_omnivoice_cpp_installer_thread = None
_omnivoice_cpp_install_lock = threading.Lock()
_omnivoice_cpp_install_progress = {
    "status": "idle",
    "total": 4,
    "completed": 0,
    "current": "",
    "message": "",
}
_omnivoice_cpp_voice_lock = threading.Lock()
_omnivoice_cpp_voice_progress = {
    "status": "idle",
    "phase": "",
    "total": 0,
    "completed": 0,
    "succeeded": 0,
    "failed": 0,
    "current": "",
    "error": "",
}


def _update_omnivoice_cpp_voice_progress(**updates):
    with _omnivoice_cpp_voice_lock:
        _omnivoice_cpp_voice_progress.update(updates)


def _omnivoice_cpp_voice_progress_snapshot():
    with _omnivoice_cpp_voice_lock:
        return dict(_omnivoice_cpp_voice_progress)


def _get_omnivoice_cpp_gpu_status(settings, requested_device=None):
    """GPU status for the omnivoice_cpp (ggml/Vulkan) provider.

    ggml supplies Vulkan device identity/capacity and Windows supplies
    system-wide dedicated-memory usage across the game and other processes.
    Includes dll_present/models_present so the UI can show install state.
    """
    configured_device = settings.get('tts', {}).get('omnivoice_cpp', {}).get('device', 'auto')
    server_device = requested_device or configured_device or 'auto'

    result = {
        "cuda_available": False,
        "vram_free_gb": None,
        "vram_total_gb": None,
        "vram_used_gb": None,
        "model_loaded": False,
        "model_on_gpu": False,
        "server_device": server_device,
        "selected_device": configured_device or 'auto',
        "selected_gpu_index": None,
        "gpu_name": None,
        "gpus": [],
        "dll_present": False,
        "runtime_present": False,
        "missing_runtime_files": [],
        "models_present": False,
    }

    try:
        gpus = detect_vulkan_gpus(force_refresh=True, log=False)
        result["gpus"] = [dict(gpu) for gpu in gpus]

        selected_gpu = next((gpu for gpu in gpus if gpu["device"] == server_device), None)
        if selected_gpu is None and gpus:
            selected_gpu = gpus[0]
        if selected_gpu is not None:
            result["selected_gpu_index"] = selected_gpu["index"]
            result["gpu_name"] = selected_gpu["name"]
            for field in ("vram_total_gb", "vram_free_gb", "vram_used_gb"):
                result[field] = selected_gpu.get(field)
    except Exception as e:
        print(f"[Config] Error enumerating Vulkan GPUs: {e}")

    try:
        result["dll_present"] = bool(omnivoice_cpp_engine.dll_present())
        result["runtime_present"] = bool(omnivoice_cpp_engine.runtime_present())
        result["missing_runtime_files"] = omnivoice_cpp_engine.missing_runtime_files()
        result["models_present"] = bool(omnivoice_cpp_engine.models_present())
        result["model_loaded"] = bool(omnivoice_cpp_engine.is_loaded())
        result["model_on_gpu"] = bool(
            result["model_loaded"]
            and result["gpus"]
            and str(server_device).strip().lower() != "cpu"
        )
    except Exception as e:
        print(f"[Config] Error reading omnivoice_cpp engine status: {e}")

    return jsonify(result)


def _run_omnivoice_cpp_install():
    """Background dependency installation started by activation or the retry button."""
    def update_progress(completed, total, message):
        with _omnivoice_cpp_install_lock:
            _omnivoice_cpp_install_progress.update({
                "status": "installing",
                "total": total,
                "completed": completed,
                "current": message,
                "message": message,
            })

    try:
        omnivoice_cpp_engine.install_dependencies(update_progress)
    except Exception as exc:
        print(f"[OmniVoiceCpp Setup] Dependency install failed: {exc}")
        with _omnivoice_cpp_install_lock:
            _omnivoice_cpp_install_progress.update({
                "status": "error",
                "current": "",
                "message": str(exc),
            })
        return

    try:
        detect_vulkan_gpus(force_refresh=True, log=True)
    except Exception as exc:
        print(f"[OmniVoiceCpp Setup] Post-install GPU refresh failed (non-fatal): {exc}")

    with _omnivoice_cpp_install_lock:
        _omnivoice_cpp_install_progress.update({
            "status": "complete",
            "total": 4,
            "completed": 4,
            "current": "",
            "message": "OmniVoice (Vulkan) is ready.",
        })

    try:
        settings = load_settings()
        if settings.get('tts', {}).get('provider') == 'omnivoice_cpp':
            tts_service.clear_provider_cache('omnivoice_cpp')
            warmed_up = omnivoice_cpp_engine.warm_up()
            provider_still_active = (
                load_settings().get('tts', {}).get('provider') == 'omnivoice_cpp'
            )
            if not provider_still_active:
                omnivoice_cpp_engine.unload()
                print("[OmniVoiceCpp Setup] Provider changed during warm-up; worker unloaded")
            elif warmed_up:
                print("[OmniVoiceCpp Setup] Dependencies installed and worker preloaded")
            else:
                print("[OmniVoiceCpp Setup] Dependencies installed, but worker preload failed")
    except Exception as exc:
        print(f"[OmniVoiceCpp Setup] Post-install warm-up failed (non-fatal): {exc}")


def _start_omnivoice_cpp_install():
    """Start one installer thread. Returns True only when a new job was started."""
    global _omnivoice_cpp_installer_thread

    with _omnivoice_cpp_install_lock:
        if _omnivoice_cpp_installer_thread is not None and _omnivoice_cpp_installer_thread.is_alive():
            return False
        _omnivoice_cpp_install_progress.update({
            "status": "installing",
            "total": 4,
            "completed": 0,
            "current": "Starting OmniVoice installation...",
            "message": "Starting OmniVoice installation...",
        })
        _omnivoice_cpp_installer_thread = threading.Thread(
            target=_run_omnivoice_cpp_install,
            name="omnivoice-cpp-installer",
            daemon=True,
        )
        _omnivoice_cpp_installer_thread.start()
        return True


# Cached counts for the status poll; a full voice_references scan (including
# sidecar inspection) is too heavy to run on every poll tick.
_omnivoice_cpp_voice_counts_cache = {
    "at": 0.0,
    "setup": 0,
    "transcripts": 0,
    "tokens": 0,
}


def _omnivoice_cpp_stt_available():
    """Check STT configuration without importing/loading the selected model."""
    try:
        return bool(stt_service.is_available())
    except Exception as exc:
        print(f"[OmniVoiceCpp Setup] STT availability check failed: {exc}")
        return False


@config_bp.route('/api/tts/omnivoice-cpp/status', methods=['GET'])
def get_omnivoice_cpp_status():
    """Return runtime, model-install, and voice-preparation status."""
    model_files = [
        omnivoice_cpp_engine.MODEL_FILENAME,
        omnivoice_cpp_engine.TOKENIZER_FILENAME,
        omnivoice_cpp_engine.UPSCALER_FILENAME,
    ]
    completed_models = [
        name for name in model_files
        if omnivoice_cpp_engine.model_file_ready(name)
    ]
    runtime_present = omnivoice_cpp_engine.runtime_present()
    models_present = omnivoice_cpp_engine.models_present()
    with _omnivoice_cpp_install_lock:
        install_progress = dict(_omnivoice_cpp_install_progress)
        install_running = bool(
            _omnivoice_cpp_installer_thread is not None
            and _omnivoice_cpp_installer_thread.is_alive()
        )

    completed_dependencies = int(runtime_present) + len(completed_models)
    if runtime_present and models_present:
        install_progress.update({
            "status": "complete",
            "completed": 4,
            "total": 4,
            "current": "",
            "message": "OmniVoice (Vulkan) is ready.",
        })
    elif install_running:
        install_progress["status"] = "installing"
        install_progress["completed"] = max(
            completed_dependencies,
            int(install_progress.get("completed", 0)),
        )
    elif install_progress.get("status") == "error":
        install_progress["completed"] = completed_dependencies
    else:
        next_item = (
            f"OmniVoice runtime {omnivoice_cpp_engine.RUNTIME_VERSION}"
            if not runtime_present
            else next((name for name in model_files if name not in completed_models), "")
        )
        install_progress.update({
            "status": "idle",
            "completed": completed_dependencies,
            "total": 4,
            "current": next_item,
            "message": "Required runtime and model files will download automatically when this provider is activated.",
        })

    stt_configured = _omnivoice_cpp_stt_available()

    now = time.monotonic()
    if now - _omnivoice_cpp_voice_counts_cache["at"] > 10.0:
        missing_voices = _collect_untokenized_voices()
        _omnivoice_cpp_voice_counts_cache["setup"] = len(missing_voices)
        _omnivoice_cpp_voice_counts_cache["transcripts"] = sum(
            1 for path in missing_voices if not _has_reference_transcript(path)
        )
        _omnivoice_cpp_voice_counts_cache["tokens"] = sum(
            1 for path in missing_voices if not _has_omnivoice_audio_codes(path)
        )
        _omnivoice_cpp_voice_counts_cache["at"] = now

    return jsonify({
        "dll_present": omnivoice_cpp_engine.dll_present(),
        "runtime_present": runtime_present,
        "runtime_version": omnivoice_cpp_engine.RUNTIME_VERSION,
        "missing_runtime_files": omnivoice_cpp_engine.missing_runtime_files(),
        "models_present": models_present,
        "install_progress": install_progress,
        "voices_needing_setup": _omnivoice_cpp_voice_counts_cache["setup"],
        "transcripts_needing_setup": _omnivoice_cpp_voice_counts_cache["transcripts"],
        "tokens_needing_setup": _omnivoice_cpp_voice_counts_cache["tokens"],
        # Kept for older config pages that only understood transcript setup.
        "voices_needing_transcripts": _omnivoice_cpp_voice_counts_cache["transcripts"],
        "stt_configured": stt_configured,
        "voice_progress": _omnivoice_cpp_voice_progress_snapshot(),
    })


@config_bp.route('/api/tts/omnivoice-cpp/install', methods=['POST'])
@config_bp.route('/api/tts/omnivoice-cpp/install-models', methods=['POST'])
def install_omnivoice_cpp_dependencies():
    """Install the pinned runtime and models in the background."""
    if omnivoice_cpp_engine.is_available():
        return jsonify({"status": "already_installed"}), 200
    _start_omnivoice_cpp_install()
    return jsonify({"status": "installing"}), 202


@config_bp.route('/api/tts/omnivoice-cpp/restart-worker', methods=['POST'])
def restart_omnivoice_cpp_worker():
    """Restart the OmniVoice (Vulkan) worker so device changes take effect.

    GGML_BACKEND is read at worker start, so a GPU change requires a full
    worker restart: unload, clear the cached provider, then warm up again.
    """
    try:
        omnivoice_cpp_engine.unload()
        tts_service.clear_provider_cache('omnivoice_cpp')
        print("[Config] OmniVoice (Vulkan) worker unloaded for manual restart")
    except Exception as e:
        print(f"[Config] Error unloading OmniVoice (Vulkan) worker: {e}")
        return jsonify({"status": "error", "error": str(e)}), 500

    warming_up = False
    try:
        if omnivoice_cpp_engine.is_available():
            def warmup_omnivoice_cpp_after_restart():
                try:
                    if omnivoice_cpp_engine.warm_up():
                        print("[Config] OmniVoice (Vulkan) worker restarted")
                    else:
                        print("[Config] OmniVoice (Vulkan) worker restart failed")
                except Exception as e:
                    print(f"[Config] OmniVoice (Vulkan) warm-up after restart failed: {e}")
            threading.Thread(target=warmup_omnivoice_cpp_after_restart, daemon=True).start()
            warming_up = True
    except Exception as e:
        print(f"[Config] Error starting OmniVoice (Vulkan) warm-up: {e}")

    return jsonify({"status": "ok", "warming_up": warming_up})


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
    if provider == 'omnivoice_cpp' or request.args.get('provider') == 'omnivoice_cpp':
        return _get_omnivoice_cpp_gpu_status(settings, requested_device)
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
    """Use the provider-neutral voice-reference selection rule."""
    return is_preparable_reference(path)


def _has_reference_transcript(path) -> bool:
    try:
        return bool(read_reference_transcript(path))
    except Exception:
        return False


def _has_omnivoice_audio_codes(path) -> bool:
    """Return whether a reference has a readable, structurally valid token cache."""
    token_path = Path(path).with_suffix(".tokens.pt")
    if not token_path.is_file():
        return False
    try:
        load_omnivoice_token_cache(token_path)
        return True
    except Exception:
        return False


def _count_untokenized_voices() -> int:
    """Count local OmniVoice references missing transcript or audio codes."""
    return len(_collect_untokenized_voices())


def _are_omnivoice_deps_installed() -> bool:
    """Check if all separately installed OmniVoice dependencies are complete.

    find_spec('torch') returns True as soon as pip creates the package
    directory, long before the install completes. We verify a late-written
    DLL exists to confirm the install actually finished, then check every
    additional module installed by install_omnivoice.bat.
    """
    spec = importlib.util.find_spec("torch")
    if spec is None:
        return False
    try:
        torch_dir = os.path.dirname(spec.origin)
        # torch_cpu.dll is one of the last files written during install.
        if not os.path.exists(os.path.join(torch_dir, "lib", "torch_cpu.dll")):
            return False
    except Exception:
        return False

    required_distributions = (
        "torch",
        "torchaudio",
        "transformers",
        "accelerate",
        "safetensors",
        "soundfile",
    )
    try:
        return all(
            importlib.util.find_spec(distribution_name) is not None
            and bool(importlib.metadata.version(distribution_name))
            for distribution_name in required_distributions
        )
    except importlib.metadata.PackageNotFoundError:
        return False


@config_bp.route('/api/tts/omnivoice/status', methods=['GET'])
def get_omnivoice_status():
    """OmniVoice provider status: GPU, deps, model, voices, STT."""
    from utils.gpu_info import get_gpu_info_dict

    deps_installed = _are_omnivoice_deps_installed()

    model_loaded = False
    if deps_installed:
        try:
            from services.omnivoice_engine import is_loaded
            model_loaded = is_loaded()
        except Exception:
            pass

    missing_voices = _collect_untokenized_voices()
    voices_needing_setup = len(missing_voices)
    transcripts_needing_setup = sum(
        1 for path in missing_voices if not _has_reference_transcript(path)
    )

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
        "transcripts_needing_setup": transcripts_needing_setup,
        "stt_configured": stt_configured,
        "setup_progress": _omnivoice_setup_progress,
    })


@config_bp.route('/api/tts/omnivoice/install-deps', methods=['POST'])
def install_omnivoice_deps():
    """Launch OmniVoice dependency installer in a visible console window."""
    from utils.gpu_info import is_cuda_compatible

    if _are_omnivoice_deps_installed():
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
    deps_installed = _are_omnivoice_deps_installed()
    flag_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", ".omnivoice_deps_installed")
    flag_exists = os.path.exists(flag_path)
    return jsonify({
        "deps_installed": deps_installed,
        "install_complete": flag_exists,
        "restart_needed": flag_exists and not deps_installed,
    })


def _collect_untokenized_voices():
    """Return local OmniVoice references missing transcript or audio codes."""
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
            if not _has_omnivoice_audio_codes(path) or not _has_reference_transcript(path):
                missing.append(path)
    return missing


def _transcribe_audio_file(audio_path):
    """Read or generate a sidecar through the shared reference pipeline."""
    try:
        return ensure_reference_transcript(audio_path)
    except Exception as e:
        print(f"[OmniVoice Setup] Reference transcription failed for {Path(audio_path).name}: {e}")
        return None


def _prepare_omnivoice_cpp_voices(voices):
    """Create missing transcripts and native-compatible token sidecars."""
    total = len(voices)
    succeeded = 0
    failed = 0
    transcription_attempts = 0
    transcription_failures = 0
    manager = None
    worker_was_loaded = omnivoice_cpp_engine.is_loaded()
    try:
        for index, voice_path in enumerate(voices):
            needs_transcript = not _has_reference_transcript(voice_path)
            _update_omnivoice_cpp_voice_progress(
                status="processing",
                phase="transcribing" if needs_transcript else "encoding",
                current=voice_path.stem,
                completed=index,
                succeeded=succeeded,
                failed=failed,
            )

            transcript = _transcribe_audio_file(voice_path)
            if not transcript or not str(transcript).strip():
                failed += 1
                if needs_transcript:
                    transcription_failures += 1
            elif _has_omnivoice_audio_codes(voice_path):
                succeeded += 1
            else:
                _update_omnivoice_cpp_voice_progress(
                    phase="loading" if manager is None else "encoding",
                    current=voice_path.stem,
                )
                if manager is None:
                    manager = omnivoice_cpp_engine._get_manager()
                encoded = manager.pretokenize_voice(
                    str(voice_path),
                    ref_text=transcript,
                )
                if encoded and _has_omnivoice_audio_codes(voice_path):
                    succeeded += 1
                else:
                    failed += 1
                    if not omnivoice_cpp_engine.is_loaded():
                        message = "The OmniVoice worker stopped while encoding voice references."
                        _update_omnivoice_cpp_voice_progress(
                            status="error",
                            phase="",
                            current="",
                            completed=index + 1,
                            failed=failed,
                            error=message,
                        )
                        print(f"[OmniVoiceCpp Setup] {message}")
                        return

            _update_omnivoice_cpp_voice_progress(
                completed=index + 1,
                succeeded=succeeded,
                failed=failed,
            )

            if needs_transcript:
                transcription_attempts += 1
            if (
                transcription_attempts >= 5
                and transcription_failures / transcription_attempts > 0.5
            ):
                message = (
                    f"Stopped after {transcription_failures} of "
                    f"{transcription_attempts} transcriptions failed. "
                    "Check the STT configuration."
                )
                _update_omnivoice_cpp_voice_progress(
                    status="error",
                    phase="",
                    current="",
                    error=message,
                )
                print(f"[OmniVoiceCpp Setup] {message}")
                return

        _update_omnivoice_cpp_voice_progress(
            status="done",
            phase="",
            total=total,
            completed=total,
            succeeded=succeeded,
            failed=failed,
            current="",
            error="" if failed == 0 else f"{failed} voice reference(s) could not be prepared.",
        )
        print(f"[OmniVoiceCpp Setup] Done. Prepared: {succeeded}, Failed: {failed}")
    except Exception as exc:
        _update_omnivoice_cpp_voice_progress(
            status="error",
            phase="",
            current="",
            error=str(exc),
        )
        print(f"[OmniVoiceCpp Setup] Preparation failed: {exc}")
    finally:
        _omnivoice_cpp_voice_counts_cache["at"] = 0.0
        if manager is not None and not worker_was_loaded:
            try:
                provider_is_active = (
                    load_settings(raw=True).get("tts", {}).get("provider")
                    == "omnivoice_cpp"
                )
            except Exception:
                provider_is_active = False
            try:
                if not provider_is_active:
                    omnivoice_cpp_engine.unload()
            except Exception as exc:
                print(f"[OmniVoiceCpp Setup] Worker cleanup failed (non-fatal): {exc}")


@config_bp.route('/api/tts/omnivoice-cpp/prepare-voices', methods=['POST'])
def prepare_omnivoice_cpp_voices():
    """Create missing transcript and token sidecars for native OmniVoice."""
    global _omnivoice_cpp_voice_progress

    with _omnivoice_cpp_voice_lock:
        if _omnivoice_cpp_voice_progress.get("status") == "processing":
            return jsonify({"status": "processing"}), 202

        voices = _collect_untokenized_voices()
        if not voices:
            _omnivoice_cpp_voice_progress = {
                "status": "done",
                "phase": "",
                "total": 0,
                "completed": 0,
                "succeeded": 0,
                "failed": 0,
                "current": "",
                "error": "",
            }
            return jsonify({"status": "already_prepared"}), 200

        needs_transcription = any(
            not _has_reference_transcript(path) for path in voices
        )
        if needs_transcription and not _omnivoice_cpp_stt_available():
            return jsonify({
                "status": "error",
                "error": "The selected STT service is not available or fully configured",
            }), 400

        _omnivoice_cpp_voice_progress = {
            "status": "processing",
            "phase": "transcribing" if needs_transcription else "loading",
            "total": len(voices),
            "completed": 0,
            "succeeded": 0,
            "failed": 0,
            "current": "",
            "error": "",
        }
        threading.Thread(
            target=_prepare_omnivoice_cpp_voices,
            args=(voices,),
            daemon=True,
        ).start()

    return jsonify({"status": "processing", "total": len(voices)}), 202


def _pretokenize_all_voices():
    """Background task: shared transcript pass, then local audio-code pass."""
    global _omnivoice_setup_progress

    missing = _collect_untokenized_voices()
    if not missing:
        print("[OmniVoice Setup] All voices already tokenized")
        _omnivoice_setup_progress = {"status": "done", "total": 0, "completed": 0, "current": ""}
        return

    total = len(missing)
    needs_transcription = any(not _has_reference_transcript(path) for path in missing)
    _omnivoice_setup_progress = {
        "status": "transcribing" if needs_transcription else "processing",
        "total": total,
        "completed": 0,
        "current": "",
    }
    print(f"[OmniVoice Setup] Processing {total} voice reference(s)...")

    prepared = []
    failures = 0
    for index, path in enumerate(missing):
        _omnivoice_setup_progress["current"] = path.stem
        transcript = _transcribe_audio_file(path)
        if transcript:
            prepared.append({"path": str(path), "ref_text": transcript})
        else:
            failures += 1
        _omnivoice_setup_progress["completed"] = index + 1
        if index + 1 >= 5 and failures / (index + 1) > 0.5:
            _omnivoice_setup_progress = {
                "status": "error", "total": total, "completed": index + 1,
                "current": "STT is failing for most reference voices.",
            }
            return

    voices = [
        entry for entry in prepared
        if not _has_omnivoice_audio_codes(entry["path"])
    ]
    if not voices:
        _omnivoice_setup_progress = {
            "status": "done", "total": total, "completed": total, "current": ""
        }
        return

    _omnivoice_setup_progress = {
        "status": "loading", "total": len(voices), "completed": 0,
        "current": "Loading OmniVoice model...",
    }

    from services.omnivoice_engine import _get_manager

    # Ensure worker is started (may download/load model — takes a while first time)
    manager = _get_manager()
    if not manager.ensure_started():
        _omnivoice_setup_progress = {"status": "error", "total": total, "completed": 0, "current": "Failed to start OmniVoice worker"}
        print("[OmniVoice Setup] Failed to start worker")
        return

    # Send transcript-ready voices to the worker in one batch. The worker loads
    # the audio encoder once and unloads it after the entire batch.
    _omnivoice_setup_progress = {"status": "processing", "total": len(voices), "completed": 0, "current": ""}

    if voices:
        def on_progress(completed, batch_total):
            _omnivoice_setup_progress["completed"] = completed
            _omnivoice_setup_progress["total"] = batch_total
            if completed < batch_total and completed < len(voices):
                _omnivoice_setup_progress["current"] = os.path.splitext(os.path.basename(voices[completed]["path"]))[0]

        try:
            results = manager.pretokenize_batch(voices, on_progress=on_progress)
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
    if not _are_omnivoice_deps_installed():
        return jsonify({"status": "error", "error": "Dependencies not installed"}), 400

    missing_voices = _collect_untokenized_voices()
    needs_transcription = any(
        not _has_reference_transcript(path) for path in missing_voices
    )
    settings = load_settings(raw=True)
    stt_provider = settings.get('stt', {}).get('provider', 'none')
    if needs_transcription and stt_provider == 'none':
        return jsonify({"status": "error", "error": "No STT service configured"}), 400

    threading.Thread(target=_pretokenize_all_voices, daemon=True).start()
    return jsonify({"status": "processing"}), 202


@config_bp.route('/api/server/restart', methods=['POST'])
def restart_server():
    """Restart the server. Writes restart flag and exits; start_server.bat re-launches."""
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
