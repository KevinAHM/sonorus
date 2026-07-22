"""
Dialogue History API endpoints for Sonorus.

Handles CRUD operations for dialogue history, import/export.
"""

import json
import os
import re
from urllib.parse import quote

from flask import Blueprint, request, jsonify, Response, send_file

from utils.dialogue import (
    load_dialogue_history,
    load_dialogue_history_fast_with_raw_count,
    filter_dialogue_history,
)
from utils.localization import get_display_name, id_from_name
from utils import tts_archive
from utils.tts_archive import get_history_entry_archive_paths

dialogue_bp = Blueprint('dialogue', __name__)

# This will be set by server.py to provide game context
_load_game_context = None


def set_load_game_context(func):
    """Set the function used to load game context."""
    global _load_game_context
    _load_game_context = func


def _get_game_context():
    """Get game context, using the configured function or empty dict."""
    if _load_game_context:
        return _load_game_context()
    return {}


def _normalize_npc_reference(value):
    """Normalize NPC IDs and display names for loose comparisons."""
    return re.sub(r'\s+', '', str(value or '')).strip().lower()


def _entry_targets_npc(entry, npc_id):
    """Return True when an entry's target resolves to the given NPC."""
    if not isinstance(entry, dict) or not npc_id:
        return False

    target_id = entry.get('targetId')
    if target_id:
        return _normalize_npc_reference(target_id) == _normalize_npc_reference(npc_id)

    target = entry.get('target')
    if not target:
        return False

    candidates = {
        _normalize_npc_reference(npc_id),
        _normalize_npc_reference(get_display_name(npc_id)),
    }
    target_normalized = _normalize_npc_reference(target)
    if target_normalized in candidates:
        return True

    try:
        resolved_target = id_from_name(str(target))
    except Exception:
        resolved_target = None

    return _normalize_npc_reference(resolved_target) in candidates


def _collect_affected_npcs(entries):
    """Collect significant NPC IDs affected by a history mutation."""
    npc_ids = set()

    try:
        from utils.text_utils import is_significant_npc
    except Exception:
        is_significant_npc = None

    for entry in entries or []:
        if not isinstance(entry, dict):
            continue

        voice_name = entry.get('voiceName')
        if voice_name and voice_name.lower() != 'player':
            if is_significant_npc is None or is_significant_npc(voice_name):
                npc_ids.add(voice_name)

        earshot = entry.get('earshot', [])
        if isinstance(earshot, list):
            for npc_id in earshot:
                if not npc_id or str(npc_id).lower() == 'player':
                    continue
                if is_significant_npc is None or is_significant_npc(npc_id):
                    npc_ids.add(npc_id)

    return npc_ids


def _build_last_timestamp_map(entries):
    """Get latest remaining timestamp per affected NPC from the current history."""
    latest = {}
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue

        timestamp = entry.get('timestamp') or 0
        if timestamp <= 0:
            continue

        for npc_id in _collect_affected_npcs([entry]):
            latest[npc_id] = max(latest.get(npc_id, 0), timestamp)

    return latest


def _reset_memory_state_after_history_rewrite(affected_npc_ids, final_history, reason):
    """
    Repair memory tracking after a bulk dialogue-history rewrite.

    The rewrite regenerates SQLite row IDs, so we must drop processing checkpoints
    globally. For affected NPCs, we use the rewrite-specific repair path that may
    advance indexing state for real chapter data, while avoiding metadata-only
    chapter files for NPCs that do not have real chapters yet.
    """
    try:
        from utils.memory import ChapterManager
        from utils.memory_queue import reset_all_processing_state, clear_unsynced_chapters_for_npc

        reset_all_processing_state()

        latest_by_npc = _build_last_timestamp_map(final_history)
        chapter_mgr = ChapterManager()
        repaired = 0
        for npc_id in sorted(set(affected_npc_ids)):
            clear_unsynced_chapters_for_npc(npc_id)
            last_timestamp = latest_by_npc.get(npc_id, 0)
            if last_timestamp > 0:
                chapter_mgr.repair_after_history_rewrite(npc_id, last_timestamp)
            else:
                chapter_mgr.clear_npc_data(npc_id)
            repaired += 1

        print(f"[History] Reset memory state after {reason}: checkpoints reset, {repaired} NPCs repaired")
    except Exception as e:
        print(f"[History] Warning: failed to reset memory state after {reason}: {e}")


