"""
Owl Post orchestrators: background timers that generate NPC-initiated mail and
bulletin-board content.

OwlMailOrchestrator  — periodic cycle that replies to unanswered letters and
                       decides whether an NPC should write a new one.
OwlBoardOrchestrator — periodic cycle that generates threads and replies on
                       bulletin boards.
"""

from __future__ import annotations

import os
import random
import re
import threading
import time
from typing import Optional

import llm
from utils.dialogue_db import get_entries_for_npc, get_recent_meaningful_npcs, _game_datetime_to_minutes, _parse_game_time, _parse_game_date
from utils.localization import get_display_name
from utils.memory import load_bio, format_bio_for_context, get_character_background_context
from utils.settings import is_llm_provider_feature_disabled, load_settings
from utils.owl_custom_characters import get_custom_owl_character_bio
from utils.character_bios import get_editor_guidance, get_player_static_bio, get_static_bio
from utils.owl_post_db import (
    get_current_game_minutes,
    get_unanswered_player_mail,
    get_recent_mail_for_npc,
    get_unread_mail_count,
    send_mail,
    get_all_boards,
    get_unread_count_per_board,
    get_recent_board_threads,
    get_thread_posts,
    create_board_post,
    load_board_roster,
    get_eval_state,
    save_eval_state,
    log_owl_event,
    update_mail_summary,
    insert_mail_proposal,
    thread_has_correspondent,
)
from utils.owl_custom_characters import is_allowed_owl_mail_recipient
from utils.llm_utils import parse_commitment_actions, strip_commitment_action_tags, extract_json
from utils.dialogue import load_dialogue_history, _npc_witnessed
from utils.commitments import build_owl_mail_commitment_instructions, build_commitment_context
from constants import (
    OWL_MAIL_MIN_DISTANCE, OWL_MAIL_CONVERSATION_COOLDOWN,
    OWL_MAIL_ORCHESTRATOR_INTERVAL, OWL_BOARD_ORCHESTRATOR_INTERVAL,
    OWL_BOARD_UNREAD_CAP, OWL_BOARD_REPLY_STAGGER_MIN, OWL_BOARD_REPLY_STAGGER_MAX,
    OWL_MAIL_UNSOLICITED_QUIET_HOURS_START, OWL_MAIL_UNSOLICITED_QUIET_HOURS_END,
    OWL_MAIL_MIN_NEW_ENTRIES, OWL_MAIL_DIALOGUE_CHAR_BUDGET,
    OWL_MAIL_MIN_HOURS_SINCE_CONTACT,
    get_excluded_npcs,
    is_excluded_npc,
)


# ---------------------------------------------------------------------------
# Dependency injection — set by server.py
# ---------------------------------------------------------------------------

_load_game_context = None
_lua_socket = None


def set_load_game_context(func):
    """Inject the callable that returns the current game context dict."""
    global _load_game_context
    _load_game_context = func


def set_lua_socket(socket):
    """Inject the lua socket for sending notifications."""
    global _lua_socket
    _lua_socket = socket


def _send_notification(text):
    """Send an in-game notification if lua socket is available."""
    if _lua_socket and text:
        try:
            _lua_socket.send_notification(text)
        except Exception:
            pass


_OWL_HOOT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sounds", "owl-hoot.mp3")


def _play_owl_hoot():
    """Play owl hoot sound in a background thread. Silently does nothing on failure."""
    def _play():
        try:
            import soundfile as sf
            import sounddevice as sd
            if not os.path.exists(_OWL_HOOT_PATH):
                return
            data, sr = sf.read(_OWL_HOOT_PATH, dtype='float32')
            sd.play(data, sr)
            sd.wait()
        except Exception:
            pass
    threading.Thread(target=_play, daemon=True).start()


def _get_game_context():
    if _load_game_context:
        return _load_game_context()
    return None


def _is_quiet_hours(game_minutes: float) -> bool:
    """True during in-game night — suppresses unsolicited mail and board posts."""
    hour = (int(game_minutes) % 1440) // 60
    return hour >= OWL_MAIL_UNSOLICITED_QUIET_HOURS_START or hour < OWL_MAIL_UNSOLICITED_QUIET_HOURS_END


def send_owl_post_summary():
    """Send a loading-screen summary notification with unread mail/board counts.

    Called from server.py when loading screen ends (OnLoadingScreenFinished).
    """
    ctx = _get_game_context()
    if not ctx:
        return
    gm = get_current_game_minutes(ctx)

    settings = load_settings()
    if is_llm_provider_feature_disabled("owl_post", settings):
        return
    boards_enabled = settings.get("owl_post", {}).get("boards_enabled", True)

    mail_count = get_unread_mail_count(gm)

    parts = []
    if mail_count:
        parts.append(f"{mail_count} owl {'letter' if mail_count == 1 else 'letters'}")
    if boards_enabled:
        board_counts = get_unread_count_per_board(gm)
        board_total = sum(board_counts.values())
        if board_total:
            parts.append(f"{board_total} new board {'post' if board_total == 1 else 'posts'}")

    if parts:
        _send_notification(f"Owl Post: {' and '.join(parts)} awaiting you.")


# ---------------------------------------------------------------------------
# Helpers shared by both orchestrators
# ---------------------------------------------------------------------------

# Flexible <summary> tag parser — handles missing close tag, ```xml wrapping, etc.
_SUMMARY_RE = re.compile(
    r'<summary>\s*'     # opening tag
    r'(.*?)'            # content (non-greedy)
    r'(?:</summary>)',  # closing tag
    re.DOTALL,
)
_SUMMARY_OPEN_RE = re.compile(r'<summary>\s*(.*)', re.DOTALL)


def _get_summarize_model() -> str:
    """Get model for letter summarization."""
    settings = load_settings()
    owl_model = settings.get("owl_post", {}).get("summarize_model", "")
    if owl_model:
        return owl_model
    # Fall back to orchestrator model, then interjection model
    orch = settings.get("owl_post", {}).get("orchestrator_model", "")
    return orch or settings.get("conversation", {}).get("interjection_model", "gemini-2.5-flash-lite")


