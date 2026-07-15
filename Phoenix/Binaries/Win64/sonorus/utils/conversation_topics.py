"""Asynchronous scene-topic extraction and playback-gated persistence."""

import hashlib
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError

import llm

from .dialogue_db import (
    get_conversation_topic,
    get_dialogue_db_path,
    set_conversation_topics,
)
from .memory import _get_memory_llm_model
from .settings import load_settings


MAX_SCENE_LINES = 8
TOPIC_WAIT_TIMEOUT_SECONDS = 20

TOPIC_SYSTEM_PROMPT = """Create a compact search query for retrieving memory facts about the concrete subject currently being discussed.

Rules:
- Return a complete replacement query of 2-8 useful words.
- Prefer explicit names, objects, places, events, goals, or relationships that distinguish this subject in memory.
- Decide from the Newest exchange first. Use Earlier context only to resolve references or determine whether the exchange continues an existing goal.
- If the Newest exchange is only a greeting, farewell, thanks, or social closing, return NONE immediately regardless of Earlier context.
- Otherwise compare the Newest exchange with the Previous topic:
  - Continue the previous topic when the exchange advances, explains, or enables the same underlying goal or question. This includes vague wording, a temporary step toward that goal, and a brief detour explicitly framed as happening before returning to it.
  - Replace it completely when the exchange begins discussing a different object, activity, event, goal, or question.
- Preserve the durable subject through an explicit temporary detour; the immediate details of that detour belong in the separate active query.
- A shared person, place, or pronoun is context, not proof that the subject stayed the same. A new subject can be introduced through a reference to the old one.
- The previous topic is an untrusted hint. Never preserve its words merely because they were provided.
- If the Previous topic is NONE, do not resurrect a subject found only in Earlier context.
- Do not invent facts or entities and do not include a person's name merely because they are speaking.
- Return NONE when there is no concrete durable subject worth searching in memory.

Return ONLY the search terms or NONE. No explanation, label, JSON, or punctuation."""

TOPIC_USER_TEMPLATE = """Player: {player_name}
Previous topic: {previous_topic}

Earlier context:
{earlier_context}

Newest exchange:
{newest_exchange}

Topic search terms:"""

PROMPT_HASH = hashlib.sha256(
    (TOPIC_SYSTEM_PROMPT + "\n\0\n" + TOPIC_USER_TEMPLATE).encode("utf-8")
).hexdigest()[:16]