def _entry_signature(entry):
    """Stable dedup signature for raw history entries."""
    if not isinstance(entry, dict):
        return None
    return (
        entry.get('timestamp', 0),
        entry.get('voiceName', ''),
        entry.get('text', ''),
    )


def _repair_memory_state_after_in_place_mutation(affected_npc_ids, reason):
    """
    Repair only the NPCs touched by an in-place mutation.

    Unlike the bulk rewrite path, SQLite row IDs remain stable here, so we do not
    need to reset queue checkpoints globally. We only clear unsynced staged
    chapters and use the lightweight in-place repair path, which preserves
    indexing progress.
    """
    try:
        from utils.memory import ChapterManager
        from utils.memory_queue import clear_unsynced_chapters_for_npc

        current_history = load_dialogue_history(_get_game_context)
        latest_by_npc = _build_last_timestamp_map(current_history)
        chapter_mgr = ChapterManager()
        repaired = 0

        for npc_id in sorted(set(affected_npc_ids)):
            clear_unsynced_chapters_for_npc(npc_id)
            last_timestamp = latest_by_npc.get(npc_id, 0)
            chapter_mgr.repair_after_in_place_history_edit(npc_id, last_timestamp)

            repaired += 1

        print(f"[History] Repaired memory state after {reason} for {repaired} NPCs")
    except Exception as e:
        print(f"[History] Warning: failed to repair memory state after {reason}: {e}")


def _queue_memory_after_in_place_append(affected_npc_ids, reason):
    """Invalidate caches and queue affected NPCs after append-only history mutations."""
    try:
        from utils.memory import ChapterManager
        from utils.memory_queue import queue_npcs_for_processing

        chapter_mgr = ChapterManager()
        npc_ids = sorted(set(affected_npc_ids))
        for npc_id in npc_ids:
            chapter_mgr.invalidate_memory_cache(npc_id)

        if npc_ids:
            queue_npcs_for_processing(npc_ids, priority=5)

        print(f"[History] Queued memory refresh after {reason} for {len(npc_ids)} NPCs")
    except Exception as e:
        print(f"[History] Warning: failed to queue memory refresh after {reason}: {e}")


def _delete_history_rows_in_place(entry_ids, reason):
    """Delete raw dialogue rows by DB ID and repair affected memory state."""
    from utils.dialogue_db import delete_entries_by_ids, get_entries_by_ids

    entry_ids = sorted(set(entry_ids))
    if not entry_ids:
        return 0, set()

    deleted_entries = get_entries_by_ids(entry_ids)
    affected_npcs = _collect_affected_npcs(deleted_entries)
    deleted_count = delete_entries_by_ids(entry_ids)

    if deleted_count > 0:
        _repair_memory_state_after_in_place_mutation(affected_npcs, reason)

    return deleted_count, affected_npcs


def _attach_archive_metadata(entry):
    """Add archived TTS paths and URLs for a history entry when present."""
    if not isinstance(entry, dict):
        return entry

    archive_paths = get_history_entry_archive_paths(entry)
    if not archive_paths:
        return entry

    enriched = dict(entry)
    archive_urls = [
        f"/api/dialogue-history/archive/{quote(os.path.basename(path))}"
        for path in archive_paths
    ]
    enriched['ttsArchivePaths'] = archive_paths
    enriched['ttsArchiveUrls'] = archive_urls
    enriched['ttsArchivePath'] = archive_paths[0]
    enriched['ttsArchiveUrl'] = archive_urls[0]
    return enriched