def summarize_letter(mail_id: int, sender: str, subject: str, body: str):
    """Generate a concise summary of a letter and store it.

    Can be called from anywhere (orchestrator, API routes, etc.).
    """
    model = _get_summarize_model()
    display = _get_display(sender)

    prompt = (
        f"Summarize this letter as a single concise paragraph.\n\n"
        f"## Rules\n"
        f"1. **Core content only**: Focus on key information — requests, proposals, reactions, decisions. Remove greetings, sign-offs, pleasantries, and filler.\n"
        f"2. **Direct language**: Replace flowery phrasing with direct statements.\n"
        f"3. **Preserve perspective**: The summary must remain addressed to the recipient, as if still part of the letter. Do not narrate or describe what the letter says — write as the author speaking to the recipient.\n"
        f"4. **Preserve names**: Keep all character and location names exactly as written.\n"
        f"5. **Preserve meaning**: Accurately convey the intent of the original letter.\n"
        f"6. **Single paragraph**: No formatting, no bullet points.\n\n"
        f"## Letter\n"
        f"**From:** {display}\n"
        f"**Subject:** {subject}\n\n"
        f"{body}\n\n"
        f"Output your summary inside <summary> tags."
    )

    result = llm.chat_simple(
        prompt, model=model, temperature=0.2, max_tokens=2048,
        context="owl_mail_summarize",
    )
    if result:
        summary = _parse_summary_tag(result)
        if summary:
            update_mail_summary(mail_id, summary)
            print(f"[OwlMail] Summarized mail {mail_id}: {summary[:80]}...")


def _parse_summary_tag(text: str) -> Optional[str]:
    """Extract summary content from LLM response.

    Handles: <summary>text</summary>, unclosed <summary>text,
    ```xml wrapping, code fences around/inside tags.
    """
    # Strip code fences that some models wrap around the whole response
    cleaned = text.strip()
    cleaned = re.sub(r'^```(?:xml)?\s*', '', cleaned)
    cleaned = re.sub(r'\s*```$', '', cleaned)
    cleaned = cleaned.strip()

    # Try closed tag first
    m = _SUMMARY_RE.search(cleaned)
    if m:
        return m.group(1).strip()

    # Unclosed tag — take everything after <summary>
    m = _SUMMARY_OPEN_RE.search(cleaned)
    if m:
        return m.group(1).strip()

    # No tags at all — return the whole response as fallback
    return cleaned if cleaned else None


_MEANINGFUL_ENTRY_TYPES = {'dialogue', 'cutscene', 'combat', 'commitment'}


def _filter_meaningful_entries(entries):
    """Filter out ambient chatter and non-meaningful entry types.

    Keeps: AI dialogue, cutscene, combat, commitments.
    Drops: chatter, spells, location, broom, mount, prompt.
    """
    result = []
    for e in entries:
        entry_type = e.get('type') or 'dialogue'
        if entry_type not in _MEANINGFUL_ENTRY_TYPES:
            continue
        if entry_type == 'dialogue' and not e.get('isAIResponse') and not e.get('isPlayer'):
            continue
        result.append(e)
    return result


def _extract_and_store_proposals(mail_id, npc_id, actions):
    """Store parsed commitment proposals from a letter into the DB.

    Called after send_mail with the mail_id and the pre-parsed actions list.
    """
    for action in actions:
        if action["type"] == "meet":
            proposal_id = insert_mail_proposal(
                mail_id=mail_id,
                npc_id=npc_id,
                target=action["target"],
                location=action["location"],
                datetime_str=action["datetime"],
            )
            if proposal_id:
                print(f"[OwlMail] Stored proposal {proposal_id} from {npc_id}: "
                      f"Meet at {action['location']} on {action['datetime']}")
        # TODO: NPC cancellation via letter not yet supported
        elif action["type"] == "cancel":
            print(f"[OwlMail] NPC {npc_id} requested cancel of {action['commitment_id']} (not yet implemented for mail)")


def _get_owl_prompt(key: str) -> str:
    """Load an owl post prompt from settings, falling back to DEFAULT_SETTINGS."""
    settings = load_settings()
    prompt = settings.get("prompts", {}).get(key, "")
    if not prompt:
        from utils.settings import DEFAULT_SETTINGS
        prompt = DEFAULT_SETTINGS.get("prompts", {}).get(key, "")
    return prompt


def _parse_board_json(text: str) -> Optional[list[dict]]:
    """Parse board thread/reply JSON from LLM output.

    Uses the robust extract_json helper to handle fences, prefixes, etc.
    Returns a list of post dicts or None on failure.
    """
    parsed = extract_json(text)
    if not isinstance(parsed, list):
        return None
    # Validate each item has at minimum author + body
    posts = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        author = str(item.get("author", "")).strip()
        body = str(item.get("body", "")).strip()
        if not author or not body:
            continue
        post = {"author": author, "body": body}
        post_type = str(item.get("type", "reply")).strip().lower()
        post["type"] = post_type if post_type in ("topic", "reply") else "reply"
        post["title"] = str(item.get("title", "")).strip()
        posts.append(post)
    return posts if posts else None


def _get_board_rules_text() -> str:
    """Load board rules from settings, return formatted block or empty string."""
    rules = _get_owl_prompt("owl_board_rules").strip()
    if not rules:
        return ""
    return f"Rules (you must follow these):\n{rules}\n\n"


def _get_npc_board_background(npc_id: str) -> Optional[str]:
    """Load lightweight background context for Owl board flows."""
    custom_bio = get_custom_owl_character_bio(npc_id)
    if custom_bio is not None:
        custom_bio = custom_bio.strip()
        return custom_bio or None

    bio = load_bio(npc_id)
    if bio and isinstance(bio, dict):
        formatted = format_bio_for_context(bio)
        if formatted:
            return re.sub(r"\s+", " ", formatted).strip()

    static_bio = get_static_bio(npc_id=npc_id)
    if static_bio:
        return static_bio

    guidance = get_editor_guidance(npc_id=npc_id)
    if guidance:
        return guidance
    return None


def _get_npc_mail_background_context(npc_id: str, player_name: str) -> Optional[str]:
    """Build shared character background context for Owl Mail flows."""
    custom_bio = get_custom_owl_character_bio(npc_id)
    if custom_bio is not None:
        custom_bio = custom_bio.strip()
        sections = []
        if custom_bio:
            sections.append(f"### About You\n**Background:** {custom_bio}")
        player_bio = get_player_static_bio(player_name=player_name)
        if player_bio:
            sections.append(f"### About {player_name}\n**Background:** {player_bio}")
        return "\n\n".join(sections) if sections else None

    return get_character_background_context(
        npc_id=npc_id,
        npc_name=_get_display(npc_id),
        player_name=player_name,
    )


def _filter_dialogue_since_last_mail(dialogue_history: list, mail_history: list) -> list:
    """Filter dialogue to entries after the last mail, then truncate to character budget.

    Returns the filtered list (most recent entries within budget).
    """
    if not dialogue_history:
        return []

    # Time filter: only entries after the last letter
    last_mail_time = 0
    if mail_history:
        last_mail_time = max(m.get("sent_at", 0) for m in mail_history)

    if last_mail_time:
        filtered = []
        for e in dialogue_history:
            gt = _parse_game_time(e.get("gameTime", ""))
            gd = _parse_game_date(e.get("gameDate", ""))
            if gt is not None and gd is not None:
                entry_gm = _game_datetime_to_minutes(gd, gt)
                if entry_gm > last_mail_time:
                    filtered.append(e)
            else:
                filtered.append(e)
    else:
        filtered = dialogue_history

    if not filtered:
        return []

    # Budget truncation: walk backwards, keep most recent within limit
    total = 0
    start = 0
    for i in range(len(filtered) - 1, -1, -1):
        total += len(filtered[i].get('text', '')) + 40  # ~40 chars overhead per line
        if total > OWL_MAIL_DIALOGUE_CHAR_BUDGET:
            start = i + 1
            break

    return filtered[start:]


