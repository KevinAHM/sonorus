"""
Event-driven unsolicited commentary orchestration.
Buffers high-signal gameplay events and decides whether a companion should comment.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional

from .agents import run_event_commentary_agent
from .dialogue import _game_datetime_to_minutes
from .localization import get_display_name
from .settings import load_settings


@dataclass
class NormalizedGameEvent:
    event_type: str
    timestamp: float
    priority: int
    summary: str
    location_key: Optional[str]
    raw: dict


class EventCommentaryOrchestrator:
    """Buffer and gate unsolicited commentary triggers."""

    EVENT_PRIORITIES = {
        "cinematic:end": 3,
        "combat:end": 2,
        "location:change": 1,
    }

    def __init__(
        self,
        lua_socket,
        conv_state,
        load_dialogue_history_func: Callable[[dict], list],
        generate_response_func: Callable[..., Optional[str]],
        play_commentary_turn_func: Callable[..., bool],
        stream_commentary_turn_func: Optional[Callable[..., bool]] = None,
    ):
        self.lua_socket = lua_socket
        self.conv_state = conv_state
        self.load_dialogue_history = load_dialogue_history_func
        self.generate_response = generate_response_func
        self.play_commentary_turn = play_commentary_turn_func
        self.stream_commentary_turn = stream_commentary_turn_func

        self.buffer = deque()
        self.buffer_lock = threading.Lock()
        self.flush_timer = None
        self.flush_lock = threading.Lock()

        self.last_comment_at = None
        self.last_comment_by_type = {}
        self.last_location_comment_at = {}
        self.last_selector_topic = None

    def handle_event(self, raw_event: dict) -> None:
        settings = load_settings()
        commentary = settings.get("commentary", {})
        if not commentary.get("enabled", True):
            print(f"[Commentary] Ignoring {raw_event.get('event')}: commentary disabled")
            return

        normalized = self._normalize_event(raw_event, commentary)
        if not normalized:
            print(f"[Commentary] Ignoring {raw_event.get('event')}: normalization rejected")
            return

        if not self._is_immediately_eligible():
            print(f"[Commentary] Ignoring {normalized.event_type}: conversation not idle or playback active")
            return

        # Use cached context only here. This callback runs on the socket receive thread,
        # so we must not block waiting for a fresh context response.
        cached_context = self.lua_socket.get_game_context() or {}
        if cached_context and not self._is_context_commentary_allowed(cached_context):
            print(f"[Commentary] Ignoring {normalized.event_type}: cached context gate failed ({self._context_gate_reason(cached_context)})")
            return

        with self.buffer_lock:
            event_window = max(0.1, float(commentary.get("aggregation_window_seconds", 4)))
            self.buffer.append(normalized)
            self.buffer = deque(ev for ev in self.buffer if (time.time() - ev.timestamp) <= event_window)
            if self.flush_timer:
                self.flush_timer.cancel()
            self.flush_timer = threading.Timer(event_window, self.flush_if_ready)
            self.flush_timer.daemon = True
            self.flush_timer.start()

    def flush_if_ready(self) -> None:
        with self.flush_lock:
            with self.buffer_lock:
                buffered_events = list(self.buffer)
                self.buffer.clear()
                self.flush_timer = None

            if not buffered_events:
                return

            settings = load_settings()
            commentary = settings.get("commentary", {})
            if not commentary.get("enabled", True):
                print("[Commentary] Flush aborted: commentary disabled")
                return
            event_window = max(0.1, float(commentary.get("aggregation_window_seconds", 4)))

            light_context = self.lua_socket.request_context_refresh(
                groups=["player", "state", "time", "zone", "companion", "mission"],
                timeout=1.0
            )

            if not self._is_context_commentary_allowed(light_context):
                print(f"[Commentary] Flush aborted: context gate failed ({self._context_gate_reason(light_context)})")
                return

            dialogue_history = self.load_dialogue_history(light_context) or []
            selected_event = self._select_event_for_batch(buffered_events, commentary, light_context, dialogue_history)
            if not selected_event:
                print("[Commentary] Flush aborted: no eligible event survived gating")
                return

            player_name = light_context.get("playerName", "Player")
            companion_id = light_context.get("companionId")
            eligible_speakers = [{
                "id": companion_id,
                "display_name": get_display_name(companion_id),
            }]

            recent_events = self._build_selector_events(selected_event, buffered_events, event_window)
            selector_dialogue = self._select_recent_dialogue_for_event(dialogue_history, selected_event)

            if selected_event.event_type == "cinematic:end":
                selector = {
                    "worth_commenting": "yes",
                    "speaker_id": companion_id,
                    "target_id": "player",
                    "topic": "recent cutscene",
                    "timing": "considering",
                    "scene": "",
                    "relevance": "Recent cutscene dialogue is the immediate moment to react to.",
                    "why": "Companions should always react after a validated cinematic.",
                    "raw": "",
                }
                print(f"[Commentary] Forcing commentary for validated cinematic:end by {companion_id}")
            else:
                selector = run_event_commentary_agent(
                    eligible_speakers=eligible_speakers,
                    player_name=player_name,
                    current_location=light_context.get("zoneLocation") or light_context.get("location", "Unknown"),
                    time_of_day=light_context.get("timeFormatted") or light_context.get("gameTime") or "Unknown",
                    time_since_last_comment=self._format_elapsed(self.last_comment_at),
                    primary_event=selected_event.summary,
                    recent_events=recent_events,
                    recent_dialogue=selector_dialogue,
                    frequency_label="default",
                    notable_locations=commentary.get("notable_locations", []),
                )

            if selector.get("worth_commenting") != "yes":
                return

            self._maybe_capture_vision(commentary)

            full_context = self.lua_socket.request_context_refresh(
                groups=["position", "state", "player", "time", "zone", "gear", "companion", "mission"],
                timeout=1.0
            )
            if not self._is_context_commentary_allowed(full_context):
                return

            if self.stream_commentary_turn:
                played = self.stream_commentary_turn(
                    selector["speaker_id"],
                    selector["target_id"],
                    full_context,
                    selector.get("topic"),
                    selected_event.event_type,
                    recent_events=recent_events,
                )
            else:
                response = self.generate_response(
                    selector["speaker_id"],
                    selector["target_id"],
                    full_context,
                    pending_entries=None,
                    mode="commentary",
                    topic=selector.get("topic"),
                    recent_events=recent_events,
                )
                if not response:
                    return

                played = self.play_commentary_turn(
                    selector["speaker_id"],
                    selector["target_id"],
                    response,
                    full_context,
                    selector.get("topic"),
                    selected_event.event_type,
                )
            if played:
                now = time.time()
                self.last_comment_at = now
                self.last_comment_by_type[selected_event.event_type] = now
                if selected_event.location_key:
                    self.last_location_comment_at[selected_event.location_key.lower()] = now
                self.last_selector_topic = selector.get("topic")

    def _maybe_capture_vision(self, commentary: dict) -> None:
        if not commentary.get("use_vision", True):
            return

        settings = load_settings()
        vision_settings = settings.get("agents", {}).get("vision", {})
        if not vision_settings.get("enabled", True):
            return

        try:
            import vision_agent
        except Exception:
            return

        try:
            vision_runtime = vision_agent.get_vision_settings()
            agent = vision_agent.get_agent()
            if not agent:
                return
            agent.capture_now()
            if vision_runtime.get("wait_for_capture", False):
                wait_timeout = float(vision_runtime.get("wait_timeout_seconds", 5))
                agent.wait_for_capture(timeout=wait_timeout)
        except Exception as e:
            print(f"[Commentary] Vision capture failed: {e}")

    def _is_immediately_eligible(self) -> bool:
        return self.conv_state.state == "idle" and not self.lua_socket.pipeline_active

    def _is_context_commentary_allowed(self, context: dict) -> bool:
        if self.conv_state.state != "idle":
            return False
        if self.lua_socket.pipeline_active:
            return False
        if context.get("isGamePaused"):
            return False
        if context.get("inCinematic"):
            return False
        if context.get("inCombat"):
            return False
        if context.get("companionForcedWaiting"):
            return False
        if not context.get("hasCompanion") or not context.get("companionId"):
            return False
        return True

    def _context_gate_reason(self, context: dict) -> str:
        if self.conv_state.state != "idle":
            return f"conv_state={self.conv_state.state}"
        if self.lua_socket.pipeline_active:
            return "pipeline_active"
        if context.get("isGamePaused"):
            return "game_paused"
        if context.get("inCinematic"):
            return "in_cinematic"
        if context.get("inCombat"):
            return "in_combat"
        if context.get("companionForcedWaiting"):
            return "companion_forced_waiting"
        if not context.get("hasCompanion"):
            return "no_companion"
        if not context.get("companionId"):
            return "missing_companion_id"
        return "unknown"

    def _fails_cooldowns(self, event: NormalizedGameEvent, commentary: dict) -> bool:
        now = time.time()

        global_cd = float(commentary.get("global_cooldown_seconds", 60))
        if self.last_comment_at and (now - self.last_comment_at) < global_cd:
            return True

        per_event = commentary.get("event_cooldowns", {})
        event_cd = float(per_event.get(event.event_type, 0))
        last_event_time = self.last_comment_by_type.get(event.event_type)
        if last_event_time and (now - last_event_time) < event_cd:
            return True

        if event.location_key:
            location_cd = float(commentary.get("same_location_cooldown_seconds", 600))
            last_location_time = self.last_location_comment_at.get(event.location_key.lower())
            if last_location_time and (now - last_location_time) < location_cd:
                return True

        return False

    def _normalize_event(self, raw_event: dict, commentary: dict) -> Optional[NormalizedGameEvent]:
        event_type = raw_event.get("event")
        data = raw_event.get("data", {}) if isinstance(raw_event.get("data"), dict) else {}
        priority = self.EVENT_PRIORITIES.get(event_type)
        if priority is None:
            return None

        timestamp = time.time()
        location_key = None

        if event_type == "cinematic:end":
            summary = "Cinematic ended"
        elif event_type == "combat:end":
            summary = self._summarize_combat(data)
        elif event_type == "location:change":
            new_location = (data.get("newLocation") or "").strip()
            old_location = (data.get("oldLocation") or "").strip()
            if not new_location:
                return None
            location_key = new_location
            if old_location:
                summary = f"Location changed to {new_location} from {old_location}"
            else:
                summary = f"Location changed to {new_location}"
        else:
            return None

        return NormalizedGameEvent(
            event_type=event_type,
            timestamp=timestamp,
            priority=priority,
            summary=summary,
            location_key=location_key,
            raw=data,
        )

    def _has_recent_cutscene_history(self, dialogue_history: list, lookback: int = 3) -> bool:
        recent_entries = [entry for entry in dialogue_history if isinstance(entry, dict)][-lookback:]
        for entry in reversed(recent_entries):
            entry_type = (entry.get("type") or "").strip().lower()
            if entry_type == "cutscene":
                return True
        return False

    def _has_recent_location_history(
        self,
        dialogue_history: list,
        companion_id: Optional[str],
        location_name: Optional[str],
        current_game_date: Optional[str],
        current_game_time: Optional[str],
        revisit_cooldown_minutes: int,
    ) -> bool:
        if not companion_id or not location_name or revisit_cooldown_minutes <= 0:
            return False

        current_mins = _game_datetime_to_minutes(current_game_date, current_game_time)
        if current_mins is None:
            return False

        location_key = location_name.strip().lower()
        companion_key = companion_id.strip().lower()
        skipped_current_entry = False

        for entry in reversed(dialogue_history):
            if not isinstance(entry, dict):
                continue
            if (entry.get("type") or "").strip().lower() != "location":
                continue

            entry_location = (entry.get("location") or "").strip().lower()
            if entry_location != location_key:
                continue

            earshot = entry.get("earshot") or []
            if not isinstance(earshot, list):
                continue
            if companion_key not in {str(w).strip().lower() for w in earshot}:
                continue

            entry_mins = _game_datetime_to_minutes(entry.get("gameDate"), entry.get("gameTime"))
            if entry_mins is None:
                continue

            gap = current_mins - entry_mins
            # Lua records the current location transition to dialogue history before it emits
            # the matching game_event. Skip that just-recorded entry so we only gate on older visits.
            if gap == 0 and not skipped_current_entry:
                skipped_current_entry = True
                continue
            if 0 <= gap < revisit_cooldown_minutes:
                return True
            if gap >= revisit_cooldown_minutes:
                return False

        return False

    def _select_event_for_batch(self, buffered_events, commentary: dict, light_context: dict, dialogue_history: list) -> Optional[NormalizedGameEvent]:
        sorted_events = sorted(buffered_events, key=lambda ev: (ev.priority, ev.timestamp), reverse=True)
        for event in sorted_events:
            if self._fails_cooldowns(event, commentary):
                print(f"[Commentary] Ignoring {event.event_type}: cooldown gate failed")
                continue
            if event.event_type == "cinematic:end" and not self._has_recent_cutscene_history(dialogue_history):
                print("[Commentary] Ignoring cinematic:end without recent cutscene history")
                continue
            if event.event_type == "location:change" and self._has_recent_location_history(
                dialogue_history,
                companion_id=light_context.get("companionId"),
                location_name=event.location_key,
                current_game_date=light_context.get("dateFormatted"),
                current_game_time=light_context.get("timeFormatted") or light_context.get("gameTime"),
                revisit_cooldown_minutes=int(commentary.get("location_revisit_cooldown_game_minutes", 720)),
            ):
                print(f"[Commentary] Ignoring recent location revisit: {event.location_key}")
                continue
            return event
        return None

    def _summarize_combat(self, data: dict) -> str:
        summary = (data.get("summary") or "").strip()
        if summary:
            return summary

        enemy_counts = data.get("enemyCounts") or {}
        enemy_parts = []
        if isinstance(enemy_counts, dict):
            for name, count in enemy_counts.items():
                try:
                    count_int = int(count)
                except Exception:
                    count_int = count
                if count_int == 1:
                    enemy_parts.append(str(name))
                else:
                    enemy_parts.append(f"{name} ({count_int})")

        parts = []
        if enemy_parts:
            parts.append("Combat ended: " + ", ".join(sorted(enemy_parts)))
        else:
            parts.append("Combat ended")

        player_pct = data.get("playerDamagePct")
        companion_pct = data.get("companionDamagePct")
        if player_pct is not None and companion_pct is not None:
            parts.append(f"Player dealt {player_pct}%, companion dealt {companion_pct}%.")

        return " ".join(parts)

    def _build_selector_events(self, selected_event: NormalizedGameEvent, buffered_events: list[NormalizedGameEvent], event_window: float) -> list[str]:
        now = time.time()
        recent = [
            ev for ev in buffered_events
            if (now - ev.timestamp) <= event_window
        ]
        recent.sort(key=lambda ev: ev.timestamp, reverse=True)

        events = [selected_event.summary]
        for ev in recent:
            if ev is selected_event or ev.summary == selected_event.summary:
                continue
            if selected_event.event_type == "cinematic:end" and ev.event_type == "location:change":
                continue
            events.append(ev.summary)
            if len(events) >= 3:
                break
        return events

    def _select_recent_dialogue_for_event(self, dialogue_history: list, selected_event: NormalizedGameEvent) -> list:
        if not dialogue_history:
            return []

        if selected_event.event_type == "cinematic:end":
            cutscene_entries = [
                entry for entry in dialogue_history
                if isinstance(entry, dict) and (entry.get("type") or "").strip().lower() == "cutscene"
            ]
            if cutscene_entries:
                return cutscene_entries[-6:]

        spoken_types = {"dialogue", "cutscene", "prompt"}
        spoken_entries = [
            entry for entry in dialogue_history
            if isinstance(entry, dict) and (entry.get("type") or "").strip().lower() in spoken_types
        ]
        return spoken_entries[-4:]

    def _format_elapsed(self, timestamp: Optional[float]) -> str:
        if not timestamp:
            return "never"
        seconds = max(0, int(time.time() - timestamp))
        if seconds < 60:
            return f"{seconds} seconds ago"
        minutes = seconds // 60
        if minutes < 60:
            return f"{minutes} minutes ago"
        hours = minutes // 60
        return f"{hours} hours ago"
