"""
Dialogue History API endpoints for Sonorus.

Handles CRUD operations for dialogue history, import/export.
"""

import json
import re

from flask import Blueprint, request, jsonify, Response

from utils.dialogue import (
    load_dialogue_history,
    save_dialogue_history,
    filter_dialogue_history,
)
from utils.localization import get_display_name

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


# ============================================
# Dialogue History API Endpoints
# ============================================

@dialogue_bp.route('/api/dialogue-history', methods=['GET'])
def get_dialogue_history():
    """Get filtered dialogue history."""
    history = load_dialogue_history(_get_game_context)
    filtered = filter_dialogue_history(history)
    return jsonify(filtered)


@dialogue_bp.route('/api/dialogue-history', methods=['DELETE'])
def clear_dialogue_history():
    """Clear all dialogue history."""
    from utils.dialogue_db import clear_all_entries
    clear_all_entries()
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

        # Load existing history
        existing = load_dialogue_history(_get_game_context)

        # Create set of existing entry signatures for dedup
        existing_sigs = set()
        for entry in existing:
            sig = (entry.get('timestamp', 0), entry.get('voiceName', ''), entry.get('text', ''))
            existing_sigs.add(sig)

        # Add new entries that don't already exist
        added = 0
        for entry in data:
            sig = (entry.get('timestamp', 0), entry.get('voiceName', ''), entry.get('text', ''))
            if sig not in existing_sigs:
                existing.append(entry)
                existing_sigs.add(sig)
                added += 1

        # Sort by timestamp
        existing.sort(key=lambda x: x.get('timestamp', 0))

        save_dialogue_history(existing)
        print(f"[History] Imported {added} new entries")
        return jsonify({"status": "ok", "added": added, "total": len(existing)})
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

        # Load existing full history
        existing = load_dialogue_history(_get_game_context)

        # Create dedup signature set for ALL existing entries
        existing_sigs = set()
        for entry in existing:
            sig = (entry.get('timestamp', 0), entry.get('voiceName', ''), entry.get('text', ''))
            existing_sigs.add(sig)

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

        # Add new entries that don't already exist
        added = 0
        for entry in relevant_imports:
            sig = (entry.get('timestamp', 0), entry.get('voiceName', ''), entry.get('text', ''))
            if sig not in existing_sigs:
                existing.append(entry)
                existing_sigs.add(sig)
                added += 1

        # Sort by timestamp
        existing.sort(key=lambda x: x.get('timestamp', 0))

        save_dialogue_history(existing)
        print(f"[History] Imported {added} new entries for NPC {npc_id}")
        return jsonify({"status": "ok", "added": added, "total": len(existing)})
    except Exception as e:
        print(f"[History] Import error for NPC {npc_id}: {e}")
        return jsonify({"error": str(e)}), 400


@dialogue_bp.route('/api/dialogue-history/clear-npc/<npc_id>', methods=['DELETE'])
def clear_npc_from_history(npc_id):
    """Remove an NPC from all dialogue history (earshot arrays and as speaker).

    This removes the NPC from memory - they won't remember conversations they witnessed,
    and conversations where they spoke will be deleted entirely.
    """
    try:
        dialogue_history = load_dialogue_history(_get_game_context)

        entries_removed = 0
        updated_history = []

        for entry in dialogue_history:
            # If NPC was the speaker, remove entire entry
            if entry.get('voiceName') == npc_id:
                entries_removed += 1
                continue

            # Remove NPC from earshot array
            earshot = entry.get('earshot', [])
            if npc_id in earshot:
                earshot = [e for e in earshot if e != npc_id]
                entry['earshot'] = earshot

                # If no witnesses left and not player/AI entry, remove entry
                if not earshot and not entry.get('isPlayer') and not entry.get('isAIResponse'):
                    entries_removed += 1
                    continue

            updated_history.append(entry)

        save_dialogue_history(updated_history)
        print(f"[History] Cleared NPC '{npc_id}' - removed {entries_removed} entries")
        return jsonify({"success": True, "entries_removed": entries_removed})
    except Exception as e:
        print(f"[History] Clear NPC error: {e}")
        return jsonify({"error": str(e)}), 400


@dialogue_bp.route('/api/dialogue-history/entries', methods=['DELETE'])
def delete_dialogue_entries():
    """Delete specific dialogue history entries by timestamp."""
    try:
        data = request.get_json()
        timestamps = set(data.get('timestamps', []))
        if not timestamps:
            return jsonify({"status": "error", "message": "No timestamps provided"}), 400

        dialogue_history = load_dialogue_history(_get_game_context)
        original_count = len(dialogue_history)
        dialogue_history = [e for e in dialogue_history if e.get('timestamp') not in timestamps]
        deleted_count = original_count - len(dialogue_history)

        save_dialogue_history(dialogue_history)
        print(f"[History] Deleted {deleted_count} entries")
        return jsonify({"status": "ok", "deleted": deleted_count})
    except Exception as e:
        print(f"[History] Delete entries error: {e}")
        return jsonify({"error": str(e)}), 400