def _build_context_with_separator(dialogue_history: list, mail_history: list) -> str:
    """Build a combined context block showing mail history then conversation since.

    Expects dialogue_history to already be filtered (since last mail, within budget).
    """
    sections = []

    if mail_history:
        capped = mail_history[-50:]
        cutoff = max(0, len(capped) - 3)
        lines = []
        for i, m in enumerate(capped):
            sender = _get_display(m['sender'])
            subject = m['subject']
            if i < cutoff and m.get('summary'):
                lines.append(f"- From {sender}: {subject} — {m['summary']}")
            else:
                lines.append(f"- From {sender}: {subject} — {m['body']}")
        sections.append(f"Previous letters:\n" + "\n".join(lines))

    if mail_history and dialogue_history:
        sections.append("----- Last Letter Correspondence Ends Here -----")

    if dialogue_history:
        dialogue_lines = "\n".join(
            f"- {_get_display(e.get('voiceName', '?'))}: {e.get('text', '')}"
            for e in dialogue_history
        )
        label = "Conversation since last letter:" if mail_history else "Recent conversation:"
        sections.append(f"{label}\n{dialogue_lines}")
    elif not mail_history:
        sections.append("(no recent interaction)")

    return "\n\n".join(sections)


def _get_last_contact_game_minutes(npc_id: str, mail_history: list, dialogue_history: list) -> float:
    """Get the most recent interaction time (game minutes) for an NPC.

    Checks both mail correspondence (sent_at) and dialogue entries.
    Returns 0 if no interaction found.
    """
    last_gm = 0

    # Last mail time
    if mail_history:
        last_mail_gm = max(m.get("sent_at", 0) for m in mail_history)
        last_gm = max(last_gm, last_mail_gm)

    # Last dialogue entry time for this NPC (scan backwards for efficiency)
    for e in reversed(dialogue_history):
        if _npc_witnessed(e, npc_id):
            gt = _parse_game_time(e.get("gameTime", ""))
            gd = _parse_game_date(e.get("gameDate", ""))
            if gt is not None and gd is not None:
                entry_gm = _game_datetime_to_minutes(gd, gt)
                last_gm = max(last_gm, entry_gm)
                break

    return last_gm


def _format_hours_since_contact(hours: float) -> str:
    """Format hours since last contact as a human-readable string."""
    if hours < 1:
        minutes = max(1, int(hours * 60))
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    elif hours < 24:
        h = int(hours)
        return f"about {h} hour{'s' if h != 1 else ''}"
    else:
        days = int(hours / 24)
        return f"about {days} day{'s' if days != 1 else ''}"


def _get_display(npc_id: str) -> str:
    """Friendly display name for an NPC id."""
    if npc_id == "player":
        return _get_player_name()
    return get_display_name(npc_id) or npc_id


def _get_player_name() -> str:
    """Get the player's character name from game context."""
    ctx = _get_game_context()
    return ctx.get("playerName", "the student") if ctx else "the student"


def _get_player_bio() -> str:
    """Get the player's bio from settings."""
    return get_player_static_bio(player_name=_get_player_name())


# ============================================================================
# OwlMailOrchestrator
# ============================================================================