GENERIC_NPC_PREFIXES = (
    'AdultMale', 'AdultFemale', 'ElderlyMale', 'ElderlyFemale',
    'ChildMale', 'ChildFemale', 'TeenMale', 'TeenFemale'
)


def _is_named_npc(voice_name):
    if not voice_name:
        return False
    lower = str(voice_name).lower()
    if lower in ('player', 'playermale', 'playerfemale'):
        return False
    return not any(str(voice_name).startswith(prefix) for prefix in GENERIC_NPC_PREFIXES)


def _prettify_voice_name(voice_name):
    if not voice_name:
        return 'Unknown'

    lower = str(voice_name).lower()
    if lower in ('player', 'playermale', 'playerfemale'):
        return 'Player'

    try:
        display_name = get_display_name(voice_name)
    except Exception:
        display_name = None

    if display_name:
        return display_name

    pretty = re.sub(r'([a-z])([A-Z])', r'\1 \2', str(voice_name))
    return re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', pretty)


def _build_history_npc_options(history):
    npc_ids = set()
    for entry in history or []:
        if not isinstance(entry, dict):
            continue

        voice_name = entry.get('voiceName') or ''
        if _is_named_npc(voice_name):
            npc_ids.add(voice_name)

        earshot = entry.get('earshot')
        if isinstance(earshot, list):
            for npc_id in earshot:
                if _is_named_npc(npc_id):
                    npc_ids.add(npc_id)

    return [
        {'id': npc_id, 'name': _prettify_voice_name(npc_id)}
        for npc_id in sorted(npc_ids, key=lambda value: _prettify_voice_name(value).lower())
    ]


def _entry_matches_history_perspective(entry, npc_id):
    if not isinstance(entry, dict) or not npc_id:
        return False

    entry_voice_name = entry.get('voiceName')
    if _normalize_npc_reference(entry_voice_name) == _normalize_npc_reference(npc_id):
        return True

    earshot = entry.get('earshot')
    if isinstance(earshot, list):
        for witness_id in earshot:
            if _normalize_npc_reference(witness_id) == _normalize_npc_reference(npc_id):
                return True

    return 'earshot' not in entry


def _slice_reversed_history(history, start, end):
    reversed_history = list(reversed(history or []))
    return reversed_history[start:end]