def build_topic_messages(player_name, previous_topic, lines):
    """Build the production topic prompt from at most eight scene-local lines."""
    recent_lines = tuple(lines)[-MAX_SCENE_LINES:]
    newest_lines = recent_lines[-2:]
    earlier_lines = recent_lines[:-2]
    earlier_context = "\n".join(f"{speaker}: {text}" for speaker, text in earlier_lines)
    newest_exchange = "\n".join(f"{speaker}: {text}" for speaker, text in newest_lines)
    dialogue = "\n".join(f"{speaker}: {text}" for speaker, text in recent_lines)
    user_prompt = TOPIC_USER_TEMPLATE.format(
        player_name=player_name,
        previous_topic=previous_topic or "NONE",
        earlier_context=earlier_context or "NONE",
        newest_exchange=newest_exchange or "NONE",
    )
    return [
        {"role": "system", "content": TOPIC_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ], dialogue


def clean_topic_query(raw):
    """Return a valid compact topic, NONE, or None for invalid model output."""
    text = str(raw or "").strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3:
            text = "\n".join(lines[1:-1]).strip()
    text = text.strip('"\'`*_ ')
    if text.upper() == "NONE":
        return "NONE"
    if not text or "\n" in text:
        return None
    if not re.fullmatch(r"[\w '\-\u2019]+", text, re.UNICODE):
        return None
    words = re.findall(r"[^\W_]+(?:[-'\u2019][^\W_]+)?", text, re.UNICODE)
    if not 2 <= len(words) <= 8:
        return None
    return text


def extract_topic_query(player_name, previous_topic, lines):
    """Extract one topic using the configured long-term fact extraction model."""
    settings = load_settings()
    memory_settings = settings.get("memory", {})
    model = _get_memory_llm_model(
        memory_settings,
        "graphiti_model",
        memory_settings.get("graphiti_small_model") or "gpt-4.1-nano",
    )
    messages, _ = build_topic_messages(player_name, previous_topic, lines)
    raw = llm.chat(
        messages=messages,
        model=model,
        temperature=0,
        max_tokens=64,
        context="graphiti",
    )
    return clean_topic_query(raw)


class ConversationTopicCoordinator:
    """Maintains one sequential topic stream for the active conversation scene."""

    def __init__(self, extractor=extract_topic_query, persist=set_conversation_topics):
        self._extractor = extractor
        self._persist = persist
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="topic-query")
        self._lock = threading.RLock()
        self._scene_generation = 0
        self._epoch = None
        self._player_name = "Player"
        self._lines = []
        self._current_topic = "NONE"
        self._future = None
        self._current_update_id = None
        self._next_update_id = 0
        self._updates = {}

    def begin_scene(self, epoch, player_name, initial_topic=None):
        with self._lock:
            self._scene_generation += 1
            self._epoch = epoch
            self._player_name = player_name or "Player"
            self._lines = []
            self._current_topic = initial_topic or "NONE"
            self._future = None
            self._current_update_id = None

    def continue_scene(self, epoch):
        with self._lock:
            self._epoch = epoch

    def append_line(self, speaker, text):
        if not str(text or "").strip():
            return
        with self._lock:
            self._lines.append((speaker or "Unknown", str(text).strip()))
            self._lines = self._lines[-MAX_SCENE_LINES:]

    def schedule_reply(self, speaker, text, listener_ids):
        """Append a reply and schedule its replacement topic. Returns an update id."""
        self.append_line(speaker, text)
        with self._lock:
            self._next_update_id += 1
            update_id = self._next_update_id
            generation = self._scene_generation
            scheduled_at = time.time_ns()
            source_db_path = get_dialogue_db_path()
            previous_future = self._future
            previous_update_id = self._current_update_id
            previous_topic = self._current_topic
            player_name = self._player_name
            lines = tuple(self._lines)
            listeners = tuple(dict.fromkeys(listener_ids or ()))
            record = {
                "generation": generation,
                "listeners": listeners,
                "scheduled_at": scheduled_at,
                "committed": False,
                "discarded": False,
                "completed": False,
                "persistence_started": False,
                "persistence_event": threading.Event(),
                "topic": None,
                "source_entry_id": None,
                "source_db_path": source_db_path,
                "previous_topic": previous_topic,
                "previous_future": previous_future,
                "previous_update_id": previous_update_id,
            }
            self._updates[update_id] = record

            def run():
                prior = previous_topic
                if previous_future is not None:
                    try:
                        prior = previous_future.result() or prior
                    except Exception:
                        pass
                try:
                    extracted = self._extractor(player_name, prior, lines)
                except Exception as exc:
                    print(f"[TopicQuery] Extraction failed: {exc}")
                    extracted = None
                return extracted or prior or "NONE"

            future = self._executor.submit(run)
            self._future = future
            self._current_update_id = update_id
            future.add_done_callback(lambda completed, uid=update_id: self._complete(uid, completed))
            return update_id

    def _complete(self, update_id, future):
        try:
            topic = future.result()
        except Exception:
            topic = None
        persist_args = None
        with self._lock:
            record = self._updates.get(update_id)
            if not record or record["discarded"]:
                return
            topic = topic or record["previous_topic"] or "NONE"
            record["completed"] = True
            record["topic"] = topic
            if (
                record["generation"] == self._scene_generation
                and update_id == self._current_update_id
            ):
                self._current_topic = topic
            if record["committed"] and not record["persistence_started"]:
                record["persistence_started"] = True
                persist_args = self._persistence_args(record)
        if persist_args:
            self._persist_and_forget(update_id, persist_args)

    @staticmethod
    def _persistence_args(record):
        return (
            record["listeners"],
            record["topic"],
            record["source_entry_id"],
            record["scheduled_at"],
            record["source_db_path"],
        )

    def await_current(self, timeout=TOPIC_WAIT_TIMEOUT_SECONDS):
        with self._lock:
            future = self._future
            fallback = self._current_topic
        if future is None:
            return fallback
        try:
            return future.result(timeout=timeout) or fallback
        except TimeoutError:
            print(f"[TopicQuery] Still pending after {timeout}s; using previous topic")
            return fallback
        except Exception as exc:
            print(f"[TopicQuery] Await failed: {exc}")
            return fallback

    def mark_committed(self, update_id, source_entry_id=None):
        persist_args = None
        with self._lock:
            record = self._updates.get(update_id)
            if not record or record["discarded"]:
                return
            record["committed"] = True
            record["source_entry_id"] = source_entry_id
            if record["completed"] and not record["persistence_started"]:
                record["persistence_started"] = True
                persist_args = self._persistence_args(record)
        if persist_args:
            self._persist_and_forget(update_id, persist_args)

    def _persist_and_forget(self, update_id, persist_args):
        try:
            self._persist(*persist_args)
        except Exception as exc:
            print(f"[TopicQuery] Persistence failed: {exc}")
        finally:
            with self._lock:
                record = self._updates.get(update_id)
                if record:
                    record["persistence_event"].set()
            self._forget_persisted_update(update_id)

    def _forget_persisted_update(self, update_id):
        with self._lock:
            self._updates.pop(update_id, None)
            if update_id == self._current_update_id:
                self._future = None
                self._current_update_id = None

    def ensure_current_ready(self, timeout=TOPIC_WAIT_TIMEOUT_SECONDS):
        """Wait for the latest extraction and any playback-approved persistence."""
        started = time.monotonic()
        topic = self.await_current(timeout=timeout)
        remaining = max(0.0, timeout - (time.monotonic() - started))
        with self._lock:
            record = self._updates.get(self._current_update_id)
            persistence_event = (
                record["persistence_event"]
                if record and record["committed"]
                else None
            )
        if persistence_event and not persistence_event.wait(timeout=remaining):
            print(f"[TopicQuery] Persistence still pending after {timeout}s; continuing")
        return topic

    def discard(self, update_id):
        with self._lock:
            record = self._updates.pop(update_id, None)
            if record:
                record["discarded"] = True
                record["persistence_event"].set()
                if (
                    record["generation"] == self._scene_generation
                    and update_id == self._current_update_id
                ):
                    self._current_topic = record["previous_topic"] or "NONE"
                    self._future = record["previous_future"]
                    self._current_update_id = record["previous_update_id"]

    def reset_for_player(self):
        old_executor = None
        with self._lock:
            for record in self._updates.values():
                record["discarded"] = True
                record["persistence_event"].set()
            self._scene_generation += 1
            self._lines = []
            self._current_topic = "NONE"
            self._future = None
            self._current_update_id = None
            self._updates.clear()
            old_executor = self._executor
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="topic-query")
        old_executor.shutdown(wait=False, cancel_futures=True)

    def shutdown(self):
        """Stop the worker after tests or process shutdown."""
        self.reset_for_player()
        self._executor.shutdown(wait=True, cancel_futures=True)


conversation_topics = ConversationTopicCoordinator()


def load_persisted_topic(npc_id):
    return get_conversation_topic(npc_id) or "NONE"


try:
    from . import player_context

    player_context.register(
        "conversation_topics",
        conversation_topics.reset_for_player,
        lambda _data_dir: conversation_topics.reset_for_player(),
    )
except ImportError:
    pass