class OwlMailOrchestrator:
    """Background timer that generates NPC-initiated letters to the player."""

    def __init__(self):
        self._timer: Optional[threading.Timer] = None
        self._running = False
        self._lock = threading.Lock()
        self._last_conversation_times: dict[str, float] = {}
        self._last_unread_count: int = -1  # -1 = not yet initialized

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """Start the mail orchestrator timer."""
        if self._running:
            return
        self._running = True
        self._schedule_next()
        print("[OwlMail] Orchestrator started")

    def stop(self):
        """Stop the mail orchestrator timer."""
        self._running = False
        if self._timer:
            self._timer.cancel()
            self._timer = None
        print("[OwlMail] Orchestrator stopped")

    def record_conversation(self, npc_id: str):
        """Record that an in-person conversation just happened with this NPC."""
        self._last_conversation_times[npc_id] = time.time()

    # ------------------------------------------------------------------
    # Timer plumbing
    # ------------------------------------------------------------------

    def _get_owl_setting(self, key, fallback):
        """Read an owl_post setting with fallback to constants default."""
        settings = load_settings()
        return settings.get("owl_post", {}).get(key, fallback)

    def _get_orchestrator_model(self):
        """Get model for classifier/decision calls (defaults to interjection model)."""
        settings = load_settings()
        owl_model = settings.get("owl_post", {}).get("orchestrator_model", "")
        return owl_model or settings.get("conversation", {}).get("interjection_model", "gemini-2.5-flash-lite")

    def _get_mail_model(self):
        """Get model for letter generation (defaults to chat model)."""
        settings = load_settings()
        owl_model = settings.get("owl_post", {}).get("mail_model", "")
        return owl_model or settings.get("conversation", {}).get("chat_model")

    def _schedule_next(self):
        if not self._running:
            return
        interval = self._get_owl_setting("mail_interval", OWL_MAIL_ORCHESTRATOR_INTERVAL)
        self._timer = threading.Timer(interval, self._tick)
        self._timer.daemon = True
        self._timer.start()

    def _tick(self):
        try:
            self._run_cycle()
        except Exception as e:
            print(f"[OwlMail] Error in orchestrator cycle: {e}")
            log_owl_event("error", None, f"Mail cycle error: {e}")
        finally:
            self._schedule_next()

    # ------------------------------------------------------------------
    # Immersion guards
    # ------------------------------------------------------------------

    def _is_npc_nearby(self, npc_id: str, ctx: dict) -> bool:
        """True if the NPC is within OWL_MAIL_MIN_DISTANCE of the player."""
        nearby = ctx.get("nearbyNpcs") or []
        for npc in nearby:
            vid = npc.get("voiceId") or npc.get("voice_id", "")
            if vid == npc_id:
                dist = npc.get("distance", 0)
                return dist < OWL_MAIL_MIN_DISTANCE
        return False

    def _is_on_cooldown(self, npc_id: str) -> bool:
        """True if we are still in cooldown since the last in-person conversation."""
        last_time = self._last_conversation_times.get(npc_id)
        if last_time is None:
            return False
        cooldown = self._get_owl_setting("conversation_cooldown", OWL_MAIL_CONVERSATION_COOLDOWN)
        return (time.time() - last_time) < cooldown

    def _is_conversation_active(self, ctx: dict) -> bool:
        return ctx.get("conversationActive", False)

    def _get_mail_blocked_npcs(self, ctx: dict) -> dict[str, str]:
        """NPC IDs that must not send owl mail, mapped to the skip reason."""
        blocked = {
            npc_id: "Cannot use Owl Post"
            for npc_id in get_excluded_npcs(ctx.get("missionStatuses"))
        }

        companion_id = str(ctx.get("companionId", "") or "").strip()
        if companion_id:
            blocked[companion_id.lower()] = "Currently accompanying player"

        for follower_id in ctx.get("followers", []) or []:
            follower_id = str(follower_id or "").strip()
            if follower_id:
                blocked[follower_id.lower()] = "Currently accompanying player"

        return blocked

    # ------------------------------------------------------------------
    # LLM: classifier
    # ------------------------------------------------------------------

    def _should_npc_write(self, npc_id: str, dialogue_history: list,
                          mail_history: list, background_context: Optional[str],
                          hours_since_contact: Optional[float] = None) -> bool:
        """Lightweight yes/no classifier — should this character send a letter?"""
        model = self._get_orchestrator_model()

        display = _get_display(npc_id)
        player_name = _get_player_name()
        context_block = _build_context_with_separator(dialogue_history, mail_history)

        time_context = ""
        if hours_since_contact is not None:
            time_context = f"\n\nTheir last interaction was {_format_hours_since_contact(hours_since_contact)} ago."

        classifier_instructions = _get_owl_prompt("owl_mail_classifier").replace("{npc_name}", display).replace("{player_name}", player_name)
        prompt = (
            f"You are evaluating whether {display} would send a follow-up letter "
            f"to {player_name}.\n\n"
            f"{context_block}\n\n"
            f"{f'## Character Background\\n{background_context}' if background_context else ''}"
            f"{time_context}\n\n"
            f"{classifier_instructions}"
        )

        result = llm.chat_simple(
            prompt, model=model, temperature=0.1, max_tokens=8192,
            context="owl_mail_classifier",
        )
        if not result:
            return False
        return result.strip().lower().startswith("yes")

    # ------------------------------------------------------------------
    # LLM: letter summarizer
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # LLM: letter summarizer (delegates to module-level function)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # LLM: letter generation
    # ------------------------------------------------------------------

    def _generate_letter(self, npc_id: str, dialogue_history: list,
                         mail_history: list, background_context: Optional[str],
                         is_reply: bool = False,
                         player_letter: Optional[dict] = None,
                         ctx: Optional[dict] = None,
                         hours_since_contact: Optional[float] = None):
        """Generate a letter from a character. Returns (subject, body, mail_type) or (None, None, None)."""
        model = self._get_mail_model()

        display = _get_display(npc_id)
        player_name = _get_player_name()
        interaction_context = _build_context_with_separator(dialogue_history, mail_history)

        # --- Build structured prompt ---
        sections = []

        # Role
        if is_reply and player_letter:
            sections.append(
                f"You are {display}, writing a reply to a letter from {player_name}."
            )
        else:
            sections.append(
                f"You are {display}, writing a follow-up letter to {player_name}."
            )

        # Player's letter (reply only)
        if is_reply and player_letter:
            sections.append(
                f"## {player_name}'s Letter\n"
                f"**Subject:** {player_letter.get('subject', '')}\n\n"
                f"{player_letter.get('body', '')}"
            )

        # Relevant memories (reply only — query the player's letter)
        if is_reply and player_letter:
            try:
                from utils.memory import search_relevant_facts
                settings_check = load_settings()
                if settings_check.get('memory', {}).get('enabled', True):
                    query_text = player_letter.get('body', '')
                    if query_text:
                        current_game_date = ""
                        current_game_time = ""
                        if ctx:
                            y, m, d = ctx.get("year"), ctx.get("month"), ctx.get("day")
                            current_game_date = f"{y}/{int(m):02d}/{int(d):02d}" if y and m and d else ctx.get("dateFormatted", "")
                            current_game_time = ctx.get("timeFormatted", "") or ctx.get("time", "")
                        facts = search_relevant_facts(
                            npc_id=npc_id,
                            query=query_text,
                            npc_name=display,
                            player_name=player_name,
                            current_game_date=current_game_date,
                            current_game_time=current_game_time,
                        )
                        if facts:
                            sections.append(
                                "## Relevant Memories\n"
                                + "\n".join(f"- {fact}" for fact in facts)
                            )
                            print(f"[OwlMail] Added {len(facts)} relevant facts for {npc_id} reply")
            except Exception as e:
                print(f"[OwlMail] Memory search failed: {e}")

        # Interaction context
        if interaction_context:
            time_note = ""
            if hours_since_contact is not None:
                time_note = f"\n\n(Their last interaction was {_format_hours_since_contact(hours_since_contact)} ago.)"
            sections.append(f"## Context\n{interaction_context}{time_note}")

        # Character info
        info_parts = []
        player_house = ctx.get("playerHouse") if ctx else None
        if player_house:
            info_parts.append(f"{player_name} is in {player_house}.")
        if background_context:
            info_parts.append(background_context)
        if info_parts:
            sections.append("\n".join(info_parts))

        # Commitment instructions (if enabled)
        settings = load_settings()
        if settings.get("commitments", {}).get("enabled", False):
            action_instructions = build_owl_mail_commitment_instructions(player_name, npc_id=npc_id)
            commit_ctx = build_commitment_context(npc_id, player_name=player_name)

            date_str = ""
            if ctx:
                date_fmt = ctx.get("dateFormatted", "")
                time_fmt = ctx.get("timeFormatted", "")
                if date_fmt and time_fmt:
                    date_str = f"**Current date/time:** {date_fmt} {time_fmt}\n\n"

            commitment_section = f"{date_str}{action_instructions}"
            if commit_ctx:
                commitment_section += f"\n\n{commit_ctx}"
            sections.append(commitment_section)

        # World facts and NPC story milestones (from mission completion)
        try:
            from utils.world_facts import get_global_facts, get_npc_facts
            mission_statuses = ctx.get('missionStatuses') if ctx else None
            global_facts = get_global_facts(mission_statuses, player_name=player_name)
            if global_facts:
                sections.append(f"## World State\n{global_facts}")
            npc_story = get_npc_facts(mission_statuses, npc_id, player_name=player_name)
            if npc_story:
                sections.append(npc_story)
        except Exception:
            pass

        # Writing instructions
        letter_instructions = _get_owl_prompt("owl_mail_letter")

        # Type selection — only unsolicited letters can choose type
        if is_reply:
            type_instructions = ""
            format_block = (
                "Format your response as:\n"
                "Subject: [subject line]\n\n"
                "[letter body]"
            )
        else:
            type_instructions = (
                "\n\n## Letter Type\n"
                "You may send one of the following types of letter:\n"
                "- **letter** — A regular letter. Personal, in-character, conversational.\n"
                "- **howler** — A FURIOUS letter. The envelope is red and screams when opened. "
                "Write with MANY exclamation marks. Over-the-top angry and dramatic. "
                "Write in normal case (not all caps) — the display will handle uppercasing. "
                f"Only use this when you ({display}) have genuine reason to be upset, frustrated, or outraged.\n"
            )
            format_block = (
                "Format your response as:\n"
                "Type: [letter or howler]\n"
                "Subject: [subject line]\n\n"
                "[letter body]"
            )

        sections.append(
            f"## Instructions\n"
            f"{letter_instructions}"
            f"{type_instructions}\n\n"
            f"{format_block}"
        )

        prompt = "\n\n".join(sections)

        result = llm.chat_simple(
            prompt, model=model, temperature=0.9, max_tokens=8192,
            context="owl_mail_generate",
        )
        if not result:
            return None, None, None

        # Parse type, subject, and body
        remaining = result.strip()
        mail_type = "letter"

        # Extract Type: line if present
        first_line, _, after_first = remaining.partition("\n")
        if first_line.lower().startswith("type:"):
            parsed_type = first_line[len("type:"):].strip().lower()
            if parsed_type in ("letter", "howler"):
                mail_type = parsed_type
            remaining = after_first.strip()

        # Force replies to always be regular letters
        if is_reply:
            mail_type = "letter"

        # Extract Subject: line
        subject = "A letter"
        body = remaining
        first_line, _, after_first = remaining.partition("\n")
        if first_line.lower().startswith("subject:"):
            subject = first_line[len("subject:"):].strip()
            body = after_first.strip()

        return subject, body, mail_type

    # ------------------------------------------------------------------
    # Recent dialogue NPC extraction
    # ------------------------------------------------------------------

    def _get_recent_dialogue_npcs(self) -> list[str]:
        """Return unique NPC voice_names from recent meaningful dialogue history.

        Single SQL query — returns only NPCs with AI dialogue, cutscene,
        combat, or commitment entries, ordered by most recent interaction.
        """
        return get_recent_meaningful_npcs()

    # ------------------------------------------------------------------
    # Qualifying event filter for eval persistence
    # ------------------------------------------------------------------

    def _get_qualifying_entries(self, npc_id: str, history: list) -> list:
        """Filter pre-filtered history to entries witnessed by this NPC.

        Expects history to already be filtered to meaningful types.
        """
        return [e for e in history if _npc_witnessed(e, npc_id)]

    # ------------------------------------------------------------------
    # Main cycle
    # ------------------------------------------------------------------

    def _run_cycle(self):
        """One orchestrator tick — reply to unanswered mail, then consider new letters."""
        settings = load_settings()
        if not settings.get("server", {}).get("enabled", True):
            return
        if is_llm_provider_feature_disabled("owl_post", settings):
            return
        if not settings.get("owl_post", {}).get("enabled", True):
            return

        # Request fresh lightweight context (distance-only NPC scan + game state)
        if _lua_socket:
            ctx = _lua_socket.request_context_refresh(
                groups=["nearby_lean", "state", "companion"],
                timeout=3.0,
                params={"nearby_lean_distance": OWL_MAIL_MIN_DISTANCE},
            )
        else:
            ctx = _get_game_context()
        if not ctx:
            return

        if ctx.get("isGamePaused", False):
            return

        if self._is_conversation_active(ctx):
            return

        gm = get_current_game_minutes(ctx)

        # --- 0. Check for newly arrived mail and notify ---
        unread = get_unread_mail_count(gm)
        if self._last_unread_count == -1:
            # First tick — just record baseline, don't spam
            self._last_unread_count = unread
        elif unread > self._last_unread_count:
            new_count = unread - self._last_unread_count
            if new_count == 1:
                _send_notification("An owl has delivered a letter for you.")
            else:
                _send_notification(f"You have {new_count} new owl letters.")
            _play_owl_hoot()
            self._last_unread_count = unread
        elif unread < self._last_unread_count:
            # Player read some mail — update baseline
            self._last_unread_count = unread

        # Companions and followers count as nearby and must never send owl mail.
        blocked_mail_npcs = self._get_mail_blocked_npcs(ctx)
        mission_statuses = ctx.get("missionStatuses")

        # --- 1. Reply to unanswered player mail ---
        unanswered = get_unanswered_player_mail(gm)
        for mail_entry in unanswered:
            npc_id = mail_entry["recipient"]
            thread_id = mail_entry.get("thread_id")
            npc_key = str(npc_id or "").strip().lower()

            if not is_allowed_owl_mail_recipient(npc_id, mission_statuses=mission_statuses) and not thread_has_correspondent(thread_id, npc_id):
                log_owl_event("mail_skip", npc_id, "Recipient no longer allowed for Owl Mail")
                continue

            block_reason = blocked_mail_npcs.get(npc_key)
            if not block_reason and is_excluded_npc(npc_id, mission_statuses):
                block_reason = "Cannot use Owl Post"
            if block_reason:
                if block_reason != "Cannot use Owl Post":
                    log_owl_event("mail_skip", npc_id, block_reason)
                continue

            if self._is_npc_nearby(npc_id, ctx):
                log_owl_event("mail_skip", npc_id, "Nearby — can talk in person")
                continue
            if self._is_on_cooldown(npc_id):
                log_owl_event("mail_skip", npc_id, "Conversation cooldown")
                continue
            if self._is_conversation_active(ctx):
                log_owl_event("mail_skip", npc_id, "Conversation active")
                continue

            background_context = _get_npc_mail_background_context(npc_id, _get_player_name())
            all_dialogue = _filter_meaningful_entries(get_entries_for_npc(npc_id))
            mail_history = get_recent_mail_for_npc(npc_id, thread_id=mail_entry["thread_id"])
            dialogue_history = _filter_dialogue_since_last_mail(all_dialogue, mail_history)

            subject, body, mail_type = self._generate_letter(
                npc_id, dialogue_history, mail_history, background_context,
                is_reply=True, player_letter=mail_entry, ctx=ctx,
            )
            if subject and body:
                thread_id = mail_entry["thread_id"]
                # Parse proposals from raw body, strip tags, store cleaned body
                actions = parse_commitment_actions(body)
                cleaned_body = strip_commitment_action_tags(body).strip() if actions else body
                mail_id, thread_id = send_mail(npc_id, "player", subject, cleaned_body, gm, gm, thread_id, player_name=_get_player_name(), mail_type=mail_type)
                log_owl_event("mail_reply", npc_id, f"Re: {subject}")
                print(f"[OwlMail] {npc_id} replied to player mail")
                if mail_id:
                    if actions:
                        _extract_and_store_proposals(mail_id, npc_id, actions)
                    summarize_letter(mail_id, npc_id, subject, cleaned_body)

        # --- 2. NPC-initiated letters (max 1 per cycle) ---
        if _is_quiet_hours(gm):
            return

        recent_npcs = self._get_recent_dialogue_npcs()
        history = _filter_meaningful_entries(load_dialogue_history(_get_game_context))
        sent_this_cycle = False

        for npc_id in recent_npcs:
            if sent_this_cycle:
                break
            npc_key = str(npc_id or "").strip().lower()

            block_reason = blocked_mail_npcs.get(npc_key)
            if not block_reason and is_excluded_npc(npc_id, mission_statuses):
                block_reason = "Cannot use Owl Post"
            if block_reason:
                if block_reason != "Cannot use Owl Post":
                    log_owl_event("mail_skip", npc_id, block_reason)
                continue
            if self._is_npc_nearby(npc_id, ctx):
                log_owl_event("mail_skip", npc_id, "Nearby — can talk in person")
                continue
            if self._is_on_cooldown(npc_id):
                log_owl_event("mail_skip", npc_id, "Conversation cooldown")
                continue
            if self._is_conversation_active(ctx):
                log_owl_event("mail_skip", npc_id, "Conversation active")
                continue

            # Skip if last mail in thread was already from this NPC (avoid double-send)
            existing = get_recent_mail_for_npc(npc_id, limit=1)
            if existing and existing[-1]["sender"] == npc_id:
                log_owl_event("mail_skip", npc_id, "Already sent, awaiting reply")
                continue

            # Skip if last contact was too recent (min 6 in-game hours by default)
            last_contact_gm = _get_last_contact_game_minutes(npc_id, existing, history)
            hours_since_contact = (gm - last_contact_gm) / 60 if last_contact_gm > 0 else None
            if hours_since_contact is not None:
                min_hours = self._get_owl_setting("min_hours_since_contact", OWL_MAIL_MIN_HOURS_SINCE_CONTACT)
                if hours_since_contact < min_hours:
                    log_owl_event("mail_skip", npc_id, f"Too recent ({hours_since_contact:.1f}h < {min_hours}h)")
                    continue

            # --- Persistence check: skip if context hasn't changed enough ---
            qualifying = self._get_qualifying_entries(npc_id, history)
            if not qualifying:
                continue

            # High-water mark: max entry ID from qualifying entries
            max_entry_id = max(
                (max(e.get('sourceEntryIds') or [0]) for e in qualifying),
                default=0
            )

            eval_state = get_eval_state(npc_id)
            if eval_state:
                entries_since = sum(
                    1 for e in qualifying
                    if max(e.get('sourceEntryIds') or [0]) > eval_state['last_eval_entry_id']
                )
                if entries_since < OWL_MAIL_MIN_NEW_ENTRIES:
                    log_owl_event("mail_skip", npc_id, f"Not enough new entries ({entries_since}/{OWL_MAIL_MIN_NEW_ENTRIES})")
                    continue

            # --- Context changed: run classifier ---
            background_context = _get_npc_mail_background_context(npc_id, _get_player_name())
            all_dialogue = _filter_meaningful_entries(get_entries_for_npc(npc_id))
            mail_history = get_recent_mail_for_npc(npc_id)
            dialogue_history = _filter_dialogue_since_last_mail(all_dialogue, mail_history)

            wrote = False
            if self._should_npc_write(npc_id, dialogue_history, mail_history, background_context, hours_since_contact=hours_since_contact):
                subject, body, mail_type = self._generate_letter(
                    npc_id, dialogue_history, mail_history, background_context,
                    ctx=ctx, hours_since_contact=hours_since_contact,
                )
                if subject and body:
                    actions = parse_commitment_actions(body)
                    cleaned_body = strip_commitment_action_tags(body).strip() if actions else body
                    mail_id, _ = send_mail(npc_id, "player", subject, cleaned_body, gm, gm, None, player_name=_get_player_name(), mail_type=mail_type)
                    type_label = " (HOWLER)" if mail_type == "howler" else ""
                    log_owl_event("mail_sent", npc_id, f"{subject}{type_label}")
                    print(f"[OwlMail] {npc_id} sent a {mail_type}")
                    if mail_id:
                        if actions:
                            _extract_and_store_proposals(mail_id, npc_id, actions)
                        summarize_letter(mail_id, npc_id, subject, cleaned_body)
                    wrote = True
                    sent_this_cycle = True

            # Persist eval state regardless of result
            save_eval_state(npc_id, max_entry_id, "yes" if wrote else "no")
            if not wrote:
                log_owl_event("eval_no", npc_id, "Classifier decided not to write")


