"""
Owl Post API endpoints for Sonorus.

Handles mail (send/receive letters) and bulletin board operations.
"""

import os
import time
import threading
import wave
from urllib.parse import urlencode

import numpy as np

from flask import Blueprint, request, jsonify, send_file, make_response, redirect

from utils.settings import SONORUS_DIR, load_settings
from utils.owl_custom_characters import (
    get_allowed_owl_mail_recipient_ids,
    get_custom_owl_character,
    is_allowed_owl_mail_recipient,
)
from utils.owl_post_db import (
    get_player_mail, send_mail, get_mail_thread, mark_mail_read, delete_mail, delete_mail_thread,
    get_unread_mail_count, get_current_game_minutes,
    get_all_boards, get_board_by_slug, mark_board_visited,
    is_board_unlocked, unlock_board,
    get_board_threads, get_thread_posts, create_board_post,
    mark_post_read, mark_thread_read,
    get_unread_count_per_board, delete_future_replies,
    get_connection,
    get_owl_log, clear_owl_log, clear_all_board_posts, clear_all_mail,
    get_proposals_for_thread, get_proposal, update_proposal_status,
    get_pending_proposal_counts,
    thread_has_correspondent,
)
from utils.owl_delivery import calculate_delivery_minutes


OWLPOST_HTML = os.path.join(SONORUS_DIR, "owlpost.html")

owlpost_bp = Blueprint('owlpost', __name__, url_prefix='/owlpost')


def _is_player_context_ready():
    try:
        from utils import player_context
        return player_context.is_ready()
    except Exception:
        return False


def _no_player_loaded_response():
    return jsonify({"success": False, "error": "Player context not ready"}), 400


# Injected by server.py
_lua_socket = None
_load_game_context = None
_board_orchestrator = None


def set_lua_socket(socket):
    global _lua_socket
    _lua_socket = socket


def set_load_game_context(func):
    global _load_game_context
    _load_game_context = func


def set_board_orchestrator(orchestrator):
    global _board_orchestrator
    _board_orchestrator = orchestrator


def _get_game_context():
    if _load_game_context:
        return _load_game_context()
    return None


# ============================================
# Access Control
# ============================================

def _check_board_access(board, player_house):
    """Check whether the player can access a board.

    Returns (allowed: bool, reason: str or None).
    """
    access_type = board.get("access_type", "public")

    if access_type == "public":
        return (True, None)

    if access_type == "decorative":
        return (False, "This board is sealed with an enchantment.")

    if access_type == "house_locked":
        board_house = (board.get("house") or "").lower()
        p_house = (player_house or "").lower()
        if board_house == p_house:
            return (True, None)
        return (False, f"Only {board.get('house', 'house')} students may access this board.")

    if access_type == "password_locked":
        if is_board_unlocked(board["id"]):
            return (True, None)
        return (False, "This board is locked. A password is required.")

    return (False, "Unknown access type.")


# ============================================
# Owl Post Page & Static Files
# ============================================

@owlpost_bp.route('/')
def owlpost_page():
    """Serve the main Owl Post page. Injects boards param from settings."""
    if 'boards' not in request.args:
        settings = load_settings(raw=True)
        boards = '1' if settings.get('owl_post', {}).get('boards_enabled', True) else '0'
        args = request.args.to_dict()
        args['boards'] = boards
        return redirect(f'/owlpost/?{urlencode(args)}')
    if os.path.exists(OWLPOST_HTML):
        return send_file(OWLPOST_HTML)
    return "Owl Post page not found", 404


@owlpost_bp.route('/api/hotkey')
def api_hotkey():
    """Return the configured owl post hotkey for JS close-on-key."""
    settings = load_settings(raw=True)
    key = settings.get('input', {}).get('owlpost_hotkey', 'backquote')
    return jsonify({"hotkey": key})


@owlpost_bp.route('/api/display-names')
def api_display_names():
    """Return a mapping of voice IDs to display names for the frontend."""
    try:
        from utils.localization import get_display_name
        from utils.owl_custom_characters import load_builtin_owl_mail_recipient_ids, load_custom_owl_characters
        names = {}
        for voice_id in load_builtin_owl_mail_recipient_ids():
            names[voice_id] = get_display_name(voice_id)
        for entry in load_custom_owl_characters():
            char_id = entry.get("id")
            if not char_id:
                continue
            names[char_id] = get_display_name(char_id)
        return jsonify(names)
    except Exception as e:
        print(f"[OwlPost] Error loading display names: {e}")
        return jsonify({}), 200


