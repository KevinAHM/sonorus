"""
Commitments API endpoints for Sonorus.

Handles CRUD operations for NPC meeting commitments.
"""

from flask import Blueprint, request, jsonify

from constants import LOCATION_ACTIVITIES, COMMITMENT_WAIT_TIME_MIN, COMMITMENT_TRAVEL_TIME_MIN
from utils import commitments_db
from utils.commitments import (
    create_commitment_from_ui,
    _get_current_game_time_str,
    _add_game_minutes,
    _subtract_game_minutes,
    _inject_commitment_event,
)

commitments_bp = Blueprint('commitments', __name__)

# Injected by server.py
_lua_socket = None
_load_game_context = None


def set_lua_socket(socket):
    global _lua_socket
    _lua_socket = socket


def set_load_game_context(func):
    global _load_game_context
    _load_game_context = func


def _get_game_context():
    if _load_game_context:
        return _load_game_context()
    return {}


# ============================================
# Locations API
# ============================================

# Pre-compute grouped locations for the dropdown
_COMMON_ROOM_IDS = {"HOG_GryffindorTower", "HOG_HufflepuffBasement", "HOG_Ravenclaw_CommonRoom", "HOG_Slytherin_CommonRoom"}

def _get_grouped_locations():
    """Return LOCATION_ACTIVITIES grouped for UI dropdowns."""
    groups = {
        "Hogsmeade": [],
        "Hogwarts": [],
        "Common Rooms": [],
    }
    for loc_id, entry in LOCATION_ACTIVITIES.items():
        item = {"id": loc_id, "display": entry["display"]}
        if loc_id.startswith("HM_"):
            groups["Hogsmeade"].append(item)
        elif loc_id in _COMMON_ROOM_IDS:
            groups["Common Rooms"].append(item)
        elif loc_id.startswith("HOG_"):
            groups["Hogwarts"].append(item)

    # Sort within each group
    for group in groups.values():
        group.sort(key=lambda x: x["display"])

    return groups


_cached_locations = _get_grouped_locations()


@commitments_bp.route('/api/commitments/locations', methods=['GET'])
def get_locations():
    """Return grouped location list for UI dropdowns."""
    return jsonify(_cached_locations)


# ============================================
# Commitments CRUD
# ============================================

@commitments_bp.route('/api/commitments', methods=['GET'])
def get_commitments():
    """List all commitments, optionally filtered by NPC."""
    npc_id = request.args.get('npc_id')
    commitments = commitments_db.get_all_commitments(npc_id=npc_id)
    return jsonify(commitments)


@commitments_bp.route('/api/commitments/<commitment_id>', methods=['GET'])
def get_commitment(commitment_id):
    """Get a single commitment by ID."""
    commitment = commitments_db.get_commitment(commitment_id)
    if not commitment:
        return jsonify({"error": "Commitment not found"}), 404
    return jsonify(commitment)


@commitments_bp.route('/api/commitments', methods=['POST'])
def create_commitment():
    """Create a new commitment from the UI."""
    try:
        data = request.get_json()
        npc_id = data.get('npc_id')
        location_id = data.get('location_id')
        game_time_start = data.get('game_time_start')

        if not all([npc_id, location_id, game_time_start]):
            return jsonify({"error": "Missing required fields: npc_id, location_id, game_time_start"}), 400

        game_context = _get_game_context()
        player_name = game_context.get('playerName', 'The player')

        success, error_msg, commitment = create_commitment_from_ui(
            npc_id=npc_id,
            location_id=location_id,
            game_time_start=game_time_start,
            game_context=game_context,
            lua_socket=_lua_socket,
            player_name=player_name,
        )

        if not success:
            return jsonify({"error": error_msg}), 400

        print(f"[Commitments] UI created: {commitment['id']} - {npc_id} at {commitment['location_display']}")
        return jsonify(commitment), 201

    except Exception as e:
        print(f"[Commitments] Error creating commitment: {e}")
        return jsonify({"error": str(e)}), 400