def _build_dialogue_history_view_payload(requested_npc_id, page=1, page_size=100, recent_limit=10):
    try:
        page = max(1, int(page or 1))
    except (TypeError, ValueError):
        page = 1

    try:
        page_size = max(1, int(page_size or 100))
    except (TypeError, ValueError):
        page_size = 100

    try:
        recent_limit = max(1, int(recent_limit or 10))
    except (TypeError, ValueError):
        recent_limit = 10

    base_history, raw_count = load_dialogue_history_fast_with_raw_count(_get_game_context)
    visible_history = filter_dialogue_history(base_history)
    npc_options = _build_history_npc_options(visible_history)
    valid_npc_ids = {item['id'] for item in npc_options}

    normalized_requested = requested_npc_id if requested_npc_id in valid_npc_ids else ''
    if normalized_requested:
        selected_history = [
            entry for entry in visible_history
            if _entry_matches_history_perspective(entry, normalized_requested)
        ]
    else:
        selected_history = visible_history

    visible_count = len(selected_history)
    total_pages = max(1, (visible_count + page_size - 1) // page_size) if visible_count > 0 else 1
    page = max(1, min(page, total_pages))
    start = (page - 1) * page_size
    end = start + page_size

    recent_entries = _slice_reversed_history(selected_history, 0, recent_limit)
    page_entries = _slice_reversed_history(selected_history, start, end)

    return {
        'recent_entries': [_attach_archive_metadata(entry) for entry in recent_entries],
        'page_entries': [_attach_archive_metadata(entry) for entry in page_entries],
        'page': page,
        'page_size': page_size,
        'total_pages': total_pages,
        'visible_count': visible_count,
        'raw_count': raw_count if not normalized_requested else None,
        'npc_options': npc_options,
        'selected_npc_id': normalized_requested or 'all',
    }


# ============================================
# Dialogue History API Endpoints
# ============================================

@dialogue_bp.route('/api/dialogue-history', methods=['GET'])
def get_dialogue_history():
    """Get filtered dialogue history."""
    history = load_dialogue_history(_get_game_context)
    filtered = filter_dialogue_history(history)
    return jsonify([_attach_archive_metadata(entry) for entry in filtered])


@dialogue_bp.route('/api/dialogue-history/view', methods=['GET'])
def get_dialogue_history_view():
    """Get the current paged/filterable dialogue history view for the config UI."""
    payload = _build_dialogue_history_view_payload(
        requested_npc_id=(request.args.get('npc_id') or '').strip(),
        page=request.args.get('page', 1),
        page_size=request.args.get('page_size', 100),
        recent_limit=request.args.get('recent_limit', 10),
    )
    return jsonify(payload)


@dialogue_bp.route('/api/dialogue-history/archive/<path:filename>', methods=['GET'])
def get_dialogue_history_archive(filename):
    """Serve an archived TTS WAV file by basename."""
    safe_name = os.path.basename(filename or "")
    if safe_name != filename or not safe_name.lower().endswith('.wav'):
        return jsonify({"error": "Invalid archive filename"}), 400

    archive_path = os.path.join(tts_archive.TTS_ARCHIVE_DIR, safe_name)
    if not os.path.isfile(archive_path):
        return jsonify({"error": "Archive not found"}), 404

    return send_file(archive_path, mimetype='audio/wav', conditional=True)


@dialogue_bp.route('/api/dialogue-history', methods=['DELETE'])
def clear_dialogue_history():
    """Clear all dialogue history."""
    history = load_dialogue_history(_get_game_context)
    affected_npcs = _collect_affected_npcs(history)

    from utils.dialogue_db import clear_all_entries
    clear_all_entries()
    _reset_memory_state_after_history_rewrite(affected_npcs, [], "clear all history")
    print("[History] Cleared")
    return jsonify({"status": "ok"})


@dialogue_bp.route('/api/dialogue-history/export', methods=['GET'])
def export_dialogue_history():
    """Export dialogue history as JSON file."""
    history = load_dialogue_history(_get_game_context)
    response = Response(
        json.dumps(history, indent=2),
        mimetype='application/json',
        headers={'Content-Disposition': 'attachment;filename=dialogue_history.json'}
    )
    return response


@dialogue_bp.route('/api/dialogue-history/export/<npc_id>', methods=['GET'])
def export_npc_dialogue_history(npc_id):
    """Export dialogue history for a specific NPC (entries they spoke or witnessed)."""
    # Load full history
    full_history = load_dialogue_history(_get_game_context)

    # Filter to entries where NPC was speaker or in earshot
    # Note: Legacy entries without earshot field are NOT included since we can't
    # determine if they're related to this NPC. Use "all" export for legacy data.
    npc_history = []
    for entry in full_history:
        # NPC was the speaker
        if entry.get('voiceName') == npc_id:
            npc_history.append(entry)
            continue

        # NPC was in earshot (witnessed the conversation)
        earshot = entry.get('earshot', [])
        if isinstance(earshot, list) and npc_id in earshot:
            npc_history.append(entry)

    # Create filename with NPC name (sanitize for Content-Disposition header)
    display_name = get_display_name(npc_id) or npc_id
    # Keep only alphanumeric, spaces, hyphens, underscores; replace spaces with underscores
    safe_name = re.sub(r'[^\w\s-]', '', display_name).replace(' ', '_')
    if not safe_name:
        safe_name = 'npc'
    filename = f'dialogue_{safe_name}.json'

    response = Response(
        json.dumps(npc_history, indent=2),
        mimetype='application/json',
        headers={'Content-Disposition': f'attachment;filename={filename}'}
    )
    return response


@dialogue_bp.route('/api/dialogue-history/import', methods=['POST'])
def import_dialogue_history():
    """Import dialogue history from JSON file, merging with existing."""
    try:
        data = request.get_json()
        if not isinstance(data, list):
            return jsonify({"error": "Invalid format - expected array"}), 400

        from utils.dialogue_db import append_entry, get_entry_count, load_all_entries

        existing = load_all_entries()
        existing_sigs = {_entry_signature(entry) for entry in existing if _entry_signature(entry) is not None}

        added = 0
        affected_npcs = set()
        for entry in data:
            sig = _entry_signature(entry)
            if sig is None or sig in existing_sigs:
                continue
            if not append_entry(entry):
                continue
            existing_sigs.add(sig)
            affected_npcs.update(_collect_affected_npcs([entry]))
            added += 1

        if added > 0:
            _queue_memory_after_in_place_append(affected_npcs, "import history")

        print(f"[History] Imported {added} new entries")
        return jsonify({"status": "ok", "added": added, "total": get_entry_count()})
    except Exception as e:
        print(f"[History] Import error: {e}")
        return jsonify({"error": str(e)}), 400


@dialogue_bp.route('/api/dialogue-history/import/<npc_id>', methods=['POST'])
def import_npc_dialogue_history(npc_id):
    """Import dialogue history for a specific NPC, merging with existing."""
    try:
        data = request.get_json()
        if not isinstance(data, list):
            return jsonify({"error": "Invalid format - expected array"}), 400

        from utils.dialogue_db import append_entry, get_entry_count, load_all_entries

        existing = load_all_entries()
        existing_sigs = {_entry_signature(entry) for entry in existing if _entry_signature(entry) is not None}

        # Filter imported entries to only those relevant to this NPC
        # (entries where NPC is speaker or in earshot)
        # Note: Legacy entries without earshot field are skipped since we can't
        # determine if they're related to this NPC. Use "all" import for legacy data.
        relevant_imports = []
        for entry in data:
            # Check if NPC is speaker
            if entry.get('voiceName') == npc_id:
                relevant_imports.append(entry)
                continue

            # Check if NPC is in earshot
            earshot = entry.get('earshot', [])
            if isinstance(earshot, list) and npc_id in earshot:
                relevant_imports.append(entry)

        added = 0
        affected_npcs = set()
        for entry in relevant_imports:
            sig = _entry_signature(entry)
            if sig is None or sig in existing_sigs:
                continue
            if not append_entry(entry):
                continue
            existing_sigs.add(sig)
            affected_npcs.update(_collect_affected_npcs([entry]))
            added += 1

        if npc_id:
            affected_npcs.add(npc_id)

        if added > 0:
            _queue_memory_after_in_place_append(affected_npcs, f"import history for {npc_id}")

        print(f"[History] Imported {added} new entries for NPC {npc_id}")
        return jsonify({"status": "ok", "added": added, "total": get_entry_count()})
    except Exception as e:
        print(f"[History] Import error for NPC {npc_id}: {e}")
        return jsonify({"error": str(e)}), 400


@dialogue_bp.route('/api/dialogue-history/clear-npc/<npc_id>', methods=['DELETE'])
def clear_npc_from_history(npc_id):
    """Remove an NPC from all dialogue history (earshot arrays and as speaker).

    This removes the NPC from memory - they won't remember conversations they witnessed,
    and conversations where they spoke will be deleted entirely. Orphaned player
    dialogue tied only to that NPC is also removed once no witnesses remain.
    """
    try:
        from utils.dialogue_db import load_all_entries, update_entry_by_id

        dialogue_history = load_all_entries()
        affected_entries = [
            entry for entry in dialogue_history
            if (
                entry.get('voiceName') == npc_id or
                npc_id in (entry.get('earshot', []) or []) or
                _entry_targets_npc(entry, npc_id)
            )
        ]
        affected_npcs = _collect_affected_npcs(affected_entries)
        affected_npcs.add(npc_id)

        entries_removed = 0
        entries_updated = 0
        entry_ids_to_delete = []

        for entry in dialogue_history:
            source_ids = entry.get('sourceEntryIds', [])
            entry_id = source_ids[0] if source_ids else None
            if not entry_id:
                continue

            # If NPC was the speaker, remove entire entry
            if entry.get('voiceName') == npc_id:
                entry_ids_to_delete.append(entry_id)
                entries_removed += 1
                continue

            original_earshot = list(entry.get('earshot', []) or [])
            npc_was_witness = npc_id in original_earshot
            targets_npc = _entry_targets_npc(entry, npc_id)

            # Remove NPC from earshot array
            earshot = original_earshot
            if npc_was_witness:
                earshot = [e for e in original_earshot if e != npc_id]
                entry['earshot'] = earshot

                # If no witnesses left and not player/AI entry, remove entry
                if not earshot and not entry.get('isPlayer') and not entry.get('isAIResponse'):
                    entry_ids_to_delete.append(entry_id)
                    entries_removed += 1
                    continue

            entry_type = entry.get('type') or 'dialogue'
            is_player_dialogue = entry.get('isPlayer') and entry_type in ('dialogue', 'chatter', 'cutscene')
            if not earshot and is_player_dialogue and (targets_npc or npc_was_witness):
                entry_ids_to_delete.append(entry_id)
                entries_removed += 1
                continue

            if npc_was_witness and update_entry_by_id(entry_id, entry):
                entries_updated += 1

        deleted_count, _ = _delete_history_rows_in_place(
            entry_ids_to_delete,
            f"clear NPC {npc_id} from history",
        )
        if entries_updated > 0 and deleted_count == 0:
            _repair_memory_state_after_in_place_mutation(
                affected_npcs,
                f"clear NPC {npc_id} from history",
            )
        print(f"[History] Cleared NPC '{npc_id}' - removed {entries_removed} entries")
        return jsonify({"success": True, "entries_removed": entries_removed})
    except Exception as e:
        print(f"[History] Clear NPC error: {e}")
        return jsonify({"error": str(e)}), 400


@dialogue_bp.route('/api/dialogue-history/entries', methods=['DELETE'])
def delete_dialogue_entries():
    """Delete specific dialogue history entries by row ID or timestamp."""
    try:
        data = request.get_json()
        entry_ids = sorted({
            int(entry_id) for entry_id in (data or {}).get('entry_ids', [])
            if str(entry_id).strip().isdigit()
        })

        if entry_ids:
            deleted_count, _ = _delete_history_rows_in_place(
                entry_ids,
                "delete history entries by row ID",
            )
            print(f"[History] Deleted {deleted_count} entries by row ID")
            return jsonify({"status": "ok", "deleted": deleted_count})

        timestamps = set((data or {}).get('timestamps', []))
        if not timestamps:
            return jsonify({"status": "error", "message": "No entry IDs or timestamps provided"}), 400

        from utils.dialogue_db import load_all_entries

        timestamp_entry_ids = []
        for entry in load_all_entries():
            if entry.get('timestamp') in timestamps:
                timestamp_entry_ids.extend(entry.get('sourceEntryIds', []))

        deleted_count, _ = _delete_history_rows_in_place(
            timestamp_entry_ids,
            "delete history entries by timestamp",
        )
        print(f"[History] Deleted {deleted_count} entries by timestamp")
        return jsonify({"status": "ok", "deleted": deleted_count})
    except Exception as e:
        print(f"[History] Delete entries error: {e}")
        return jsonify({"error": str(e)}), 400