@owlpost_bp.route('/api/recipients')
def api_recipients():
    """Return allowed Owl Mail compose recipients."""
    try:
        from utils.localization import get_display_name

        ctx = _get_game_context() or {}
        mission_statuses = ctx.get("missionStatuses")
        recipients = []
        for voice_id in get_allowed_owl_mail_recipient_ids(mission_statuses=mission_statuses):
            custom_entry = get_custom_owl_character(voice_id)
            recipients.append({
                "id": voice_id,
                "name": (
                    (custom_entry.get("name") if custom_entry else None)
                    or get_display_name(voice_id)
                    or voice_id
                ),
                "custom": custom_entry is not None,
            })

        recipients.sort(key=lambda item: ((item.get("name") or item.get("id") or "").lower(), item.get("id") or ""))
        return jsonify({"recipients": recipients})
    except Exception as e:
        print(f"[OwlPost] Error loading recipients: {e}")
        return jsonify({"recipients": []}), 200


STATIC_CACHE_MAX_AGE = 86400  # 24 hours

MIME_MAP = {
    '.ttf': 'font/ttf',
    '.otf': 'font/otf',
    '.woff2': 'font/woff2',
    '.css': 'text/css',
    '.js': 'application/javascript',
    '.mp3': 'audio/mpeg',
    '.wav': 'audio/wav',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.svg': 'image/svg+xml',
}


def _send_cached(file_path, fallback_mimetype=None):
    """Send a static file with cache headers."""
    ext = os.path.splitext(file_path)[1].lower()
    mimetype = MIME_MAP.get(ext, fallback_mimetype)
    resp = make_response(send_file(file_path, mimetype=mimetype))
    resp.headers['Cache-Control'] = f'public, max-age={STATIC_CACHE_MAX_AGE}'
    return resp


@owlpost_bp.route('/js/<path:filename>')
def serve_js(filename):
    """Serve static JS files from sonorus/js/ folder."""
    js_file = os.path.join(SONORUS_DIR, "js", filename)
    if os.path.exists(js_file):
        return _send_cached(js_file)
    return "File not found", 404


@owlpost_bp.route('/css/<path:filename>')
def serve_css(filename):
    """Serve static files from sonorus/css/ folder (CSS, fonts)."""
    css_file = os.path.join(SONORUS_DIR, "css", filename)
    if os.path.exists(css_file):
        return _send_cached(css_file)
    return "File not found", 404


@owlpost_bp.route('/sounds/<path:filename>')
def serve_sounds(filename):
    """Serve sound files from sonorus/sounds/ folder."""
    snd_file = os.path.join(SONORUS_DIR, "sounds", filename)
    if os.path.exists(snd_file):
        return _send_cached(snd_file)
    return "File not found", 404


@owlpost_bp.route('/images/<path:filename>')
def serve_owlpost_images(filename):
    """Serve static image files from sonorus/images/ folder."""
    img_file = os.path.join(SONORUS_DIR, "images", filename)
    if os.path.exists(img_file):
        return _send_cached(img_file)
    return "File not found", 404


# ============================================
# Mail API
# ============================================

@owlpost_bp.route('/api/mail', methods=['GET'])
def api_get_mail():
    """Get player inbox and sent mail."""
    try:
        ctx = _get_game_context()
        if ctx is None:
            return jsonify({"error": "Game not connected"}), 503
        current_minutes = get_current_game_minutes(ctx)
        mail = get_player_mail(current_minutes)
        # Attach pending proposal flag per mail
        mail_ids = [m["id"] for m in mail]
        pending_map = get_pending_proposal_counts(mail_ids)
        for m in mail:
            m["pending_proposals"] = pending_map.get(m["id"], 0)
        return jsonify({"mail": mail})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@owlpost_bp.route('/api/mail', methods=['POST'])