@commitments_bp.route('/api/commitments/<commitment_id>', methods=['PUT'])
def update_commitment(commitment_id):
    """Modify an existing non-terminal commitment."""
    try:
        data = request.get_json()
        commitment = commitments_db.get_commitment(commitment_id)
        if not commitment:
            return jsonify({"error": "Commitment not found"}), 404

        if commitment["status"] in ('completed', 'no_show', 'cancelled'):
            return jsonify({"error": f"Cannot edit a {commitment['status']} commitment"}), 400

        # Build update fields
        updates = {}
        location_id = data.get('location_id')
        game_time_start = data.get('game_time_start')

        if location_id and location_id != commitment['location_id']:
            if location_id not in LOCATION_ACTIVITIES:
                return jsonify({"error": f"Unknown location: {location_id}"}), 400
            loc_entry = LOCATION_ACTIVITIES[location_id]
            updates['location_id'] = location_id
            updates['location_display'] = loc_entry['display']
            updates['activity_id'] = loc_entry['activity']

        if game_time_start and game_time_start != commitment['game_time_start']:
            updates['game_time_start'] = game_time_start
            updates['game_time_end'] = _add_game_minutes(game_time_start, COMMITMENT_WAIT_TIME_MIN)
            updates['override_apply_time'] = _subtract_game_minutes(game_time_start, COMMITMENT_TRAVEL_TIME_MIN)

        if not updates:
            return jsonify(commitment)

        # Recalculate times if location changed but time didn't (or vice versa)
        final_start = updates.get('game_time_start', commitment['game_time_start'])
        final_end = updates.get('game_time_end', commitment['game_time_end'])
        final_apply = updates.get('override_apply_time', commitment['override_apply_time'])
        final_npc = commitment['npc_id']

        # Conflict check (exclude self)
        conflicts = commitments_db.get_commitments_in_window(final_npc, final_apply, final_end)
        conflicts = [c for c in conflicts if c['id'] != commitment_id]
        if conflicts:
            conflict_locs = ", ".join(c["location_display"] for c in conflicts)
            return jsonify({"error": f"Conflicts with existing commitment(s) at {conflict_locs}"}), 400

        # Apply update
        success = commitments_db.update_commitment(commitment_id, **updates)
        if not success:
            return jsonify({"error": "Failed to update commitment"}), 500

        # If commitment was active, deactivate old and activate new
        was_active = commitment["status"] in ("active", "arrived")
        if was_active and _lua_socket:
            _lua_socket.send_deactivate_commitment(commitment["npc_id"], commitment["activity_id"])
            new_activity = updates.get('activity_id', commitment['activity_id'])
            new_location = updates.get('location_id', commitment['location_id'])
            _lua_socket.send_activate_commitment(commitment["npc_id"], new_activity, new_location)

        updated = commitments_db.get_commitment(commitment_id)
        print(f"[Commitments] Updated: {commitment_id}")
        return jsonify(updated)

    except Exception as e:
        print(f"[Commitments] Error updating commitment: {e}")
        return jsonify({"error": str(e)}), 400


@commitments_bp.route('/api/commitments/<commitment_id>', methods=['DELETE'])
def delete_commitment(commitment_id):
    """Cancel a commitment."""
    try:
        commitment = commitments_db.get_commitment(commitment_id)
        if not commitment:
            return jsonify({"error": "Commitment not found"}), 404

        status = commitment["status"]
        was_active = status in ("active", "arrived")
        is_terminal = status in ("completed", "no_show", "cancelled")

        commitments_db.cancel_commitment(commitment_id)

        # Release Lua override if active
        if was_active and _lua_socket:
            _lua_socket.send_deactivate_commitment(commitment["npc_id"], commitment["activity_id"])

        # Only inject cancellation event for non-terminal commitments
        if not is_terminal:
            game_context = _get_game_context()
            current_time = _get_current_game_time_str(game_context) if game_context else None
            player_name = game_context.get('playerName', 'The player') if game_context else 'The player'
            _inject_commitment_event(commitment, "player_cancelled", player_name, current_time)

        print(f"[Commitments] Cancelled via UI: {commitment_id} (was {status})")
        return jsonify({"status": "ok", "id": commitment_id})

    except Exception as e:
        print(f"[Commitments] Error cancelling commitment: {e}")
        return jsonify({"error": str(e)}), 400