# ============================================================================
# OwlBoardOrchestrator
# ============================================================================

class OwlBoardOrchestrator:
    """Background timer that generates NPC threads and replies on bulletin boards."""

    def __init__(self):
        self._timer: Optional[threading.Timer] = None
        self._running = False
        self._board_locks: dict[str, threading.Lock] = {}
        self._roster: Optional[dict] = None
        self._daily_post_count: int = 0      # threads generated on current game day
        self._daily_post_day: int = -1        # game day number for the counter
        self._last_thread_gm: float = 0.0    # game minutes when last thread was generated

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """Start the board orchestrator timer."""
        if self._running:
            return
        self._running = True
        self._roster = load_board_roster()
        self._schedule_next()
        print("[OwlBoard] Orchestrator started")

    def stop(self):
        """Stop the board orchestrator timer."""
        self._running = False
        if self._timer:
            self._timer.cancel()
            self._timer = None
        print("[OwlBoard] Orchestrator stopped")

    # ------------------------------------------------------------------
    # Per-board lock
    # ------------------------------------------------------------------

    _board_locks_guard = threading.Lock()

    def get_board_lock(self, slug: str) -> threading.Lock:
        """Get or create a per-board generation lock."""
        with self._board_locks_guard:
            if slug not in self._board_locks:
                self._board_locks[slug] = threading.Lock()
            return self._board_locks[slug]

    # ------------------------------------------------------------------
    # Timer plumbing
    # ------------------------------------------------------------------

    def _get_board_model(self):
        """Get model for board LLM calls (thread gen + reply gen)."""
        settings = load_settings()
        owl_model = settings.get("owl_post", {}).get("board_model", "")
        return owl_model or settings.get("conversation", {}).get("chat_model")

    def _schedule_next(self):
        if not self._running:
            return
        settings = load_settings()
        interval = settings.get("owl_post", {}).get("board_interval", OWL_BOARD_ORCHESTRATOR_INTERVAL)
        self._timer = threading.Timer(interval, self._tick)
        self._timer.daemon = True
        self._timer.start()

    def _tick(self):
        try:
            self._run_cycle()
        except Exception as e:
            print(f"[OwlBoard] Error in orchestrator cycle: {e}")
            log_owl_event("error", None, f"Board cycle error: {e}")
        finally:
            self._schedule_next()

    # ------------------------------------------------------------------
    # Board selection
    # ------------------------------------------------------------------

    def _pick_board(self, boards: list, unread_counts: dict,
                    player_house: Optional[str] = None) -> Optional[dict]:
        """Pick the board most in need of content. Returns board dict or None."""
        candidates = []
        for b in boards:
            if b["access_type"] == "decorative":
                continue
            # Skip house-locked boards the player can never see
            if b["access_type"] == "house_locked" and b.get("house") and player_house:
                if b["house"].lower() != player_house.lower():
                    continue
            unread = unread_counts.get(b["id"], 0)
            if unread >= OWL_BOARD_UNREAD_CAP:
                continue
            candidates.append((b, unread))

        if not candidates:
            return None

        candidates.sort(key=lambda x: (x[1], random.random()))
        return candidates[0][0]

    # ------------------------------------------------------------------
    # NPC pool
    # ------------------------------------------------------------------

    def _get_npc_pool(self, board_slug: str) -> list[str]:
        """Get the NPC pool for a board from the roster."""
        if not self._roster:
            return []
        entry = self._roster.get(board_slug, {})
        members = entry.get("members", [])
        if members:
            return members
        # Role-based boards: collect all NPCs from house rosters
        roles = entry.get("roles", [])
        if roles:
            all_npcs: set[str] = set()
            for _slug, data in self._roster.items():
                if "members" in data:
                    all_npcs.update(data["members"])
            return list(all_npcs)
        return []

    # ------------------------------------------------------------------
    # LLM: thread generation
    # ------------------------------------------------------------------

    def _generate_thread(self, board: dict, npc_pool: list[str],
                         game_minutes: float, game_date: str = "") -> Optional[list[dict]]:
        """Generate an entire thread (topic + replies) in one LLM call."""
        model = self._get_board_model()

        # Pick 3-5 participants
        participants = random.sample(npc_pool, min(random.randint(3, 5), len(npc_pool)))
        if not participants:
            return None

        # Load bios for context
        bios: dict[str, str] = {}
        for npc_id in participants:
            board_background = _get_npc_board_background(npc_id)
            if board_background:
                bios[npc_id] = board_background

        # Recent threads to avoid repetition
        recent = get_recent_board_threads(board["id"], limit=100)
        recent_text = ""
        if recent:
            from utils.dialogue_db import _minutes_to_game_datetime
            MONTH_NAMES = ['', 'January', 'February', 'March', 'April', 'May', 'June',
                           'July', 'August', 'September', 'October', 'November', 'December']
            def _fmt_date(game_min):
                try:
                    (y, mo, d), _ = _minutes_to_game_datetime(int(game_min))
                    return f"{MONTH_NAMES[mo]} {d}, {y}"
                except Exception:
                    return ""
            recent_text = "Previous topics on this board (do not repeat these):\n" + "\n".join(
                f"- \"{t['title']}\" by {_get_display(t['author'])}, {_fmt_date(t['created_at'])}"
                for t in recent if t.get("title")
            )

        bio_text = "\n".join(
            f"- {_get_display(npc)}: {bio}" for npc, bio in bios.items()
        )
        display_names = ", ".join(_get_display(p) for p in participants)
        name_map = ", ".join(f"{_get_display(p)} ({p})" for p in participants)

        # World facts for thread context
        world_facts_text = ""
        try:
            from utils.world_facts import get_global_facts
            thread_ctx = _get_game_context()
            mission_statuses = thread_ctx.get('missionStatuses') if thread_ctx else None
            global_facts = get_global_facts(mission_statuses)
            if global_facts:
                world_facts_text = f"World state: {global_facts}\n"
        except Exception:
            pass

        thread_instructions = _get_owl_prompt("owl_board_thread")
        board_rules = _get_board_rules_text()
        prompt = (
            f"You are generating a bulletin board thread for the \"{board['name']}\" "
            f"board at Hogwarts.\n\n"
            f"{f'Current date: {game_date}\n' if game_date else ''}"
            f"{world_facts_text}"
            f"Participants: {display_names}\n"
            f"{f'About them:\n{bio_text}' if bio_text else ''}\n\n"
            f"{recent_text}\n\n"
            f"{board_rules}"
            f"{thread_instructions}\n\n"
            f"Format your response as a JSON array. Each element has: type, author, title (topic only), body.\n"
            f"Use these exact author IDs: {name_map}. "
            f"Use the ID form (e.g. \"{participants[0]}\"), not the display name.\n\n"
            f"Example:\n"
            f"```json\n"
            f"[\n"
            f'  {{"type": "topic", "author": "{participants[0]}", "title": "Thread title here", "body": "Opening post text"}},\n'
            f'  {{"type": "reply", "author": "{participants[1] if len(participants) > 1 else participants[0]}", "body": "Reply text"}}\n'
            f"]\n"
            f"```\n"
            f"Output ONLY the JSON array, no other text."
        )

        result = llm.chat_simple(
            prompt, model=model, temperature=1.0, max_tokens=8192,
            context="owl_board_generate",
        )
        if not result:
            log_owl_event("board_skip", None, f"Empty LLM response for board '{board['name']}'")
            return None

        posts = _parse_board_json(result)
        if not posts:
            preview = re.sub(r"\s+", " ", result).strip()
            if len(preview) > 160:
                preview = preview[:157] + "..."
            print(f"[OwlBoard] Invalid thread output for board '{board['name']}': {preview}")
            log_owl_event("board_skip", None, f"Invalid thread output for board '{board['name']}'")
            return None

        if posts[0].get("type") != "topic":
            preview = re.sub(r"\s+", " ", result).strip()
            if len(preview) > 160:
                preview = preview[:157] + "..."
            print(f"[OwlBoard] Thread output missing topic for board '{board['name']}': {preview}")
            log_owl_event("board_skip", None, f"Thread output missing topic for board '{board['name']}'")
            return None

        return posts

    # ------------------------------------------------------------------
    # Store generated thread
    # ------------------------------------------------------------------

    def _store_thread(self, board_id: int, posts: list[dict],
                      game_minutes: float) -> bool:
        """Persist a generated thread with staggered reply visibility."""
        if not posts or posts[0]["type"] != "topic":
            return False

        topic = posts[0]
        root_id = create_board_post(
            board_id, topic["author"], topic["title"], topic["body"],
            game_minutes, game_minutes,  # topic visible immediately
        )
        if root_id is None:
            return False

        stagger = game_minutes
        for reply in posts[1:]:
            stagger += random.randint(OWL_BOARD_REPLY_STAGGER_MIN, OWL_BOARD_REPLY_STAGGER_MAX)
            create_board_post(
                board_id, reply["author"], None, reply["body"],
                game_minutes, stagger,
                root_post_id=root_id, parent_id=root_id,
            )

        print(f"[OwlBoard] Generated thread '{topic['title']}' with "
              f"{len(posts) - 1} replies on board {board_id}")
        return True

    # ------------------------------------------------------------------
    # Public: generate replies to a player post
    # ------------------------------------------------------------------

    def generate_replies_to_player(self, board: dict, root_post_id: int,
                                   game_minutes: float) -> None:
        """Generate NPC replies after a player posts to a thread.

        Called from the API route. Acquires the board lock non-blocking so
        concurrent generation requests are silently dropped.
        """
        lock = self.get_board_lock(board["slug"])
        if not lock.acquire(blocking=False):
            return

        try:
            model = self._get_board_model()

            posts = get_thread_posts(root_post_id, game_minutes)
            if not posts:
                return

            npc_pool = self._get_npc_pool(board["slug"])
            if not npc_pool:
                return

            participants = random.sample(npc_pool, min(random.randint(2, 4), len(npc_pool)))

            # Build thread context
            thread_text = "\n".join(
                f"{'[Topic] ' if p.get('title') else ''}"
                f"{_get_display(p['author'])}: "
                f"{p.get('title', '')} {p['body']}"
                for p in posts
            )

            bios: dict[str, str] = {}
            for npc_id in participants:
                board_background = _get_npc_board_background(npc_id)
                if board_background:
                    bios[npc_id] = board_background

            bio_text = "\n".join(
                f"- {_get_display(npc)}: {bio}" for npc, bio in bios.items()
            )
            display_names = ", ".join(_get_display(p) for p in participants)
            name_map = ", ".join(f"{_get_display(p)} ({p})" for p in participants)

            # Player context
            player_name = _get_player_name()
            player_bio = _get_player_bio()
            ctx = _get_game_context()
            player_house = ctx.get("playerHouse") if ctx else None
            player_parts = []
            if player_house:
                player_parts.append(f"{player_name} is in {player_house}.")
            if player_bio:
                player_parts.append(player_bio)
            player_ctx = f"\nAbout {player_name}: {' '.join(player_parts)}\n" if player_parts else ""

            board_rules = _get_board_rules_text()
            prompt = (
                f"You are generating replies to a bulletin board thread on the "
                f"\"{board['name']}\" board at Hogwarts.\n\n"
                f"The thread so far:\n{thread_text}\n\n"
                f"Available responders: {display_names}\n"
                f"{f'About them:\n{bio_text}' if bio_text else ''}"
                f"{player_ctx}\n"
                f"{board_rules}"
                f"{_get_owl_prompt('owl_board_reply')}\n\n"
                f"Format your response as a JSON array. Each element has: author, body.\n"
                f"Use these exact author IDs: {name_map}. "
                f"Use the ID form (e.g. \"{participants[0]}\"), not the display name.\n\n"
                f"Example:\n"
                f"```json\n"
                f"[\n"
                f'  {{"author": "{participants[0]}", "body": "Reply text"}}\n'
                f"]\n"
                f"```\n"
                f"Output ONLY the JSON array, no other text."
            )

            result = llm.chat_simple(
                prompt, model=model, temperature=1.0, max_tokens=8192,
                context="owl_board_reply",
            )
            if not result:
                return

            stagger = game_minutes
            replies = _parse_board_json(result)
            if not replies:
                return
            for post in replies:
                author = post.get("author", "").strip()
                body = post.get("body", "").strip()
                if not author or not body:
                    continue
                stagger += random.randint(
                    OWL_BOARD_REPLY_STAGGER_MIN, OWL_BOARD_REPLY_STAGGER_MAX,
                )
                create_board_post(
                    board["id"], author, None, body,
                    game_minutes, stagger,
                    root_post_id=root_post_id, parent_id=root_post_id,
                )

            print(f"[OwlBoard] Generated replies to post in thread {root_post_id}")
            log_owl_event("board_reply", None, f"Replies to thread {root_post_id} ({board['name']})")
        except Exception as e:
            print(f"[OwlBoard] Error generating replies: {e}")
            log_owl_event("error", None, f"Board reply error: {e}")
        finally:
            lock.release()

    # ------------------------------------------------------------------
    # Main cycle
    # ------------------------------------------------------------------

    def _run_cycle(self):
        """One board-orchestrator tick — pick a board and generate a thread."""
        ctx = _get_game_context()
        if not ctx:
            return

        if ctx.get("isGamePaused", False):
            return

        settings = load_settings()
        if not settings.get("server", {}).get("enabled", True):
            return
        if is_llm_provider_feature_disabled("owl_post", settings):
            return
        owl = settings.get("owl_post", {})
        if not owl.get("enabled", True) or not owl.get("boards_enabled", True):
            return

        gm = get_current_game_minutes(ctx)

        if _is_quiet_hours(gm):
            return

        # --- Daily post cap with spacing ---
        game_day = int(gm) // 1440
        if game_day != self._daily_post_day:
            self._daily_post_day = game_day
            self._daily_post_count = 0
        max_per_day = owl.get("max_board_posts_per_day", 0)
        if max_per_day > 0:
            if self._daily_post_count >= max_per_day:
                return
            # Spread threads across the awake window (quiet_start - quiet_end)
            awake_minutes = (OWL_MAIL_UNSOLICITED_QUIET_HOURS_START - OWL_MAIL_UNSOLICITED_QUIET_HOURS_END) * 60
            spacing = awake_minutes / max_per_day
            if self._last_thread_gm and (gm - self._last_thread_gm) < spacing:
                return

        boards = get_all_boards()
        unread_counts = get_unread_count_per_board(gm)
        player_house = ctx.get("playerHouse")

        board = self._pick_board(boards, unread_counts, player_house=player_house)
        if not board:
            log_owl_event("board_skip", None, "All boards at unread cap")
            return

        lock = self.get_board_lock(board["slug"])
        if not lock.acquire(blocking=False):
            log_owl_event("board_skip", None, f"Board '{board['name']}' busy")
            return

        try:
            npc_pool = self._get_npc_pool(board["slug"])
            if not npc_pool:
                log_owl_event("board_skip", None, f"No NPCs for board '{board['name']}'")
                return

            game_date = ctx.get("dateFormatted", "")
            posts = self._generate_thread(board, npc_pool, gm, game_date=game_date)
            if posts:
                if self._store_thread(board["id"], posts, gm):
                    self._daily_post_count += 1
                    self._last_thread_gm = gm
                    topic_title = posts[0].get("title", "Untitled") if posts else "Untitled"
                    log_owl_event("board_thread", posts[0].get("author"), f"{topic_title} ({board['name']})")
                else:
                    log_owl_event("error", None, f"Failed to store board thread for '{board['name']}'")
        except Exception as e:
            print(f"[OwlBoard] Error generating thread: {e}")
            log_owl_event("error", None, f"Board thread error: {e}")
        finally:
            lock.release()