def api_send_mail():
    """Player sends a letter."""
    try:
        data = request.get_json(force=True)
        recipient = str(data.get("recipient") or "").strip()
        subject = str(data.get("subject") or "")
        body = str(data.get("body") or "")

        if not recipient or not subject or not body:
            return jsonify({"error": "recipient, subject, and body are required"}), 400

        # Reply = reuse thread_id from frontend; new compose = None -> generates new UUID
        raw_thread_id = data.get("thread_id")
        thread_id = str(raw_thread_id).strip() if raw_thread_id is not None else None
        if thread_id == "":
            thread_id = None

        if thread_id:
            if not thread_has_correspondent(thread_id, recipient):
                return jsonify({"error": "Reply thread does not match this recipient"}), 400

        ctx = _get_game_context()
        if ctx is None:
            return jsonify({"error": "Game not connected"}), 503
        if not thread_id and not is_allowed_owl_mail_recipient(recipient, mission_statuses=ctx.get("missionStatuses")):
            return jsonify({"error": "Unknown Owl Post recipient"}), 400
        current_minutes = get_current_game_minutes(ctx)

        # Calculate delivery time
        delivery_minutes = calculate_delivery_minutes(None, ctx)
        arrives_at = current_minutes + delivery_minutes

        mail_id, thread_id = send_mail(
            sender="player",
            recipient=recipient,
            subject=subject,
            body=body,
            sent_at=current_minutes,
            arrives_at=arrives_at,
            thread_id=thread_id,
            player_name=ctx.get("playerName"),
        )

        if mail_id is None:
            return jsonify({"error": "Failed to send mail"}), 500

        # Summarize in background if long enough to be worth condensing
        if len(body) > 65:
            def _summarize():
                try:
                    from runtime.owl_orchestrator import summarize_letter
                    summarize_letter(mail_id, "player", subject, body)
                except Exception as e:
                    print(f"[OwlPost] Player letter summarize error: {e}")

            threading.Thread(target=_summarize, daemon=True).start()

        return jsonify({
            "mail_id": mail_id,
            "thread_id": thread_id,
            "arrives_at": arrives_at,
            "delivery_minutes": delivery_minutes,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@owlpost_bp.route('/api/mail/<int:mail_id>/read', methods=['POST'])
def api_mark_mail_read(mail_id):
    """Mark a mail message as read."""
    try:
        mark_mail_read(mail_id)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@owlpost_bp.route('/api/mail/<int:mail_id>', methods=['DELETE'])
def api_delete_mail(mail_id):
    """Delete a mail message and its cached audio."""
    try:
        row = delete_mail(mail_id)
        if row is None:
            return jsonify({"error": "Mail not found"}), 404

        # Clean up cached audio WAV
        sender = row.get("sender", "")
        cached = _owl_archive_path(mail_id, sender)
        if cached:
            try:
                os.remove(cached)
            except OSError:
                pass

        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@owlpost_bp.route('/api/mail/thread/<thread_id>', methods=['DELETE'])
def api_delete_thread(thread_id):
    """Delete all messages in a mail thread and their cached audio."""
    try:
        rows = delete_mail_thread(thread_id)
        if not rows:
            return jsonify({"error": "Thread not found"}), 404

        # Clean up cached audio WAVs
        for row in rows:
            cached = _owl_archive_path(row["id"], row.get("sender", ""))
            if cached:
                try:
                    os.remove(cached)
                except OSError:
                    pass

        return jsonify({"ok": True, "deleted": len(rows)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@owlpost_bp.route('/api/mail/thread/<thread_id>', methods=['GET'])
def api_get_mail_thread(thread_id):
    """Get all messages in a mail thread, including commitment proposals."""
    try:
        ctx = _get_game_context()
        if ctx is None:
            return jsonify({"error": "Game not connected"}), 503
        current_minutes = get_current_game_minutes(ctx)
        messages = get_mail_thread(thread_id, current_minutes)

        # Attach proposals to their respective messages
        proposals = get_proposals_for_thread(thread_id)
        proposals_by_mail = {}
        for p in proposals:
            proposals_by_mail.setdefault(p["mail_id"], []).append(p)
        for msg in messages:
            msg["proposals"] = proposals_by_mail.get(msg["id"], [])

        return jsonify({"messages": messages})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@owlpost_bp.route('/api/mail/proposals/<int:proposal_id>/accept', methods=['POST'])
def api_accept_proposal(proposal_id):
    """Accept a commitment proposal from a letter — creates the actual commitment."""
    try:
        proposal = get_proposal(proposal_id)
        if not proposal:
            return jsonify({"error": "Proposal not found"}), 404
        if proposal["status"] != "pending":
            return jsonify({"error": f"Proposal already {proposal['status']}"}), 400

        ctx = _get_game_context()
        if ctx is None:
            return jsonify({"error": "Game not connected"}), 503

        # Validate the meet action using existing commitment validation
        from utils.commitments import validate_meet_action
        action = {
            "target": proposal["target"],
            "location": proposal["location"],
            "datetime": proposal["datetime"],
        }
        valid, error_msg, parsed = validate_meet_action(action, proposal["npc_id"], ctx)
        if not valid:
            return jsonify({"error": error_msg}), 400

        # Create the commitment
        from utils import commitments_db
        player_name = ctx.get("playerName", "the student")
        target = proposal["target"]
        target_id = "player" if target.lower() in (player_name.lower(), "player") else target

        companion_id = ctx.get("companionId")
        is_companion = bool(companion_id and companion_id.lower() == proposal["npc_id"].lower())

        commitment_id = commitments_db.create_commitment(
            npc_id=proposal["npc_id"],
            target_id=target_id,
            location_id=parsed["location_id"],
            location_display=parsed["location_display"],
            activity_id=parsed["activity_id"],
            game_time_start=parsed["game_time_start"],
            game_time_end=parsed["game_time_end"],
            override_apply_time=parsed["override_apply_time"],
            is_companion=is_companion,
        )

        if not commitment_id:
            return jsonify({"error": "Failed to create commitment"}), 500

        update_proposal_status(proposal_id, "accepted", commitment_id)

        # Send in-game notification
        from utils.localization import get_display_name
        npc_display = get_display_name(proposal["npc_id"]) or proposal["npc_id"]
        if _lua_socket:
            try:
                _lua_socket.send_notification(
                    f"Meeting with {npc_display} at {parsed['location_display']} confirmed."
                )
            except Exception:
                pass

        return jsonify({
            "ok": True,
            "commitment_id": commitment_id,
            "location": parsed["location_display"],
            "time": parsed["game_time_start"],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@owlpost_bp.route('/api/mail/proposals/<int:proposal_id>/decline', methods=['POST'])
def api_decline_proposal(proposal_id):
    """Decline a commitment proposal from a letter."""
    try:
        proposal = get_proposal(proposal_id)
        if not proposal:
            return jsonify({"error": "Proposal not found"}), 404
        if proposal["status"] != "pending":
            return jsonify({"error": f"Proposal already {proposal['status']}"}), 400

        update_proposal_status(proposal_id, "declined")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================
# Board API
# ============================================

@owlpost_bp.route('/api/boards', methods=['GET'])
def api_list_boards():
    """List all boards with unread counts and access status."""
    try:
        ctx = _get_game_context()
        if ctx is None:
            return jsonify({"error": "Game not connected"}), 503
        current_minutes = get_current_game_minutes(ctx)
        player_house = ctx.get("playerHouse", "")

        boards = get_all_boards()
        unread_counts = get_unread_count_per_board(current_minutes)

        result = []
        for board in boards:
            allowed, reason = _check_board_access(board, player_house)
            result.append({
                "id": board["id"],
                "name": board["name"],
                "slug": board["slug"],
                "description": board["description"],
                "access_type": board["access_type"],
                "unread_count": unread_counts.get(board["id"], 0),
                "accessible": allowed,
                "locked_reason": reason,
            })

        return jsonify({"boards": result, "player_house": player_house})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@owlpost_bp.route('/api/boards/<slug>', methods=['GET'])
def api_get_board(slug):
    """Get threads for a board."""
    try:
        board = get_board_by_slug(slug)
        if not board:
            return jsonify({"error": "Board not found"}), 404

        ctx = _get_game_context()
        if ctx is None:
            return jsonify({"error": "Game not connected"}), 503
        current_minutes = get_current_game_minutes(ctx)
        player_house = ctx.get("playerHouse", "")

        allowed, reason = _check_board_access(board, player_house)
        if not allowed:
            return jsonify({"error": reason}), 403

        # Mark board as visited
        mark_board_visited(slug, current_minutes)

        threads = get_board_threads(board["id"], current_minutes)

        return jsonify({
            "board": {
                "id": board["id"],
                "name": board["name"],
                "slug": board["slug"],
                "description": board["description"],
                "access_type": board["access_type"],
            },
            "threads": threads,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@owlpost_bp.route('/api/boards/<slug>/<int:root_post_id>', methods=['GET'])
def api_get_thread(slug, root_post_id):
    """Get all posts in a board thread."""
    try:
        board = get_board_by_slug(slug)
        if not board:
            return jsonify({"error": "Board not found"}), 404

        ctx = _get_game_context()
        if ctx is None:
            return jsonify({"error": "Game not connected"}), 503
        current_minutes = get_current_game_minutes(ctx)
        player_house = ctx.get("playerHouse", "")

        allowed, reason = _check_board_access(board, player_house)
        if not allowed:
            return jsonify({"error": reason}), 403

        posts = get_thread_posts(root_post_id, current_minutes)
        return jsonify({"posts": posts})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@owlpost_bp.route('/api/boards/<slug>', methods=['POST'])
def api_create_post(slug):
    """Player posts or replies on a board."""
    try:
        board = get_board_by_slug(slug)
        if not board:
            return jsonify({"error": "Board not found"}), 404

        ctx = _get_game_context()
        if ctx is None:
            return jsonify({"error": "Game not connected"}), 503
        current_minutes = get_current_game_minutes(ctx)
        player_house = ctx.get("playerHouse", "")

        allowed, reason = _check_board_access(board, player_house)
        if not allowed:
            return jsonify({"error": reason}), 403

        data = request.get_json(force=True)
        body = data.get("body")
        title = data.get("title")
        parent_id = data.get("parent_id")
        root_post_id = data.get("root_post_id")

        if not body:
            return jsonify({"error": "body is required"}), 400

        # If this is a reply, delete future (not-yet-visible) NPC replies
        if root_post_id is not None:
            delete_future_replies(root_post_id, current_minutes)

        post_id = create_board_post(
            board_id=board["id"],
            author="player",
            title=title,
            body=body,
            created_at=current_minutes,
            visible_at=current_minutes,
            root_post_id=root_post_id,
            parent_id=parent_id,
        )

        if post_id is None:
            return jsonify({"error": "Failed to create post"}), 500

        # Trigger board orchestrator reply generation in a background thread
        # For new topics (root_post_id is None), the new post_id becomes the root
        reply_root = root_post_id if root_post_id is not None else post_id
        if _board_orchestrator is not None:
            def _generate_replies():
                try:
                    _board_orchestrator.generate_replies_to_player(
                        board=board,
                        root_post_id=reply_root,
                        game_minutes=current_minutes,
                    )
                except Exception as e:
                    print(f"[OwlPost] Error generating board replies: {e}")

            thread = threading.Thread(target=_generate_replies, daemon=True)
            thread.start()

        return jsonify({
            "post_id": post_id,
            "root_post_id": root_post_id if root_post_id is not None else post_id,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@owlpost_bp.route('/api/boards/<slug>/unlock', methods=['POST'])
def api_unlock_board(slug):
    """Submit a password to unlock a board."""
    try:
        board = get_board_by_slug(slug)
        if not board:
            return jsonify({"error": "Board not found"}), 404

        if board.get("access_type") != "password_locked":
            return jsonify({"error": "This board does not require a password"}), 400

        data = request.get_json(force=True)
        password = data.get("password", "")

        if (board.get("password") or "").lower() == password.lower():
            ctx = _get_game_context()
            if ctx is None:
                return jsonify({"error": "Game not connected"}), 503
            current_minutes = get_current_game_minutes(ctx)
            unlock_board(board["id"], current_minutes)
            return jsonify({"ok": True})
        else:
            return jsonify({"error": "Incorrect password"}), 403
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@owlpost_bp.route('/api/boards/posts/<int:post_id>/read', methods=['POST'])
def api_mark_post_read(post_id):
    """Mark a single board post as read."""
    try:
        mark_post_read(post_id)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@owlpost_bp.route('/api/boards/<slug>/<int:root_post_id>/read-all', methods=['POST'])
def api_mark_thread_read(slug, root_post_id):
    """Mark all visible posts in a thread as read."""
    try:
        board = get_board_by_slug(slug)
        if not board:
            return jsonify({"error": "Board not found"}), 404

        ctx = _get_game_context()
        if ctx is None:
            return jsonify({"error": "Game not connected"}), 503
        current_minutes = get_current_game_minutes(ctx)
        player_house = ctx.get("playerHouse", "")

        allowed, reason = _check_board_access(board, player_house)
        if not allowed:
            return jsonify({"error": reason}), 403

        mark_thread_read(root_post_id, current_minutes)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================
# TTS Archive & Dual-Mode Playback
# ============================================

OWL_MAIL_ARCHIVE_DIR = os.path.join(SONORUS_DIR, "data", "owl_mail")
MAX_OWL_MAIL_ARCHIVES = 100


def _owl_archive_reinit(data_dir):
    """Update owl mail archive directory to new player data dir."""
    global OWL_MAIL_ARCHIVE_DIR
    OWL_MAIL_ARCHIVE_DIR = os.path.join(data_dir, "owl_mail")


try:
    from utils import player_context
    player_context.register("owl_mail_archive", lambda: None, _owl_archive_reinit)
except ImportError:
    pass


def _safe_sender(sender):
    """Sanitize sender name for filesystem."""
    return ''.join(c if c.isalnum() or c in '-_' else '_' for c in sender)


def _owl_archive_filename(mail_id, sender):
    return f"mail_{mail_id}_{_safe_sender(sender)}.wav"


def _owl_archive_path(mail_id, sender):
    """Return path if cached WAV exists, else None."""
    path = os.path.join(OWL_MAIL_ARCHIVE_DIR, _owl_archive_filename(mail_id, sender))
    return path if os.path.exists(path) else None


def _save_owl_archive(mail_id, sender, pcm_bytes, sample_rate):
    """Write WAV to archive, prune oldest if over limit."""
    os.makedirs(OWL_MAIL_ARCHIVE_DIR, exist_ok=True)
    path = os.path.join(OWL_MAIL_ARCHIVE_DIR, _owl_archive_filename(mail_id, sender))
    with wave.open(path, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    # Prune oldest archives if over limit
    try:
        archives = sorted(
            [f for f in os.listdir(OWL_MAIL_ARCHIVE_DIR) if f.endswith('.wav')],
            key=lambda f: os.path.getmtime(os.path.join(OWL_MAIL_ARCHIVE_DIR, f))
        )
        while len(archives) > MAX_OWL_MAIL_ARCHIVES:
            oldest = archives.pop(0)
            os.remove(os.path.join(OWL_MAIL_ARCHIVE_DIR, oldest))
    except OSError:
        pass


# Active letter playback state — only one letter plays at a time
_letter_playback_lock = threading.Lock()
_letter_playback_abort = threading.Event()
_letter_playback_active = False
_letter_playback_mail_id = None
_letter_synthesis_pending = set()  # mail_ids with synthesis still running in background

# Conversation state — injected by server.py
_conv_state = None


def set_conv_state(cs):
    global _conv_state
    _conv_state = cs


@owlpost_bp.route('/api/mail/<int:mail_id>/read-aloud', methods=['POST'])
def api_read_letter_aloud(mail_id):
    """Stream TTS of a letter through game audio, or return cached URL."""
    global _letter_playback_active, _letter_playback_mail_id

    try:
        with get_connection() as conn:
            row = conn.execute("SELECT * FROM mail WHERE id = ?", (mail_id,)).fetchone()
        if row is None:
            return jsonify({"error": "Mail not found"}), 404

        sender = row["sender"]
        body = row["body"]
        mail_type = row["mail_type"] if "mail_type" in row.keys() else "letter"
        if sender == "player":
            return jsonify({"error": "Cannot read player's own letters"}), 400

        # Check cache — return URL for browser playback
        cached_path = _owl_archive_path(mail_id, sender)
        if cached_path:
            filename = os.path.basename(cached_path)
            return jsonify({"mode": "cached", "url": f"/owlpost/api/owl-archive/{filename}"})

        # Synthesis still running from a previous stop — WAV not saved yet
        with _letter_playback_lock:
            if mail_id in _letter_synthesis_pending:
                return jsonify({"mode": "pending"})

        # Yield to in-game conversation TTS
        if _conv_state and _conv_state.state != "idle":
            return jsonify({"error": "Voice currently in use"}), 409

        try:
            from services import tts

            provider = tts.get_provider()
            voice = provider.get_or_create_voice(sender)
            voice_id = None
            if isinstance(voice, dict):
                voice_id = voice.get('voiceId') or voice.get('voice_id')
            if not voice_id:
                raise RuntimeError("No voice ID available")
        except Exception as e:
            print(f"[OwlPost] No TTS voice available for '{sender}': {e}")
            from utils.localization import get_display_name
            sender_name = get_display_name(sender) or sender
            return jsonify({"error": f"No TTS voice available for {sender_name}."}), 404

        # Stop any currently playing letter
        _letter_playback_abort.set()
        with _letter_playback_lock:
            _letter_playback_abort.clear()
            _letter_playback_active = True
            _letter_playback_mail_id = mail_id

        def _play():
            global _letter_playback_active, _letter_playback_mail_id
            try:
                from services import tts
                from audio import create_tts_stream, get_player

                provider = tts.get_provider()

                sample_rate = provider.get_sample_rate()
                stream = create_tts_stream(sample_rate=sample_rate, channels=1)
                pcm_collector = bytearray()
                text = tts._apply_pronunciation(body)

                # Normalize repeated exclamation marks for TTS readability
                # Pocket TTS handles up to 2, other providers max 1
                if mail_type == "howler":
                    import re
                    max_bangs = 2 if tts.get_provider_name() == 'pocket' else 1
                    text = re.sub(r'!{2,}', '!' * max_bangs, text)

                with _letter_playback_lock:
                    _letter_synthesis_pending.add(mail_id)

                # Run synthesis in a sub-thread so playback can start after buffering
                synthesis_done = threading.Event()
                synthesis_ok = [False]

                def on_chunk(pcm_bytes, word_timing):
                    if pcm_bytes:
                        # Howler audio processing: gain boost + tanh saturation
                        # Applied to raw PCM so both live playback and cached WAV get the effect
                        if mail_type == "howler":
                            samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
                            samples *= 2.0
                            samples = np.tanh(samples * 1.5)
                            pcm_bytes = (samples * 32767.0).clip(-32767, 32767).astype(np.int16).tobytes()
                        stream.feed(pcm_bytes)
                        pcm_collector.extend(pcm_bytes)

                def _synthesize():
                    try:
                        synthesis_ok[0] = provider.synthesize_stream(
                            text, voice_id, on_chunk, speaker_id=sender
                        )
                    except Exception as e:
                        print(f"[OwlPost] Synthesis error: {e}")
                    finally:
                        stream.finish()
                        synthesis_done.set()

                synth_thread = threading.Thread(target=_synthesize, daemon=True)
                synth_thread.start()

                # Wait for 0.6s of audio to buffer before starting playback
                threshold_bytes = int(sample_rate * 2 * 0.6)  # 16-bit mono
                while not stream.stream_complete and stream._total_fed < threshold_bytes:
                    time.sleep(0.05)
                    if _letter_playback_abort.is_set():
                        break

                # Play through game audio (blocks until done or aborted)
                if not _letter_playback_abort.is_set():
                    player = get_player()

                    def abort_check():
                        if _letter_playback_abort.is_set():
                            return True
                        if _conv_state and _conv_state.state != "idle":
                            return True
                        return False

                    player.play_stream(stream, use_3d=False, abort_check=abort_check)

                # Wait for synthesis to finish so we can save the WAV
                synthesis_done.wait(timeout=60)

                # Save cached WAV
                if synthesis_ok[0] and pcm_collector:
                    try:
                        _save_owl_archive(mail_id, sender, bytes(pcm_collector), sample_rate)
                        print(f"[OwlPost] Cached letter audio: mail_{mail_id}")
                    except Exception as e:
                        print(f"[OwlPost] Failed to cache audio: {e}")

            except Exception as e:
                print(f"[OwlPost] TTS error: {e}")
            finally:
                with _letter_playback_lock:
                    _letter_synthesis_pending.discard(mail_id)
                    if _letter_playback_mail_id == mail_id:
                        _letter_playback_active = False
                        _letter_playback_mail_id = None

        threading.Thread(target=_play, daemon=True).start()
        return jsonify({"mode": "streaming", "mail_id": mail_id})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@owlpost_bp.route('/api/mail/stop-reading', methods=['POST'])
def api_stop_reading():
    """Stop any currently playing letter TTS (synthesis continues in background)."""
    global _letter_playback_active, _letter_playback_mail_id
    _letter_playback_abort.set()
    with _letter_playback_lock:
        _letter_playback_active = False
        _letter_playback_mail_id = None
    return jsonify({"stopped": True})


@owlpost_bp.route('/api/mail/reading-status')
def api_reading_status():
    """Check if a letter is currently being read aloud."""
    return jsonify({
        "playing": _letter_playback_active,
        "mail_id": _letter_playback_mail_id,
    })


@owlpost_bp.route('/api/mail/<int:mail_id>/check-audio')
def api_check_audio(mail_id):
    """Lightweight cache check for a letter's audio."""
    with get_connection() as conn:
        row = conn.execute("SELECT sender FROM mail WHERE id = ?", (mail_id,)).fetchone()
    if row is None:
        return jsonify({"cached": False, "pending": False})

    cached_path = _owl_archive_path(mail_id, row["sender"])
    if cached_path:
        filename = os.path.basename(cached_path)
        return jsonify({"cached": True, "url": f"/owlpost/api/owl-archive/{filename}"})

    with _letter_playback_lock:
        pending = mail_id in _letter_synthesis_pending
    return jsonify({"cached": False, "pending": pending})


@owlpost_bp.route('/api/owl-archive/<filename>')
def serve_owl_archive(filename):
    """Serve cached letter audio WAV files."""
    filepath = os.path.join(OWL_MAIL_ARCHIVE_DIR, filename)
    if not os.path.abspath(filepath).startswith(os.path.abspath(OWL_MAIL_ARCHIVE_DIR)):
        return "Forbidden", 403
    if os.path.exists(filepath):
        return send_file(filepath, mimetype='audio/wav')
    return "File not found", 404


# ============================================
# Activity Log API
# ============================================

@owlpost_bp.route('/api/log', methods=['GET'])
def get_activity_log():
    """Return recent owl post activity log entries with resolved display names."""
    if not _is_player_context_ready():
        return jsonify([])

    from utils.localization import get_display_name

    limit = request.args.get('limit', 200, type=int)
    entries = get_owl_log(limit=min(limit, 200))
    for entry in entries:
        npc_id = entry.get('npc_id')
        if npc_id:
            entry['npc_name'] = get_display_name(npc_id)
    return jsonify(entries)


@owlpost_bp.route('/api/log', methods=['DELETE'])
def clear_activity_log():
    """Clear all owl post activity log entries."""
    if not _is_player_context_ready():
        return _no_player_loaded_response()

    clear_owl_log()
    return jsonify({"success": True})


@owlpost_bp.route('/api/boards/reset', methods=['DELETE'])
def reset_all_boards():
    """Delete all board posts, preserving board definitions and unlocks."""
    if not _is_player_context_ready():
        return _no_player_loaded_response()

    clear_all_board_posts()
    return jsonify({"success": True})


@owlpost_bp.route('/api/mail/reset', methods=['DELETE'])
def reset_all_mail():
    """Delete all owl mail, mail generation state, and cached read-aloud audio."""
    try:
        if not _is_player_context_ready():
            return _no_player_loaded_response()

        rows = clear_all_mail()

        # Clean up cached audio WAVs for deleted mail, plus any orphaned archive files.
        for row in rows:
            cached = _owl_archive_path(row["id"], row.get("sender", ""))
            if cached:
                try:
                    os.remove(cached)
                except OSError:
                    pass

        try:
            archive_root = os.path.abspath(OWL_MAIL_ARCHIVE_DIR)
            if os.path.isdir(archive_root):
                for filename in os.listdir(archive_root):
                    if not filename.lower().endswith('.wav'):
                        continue
                    path = os.path.abspath(os.path.join(archive_root, filename))
                    if path.startswith(archive_root + os.sep):
                        os.remove(path)
        except OSError:
            pass

        return jsonify({"success": True, "deleted": len(rows)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
