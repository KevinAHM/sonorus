"""
NPC Long-Term Memory System for Sonorus.

Uses Graphiti with Kuzu embedded graph database to store and retrieve NPC memories.
Each NPC has their own namespace (group_id) for memory isolation based on earshot.
Kuzu is an embedded DB requiring no external setup - data is stored locally.
"""

import os
import json
import asyncio
import time
import string
import threading
from datetime import datetime, timezone
from typing import Optional, Dict, List, Any, ClassVar
from pydantic import BaseModel, Field

from .settings import load_settings, DATA_DIR, is_dev_mode
from .kuzu_executor import get_executor, TaskPriority, shutdown_executor
from .profiler import Profiler
from constants import EPISODE_CONTEXT_WINDOW

# Get shared profiler instance
_profiler = Profiler.get("chat_flow")

# Lazy imports for Graphiti (may not be installed)
_graphiti_available = False
_graphiti_instance = None

try:
    from graphiti_core import Graphiti
    from graphiti_core.llm_client.config import LLMConfig
    from graphiti_core.nodes import EpisodeType
    from graphiti_core.search.search_config_recipes import EDGE_HYBRID_SEARCH_RRF
    from graphiti_core.search.search_filters import SearchFilters, DateFilter, ComparisonOperator
    _graphiti_available = True

    # Patch Graphiti's summarize_pair prompt to prevent hallucinations
    def _patch_graphiti_prompts():
        """Override Graphiti's default summarization prompts with stricter versions."""
        try:
            from graphiti_core.prompts import prompt_library
            from graphiti_core.prompts.models import Message
            from graphiti_core.prompts.prompt_helpers import to_prompt_json

            def better_summarize_pair(context):
                """Improved summarize_pair that preserves factual accuracy."""
                return [
                    Message(
                        role='system',
                        content='You are a helpful assistant that combines summaries while preserving factual accuracy.',
                    ),
                    Message(
                        role='user',
                        content=f"""Combine the following two summaries into a single summary.

CRITICAL RULES:
1. DO NOT change the meaning of facts. "going to do X" ≠ "doing X" ≠ "did X"
2. DO NOT assume completion of incomplete actions. If something was attempted but not finished, say so.
3. PRESERVE original tense and intent. Do not upgrade "attempting" to "succeeded".
4. PRESERVE full names exactly as written. "Nellie Oggspire" stays "Nellie Oggspire", not "Nellie" or "Oggspire". Do not split or shorten names.
5. If summaries contradict, include both perspectives rather than picking one.
6. Keep under 250 characters.

Summaries:
{to_prompt_json(context['node_summaries'])}
""",
                    ),
                ]

            # Monkey-patch the prompt
            prompt_library.summarize_nodes.summarize_pair.func = better_summarize_pair
            print("[Memory] Patched Graphiti summarize_pair prompt")
        except Exception as e:
            print(f"[Memory] Warning: Could not patch Graphiti prompts: {e}")

    _patch_graphiti_prompts()

except ImportError:
    print("[Memory] graphiti-core not installed - memory system unavailable")
    print("[Memory] Install with: pip install graphiti-core")


# =============================================================================
# Custom Entity Types for Hogwarts Domain
# =============================================================================

class Character(BaseModel):
    """A person in the wizarding world (wizard, witch, goblin, ghost, etc.)."""
    house: Optional[str] = Field(None, description="Hogwarts house if student/alumni: Gryffindor, Slytherin, Hufflepuff, Ravenclaw")
    role: Optional[str] = Field(None, description="Role like 'student', 'professor', 'shopkeeper', 'auror', 'dark wizard'")
    species: Optional[str] = Field(None, description="Species if not human: goblin, house-elf, ghost, portrait")


class Creature(BaseModel):
    """A magical creature (not a person)."""
    creature_type: Optional[str] = Field(None, description="Type like 'beast', 'dragon', 'troll', 'spider'")
    danger_level: Optional[str] = Field(None, description="Threat level: harmless, moderate, dangerous, deadly")


class Location(BaseModel):
    """A place in the wizarding world."""
    region: Optional[str] = Field(None, description="Region: Hogwarts, Hogsmeade, Highlands, Feldcroft, Coastal")
    location_type: Optional[str] = Field(None, description="Type: shop, classroom, dungeon, ruins, cave, camp")


class Quest(BaseModel):
    """A mission, quest, or objective."""
    status: Optional[str] = Field(None, description="Status: active, completed, failed, blocked")
    quest_giver: Optional[str] = Field(None, description="Who gave the quest")
    objective: Optional[str] = Field(None, description="What needs to be done")


class Item(BaseModel):
    """An item, artifact, or object."""
    item_type: Optional[str] = Field(None, description="Type: wand, potion, artifact, clothing, book, portrait")
    magical: Optional[bool] = Field(None, description="Whether the item is magical")


class Faction(BaseModel):
    """A group, organization, or allegiance."""
    faction_type: Optional[str] = Field(None, description="Type: criminal, school, government, secret")
    alignment: Optional[str] = Field(None, description="Alignment: friendly, hostile, neutral")


# Edge types for relationships between entities
class Relationship(BaseModel):
    """Relationship between characters."""
    sentiment: Optional[str] = Field(None, description="Sentiment: positive, negative, neutral, complicated")
    context: Optional[str] = Field(None, description="How they know each other")


class ParticipatedIn(BaseModel):
    """Participation in an event, quest, or action."""
    role: Optional[str] = Field(None, description="Role in the event: leader, participant, observer, victim")
    outcome: Optional[str] = Field(None, description="What happened as a result")


class FeelsAbout(BaseModel):
    """How a character feels about something."""
    emotion: Optional[str] = Field(None, description="The emotion: fear, love, hatred, admiration, distrust")
    reason: Optional[str] = Field(None, description="Why they feel this way")


class MemberOf(BaseModel):
    """Membership in a faction or group."""
    role: Optional[str] = Field(None, description="Role in the group: member, leader, former member")
    loyalty: Optional[str] = Field(None, description="Loyalty level: loyal, wavering, secret")


# Entity and edge type mappings for Graphiti
ENTITY_TYPES = {
    "Character": Character,
    "Creature": Creature,
    "Location": Location,
    "Quest": Quest,
    "Item": Item,
    "Faction": Faction,
}

EDGE_TYPES = {
    "Relationship": Relationship,
    "ParticipatedIn": ParticipatedIn,
    "FeelsAbout": FeelsAbout,
    "MemberOf": MemberOf,
}

EDGE_TYPE_MAP = {
    ("Character", "Character"): ["Relationship", "FeelsAbout"],
    ("Character", "Creature"): ["FeelsAbout", "ParticipatedIn"],
    ("Character", "Quest"): ["ParticipatedIn"],
    ("Character", "Location"): ["ParticipatedIn", "FeelsAbout"],
    ("Character", "Faction"): ["MemberOf", "FeelsAbout"],
    ("Character", "Item"): ["ParticipatedIn"],
}


# =============================================================================
# Utility Functions
# =============================================================================

def game_time_to_datetime(game_date: str, game_time: str = "") -> datetime:
    """
    Convert game time (1890/12/08, 14:30) to datetime for Graphiti.

    Args:
        game_date: Date string like "1890/12/08" or "Monday, December 8th, 1890"
        game_time: Time string like "14:30" or "2:30 PM"

    Returns:
        datetime object for Graphiti's reference_time
    """
    try:
        # Try parsing YYYY/MM/DD format first
        if '/' in game_date and len(game_date.split('/')[0]) == 4:
            date_parts = game_date.split('/')
            year = int(date_parts[0])
            month = int(date_parts[1])
            day = int(date_parts[2])
        else:
            # Fallback - use current date components
            now = datetime.now()
            year, month, day = now.year, now.month, now.day

        # Parse time
        hour, minute = 12, 0  # Default to noon
        if game_time:
            time_clean = game_time.strip().upper()
            is_pm = 'PM' in time_clean
            is_am = 'AM' in time_clean
            time_clean = time_clean.replace('AM', '').replace('PM', '').strip()

            if ':' in time_clean:
                time_parts = time_clean.split(':')
                hour = int(time_parts[0])
                minute = int(time_parts[1]) if len(time_parts) > 1 else 0

                # Convert 12-hour to 24-hour
                if is_pm and hour < 12:
                    hour += 12
                elif is_am and hour == 12:
                    hour = 0

        return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
    except Exception as e:
        print(f"[Memory] Error parsing game time '{game_date}' '{game_time}': {e}")
        return datetime.now(timezone.utc)


def is_valid_timestamp(ts: any) -> bool:
    """Check if a value is a valid Unix timestamp (reasonable range)."""
    if ts is None:
        return False
    try:
        ts_int = int(ts)
        # Valid range: ~2017 to ~2286 in Unix time
        # This covers our use case where we record real timestamps
        return 1500000000 <= ts_int <= 10000000000
    except (ValueError, TypeError):
        return False


def get_chapters_dir() -> str:
    """Get the chapters directory path, creating it if needed."""
    chapters_dir = os.path.join(DATA_DIR, "chapters")
    os.makedirs(chapters_dir, exist_ok=True)
    return chapters_dir


# =============================================================================
# GraphitiManager - Handles graph database operations
# =============================================================================

class GraphitiManager:
    """Manages Graphiti instance and graph operations."""

    _instance = None
    _lock = None  # Threading lock for concurrent access

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
            cls._lock = threading.Lock()
        return cls._instance

    # Class-level flag to track if indices were built this session
    _indices_built: ClassVar[bool] = False

    def __init__(self):
        if self._initialized:
            return

        self._graphiti = None
        self._loop = None  # The loop Graphiti was initialized on
        self._busy = False  # Flag for when Graphiti is doing heavy async work
        self._initialized = True

    def _ensure_loop(self):
        """Ensure we have an event loop for Graphiti operations.

        Always creates a dedicated loop rather than risking get_event_loop()
        returning the main thread's loop or a stale loop from another context.
        This runs on the KuzuExecutor worker thread which has no event loop.
        """
        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
        return self._loop

    def _run_async(self, coro, skip_if_busy: bool = False,
                    priority: TaskPriority = TaskPriority.NORMAL,
                    timeout: float = 120.0):
        """Run async coroutine synchronously through the serialized executor.

        All kuzu/graphiti operations are serialized through KuzuExecutor to prevent
        concurrent access crashes (kuzu is not thread-safe).

        Args:
            coro: The coroutine to run
            skip_if_busy: If True, return None immediately if Graphiti is busy
            priority: Task priority for the executor queue
            timeout: Maximum time to wait for the operation
        """
        # Skip if Graphiti is busy with heavy operations (like adding episodes)
        if skip_if_busy and self._busy:
            print("[Memory] Skipping - Graphiti busy with background operation")
            return None

        def _execute_async():
            """Execute the coroutine - runs in the executor's worker thread."""
            try:
                loop = self._ensure_loop()
                return loop.run_until_complete(coro)
            except RuntimeError as e:
                if "running" in str(e).lower() or "attached to a different loop" in str(e).lower():
                    print(f"[Memory] Event loop busy, skipping operation")
                    return None
                print(f"[Memory] Runtime error: {e}")
                return None
            except Exception as e:
                print(f"[Memory] Async error: {e}")
                return None

        # Submit to executor for serialized execution
        # Note: max_retries=0 because coroutines can only be awaited once
        executor = get_executor()
        result = executor.submit(_execute_async, priority=priority, timeout=timeout, max_retries=0)

        if result.success:
            return result.value
        elif result.error:
            print(f"[Memory] Executor error: {result.error}")
            return None
        else:
            return None

    def init_graphiti(self) -> bool:
        """Initialize Graphiti with Kuzu driver based on user's LLM provider."""
        if not _graphiti_available:
            print("[Memory] Graphiti not available")
            return False

        if self._graphiti is not None:
            return True

        # Lock prevents multiple threads from submitting redundant init tasks
        # to the executor (e.g. server.py runs contextual_memory + search_facts
        # in parallel, both calling init_graphiti before the first completes)
        with self._lock:
            if self._graphiti is not None:
                return True

            executor = get_executor()
            result = executor.submit(self._init_graphiti_impl, priority=TaskPriority.HIGH, timeout=60.0)
            return result.value if result.success else False

    @staticmethod
    def _backup_kuzu_db(kuzu_db_path: str) -> bool:
        """Backup the Kuzu DB file and WAL after successful init."""
        # NOTE: Kuzu DB is a single FILE (memory.kuzu), NOT a directory.
        # WAL file is memory.kuzu.wal. Both are backed up at init (no active writes).
        import shutil
        backup_path = kuzu_db_path + ".backup"
        wal_path = kuzu_db_path + ".wal"
        wal_backup_path = wal_path + ".backup"
        try:
            if os.path.isfile(kuzu_db_path):
                shutil.copy2(kuzu_db_path, backup_path)
                if os.path.isfile(wal_path):
                    shutil.copy2(wal_path, wal_backup_path)
                print(f"[Memory/Kuzu] Backed up database to {backup_path}")
                return True
        except Exception as e:
            print(f"[Memory/Kuzu] Backup failed: {e}")
        return False

    def _init_graphiti_impl(self) -> bool:
        """Implementation of init_graphiti - runs on executor thread."""
        if self._graphiti is not None:
            return True

        try:
            from graphiti_core.driver.kuzu_driver import KuzuDriver

            settings = load_settings()
            llm_settings = settings.get('llm', {})
            memory_settings = settings.get('memory', {})
            provider = llm_settings.get('provider', 'gemini')

            # Kuzu database path - single FILE, not a directory. WAL file is memory.kuzu.wal.
            kuzu_db_path = os.path.join(DATA_DIR, "memory.kuzu")

            # Create Kuzu driver
            kuzu_driver = KuzuDriver(db=kuzu_db_path)
            # Fix for graphiti-core bug: KuzuDriver doesn't set _database attribute
            # but graphiti.py expects it when using group_id
            kuzu_driver._database = ''

            # Load FTS extension and create indices on the kuzu executor thread.
            # Uses execute_on_thread to maintain thread affinity for all C++ access.
            def _setup_fts(conn):
                try:
                    conn.execute("LOAD EXTENSION FTS;")
                except Exception:
                    pass  # Already loaded
                fts_indices = [
                    ("Episodic", "episode_content", ['content', 'source', 'source_description']),
                    ("Entity", "node_name_and_summary", ['name', 'summary']),
                    ("Community", "community_name", ['name']),
                    ("RelatesToNode_", "edge_name_and_fact", ['name', 'fact']),
                ]
                for table, index_name, columns in fts_indices:
                    cols_str = str(columns).replace('"', "'")
                    try:
                        conn.execute(f"CALL CREATE_FTS_INDEX('{table}', '{index_name}', {cols_str});")
                        print(f"[Memory/Kuzu] Created FTS index '{index_name}' on {table}")
                    except Exception as e:
                        if 'already exists' not in str(e).lower():
                            print(f"[Memory/Kuzu] FTS index error '{index_name}': {e}")

            kuzu_driver.execute_on_thread(_setup_fts)

            # Models for Graphiti
            graphiti_model = memory_settings.get('graphiti_model', 'gemini-2.5-flash-lite')
            graphiti_small_model = memory_settings.get('graphiti_small_model', 'gemini-2.5-flash-lite')
            reranker_model = memory_settings.get('reranker_model', 'gemini-2.5-flash-lite')
            max_concurrency = memory_settings.get('max_concurrency', 2)

            # Override Graphiti's global semaphore limit so ALL semaphore_gather calls
            # (embeddings, reranker, LLM, search, etc.) respect our setting
            import graphiti_core.helpers as _graphiti_helpers
            _graphiti_helpers.SEMAPHORE_LIMIT = max_concurrency

            # Import logging wrapper
            from .graphiti_llm_wrapper import wrap_graphiti_client

            if provider == 'gemini':
                from graphiti_core.llm_client.gemini_client import GeminiClient
                from graphiti_core.embedder.gemini import GeminiEmbedder, GeminiEmbedderConfig
                from graphiti_core.cross_encoder.gemini_reranker_client import GeminiRerankerClient
                from google.genai import types as gemini_types
                import llm as llm_module

                api_key = llm_settings.get('gemini', {}).get('api_key', '')
                if not api_key:
                    print("[Memory] No Gemini API key configured")
                    return False

                llm_config = LLMConfig(
                    api_key=api_key,
                    model=graphiti_model,
                    small_model=graphiti_small_model
                )

                # Get thinking config for Gemini (controls reasoning)
                reasoning_params = llm_module.get_reasoning_params('gemini', graphiti_model, 8192)
                thinking_config = gemini_types.ThinkingConfig(**reasoning_params) if reasoning_params else None
                llm_client = GeminiClient(config=llm_config, thinking_config=thinking_config)

                self._graphiti = Graphiti(
                    graph_driver=kuzu_driver,
                    llm_client=wrap_graphiti_client(llm_client, "graphiti_memory"),
                    embedder=GeminiEmbedder(config=GeminiEmbedderConfig(api_key=api_key)),
                    cross_encoder=GeminiRerankerClient(config=llm_config),
                    max_coroutines=max_concurrency
                )

            elif provider == 'openrouter':
                from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
                from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
                from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient

                # Apply monkey-patch for reasoning params
                from .graphiti_patches import patch_graphiti_reasoning
                patch_graphiti_reasoning()

                api_key = llm_settings.get('openrouter', {}).get('api_key', '')
                if not api_key:
                    print("[Memory] No OpenRouter API key configured")
                    return False

                # OpenRouter supports embeddings via /api/v1/embeddings endpoint
                llm_config = LLMConfig(
                    api_key=api_key,
                    model=graphiti_model,
                    small_model=graphiti_small_model,
                    base_url="https://openrouter.ai/api/v1"
                )
                llm_client = OpenAIGenericClient(config=llm_config)

                # Separate config for reranker (uses tiny model)
                reranker_config = LLMConfig(
                    api_key=api_key,
                    model=reranker_model,
                    base_url="https://openrouter.ai/api/v1"
                )

                self._graphiti = Graphiti(
                    graph_driver=kuzu_driver,
                    llm_client=wrap_graphiti_client(llm_client, "graphiti_memory"),
                    embedder=OpenAIEmbedder(config=OpenAIEmbedderConfig(
                        api_key=api_key,
                        embedding_model="openai/text-embedding-3-small",
                        base_url="https://openrouter.ai/api/v1"
                    )),
                    cross_encoder=OpenAIRerankerClient(config=reranker_config),
                    max_coroutines=max_concurrency
                )

            elif provider == 'openai':
                from graphiti_core.llm_client import OpenAIClient
                from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
                from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
                import llm as llm_module

                openai_settings = llm_settings.get('openai', {})
                api_key = openai_settings.get('api_key', '')
                api_url = openai_settings.get('api_url', '') or None  # None = default OpenAI URL

                if not api_key:
                    print("[Memory] No OpenAI API key configured")
                    return False

                # When responses API is disabled (custom proxy), patch OpenAIClient
                # to use chat.completions instead of responses.parse
                if not llm_module._use_responses_api():
                    from .graphiti_patches import patch_openai_no_responses_api
                    patch_openai_no_responses_api()

                llm_config = LLMConfig(
                    api_key=api_key,
                    model=graphiti_model,
                    small_model=graphiti_small_model,
                    base_url=api_url
                )

                # Get reasoning effort level for OpenAI (e.g., "minimal", "low", "medium")
                reasoning_params = llm_module.get_reasoning_params('openai', graphiti_model, 8192)
                reasoning_effort = reasoning_params.get('reasoning', {}).get('effort', 'minimal') if reasoning_params else 'minimal'
                llm_client = OpenAIClient(config=llm_config, reasoning=reasoning_effort)

                # Separate config for reranker (uses tiny model)
                reranker_config = LLMConfig(
                    api_key=api_key,
                    model=reranker_model,
                    base_url=api_url
                )

                self._graphiti = Graphiti(
                    graph_driver=kuzu_driver,
                    llm_client=wrap_graphiti_client(llm_client, "graphiti_memory"),
                    embedder=OpenAIEmbedder(config=OpenAIEmbedderConfig(
                        api_key=api_key,
                        base_url=api_url,
                        embedding_model="text-embedding-3-small"
                    )),
                    cross_encoder=OpenAIRerankerClient(config=reranker_config),
                    max_coroutines=max_concurrency
                )

            else:
                print(f"[Memory] Unsupported LLM provider: {provider}")
                return False

            print(f"[Memory/Graphiti] Initialized with Kuzu + {provider}")
            print(f"[Memory/Graphiti] Kuzu DB: {kuzu_db_path}")
            print(f"[Memory/Graphiti] LLM Model: {graphiti_model}")
            print(f"[Memory/Graphiti] Max concurrent requests: {max_concurrency}")

            # Build indices and constraints (only once per session)
            # Note: We're already on the executor thread, so run directly without _run_async
            if not GraphitiManager._indices_built:
                print("[Memory/Graphiti] Building indices and constraints...")
                loop = self._ensure_loop()
                loop.run_until_complete(self._graphiti.build_indices_and_constraints())
                GraphitiManager._indices_built = True
                print("[Memory/Graphiti] Schema setup complete")
            else:
                print("[Memory/Graphiti] Schema already built this session")

            # Backup DB after successful init so we can recover from future corruption
            self._backup_kuzu_db(kuzu_db_path)

            return True

        except ImportError as e:
            if 'kuzu' in str(e).lower():
                print("[Memory] kuzu package not installed - run: pip install graphiti-core[kuzu]")
            else:
                print(f"[Memory] Import error: {e}")
            return False
        except Exception as e:
            print(f"[Memory] Failed to initialize Graphiti: {e}")
            import traceback
            traceback.print_exc()
            kuzu_db_path = os.path.join(DATA_DIR, "memory.kuzu")
            backup_path = kuzu_db_path + ".backup"
            if os.path.exists(backup_path):
                print(f"[Memory] Memory database may be corrupted. A backup exists at: {backup_path}")
                print(f"[Memory] To restore: delete '{os.path.basename(kuzu_db_path)}' and its .wal file, rename the .backup files (remove the .backup extension), then restart.")
            elif os.path.exists(kuzu_db_path):
                print(f"[Memory] Memory database may be corrupted. No backup available.")
                print(f"[Memory] To reset: delete '{os.path.basename(kuzu_db_path)}' and its .wal file, then restart.")
            return False

    async def _add_episode_async(self, npc_id: str, chapter_title: str,
                                   content: str, reference_time: datetime,
                                   max_retries: int = 3) -> bool:
        """Add an episode to the NPC's graph namespace with retry logic."""
        if self._graphiti is None:
            return False

        print(f"[Memory/Graphiti] Adding episode '{chapter_title}' for {npc_id}...")
        print(f"[Memory/Graphiti] Episode content length: {len(content)} chars")
        print(f"[Memory/Graphiti] Reference time: {reference_time}")

        # Retrieve previous episode UUIDs to override graphiti's default of 10
        previous_episode_uuids = None
        try:
            previous_episodes = await self._graphiti.retrieve_episodes(
                reference_time=reference_time,
                last_n=EPISODE_CONTEXT_WINDOW,
                group_ids=[npc_id],
                source=EpisodeType.message
            )
            previous_episode_uuids = [ep.uuid for ep in previous_episodes]
            print(f"[Memory/Graphiti] Using {len(previous_episode_uuids)} previous episodes for context (window={EPISODE_CONTEXT_WINDOW})")
        except Exception as e:
            print(f"[Memory/Graphiti] Could not retrieve previous episodes, using graphiti default: {e}")

        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                await self._graphiti.add_episode(
                    name=f"chapter_{chapter_title.replace(' ', '_').lower()}",
                    episode_body=content,
                    source=EpisodeType.message,
                    source_description="NPC dialogue chapter",
                    reference_time=reference_time,
                    group_id=npc_id,
                    entity_types=ENTITY_TYPES,
                    edge_types=EDGE_TYPES,
                    edge_type_map=EDGE_TYPE_MAP,
                    previous_episode_uuids=previous_episode_uuids
                )
                print(f"[Memory/Graphiti] SUCCESS: Episode '{chapter_title}' added to Kuzu")
                return True
            except Exception as e:
                last_error = e
                print(f"[Memory/Graphiti] Attempt {attempt}/{max_retries} failed: {type(e).__name__}: {e}")
                if attempt < max_retries:
                    wait_time = attempt * 2  # Exponential backoff: 2s, 4s
                    print(f"[Memory/Graphiti] Retrying in {wait_time}s...")
                    await asyncio.sleep(wait_time)

        print(f"[Memory/Graphiti] FAILED after {max_retries} attempts: {last_error}")
        import traceback
        traceback.print_exc()
        return False

    def add_episode(self, npc_id: str, chapter_title: str,
                    content: str, game_date: str, game_time: str = "") -> bool:
        """Add an episode to the NPC's graph namespace (sync wrapper)."""
        if not self.init_graphiti():
            return False

        reference_time = game_time_to_datetime(game_date, game_time)

        # Set busy flag to prevent concurrent memory operations during heavy async work
        self._busy = True
        try:
            return self._run_async(
                self._add_episode_async(npc_id, chapter_title, content, reference_time)
            ) or False
        finally:
            self._busy = False

    async def _get_graph_data_async(self, npc_id: str) -> Dict[str, Any]:
        """Retrieve all entities and relationships for an NPC."""
        if self._graphiti is None:
            return {"edges": [], "entity_count": 0}

        try:
            from graphiti_core.edges import EntityEdge
            from graphiti_core.nodes import EntityNode

            driver = self._graphiti.driver

            # Get all edges for this NPC's group directly (more reliable than search)
            try:
                edge_list = await EntityEdge.get_by_group_ids(driver, [npc_id])
            except Exception as e:
                # GroupsEdgesNotFoundError means no edges yet
                if "NotFoundError" in type(e).__name__:
                    return {"edges": [], "entity_count": 0}
                raise

            # Collect unique node UUIDs
            node_uuids = set()
            for edge in edge_list:
                node_uuids.add(edge.source_node_uuid)
                node_uuids.add(edge.target_node_uuid)

            # Get nodes to build UUID -> name and UUID -> type mappings
            uuid_to_name = {}
            uuid_to_type = {}
            if node_uuids:
                try:
                    nodes = await EntityNode.get_by_uuids(driver, list(node_uuids))
                    for node in nodes:
                        uuid_to_name[node.uuid] = node.name
                        # Get the first non-Entity label as the type
                        node_type = 'Entity'
                        for label in (node.labels or []):
                            if label != 'Entity' and not label.startswith('Entity_'):
                                node_type = label
                                break
                        uuid_to_type[node.uuid] = node_type
                except Exception as e:
                    print(f"[Memory] Warning: Could not get node names: {e}")

            # Get episode UUIDs to look up chapter names
            episode_uuids = set()
            for edge in edge_list:
                for ep_uuid in (edge.episodes or []):
                    episode_uuids.add(ep_uuid)

            # Build episode UUID -> chapter name mapping
            from graphiti_core.nodes import EpisodicNode
            episode_to_chapter = {}
            if episode_uuids:
                try:
                    episodes = await EpisodicNode.get_by_uuids(driver, list(episode_uuids))
                    for ep in episodes:
                        # Episode names are like "chapter_a_midnight_trek_and_fears"
                        if ep.name.startswith('chapter_'):
                            # Use capwords to avoid uppercasing after apostrophes (e.g., "Ferdinand'S")
                            chapter_name = string.capwords(ep.name[8:].replace('_', ' '))
                            episode_to_chapter[ep.uuid] = chapter_name
                except Exception as e:
                    print(f"[Memory] Warning: Could not get episode names: {e}")

            # Build edge list with resolved names and temporal info
            edges = []
            for edge in edge_list:
                source_name = uuid_to_name.get(edge.source_node_uuid, 'Unknown')
                target_name = uuid_to_name.get(edge.target_node_uuid, 'Unknown')
                source_type = uuid_to_type.get(edge.source_node_uuid, 'Entity')
                target_type = uuid_to_type.get(edge.target_node_uuid, 'Entity')

                # Look up chapter names from episode UUIDs
                chapters = []
                for ep_uuid in (edge.episodes or []):
                    if ep_uuid in episode_to_chapter:
                        chapters.append(episode_to_chapter[ep_uuid])

                edges.append({
                    "fact": edge.fact,
                    "source": source_name,
                    "target": target_name,
                    "source_type": source_type,
                    "target_type": target_type,
                    "name": edge.name,  # Edge type/relationship name
                    "chapters": chapters,  # Which chapters this fact is from
                    "valid_at": edge.valid_at.isoformat() if edge.valid_at else None,
                    "created_at": edge.created_at.isoformat() if edge.created_at else None,
                })

            # Sort by created_at (oldest first) for chronological order
            edges.sort(key=lambda e: e.get('created_at') or '')

            return {
                "edges": edges,
                "entity_count": len(node_uuids)
            }
        except Exception as e:
            print(f"[Memory] Failed to get graph data: {e}")
            import traceback
            traceback.print_exc()
            return {"edges": [], "entity_count": 0}

    def get_graph_data(self, npc_id: str) -> Dict[str, Any]:
        """Retrieve all entities and relationships for an NPC (sync wrapper)."""
        if not self.init_graphiti():
            return {"edges": [], "entity_count": 0}

        return self._run_async(self._get_graph_data_async(npc_id)) or {"edges": [], "entity_count": 0}

    async def _delete_node_async(self, npc_id: str, node_name: str) -> Dict[str, Any]:
        """Delete a node and all its edges from the NPC's graph."""
        if self._graphiti is None:
            return {"success": False, "error": "Graphiti not initialized"}

        try:
            from graphiti_core.nodes import EntityNode

            driver = self._graphiti.driver

            # Find the node by name in this NPC's graph
            nodes = await EntityNode.get_by_group_ids(driver, [npc_id])
            target_node = None
            for node in nodes:
                if node.name.lower() == node_name.lower():
                    target_node = node
                    break

            if not target_node:
                return {"success": False, "error": f"Node '{node_name}' not found"}

            # node.delete() does DETACH DELETE which removes the node and all connected edges
            await target_node.delete(driver)

            return {"success": True}

        except Exception as e:
            print(f"[Memory] Error deleting node '{node_name}': {e}")
            import traceback
            traceback.print_exc()
            return {"success": False, "error": str(e)}

    def delete_node(self, npc_id: str, node_name: str) -> Dict[str, Any]:
        """Delete a node and all its edges (sync wrapper)."""
        if not self.init_graphiti():
            return {"success": False, "error": "Kuzu not connected"}

        self._busy = True
        try:
            return self._run_async(self._delete_node_async(npc_id, node_name)) or {"success": False, "error": "Async failed"}
        finally:
            self._busy = False

    async def _delete_edge_async(self, npc_id: str, source_name: str, target_name: str, fact: Optional[str] = None) -> Dict[str, Any]:
        """Delete a specific edge from the NPC's graph."""
        if self._graphiti is None:
            return {"success": False, "error": "Graphiti not initialized"}

        try:
            from graphiti_core.nodes import EntityNode
            from graphiti_core.edges import EntityEdge

            driver = self._graphiti.driver

            # Get all nodes to build name -> uuid mapping
            nodes = await EntityNode.get_by_group_ids(driver, [npc_id])
            name_to_uuid = {node.name.lower(): node.uuid for node in nodes}

            source_uuid = name_to_uuid.get(source_name.lower())
            target_uuid = name_to_uuid.get(target_name.lower())

            if not source_uuid or not target_uuid:
                return {"success": False, "error": "Source or target node not found"}

            # Find and delete matching edge(s)
            edges = await EntityEdge.get_by_group_ids(driver, [npc_id])
            deleted = 0
            for edge in edges:
                if edge.source_node_uuid == source_uuid and edge.target_node_uuid == target_uuid:
                    # If fact provided, match it; otherwise delete first matching edge
                    if fact is None or (edge.fact and edge.fact.lower() == fact.lower()):
                        await edge.delete(driver)
                        deleted += 1
                        if fact:  # Only delete the specific one
                            break

            if deleted == 0:
                return {"success": False, "error": "Edge not found"}

            return {"success": True, "edges_deleted": deleted}

        except Exception as e:
            print(f"[Memory] Error deleting edge: {e}")
            import traceback
            traceback.print_exc()
            return {"success": False, "error": str(e)}

    def delete_edge(self, npc_id: str, source_name: str, target_name: str, fact: Optional[str] = None) -> Dict[str, Any]:
        """Delete a specific edge (sync wrapper)."""
        if not self.init_graphiti():
            return {"success": False, "error": "Kuzu not connected"}

        self._busy = True
        try:
            return self._run_async(self._delete_edge_async(npc_id, source_name, target_name, fact)) or {"success": False, "error": "Async failed"}
        finally:
            self._busy = False

    def _clear_graph_sync(self, npc_id: str) -> Dict[str, Any]:
        """Delete all nodes and edges for an NPC's graph using sync connection."""
        try:
            import time as _time
            import kuzu
            import gc

            # Kuzu DB is a single FILE, not a directory. WAL file is memory.kuzu.wal.
            kuzu_db_path = os.path.join(DATA_DIR, "memory.kuzu")

            # Release Graphiti's DB locks (same as clear_all_memories)
            reset_memory_connection()
            gc.collect()

            # Open fresh database + connection for the delete
            print(f"[Memory/Clear] Deleting graph data for {npc_id}...")
            db = kuzu.Database(kuzu_db_path)
            conn = kuzu.Connection(db)

            # Drop FTS indices first - Kuzu hangs updating them during deletes
            fts_indices = [
                ("RelatesToNode_", "edge_name_and_fact"),
                ("Entity", "node_name_and_summary"),
                ("Episodic", "episode_content"),
                ("Community", "community_name"),
            ]
            for table, index_name in fts_indices:
                try:
                    conn.execute(f"CALL DROP_FTS_INDEX('{table}', '{index_name}')")
                    print(f"[Memory/Clear] Dropped FTS index '{index_name}'")
                except Exception:
                    pass  # Index might not exist

            # Delete edges first (no DETACH), then nodes
            queries = [
                ("RELATES_TO edges (Entity->RelatesToNode_)",
                 "MATCH (n:Entity {group_id: $group_id})-[r:RELATES_TO]->() DELETE r"),
                ("RELATES_TO edges (RelatesToNode_->Entity)",
                 "MATCH (:RelatesToNode_ {group_id: $group_id})-[r:RELATES_TO]->() DELETE r"),
                ("MENTIONS edges",
                 "MATCH (:Episodic {group_id: $group_id})-[r:MENTIONS]->() DELETE r"),
                ("HAS_MEMBER edges",
                 "MATCH (:Community {group_id: $group_id})-[r:HAS_MEMBER]->() DELETE r"),
                ("RelatesToNode_ nodes",
                 "MATCH (n:RelatesToNode_ {group_id: $group_id}) DELETE n"),
                ("Entity nodes",
                 "MATCH (n:Entity {group_id: $group_id}) DELETE n"),
                ("Episodic nodes",
                 "MATCH (n:Episodic {group_id: $group_id}) DELETE n"),
                ("Community nodes",
                 "MATCH (n:Community {group_id: $group_id}) DELETE n"),
            ]

            for label, query in queries:
                print(f"[Memory/Clear] Deleting {label}...")
                t0 = _time.time()
                conn.execute(query, {"group_id": npc_id})
                print(f"[Memory/Clear] Deleted {label} in {_time.time() - t0:.2f}s")

            conn.close()
            del db
            print(f"[Memory/Clear] All deletes complete for {npc_id}")

            # Graphiti will reinitialize on next operation via init_graphiti()
            return {"success": True}

        except Exception as e:
            print(f"[Memory] Error clearing graph for {npc_id}: {e}")
            import traceback
            traceback.print_exc()
            return {"success": False, "error": str(e)}

    def clear_graph(self, npc_id: str) -> Dict[str, Any]:
        """Delete all nodes and edges for an NPC."""
        self._busy = True
        try:
            # Run sync delete through executor (resets connection, deletes, reinits on next use)
            executor = get_executor()
            result = executor.submit(
                self._clear_graph_sync, npc_id,
                priority=TaskPriority.HIGH, timeout=30.0, max_retries=0
            )
            if result.success:
                return result.value or {"success": False, "error": "No result"}
            elif result.error:
                return {"success": False, "error": str(result.error)}
            else:
                return {"success": False, "error": "Executor failed"}
        finally:
            self._busy = False

    async def _get_node_uuid_async(self, npc_id: str, node_name: str) -> Optional[str]:
        """Get UUID for a named node in an NPC's graph."""
        if self._graphiti is None:
            return None

        try:
            from graphiti_core.nodes import EntityNode

            nodes = await EntityNode.get_by_group_ids(self._graphiti.driver, [npc_id])
            for node in nodes:
                if node.name.lower() == node_name.lower():
                    return node.uuid
            return None
        except Exception as e:
            print(f"[Memory] Error getting node UUID for '{node_name}': {e}")
            return None

    def get_node_uuid(self, npc_id: str, node_name: str) -> Optional[str]:
        """Get UUID for a named node (sync wrapper)."""
        if not self.init_graphiti():
            return None
        return self._run_async(self._get_node_uuid_async(npc_id, node_name), skip_if_busy=True)

    async def _search_facts_async(self, npc_id: str, query: str,
                                   center_node_uuid: Optional[str] = None,
                                   max_results: int = 50) -> List[str]:
        """Search for facts relevant to a query, optionally centered on a node."""
        if self._graphiti is None:
            return []

        try:
            valid_only_filter = SearchFilters(
                invalid_at=[[DateFilter(date=None, comparison_operator=ComparisonOperator.is_null)]]
            )

            # Use graphiti's hybrid search with lower similarity threshold
            from graphiti_core.search.search_config_recipes import EDGE_HYBRID_SEARCH_RRF
            from graphiti_core.search.search import search

            # sim_min_score: low to get more candidates into reranker pool
            # reranker_min_score: filters final RRF output (1/rank, so 0.4 = top 2-3)
            search_config = EDGE_HYBRID_SEARCH_RRF.model_copy(deep=True)
            search_config.edge_config.sim_min_score = 0.05
            search_config.reranker_min_score = 0.3
            search_config.limit = max_results

            try:
                results = await search(
                    self._graphiti.clients,
                    query,
                    [npc_id],
                    search_config,
                    valid_only_filter,
                    driver=self._graphiti.driver,
                    center_node_uuid=center_node_uuid,
                )
            except Exception as search_err:
                print(f"[Memory/Search] ERROR: {search_err}")
                results = None

            edge_count = len(results.edges) if results and results.edges else 0
            methods = [m.value for m in search_config.edge_config.search_methods] if search_config.edge_config else []
            reranker = search_config.edge_config.reranker.value if search_config.edge_config else 'none'
            print(f"[Memory/Search] '{query}': {edge_count} results (methods={methods}, reranker={reranker})")
            if results and results.edges and results.edge_reranker_scores:
                for i, (edge, score) in enumerate(zip(results.edges[:5], results.edge_reranker_scores[:5])):
                    print(f"  [{i}] rrf={score:.4f} | {edge.fact[:80]}")

            if not results or not results.edges:
                # Fallback to CONTAINS search if hybrid search returns nothing
                from graphiti_core.driver.driver import GraphProvider
                if self._graphiti.driver.provider == GraphProvider.KUZU:
                    keyword_facts = await self._kuzu_contains_search(npc_id, query, max_results)
                    print(f"[Memory/Search] CONTAINS fallback: {len(keyword_facts)} results")
                    return keyword_facts
                return []

            # Resolve node UUIDs to names for hover highlighting
            from graphiti_core.nodes import EntityNode
            driver = self._graphiti.driver

            node_uuids = set()
            for edge in results.edges:
                node_uuids.add(edge.source_node_uuid)
                node_uuids.add(edge.target_node_uuid)

            uuid_to_name = {}
            if node_uuids:
                try:
                    nodes = await EntityNode.get_by_uuids(driver, list(node_uuids))
                    for node in nodes:
                        uuid_to_name[node.uuid] = node.name
                except Exception:
                    pass  # Fall back to UUIDs if name lookup fails

            rich_results = []
            for edge in results.edges:
                rich_results.append({
                    "fact": edge.fact,
                    "source": uuid_to_name.get(edge.source_node_uuid, edge.source_node_uuid),
                    "target": uuid_to_name.get(edge.target_node_uuid, edge.target_node_uuid),
                })

            print(f"[Memory/Search] Returning {len(rich_results)} facts")
            return rich_results
        except Exception as e:
            print(f"[Memory] Search error: {e}")
            return []

    async def _kuzu_contains_search(self, npc_id: str, query: str, max_results: int = 50) -> List[Dict[str, str]]:
        """Fallback search for Kuzu using CONTAINS instead of broken FTS."""
        try:
            driver = self._graphiti.driver

            # Split query into words for multi-word matching
            query_words = [w.strip().lower() for w in query.split() if len(w.strip()) > 2]
            if not query_words:
                query_words = [query.lower()]

            results = []
            seen = set()

            # Search with full edge pattern to get source/target names
            # Use the driver's execute_query to go through the patched single-thread executor
            for word in query_words:
                safe_word = word.replace("'", "''")
                cypher = f"""
                    MATCH (e:Entity)-[:RELATES_TO]->(r:RelatesToNode_)-[:RELATES_TO]->(e2:Entity)
                    WHERE r.group_id = '{npc_id}'
                    AND (lower(r.fact) CONTAINS '{safe_word}' OR lower(r.name) CONTAINS '{safe_word}'
                         OR lower(e.name) CONTAINS '{safe_word}' OR lower(e2.name) CONTAINS '{safe_word}')
                    RETURN r.fact AS fact, e.name AS source, e2.name AS target
                    LIMIT {max_results}
                """
                rows, _, _ = await driver.execute_query(cypher)
                for row in (rows if isinstance(rows, list) else []):
                    fact = row.get('fact', '')
                    if fact and fact not in seen:
                        seen.add(fact)
                        results.append({
                            "fact": fact,
                            "source": row.get('source', ''),
                            "target": row.get('target', ''),
                        })

            return results[:max_results]

        except Exception as e:
            print(f"[Memory] Kuzu search error: {e}")
            return []

    def search_facts(self, npc_id: str, query: str,
                     center_node_uuid: Optional[str] = None,
                     max_results: int = 5) -> List[Dict[str, str]]:
        """Search for facts relevant to a query (sync wrapper).

        Returns list of dicts with keys: fact, source, target
        """
        if not self.init_graphiti():
            return []
        return self._run_async(
            self._search_facts_async(npc_id, query, center_node_uuid, max_results),
            skip_if_busy=True
        ) or []


# =============================================================================
# ChapterManager - Handles chapter detection and state
# =============================================================================

class ChapterManager:
    """
    Manages chapter detection and state per NPC.

    Storage format (chapters/{NpcId}.json):
    {
        "open_chapter": {
            "title": "...",
            "summary": "...",
            "location": "...",
            "start_timestamp": ..., # Unix timestamp (stable identifier)
            "start_date": "...",    # Game date for display
            "start_time": "...",    # Game time for display
            "key_events": [...],
            "context_messages": [...]
        },
        "closed_chapters": [
            {
                "title": "...",
                "summary": "...",
                "location": "...",
                "start_timestamp": ...,
                "end_timestamp": ...,
                "start_date": "...",
                "start_time": "...",
                "key_events": [...]
            },
            ...
        ]
    }

    NOTE: We use timestamps (not array indices) to identify chapter boundaries.
    This is stable regardless of filtering or pagination in the UI.
    """

    def __init__(self):
        self._chapters_dir = get_chapters_dir()

    def _get_chapter_file(self, npc_id: str) -> str:
        """Get path to NPC's chapter state file."""
        return os.path.join(self._chapters_dir, f"{npc_id}.json")

    def _get_memory_cache_file(self, npc_id: str) -> str:
        """Get path to NPC's cached memory prose file."""
        return os.path.join(self._chapters_dir, f"{npc_id}_memory.txt")

    def _load_chapter_data(self, npc_id: str) -> Dict:
        """Load full chapter data for an NPC."""
        try:
            path = self._get_chapter_file(npc_id)
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    # Migrate old format if needed
                    if 'status' in data:
                        # Old format - single chapter object
                        return self._migrate_old_format(data)
                    return data
        except Exception as e:
            print(f"[Memory] Error loading chapters for {npc_id}: {e}")
        return {"open_chapter": None, "closed_chapters": []}

    def _migrate_old_format(self, old_data: Dict) -> Dict:
        """Migrate old single-chapter format to new format (timestamps only, no indices)."""
        if old_data.get('status') == 'open':
            return {
                "open_chapter": {
                    "title": old_data.get('title', 'Untitled'),
                    "summary": old_data.get('summary', ''),
                    "location": old_data.get('location', 'Unknown'),
                    "start_timestamp": old_data.get('start_timestamp'),
                    "start_date": old_data.get('start_date', ''),
                    "start_time": old_data.get('start_time', ''),
                    "key_events": old_data.get('key_events', []),
                    "context_messages": old_data.get('context_messages', [])
                },
                "closed_chapters": []
            }
        else:
            # It was closed - add to closed_chapters
            return {
                "open_chapter": None,
                "closed_chapters": [{
                    "title": old_data.get('title', 'Untitled'),
                    "summary": old_data.get('summary', ''),
                    "location": old_data.get('location', 'Unknown'),
                    "start_timestamp": old_data.get('start_timestamp'),
                    "end_timestamp": old_data.get('end_timestamp'),
                    "start_date": old_data.get('start_date', ''),
                    "start_time": old_data.get('start_time', ''),
                    "key_events": old_data.get('key_events', [])
                }]
            }

    def _save_chapter_data(self, npc_id: str, data: Dict):
        """Save full chapter data for an NPC."""
        try:
            path = self._get_chapter_file(npc_id)
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"[Memory] Error saving chapters for {npc_id}: {e}")

    def get_open_chapter(self, npc_id: str) -> Optional[Dict]:
        """Get the currently open chapter for an NPC, if any."""
        data = self._load_chapter_data(npc_id)
        return data.get('open_chapter')

    def get_closed_chapters(self, npc_id: str) -> List[Dict]:
        """Get all closed chapters for an NPC."""
        data = self._load_chapter_data(npc_id)
        return data.get('closed_chapters', [])

    def get_all_chapters(self, npc_id: str) -> Dict:
        """Get both open and closed chapters for UI display."""
        return self._load_chapter_data(npc_id)

    def save_chapter_state(self, npc_id: str, chapter_data: Dict):
        """Save chapter state for an NPC (legacy compatibility)."""
        # Convert to new format (timestamps only, no indices)
        if chapter_data.get('status') == 'open':
            data = self._load_chapter_data(npc_id)
            data['open_chapter'] = {
                "title": chapter_data.get('title', 'Untitled'),
                "summary": chapter_data.get('summary', ''),
                "location": chapter_data.get('location', 'Unknown'),
                "start_timestamp": chapter_data.get('start_timestamp'),
                "start_date": chapter_data.get('start_date', ''),
                "start_time": chapter_data.get('start_time', ''),
                "key_events": chapter_data.get('key_events', []),
                "context_messages": chapter_data.get('context_messages', [])
            }
            self._save_chapter_data(npc_id, data)
        else:
            # Closed - this shouldn't happen through legacy path
            self._save_chapter_data(npc_id, self._migrate_old_format(chapter_data))

    def start_chapter(self, npc_id: str, title: str, location: str,
                      game_date: str, game_time: str, summary: str = "",
                      start_timestamp: int = None,
                      key_events: List[Dict] = None, context_messages: List[Dict] = None) -> Dict:
        """Start a new chapter for an NPC."""
        data = self._load_chapter_data(npc_id)

        chapter = {
            "title": title,
            "summary": summary,
            "location": location,
            "start_timestamp": start_timestamp or int(datetime.now().timestamp()),
            "start_date": game_date,
            "start_time": game_time,
            "key_events": key_events or [],
            "context_messages": context_messages or []
        }

        data['open_chapter'] = chapter
        self._save_chapter_data(npc_id, data)
        print(f"[Memory] Started chapter '{title}' for {npc_id} at ts {chapter['start_timestamp']}")
        return chapter

    def close_chapter(self, npc_id: str, summary: str,
                      end_timestamp: int = None, key_events: List[Dict] = None) -> Optional[Dict]:
        """Close the current chapter and add to closed_chapters list."""
        data = self._load_chapter_data(npc_id)
        open_chapter = data.get('open_chapter')

        if not open_chapter:
            return None

        # Build closed chapter record (timestamps only, no indices)
        closed_chapter = {
            "title": open_chapter.get('title', 'Untitled'),
            "summary": summary or open_chapter.get('summary', ''),
            "location": open_chapter.get('location', 'Unknown'),
            "start_timestamp": open_chapter.get('start_timestamp'),
            "end_timestamp": end_timestamp or int(datetime.now().timestamp()),
            "start_date": open_chapter.get('start_date', ''),
            "start_time": open_chapter.get('start_time', ''),
            "key_events": key_events or open_chapter.get('key_events', [])
        }

        # Add to closed chapters list
        closed_chapters = data.get('closed_chapters', [])
        closed_chapters.append(closed_chapter)

        # Clear open chapter
        data['open_chapter'] = None
        data['closed_chapters'] = closed_chapters

        self._save_chapter_data(npc_id, data)

        # Invalidate memory cache
        self.invalidate_memory_cache(npc_id)

        print(f"[Memory] Closed chapter '{closed_chapter['title']}' for {npc_id} "
              f"(ts {closed_chapter['start_timestamp']}-{closed_chapter['end_timestamp']})")
        return closed_chapter

    def add_events_to_chapter(self, npc_id: str, events: List[Dict]) -> bool:
        """Add events to the current open chapter without closing it."""
        data = self._load_chapter_data(npc_id)
        open_chapter = data.get('open_chapter')

        if not open_chapter:
            return False

        existing_events = open_chapter.get('key_events', [])
        existing_events.extend(events)
        open_chapter['key_events'] = existing_events

        data['open_chapter'] = open_chapter
        self._save_chapter_data(npc_id, data)
        print(f"[Memory] Added {len(events)} events to chapter '{open_chapter['title']}' for {npc_id}")
        return True

    def update_chapter_context(self, npc_id: str, new_messages: List[Dict]) -> bool:
        """Update context messages for the current open chapter."""
        data = self._load_chapter_data(npc_id)
        open_chapter = data.get('open_chapter')

        if not open_chapter:
            return False

        context = open_chapter.get('context_messages', [])
        context.extend(new_messages)
        # Keep only last 20 context messages
        open_chapter['context_messages'] = context[-20:]

        data['open_chapter'] = open_chapter
        self._save_chapter_data(npc_id, data)
        return True

    def get_cached_memory(self, npc_id: str) -> Optional[str]:
        """Get cached memory prose for an NPC, if available."""
        try:
            path = self._get_memory_cache_file(npc_id)
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as f:
                    return f.read()
        except Exception as e:
            print(f"[Memory] Error loading cached memory for {npc_id}: {e}")
        return None

    def cache_memory(self, npc_id: str, prose: str):
        """Cache compiled memory prose for an NPC."""
        try:
            path = self._get_memory_cache_file(npc_id)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(prose)
        except Exception as e:
            print(f"[Memory] Error caching memory for {npc_id}: {e}")

    def invalidate_memory_cache(self, npc_id: str):
        """Invalidate cached memory for an NPC."""
        try:
            path = self._get_memory_cache_file(npc_id)
            if os.path.exists(path):
                os.remove(path)
                print(f"[Memory] Invalidated memory cache for {npc_id}")
        except Exception as e:
            print(f"[Memory] Error invalidating cache for {npc_id}: {e}")

    def get_indexing_meta(self, npc_id: str) -> Dict:
        """Get indexing metadata for an NPC (last index timestamp)."""
        data = self._load_chapter_data(npc_id)
        return data.get('indexing_meta', {})

    def update_indexing_meta(self, npc_id: str, timestamp: int):
        """Update indexing metadata after successful chapter detection."""
        data = self._load_chapter_data(npc_id)
        data['indexing_meta'] = {
            'last_index_timestamp': timestamp
        }
        self._save_chapter_data(npc_id, data)
        print(f"[Memory] Updated indexing meta for {npc_id}: last_index_timestamp={timestamp}")

    def clear_npc_data(self, npc_id: str) -> bool:
        """Clear all chapter data, memory cache, bio, and chapter content for an NPC."""
        try:
            # Delete chapter file
            chapter_path = self._get_chapter_file(npc_id)
            if os.path.exists(chapter_path):
                os.remove(chapter_path)
                print(f"[Memory] Deleted chapters for {npc_id}")

            # Delete memory cache
            cache_path = self._get_memory_cache_file(npc_id)
            if os.path.exists(cache_path):
                os.remove(cache_path)
                print(f"[Memory] Deleted memory cache for {npc_id}")

            # Delete bio file
            bio_path = get_bio_path(npc_id)
            if os.path.exists(bio_path):
                os.remove(bio_path)
                print(f"[Memory] Deleted bio for {npc_id}")

            # Delete chapter_content files (named {npc_id}_{timestamp}.json)
            content_dir = os.path.join(DATA_DIR, "chapter_content")
            if os.path.exists(content_dir):
                # Chapter IDs are like "NpcId:timestamp" which become "NpcId_timestamp.json"
                prefix = f"{npc_id}_"
                for filename in os.listdir(content_dir):
                    if filename.startswith(prefix) and filename.endswith('.json'):
                        filepath = os.path.join(content_dir, filename)
                        os.remove(filepath)
                        print(f"[Memory] Deleted chapter content: {filename}")

            return True
        except Exception as e:
            print(f"[Memory] Error clearing NPC data for {npc_id}: {e}")
            return False


# =============================================================================
# NPC Bio System
# =============================================================================

def get_bios_dir() -> str:
    """Get the bios directory path, creating it if needed."""
    bios_dir = os.path.join(DATA_DIR, "npc_bios")
    os.makedirs(bios_dir, exist_ok=True)
    return bios_dir


def get_bio_path(npc_id: str) -> str:
    """Get path to an NPC's bio file."""
    return os.path.join(get_bios_dir(), f"{npc_id}.json")


def load_bio(npc_id: str) -> Optional[Dict]:
    """Load an NPC's bio, returning None if not found."""
    path = get_bio_path(npc_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"[Bio] Error loading bio for {npc_id}: {e}")
        return None


def save_bio(npc_id: str, bio: Dict) -> bool:
    """Save an NPC's bio atomically (write to temp, then rename)."""
    path = get_bio_path(npc_id)
    temp_path = path + ".tmp"
    try:
        bio['last_updated'] = int(datetime.now().timestamp())
        with open(temp_path, 'w', encoding='utf-8') as f:
            json.dump(bio, f, indent=2, ensure_ascii=False)
        os.replace(temp_path, path)  # Atomic rename
        return True
    except Exception as e:
        print(f"[Bio] Error saving bio for {npc_id}: {e}")
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except:
                pass
        return False


def format_bio_for_context(bio: Dict) -> str:
    """Format a bio dict as compact context for NPC prompts."""
    if not bio:
        return ""

    sections = []

    # Personality + Preferences combined
    traits = []
    if bio.get("personality"):
        traits.extend(bio["personality"][:3])  # Max 3
    if bio.get("preferences"):
        traits.extend(bio["preferences"][:3])  # Max 3
    if traits:
        sections.append("### Who You Are\n" + "\n".join(f"- {t}" for t in traits))

    # Ongoing tasks
    if bio.get("ongoing_tasks"):
        tasks = []
        for t in bio["ongoing_tasks"][:3]:  # Max 3
            task_str = f"- {t.get('task', 'Unknown task')}"
            if t.get('context'):
                task_str += f" ({t['context']})"
            tasks.append(task_str)
        sections.append("### Current Tasks\n" + "\n".join(tasks))

    # Recent completed tasks (last 2)
    if bio.get("completed_tasks"):
        recent = bio["completed_tasks"][-2:]  # Last 2
        tasks = [f"- {t.get('task', 'Unknown task')} ({t.get('outcome', 'completed')})" for t in recent]
        sections.append("### Recently Completed\n" + "\n".join(tasks))

    # Key relationships
    if bio.get("relationships"):
        rels = []
        for name, data in list(bio["relationships"].items())[:4]:  # Max 4
            if isinstance(data, dict):
                rels.append(f"- {name}: {data.get('notes', data.get('type', 'known'))}")
            else:
                rels.append(f"- {name}: {data}")
        sections.append("### Key Relationships\n" + "\n".join(rels))

    # Motivations
    if bio.get("motivations"):
        motivs = bio["motivations"][:2]  # Max 2
        sections.append("### What Drives You\n" + "\n".join(f"- {m}" for m in motivs))

    # User notes (from editor's guidance)
    if bio.get("user_notes"):
        notes = bio["user_notes"][:3]  # Max 3
        sections.append("### Additional Notes\n" + "\n".join(f"- {n}" for n in notes))

    return "\n\n".join(sections)


def generate_full_bio(npc_id: str, npc_name: str, editor_guidance: str = None) -> Optional[Dict]:
    """
    Generate a complete bio from all graph data (for migration/rebuild).

    Args:
        npc_id: NPC's internal ID
        npc_name: NPC's display name
        editor_guidance: Optional character essence/guidance from user to incorporate

    Returns the bio dict, or None if generation fails.
    """
    import llm

    graphiti_mgr = GraphitiManager()
    if not graphiti_mgr.init_graphiti():
        print(f"[Bio] Could not initialize Graphiti for {npc_id}")
        return None

    # Get all facts
    graph_data = graphiti_mgr.get_graph_data(npc_id)
    edges = graph_data.get('edges', [])
    if not edges:
        print(f"[Bio] No edges found for {npc_id}")
        return None

    # Format facts by chapter
    formatted = format_graph_for_context(edges, npc_name)

    # Build guidance section if provided
    guidance_section = ""
    if editor_guidance:
        guidance_section = f"""
EDITOR'S GUIDANCE (character essence to preserve):
{editor_guidance}

When generating the bio:
- Incorporate guidance that is STILL CONSISTENT with the facts
- If facts contradict guidance, facts take precedence
- Put guidance elements that don't fit other categories in "user_notes"

"""

    # Call LLM to generate bio
    prompt = f"""Analyze these facts about {npc_name} and create a structured bio.
{guidance_section}
FACTS:
{formatted}

Generate a JSON bio with these exact fields:
- "preferences": List of significant likes/dislikes/opinions (strings)
- "personality": List of observed personality traits (strings)
- "ongoing_tasks": List of {{"task": str, "start_date": str, "context": str}} for incomplete tasks
- "completed_tasks": List of {{"task": str, "start_date": str, "end_date": str, "outcome": str}}
- "relationships": Dict of name -> {{"type": str, "notes": str}} for SIGNIFICANT relationships only
- "motivations": List of observed goals/drives (strings)
- "user_notes": List of additional context from editor's guidance that doesn't fit above categories (strings)

RULES:
1. Only include SIGNIFICANT items - skip one-off neutral interactions
2. Use dates from chapter titles when available (e.g., "Dec 3, 1890")
3. For relationships, require meaningful interaction (helped, argued, traveled together - not just "saw them")
4. Keep each item concise (1-2 sentences max)
5. If unsure about task completion status, mark as ongoing
6. Return ONLY valid JSON, no extra text
7. FACTUAL ACCURACY - CRITICAL:
   - Task names must describe the LITERAL ACTION, not an interpretation of its effect
   - "Retrieved X for Y" is correct. "Helped Y overcome their fear" is WRONG (unless explicitly stated)
   - Do not infer character growth, emotional change, or narrative meaning
   - If the facts say "A did X for B because B was scared", the task is "Did X for B", NOT "Helped B overcome fear"
8. For user_notes: Include guidance items that are still valid but don't fit structured categories

```json
"""

    settings = load_settings()
    model = settings.get('memory', {}).get('prose_model', 'gemini-2.5-flash-lite')

    parsed = call_llm_with_retry(prompt, model, max_retries=3, context="bio_generation")
    if not parsed:
        print(f"[Bio] LLM failed to generate bio for {npc_id}")
        return None

    # Build complete bio structure
    bio = {
        "npc_id": npc_id,
        "npc_name": npc_name,
        "preferences": parsed.get("preferences", []),
        "personality": parsed.get("personality", []),
        "ongoing_tasks": parsed.get("ongoing_tasks", []),
        "completed_tasks": parsed.get("completed_tasks", []),
        "relationships": parsed.get("relationships", {}),
        "motivations": parsed.get("motivations", []),
        "user_notes": parsed.get("user_notes", [])
    }

    # Save and return
    if save_bio(npc_id, bio):
        print(f"[Bio] Generated full bio for {npc_name}")
        return bio
    return None


def update_bio_incremental(npc_id: str, npc_name: str,
                           chapter_start_ts: int, chapter_end_ts: int) -> Optional[Dict]:
    """
    Update an NPC's bio incrementally after a chapter closes.

    Queries Graphiti for edges created during the chapter timeframe,
    then uses LLM to update only affected bio sections.
    Incorporates editor's guidance from settings if available.
    """
    import llm

    # Load existing bio
    bio = load_bio(npc_id)
    if not bio:
        print(f"[Bio] No existing bio for {npc_id}, skipping incremental update")
        return None

    # Get edges from Graphiti
    graphiti_mgr = GraphitiManager()
    if not graphiti_mgr.init_graphiti():
        return bio

    # Query all edges for this NPC
    graph_data = graphiti_mgr.get_graph_data(npc_id)
    edges = graph_data.get('edges', [])

    # Filter to edges from this chapter (by created_at timestamp)
    new_edges = []
    for edge in edges:
        created = edge.get('created_at')
        if created:
            # Parse ISO timestamp to unix
            try:
                from dateutil.parser import parse as parse_date
                edge_ts = int(parse_date(created).timestamp())
                if chapter_start_ts <= edge_ts <= chapter_end_ts + 60:  # +60s buffer
                    new_edges.append(edge)
            except:
                pass

    if not new_edges:
        print(f"[Bio] No new edges for {npc_id} in chapter timeframe ({chapter_start_ts}-{chapter_end_ts})")
        return bio

    print(f"[Bio] Found {len(new_edges)} new edges for {npc_id}")

    # Format new facts
    new_facts = "\n".join(f"- {e.get('fact', '')}" for e in new_edges)

    # Load editor's guidance from settings
    settings = load_settings()
    editor_guidance = settings.get('prompts', {}).get('editor_guidance', {}).get(npc_id, '')

    guidance_section = ""
    if editor_guidance:
        guidance_section = f"""
EDITOR'S GUIDANCE (character essence to preserve):
{editor_guidance}

When updating, ensure user_notes section preserves guidance items that are still valid.

"""

    # Call LLM to update bio
    prompt = f"""Update this NPC bio based on new facts from a recent conversation.
{guidance_section}
CURRENT BIO:
```json
{json.dumps(bio, indent=2)}
```

NEW FACTS:
{new_facts}

For EACH bio section, determine if it needs updating:
- "append": Add new items (new preference, new relationship, etc.)
- "replace": Rewrite section (task completed, opinion changed, contradiction)
- "no_change": Section unaffected by new facts

Return JSON with this structure:
{{
  "preferences": {{"action": "append|replace|no_change", "items": [...]}},
  "personality": {{"action": "append|replace|no_change", "items": [...]}},
  "ongoing_tasks": {{"action": "append|replace|no_change", "items": [...]}},
  "completed_tasks": {{"action": "append|replace|no_change", "items": [...]}},
  "relationships": {{"action": "append|replace|no_change", "items": {{}}}},
  "motivations": {{"action": "append|replace|no_change", "items": [...]}},
  "user_notes": {{"action": "append|replace|no_change", "items": [...]}}
}}

RULES:
1. Only update sections with relevant new information
2. When a task completes, MOVE it from ongoing_tasks to completed_tasks (replace both)
3. Keep items concise (1-2 sentences)
4. For relationships, "items" should be a dict like {{"Name": {{"type": "...", "notes": "..."}}}}
5. Return ONLY valid JSON
6. FACTUAL ACCURACY: Describe only what literally happened. Do not embellish or infer meaning beyond stated facts.
7. Preserve user_notes that are still valid; remove any contradicted by new facts

```json
"""

    model = settings.get('memory', {}).get('prose_model', 'gemini-2.5-flash-lite')

    updates = call_llm_with_retry(prompt, model, max_retries=2, context="bio_update")
    if not updates:
        print(f"[Bio] LLM failed to update bio for {npc_id}")
        return bio

    # Apply updates to bio
    changes_made = False
    for section in ["preferences", "personality", "ongoing_tasks", "completed_tasks", "motivations", "user_notes"]:
        update = updates.get(section, {})
        action = update.get("action", "no_change")
        items = update.get("items", [])

        if action == "append" and items:
            bio[section] = bio.get(section, []) + items
            changes_made = True
        elif action == "replace" and items:
            bio[section] = items
            changes_made = True

    # Handle relationships separately (dict merge)
    rel_update = updates.get("relationships", {})
    if rel_update.get("action") == "append" and rel_update.get("items"):
        bio["relationships"] = {**bio.get("relationships", {}), **rel_update["items"]}
        changes_made = True
    elif rel_update.get("action") == "replace" and rel_update.get("items"):
        bio["relationships"] = rel_update["items"]
        changes_made = True

    # Save updated bio
    if changes_made:
        if save_bio(npc_id, bio):
            print(f"[Bio] Updated bio for {npc_name}")
    else:
        print(f"[Bio] No changes needed for {npc_name}")

    return bio


# =============================================================================
# Chapter Detection
# =============================================================================

def build_chapter_prompt(npc_name: str, npc_id: str, dialogue_entries: List[Dict],
                         open_chapter: Optional[Dict], characters_in_earshot: List[str] = None) -> str:
    """Build the prompt for chapter boundary detection."""

    # Get player name from entries if available
    player_name = "the student"
    for entry in dialogue_entries:
        if entry.get('isPlayer') and entry.get('speaker'):
            player_name = entry['speaker']
            break

    # Build character list
    characters_section = ""
    if characters_in_earshot:
        char_list = "\n".join([f"- {c}" for c in characters_in_earshot])
        characters_section = f"""## Characters {npc_name} knows about:
{char_list}
"""

    # Format new messages with timestamps
    # IMPORTANT: Include Unix timestamp so LLM can reference exact values in its response
    formatted_entries = []
    for i, entry in enumerate(dialogue_entries[-50:]):  # Last 50 entries
        timestamp = entry.get('timestamp') or 0
        game_time = entry.get('gameTime') or ''
        game_date = entry.get('gameDate') or ''
        speaker = entry.get('speaker') or 'Unknown'
        text = entry.get('text') or ''
        entry_type = entry.get('type') or 'dialogue'

        # Show both Unix timestamp AND game time so LLM knows the exact timestamp to use
        game_time_str = f"{game_date} {game_time}".strip() if game_date or game_time else ""
        time_prefix = f"[ts:{timestamp}]" if timestamp else ""
        if game_time_str:
            time_prefix = f"{time_prefix} [{game_time_str}]" if time_prefix else f"[{game_time_str}]"

        # Skip broom events - not relevant for chapter summaries
        if entry_type == 'broom':
            continue
        elif entry_type == 'location':
            formatted_entries.append(f"{time_prefix} Location: {text}")
        elif entry_type == 'spell':
            formatted_entries.append(f"{time_prefix} Spell: {text}")
        elif entry_type == 'combat':
            formatted_entries.append(f"{time_prefix} Combat: {text}")
        else:
            role = "Player" if entry.get('isPlayer') else speaker
            formatted_entries.append(f"{time_prefix} {role}: {text}")

    entries_str = "\n".join(formatted_entries) if formatted_entries else "No recent events."

    # Build open chapter context with context messages
    if open_chapter:
        context_messages = open_chapter.get('context_messages', [])
        context_str = ""
        if context_messages:
            ctx_lines = []
            for msg in context_messages[-10:]:  # Last 10 from open chapter
                ctx_lines.append(f"[{msg.get('time', '')}] {msg.get('speaker', '')}: {msg.get('text', '')}")
            context_str = f"""
### Context Messages from Current Chapter
{chr(10).join(ctx_lines)}
"""

        open_chapter_section = f"""## Current Open Chapter
Title: "{open_chapter.get('title', 'Untitled')}"
Started: {open_chapter.get('start_date', '')} {open_chapter.get('start_time', '')}
Location: {open_chapter.get('location', 'Unknown')}
Summary: {open_chapter.get('summary', 'Chapter in progress...')}
{context_str}"""

        task_section = f"""## Task

Analyze the new messages to determine chapter boundaries.

For the currently open chapter, decide whether to:
1. Close it (if there's a natural break - location change, time skip, plot resolution)
2. Continue it (keep it open)

Then identify any new chapters in the remaining messages.

Each dialogue entry includes a Unix timestamp prefix like `[ts:1768035550]`. Use these exact values for any timestamp fields in your response.

Return your analysis as JSON:

```json
{{
  "current_chapter_action": "close" or "continue",
  "close_at_timestamp": number (use the ts: value from the dialogue entry),
  "additional_events": [
    {{
      "title": "Event Title (max 32 chars)",
      "summary": "{npc_name} and [character] do action, while event occurs, leading to outcome.",
      "characters": ["Character Full Name"]
    }}
  ],
  "new_chapters": [
    {{
      "title": "Chapter title (2-6 words)",
      "start_timestamp": number,
      "end_timestamp": number or null (null if should remain open),
      "status": "closed" or "open",
      "summary": "Brief 1-2 sentence chapter summary from {npc_name}'s perspective",
      "key_events": [
        {{
          "title": "Event Title",
          "summary": "{npc_name} and [character] do action, while event occurs, leading to outcome.",
          "characters": ["Character Full Name"]
        }}
      ]
    }}
  ]
}}
```"""

        example_section = f"""## Example Outputs

### Example 1: Closing current chapter and starting new one
```json
{{
  "current_chapter_action": "close",
  "close_at_timestamp": 1768035500,
  "additional_events": [
    {{
      "title": "Lock Blocks Rescue",
      "summary": "{npc_name} and {player_name} discover the portrait is behind a level 2 lock, while Ashwinders patrol nearby, leading to abandoning the rescue attempt.",
      "characters": ["{player_name}", "Ferdinand Pratt"]
    }}
  ],
  "new_chapters": [
    {{
      "title": "Return to the Broomsticks",
      "start_timestamp": 1768035550,
      "end_timestamp": null,
      "status": "open",
      "summary": "After the failed rescue, {npc_name} returns to the Three Broomsticks with {player_name}.",
      "key_events": []
    }}
  ]
}}
```

### Example 2: Continuing current chapter
```json
{{
  "current_chapter_action": "continue",
  "additional_events": [
    {{
      "title": "Ashwinders Defeated",
      "summary": "{npc_name} watches {player_name} defeat the Ashwinder guards, while dark magic crackles through the ruins, leading to access to the inner chamber.",
      "characters": ["{player_name}"]
    }}
  ],
  "new_chapters": []
}}
```"""

    else:
        open_chapter_section = "## No Open Chapter\nThis analysis will start a new chapter."

        task_section = f"""## Task

Identify chapter boundaries in the messages and create chapters.

Each dialogue entry includes a Unix timestamp prefix like `[ts:1768035550]`. Use these exact values for any timestamp fields in your response.

Return your analysis as JSON:

```json
{{
  "new_chapters": [
    {{
      "title": "Chapter title (2-6 words)",
      "start_timestamp": number (use the ts: value from the first dialogue entry in this chapter),
      "end_timestamp": number or null (use the ts: value from the last entry, or null if chapter should remain open),
      "status": "closed" or "open",
      "summary": "Brief 1-2 sentence chapter summary from {npc_name}'s perspective",
      "key_events": [
        {{
          "title": "Event Title (max 32 chars)",
          "summary": "{npc_name} and [character] do action, while event occurs, leading to outcome.",
          "characters": ["Character Full Name"]
        }}
      ]
    }}
  ]
}}
```"""

        example_section = f"""## Example Output

```json
{{
  "new_chapters": [
    {{
      "title": "The Rescue Mission",
      "start_timestamp": 1768030000,
      "end_timestamp": 1768035000,
      "status": "closed",
      "summary": "{npc_name} accompanies {player_name} to Marunweem ruins to rescue Ferdinand Pratt's portrait from Ashwinders.",
      "key_events": [
        {{
          "title": "Ashwinder Ambush",
          "summary": "{npc_name} and {player_name} fight through Ashwinder guards, while dark wizards attempt to stop them, leading to reaching the inner ruins.",
          "characters": ["{player_name}"]
        }}
      ]
    }},
    {{
      "title": "Empty-Handed Return",
      "start_timestamp": 1768035100,
      "end_timestamp": null,
      "status": "open",
      "summary": "Unable to open the locked door, {npc_name} returns to the Three Broomsticks with {player_name}.",
      "key_events": []
    }}
  ]
}}
```"""

    guidelines = f"""## Guidelines

1. **Chapter Length**: Chapters typically span 20-50 dialogue exchanges, but follow narrative flow over strict counts

2. **Natural Breaks**: Look for:
   - Scene transitions (location changes, time skips)
   - Major plot developments or revelations
   - Shifts in conversation focus or tone
   - Resolution of conflicts or story arcs
   - Introduction of new characters or elements

3. **Open Chapters**: Leave the last chapter open if the story is ongoing

4. **Title Quality**:
   - Engaging and hint at content without spoilers
   - 2-6 words typically
   - Capture the essence or mood of what happened

5. **Key Events**: 0-3 plot-critical moments that directly and significantly alter the story state

   **Only save events that represent**:
   - **Story Initiation**: The core event starting a major plot thread or quest
   - **Critical Revelations**: Discovery of information ESSENTIAL to understanding the story (e.g., true identity, hidden purpose, crucial weakness)
   - **Major State Changes**: Actions that fundamentally alter the situation (e.g., alliance formed, enemy defeated, location discovered)
   - **Decisive Turning Points**: Moments where the story trajectory significantly shifts
   - **Resolution Events**: Definitive conclusions to story arcs or major obstacles

   **Do NOT save**:
   - Routine conversations or minor disagreements
   - Small character interactions or basic dialogue
   - Incremental progress or minor discoveries
   - Emotional reactions without plot consequences
   - Procedural actions (walking, looking, basic questioning)
   - Generic greetings or farewells

   - Each event title: max 32 characters
   - Summary format: "{npc_name} and [character] do action, while event occurs, leading to outcome."
   - Keep summaries factual and action-focused (max 264 characters)
   - Always use specific character names when known
   - Only include named characters in the characters array

6. **Summary**: 1-2 sentences capturing the main thread from {npc_name}'s perspective"""

    return f"""You are analyzing {npc_name}'s experiences to identify natural chapter breaks and create meaningful titles.

Important: This is from {npc_name}'s perspective. When writing summaries and describing events, write from their point of view using their name.

The player character is named "{player_name}".

{characters_section}
{open_chapter_section}

## New Events to Analyze
{entries_str}

{task_section}

{guidelines}

{example_section}

Return only the JSON response with no additional explanation."""


# =============================================================================
# Episode Content Generation
# =============================================================================

def build_episode_prompt(npc_name: str, chapter_title: str, chapter_summary: str,
                         key_events: List[Dict], location: str,
                         dialogue_entries: List[Dict], player_name: str = "the student") -> str:
    """
    Build prompt for generating episode content optimized for graph storage/retrieval.

    This generates rich prose that will be stored in Kuzu via Graphiti.
    """

    # Format dialogue entries
    dialogue_lines = []
    for entry in dialogue_entries[-30:]:  # Last 30 entries from this chapter
        speaker = entry.get('speaker', 'Unknown')
        text = entry.get('text', '')
        entry_type = entry.get('type', 'dialogue')

        if entry_type == 'location':
            location = entry.get('location', text.replace('Entered ', ''))
            companions = entry.get('companions')
            if companions:
                dialogue_lines.append(f"[{speaker} and {', '.join(companions)} entered {location}]")
            else:
                dialogue_lines.append(f"[{speaker} entered {location}]")
        elif entry_type in ('spell', 'broom'):
            continue  # Skip spell casts and broom events - not meaningful for long-term memory
        elif entry_type == 'combat':
            dialogue_lines.append(f"[Combat: {text}]")
        else:
            dialogue_lines.append(f"{speaker}: {text}")

    dialogue_str = "\n".join(dialogue_lines) if dialogue_lines else "No dialogue recorded."

    # Format key events
    events_str = ""
    if key_events:
        event_lines = [f"- {e.get('title', '')}: {e.get('summary', '')}" for e in key_events]
        events_str = "\n".join(event_lines)

    return f"""You are generating a memory episode for {npc_name} to be stored in a knowledge graph.

## Chapter Information
Title: {chapter_title}
Location: {location}
Summary: {chapter_summary}

## Key Events
{events_str if events_str else "None identified"}

## Raw Dialogue
{dialogue_str}

## Task

Generate a rich prose narrative of this chapter from {npc_name}'s perspective. This will be stored in a graph database for later retrieval. Each fact is stored independently, so a search for a name won't match a pronoun reference.

Optimize for:

1. **EXPLICIT NAMES**: Always use full character names, NEVER pronouns like "he", "she", "they", "him", "her", "them". Every single reference to a person must use their name.
   - BAD: "He told her about the mission"
   - GOOD: "{player_name} told {npc_name} about the mission"

2. **MAXIMIZE RELATIONSHIP CLARITY**: State relationships and connections explicitly. Don't assume context.
   - BAD: "They went to the ruins together"
   - GOOD: "{npc_name} traveled with {player_name} to the ruins near Marunweem Lake"
   - BAD: "The shopkeeper helped"
   - GOOD: "Sirona Ryan, the innkeeper at the Three Broomsticks, helped {npc_name} and {player_name}"

3. **EXTRAPOLATE TEMPORAL RELATIONSHIPS**: State when things happened and derive implicit temporal facts.
   - BAD: "Later, they found the portrait"
   - GOOD: "After defeating the Ashwinders, {npc_name} and {player_name} discovered Ferdinand Pratt's portrait"
   - EXTRAPOLATE: If someone was rescued, state they were previously captured. If a lock blocked progress, state the mission remains incomplete. If this is a second meeting, reference the first.

4. **FACTUAL DENSITY**: Pack in key facts useful for retrieval - full names, specific locations, item names, spell names, quest outcomes, stated emotions, promises made.

5. **THIRD PERSON**: Write about {npc_name} in third person (e.g., "{npc_name} traveled with {player_name}..."). This allows proper entity deduplication in the graph.

6. **EVENTS, NOT STATES**: Write about what HAPPENED (events), not what IS (states). State facts become outdated and clutter the graph.
   - BAD: "Duncan is on a quest for bravery" (state - becomes false when quest ends)
   - BAD: "Duncan is anxious about the leaf" (state - temporary)
   - BAD: "Duncan was promised a leaf" (passive state)
   - GOOD: "Duncan asked {npc_name} to retrieve a leaf" (event - always true)
   - GOOD: "Duncan expressed fear of the Venomous Tentacula" (event - always true)
   - GOOD: "{npc_name} promised Duncan to retrieve the leaf" (event - always true)
   - GOOD: "{npc_name} delivered the leaf to Duncan, completing Duncan's quest" (event with conclusion)

## Output Format

Write 2-4 paragraphs of flowing prose. No headers, no bullet points, just narrative text optimized for graph retrieval.

Begin your response with the prose directly, no preamble."""


def generate_episode_content(npc_name: str, chapter_title: str, chapter_summary: str,
                             key_events: List[Dict], location: str,
                             dialogue_entries: List[Dict], player_name: str = "the student",
                             max_retries: int = 3) -> str:
    """
    Generate episode content using LLM for storage in Graphiti.

    Returns rich prose optimized for graph retrieval with explicit names and relationships.
    """
    import llm

    settings = load_settings()
    memory_settings = settings.get('memory', {})
    model = memory_settings.get('prose_model', memory_settings.get('chapter_model', 'gemini-2.5-flash-lite'))

    prompt = build_episode_prompt(
        npc_name=npc_name,
        chapter_title=chapter_title,
        chapter_summary=chapter_summary,
        key_events=key_events,
        location=location,
        dialogue_entries=dialogue_entries,
        player_name=player_name
    )

    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            result = llm.chat(
                messages=[{"role": "user", "content": prompt}],
                model=model,
                temperature=0.7,
                max_tokens=4096,
                context="episode_generation"
            )

            if result and isinstance(result, str) and len(result.strip()) > 50:
                print(f"[Memory] Generated episode content: {len(result)} chars")
                return result.strip()
            else:
                print(f"[Memory] Attempt {attempt}/{max_retries}: insufficient content, retrying...")
        except Exception as e:
            last_error = e
            print(f"[Memory] Attempt {attempt}/{max_retries} failed: {e}")

        if attempt < max_retries:
            time.sleep(1)

    print(f"[Memory] Episode generation failed after {max_retries} attempts: {last_error}")

    # Fallback to simple format if LLM fails
    events_str = ""
    if key_events:
        event_lines = [f"- {e.get('title', '')}: {e.get('summary', '')}" for e in key_events]
        events_str = f"\n\nKey Events:\n{chr(10).join(event_lines)}"

    return f"""Chapter: {chapter_title}
Location: {location}
Summary: {chapter_summary}
{events_str}"""


def detect_chapter_boundary(npc_id: str, npc_name: str, dialogue_entries: List[Dict],
                            current_location: str, characters_in_earshot: List[str] = None) -> Dict[str, Any]:
    """
    Detect if a chapter boundary should occur for an NPC.

    Returns:
        Dict with structure matching the prompt output:
        - current_chapter_action: "close" or "continue" (if open chapter exists)
        - close_at_timestamp: number (if closing)
        - additional_events: list of events to add to current chapter
        - new_chapters: list of new chapters to create
    """
    import llm

    settings = load_settings()
    memory_settings = settings.get('memory', {})

    if not memory_settings.get('enabled', True):
        return {"current_chapter_action": "continue", "new_chapters": []}

    chapter_mgr = ChapterManager()
    open_chapter = chapter_mgr.get_open_chapter(npc_id)

    # Check if we need to initialize indexing meta for old data
    # This MUST happen before any other checks to prevent treating all entries as "new"
    indexing_meta = chapter_mgr.get_indexing_meta(npc_id)
    last_index_ts = indexing_meta.get('last_index_timestamp', 0)

    if last_index_ts == 0 and dialogue_entries:
        # No last_index_timestamp = old data without tracking
        # Initialize to current time and skip - only genuinely new entries after this will trigger
        import time
        current_ts = int(time.time())
        chapter_mgr.update_indexing_meta(npc_id, current_ts)
        print(f"[Memory] Initialized indexing meta for {npc_id} (existing data, {len(dialogue_entries)} entries)")
        return {"current_chapter_action": "continue", "new_chapters": [], "_skipped": True}

    # Check if we should skip chapter detection
    # Skip if: same location AND entries since last index below threshold
    location_changed = not open_chapter or open_chapter.get('location') != current_location

    if not location_changed:
        # Same location - check entry threshold
        entries_since_last = sum(1 for e in dialogue_entries if e.get('timestamp', 0) > last_index_ts)
        threshold = memory_settings.get('chapter_entry_threshold', 30)

        if entries_since_last < threshold:
            # Same location and under threshold - skip expensive LLM call
            # Mark as skipped so we don't update indexing meta
            return {"current_chapter_action": "continue", "new_chapters": [], "_skipped": True}
        else:
            print(f"[Memory] Entry threshold reached for {npc_id}: {entries_since_last} >= {threshold}")

    # Build and send prompt
    prompt = build_chapter_prompt(npc_name, npc_id, dialogue_entries, open_chapter, characters_in_earshot)
    model = memory_settings.get('chapter_model', 'gemini-2.5-flash-lite')

    # Use retry helper
    parsed = call_llm_with_retry(prompt, model, max_retries=2, context="chapter_detection")

    if not parsed:
        # LLM call failed - don't update indexing meta and don't checkpoint
        # so entries are retried on next processing cycle
        return {"current_chapter_action": "continue", "new_chapters": [], "_failed": True}

    print(f"[Memory] Chapter detection result: action={parsed.get('current_chapter_action', 'N/A')}, "
          f"new_chapters={len(parsed.get('new_chapters', []))}")
    return parsed


# =============================================================================
# Memory Compilation
# =============================================================================

def get_npc_community_summaries(npc_id: str, npc_name: str, max_summaries: int = 5) -> List[str]:
    """
    DEPRECATED: Not used in production. Bio system replaced community summaries.

    Get the most relevant community summaries for an NPC, ordered chronologically.

    Communities are clusters of related facts that provide high-level context
    about the NPC's experiences and relationships.

    Args:
        npc_id: NPC's internal ID (group_id in Graphiti)
        npc_name: NPC's display name to filter relevant communities
        max_summaries: Maximum number of summaries to return

    Returns:
        List of community summary strings, ordered chronologically (oldest first)
    """
    graphiti_mgr = GraphitiManager()
    if not graphiti_mgr.init_graphiti():
        return []

    try:
        from graphiti_core.nodes import CommunityNode

        async def get_communities_with_timestamps():
            driver = graphiti_mgr._graphiti.driver
            # Query communities with the earliest episode timestamp of their member edges
            # This gives us temporal ordering based on when events actually happened
            records, _, _ = await driver.execute_query(
                """
                MATCH (c:Community)-[:HAS_MEMBER]->(e:Entity)
                WHERE c.group_id = $group_id
                OPTIONAL MATCH (e)-[:RELATES_TO]-(edge)-[:MENTIONS]-(ep:Episodic)
                WITH c, min(ep.valid_at) AS earliest_event
                RETURN DISTINCT
                    c.uuid AS uuid,
                    c.name AS name,
                    c.summary AS summary,
                    earliest_event
                ORDER BY COALESCE(earliest_event, CAST('9999-12-31' AS TIMESTAMP)) ASC
                """,
                group_id=npc_id
            )
            return records

        records = graphiti_mgr._run_async(get_communities_with_timestamps(), skip_if_busy=True)
        if not records:
            return []

        # Filter to communities where NPC is actually involved
        relevant_communities = []
        npc_name_lower = npc_name.lower() if npc_name else ""
        seen_summaries = set()  # For deduplication

        for record in records:
            summary = record.get('summary', '')
            if not summary:
                continue

            # Skip duplicates (sometimes same summary appears in multiple communities)
            summary_key = summary[:100].lower()  # Use first 100 chars as key
            if summary_key in seen_summaries:
                continue

            summary_lower = summary.lower()

            # Only include if NPC name appears in summary
            if npc_name_lower and npc_name_lower not in summary_lower:
                continue

            seen_summaries.add(summary_key)
            relevant_communities.append(summary)

            if len(relevant_communities) >= max_summaries:
                break

        return relevant_communities

    except Exception as e:
        print(f"[Memory] Error getting community summaries: {e}")
        return []


def get_contextual_memory(npc_id: str, npc_name: str = None, player_name: str = "Player",
                          current_location: str = None,
                          nearby_npcs: List[str] = None,
                          mentioned_entities: List[str] = None) -> Optional[str]:
    """
    Get compact, contextual memory for an NPC's prompt.

    Always includes:
    - NPC's own node summary (self-knowledge)
    - Player node summary (what NPC knows about the player)
    - Current location summary (what NPC knows about where they are)

    Optionally includes:
    - Nearby NPC summaries (who's in the scene)
    - Mentioned entity summaries (what came up in conversation)

    Args:
        npc_id: NPC's internal ID
        npc_name: NPC's display name (e.g., "Nellie Oggspire")
        player_name: Player's character name
        current_location: Current location name (e.g., "Three Broomsticks")
        nearby_npcs: List of NPC names currently in earshot
        mentioned_entities: List of entity names mentioned recently

    Returns:
        Compact context string (~50-200 tokens) or None
    """
    settings = load_settings()
    if not settings.get('memory', {}).get('enabled', True):
        return None

    graphiti_mgr = GraphitiManager()
    if not graphiti_mgr.init_graphiti():
        return None

    # Skip if Graphiti is busy adding episodes
    if graphiti_mgr._busy:
        print("[Memory] Contextual memory skipped - Graphiti busy")
        return None

    try:
        from graphiti_core.nodes import EntityNode

        async def get_nodes():
            driver = graphiti_mgr._graphiti.driver
            return await EntityNode.get_by_group_ids(driver, [npc_id])

        nodes = graphiti_mgr._run_async(get_nodes(), skip_if_busy=True)
        if not nodes:
            return None

        # Build name -> summary lookup
        node_lookup = {}
        for node in nodes:
            summary = getattr(node, 'summary', '')
            if summary:
                node_lookup[node.name.lower()] = {
                    'name': node.name,
                    'summary': summary
                }

        sections = []

        # 1. Use structured bio (bio-only approach)
        bio = load_bio(npc_id)
        if bio:
            bio_context = format_bio_for_context(bio)
            if bio_context:
                sections.append(bio_context)
        else:
            # Fallback to old entity summary if no bio exists
            npc_node = None
            if npc_name:
                npc_lower = npc_name.lower()
                npc_node = node_lookup.get(npc_lower)
                if not npc_node:
                    # Try partial match for NPC name
                    for name, data in node_lookup.items():
                        if npc_lower in name or name in npc_lower:
                            npc_node = data
                            break
            # Fallback to "You" for backwards compatibility with old data
            if not npc_node and 'you' in node_lookup:
                npc_node = node_lookup['you']

            if npc_node:
                sections.append(f"### Your Memories\n{npc_node['summary']}")

        # 2. Always include Player (dynamic learned facts + static bio)
        player_lower = player_name.lower()

        # Get Player bio (static facts known to all NPCs)
        player_bio = settings.get('prompts', {}).get('editor_guidance', {}).get('Player', '')

        # Build Player section with both dynamic and static context
        player_parts = []

        # Try to get dynamic Graphiti summary first
        player_node = node_lookup.get(player_lower)
        if not player_node:
            # Try to find player by partial match
            for name, data in node_lookup.items():
                if player_lower in name or name in player_lower:
                    player_node = data
                    break

        if player_node:
            player_parts.append(player_node['summary'])

        # Always add static bio if it exists (additive with Graphiti summary)
        if player_bio:
            player_parts.append(f"**Background:** {player_bio}")

        if player_parts:
            sections.append(f"### About {player_name}\n" + "\n\n".join(player_parts))

        # 3. Add current location (if provided and known)
        if current_location:
            location_lower = current_location.lower()
            location_node = node_lookup.get(location_lower)
            if not location_node:
                # Try partial match for locations
                for name, data in node_lookup.items():
                    if location_lower in name or name in location_lower:
                        location_node = data
                        break
            if location_node:
                sections.append(f"### About {location_node['name']}\n{location_node['summary']}")

        # 4. Add nearby NPCs (if provided)
        already_added = {s.lower() for s in ['you', player_lower, current_location.lower() if current_location else '']}
        if nearby_npcs:
            for npc_name in nearby_npcs[:3]:  # Limit to 3 nearby NPCs
                npc_lower = npc_name.lower()
                if npc_lower in node_lookup and npc_lower not in already_added:
                    data = node_lookup[npc_lower]
                    sections.append(f"### About {data['name']}\n{data['summary']}")
                    already_added.add(npc_lower)

        # 5. Add mentioned entities (if provided)
        if mentioned_entities:
            for entity_name in mentioned_entities[:2]:  # Limit to 2 mentioned
                entity_lower = entity_name.lower()
                if entity_lower in node_lookup and entity_lower not in already_added:
                    data = node_lookup[entity_lower]
                    sections.append(f"### About {data['name']}\n{data['summary']}")
                    already_added.add(entity_lower)

        # If no memory sections, fall back to editor_guidance as last resort
        if not sections:
            # Try to get editor_guidance from settings as a fallback
            editor_guidance = settings.get('prompts', {}).get('editor_guidance', {}).get(npc_id, '')
            if editor_guidance:
                print(f"[Memory] No bio/graph data for {npc_id}, using editor_guidance as fallback")
                return f"### About You\n{editor_guidance}"
            return None

        return "\n\n".join(sections)

    except Exception as e:
        print(f"[Memory] Error getting contextual memory: {e}")
        return None


def format_graph_for_context(edges: List[Dict], npc_name: str = "You") -> str:
    """
    Format graph edges into organized, deterministic context for NPC prompts.

    Groups facts by chapter (temporal), then by type within each chapter:
    1. Your experiences & feelings (edges where source = "You")
    2. What you observed others do (edges between other characters)
    3. World knowledge (locations, objects, general facts)

    Args:
        edges: List of edge dicts with 'source', 'target', 'fact', 'name', 'chapters'
        npc_name: The NPC's name (for display)

    Returns:
        Formatted string for prompt context
    """
    if not edges:
        return ""

    from collections import defaultdict

    # Group edges by chapter
    chapter_facts = defaultdict(lambda: {"yours": [], "witnessed": [], "world": []})
    no_chapter = {"yours": [], "witnessed": [], "world": []}

    # Known location/object indicators for categorization
    location_words = {'ruins', 'lake', 'broomsticks', 'castle', 'village', 'shop', 'room', 'area', 'dungeon', 'forest', 'cave', 'hogwarts', 'hogsmeade'}
    object_words = {'portrait', 'door', 'key', 'wand', 'potion', 'book', 'letter', 'butterbeer'}

    for edge in edges:
        source = edge.get('source', 'Unknown')
        target = edge.get('target', 'Unknown')
        fact = edge.get('fact', '')
        chapters = edge.get('chapters', [])

        source_lower = source.lower()
        target_lower = target.lower()

        # Determine category
        if source == "You":
            category = "yours"
        elif any(word in source_lower for word in location_words | object_words) or \
             any(word in target_lower for word in location_words | object_words):
            if source != "You" and target != "You":
                category = "world"
            else:
                category = "yours"
        else:
            category = "witnessed"

        # Add to chapter groups (may appear in multiple chapters)
        if chapters:
            for chapter in chapters:
                chapter_facts[chapter][category].append(fact)
        else:
            no_chapter[category].append(fact)

    # Build formatted output - chapters in order (oldest first based on edge order)
    sections = []

    # Get chapter order from edges (first occurrence = oldest)
    chapter_order = []
    seen_chapters = set()
    for edge in edges:
        for ch in edge.get('chapters', []):
            if ch not in seen_chapters:
                chapter_order.append(ch)
                seen_chapters.add(ch)

    # Output each chapter
    for chapter in chapter_order:
        facts = chapter_facts[chapter]
        if not any(facts.values()):
            continue

        sections.append(f"\n**{chapter}:**")

        if facts["yours"]:
            for fact in facts["yours"]:
                sections.append(f"- {fact}")

        if facts["witnessed"]:
            for fact in facts["witnessed"]:
                sections.append(f"- {fact}")

        if facts["world"]:
            for fact in facts["world"]:
                sections.append(f"- {fact}")

    # Any facts without chapter association
    if any(no_chapter.values()):
        sections.append("\n**General Knowledge:**")
        for fact in no_chapter["yours"] + no_chapter["witnessed"] + no_chapter["world"]:
            sections.append(f"- {fact}")

    result = "\n".join(sections)
    return result.strip()


def compile_memory_prose(npc_id: str, npc_name: str) -> Optional[str]:
    """
    Compile an NPC's graph data into natural prose for their prompt.

    Returns:
        Prose string or None if no memories/error
    """
    import llm

    settings = load_settings()
    memory_settings = settings.get('memory', {})

    if not memory_settings.get('enabled', True):
        return None

    # Check cache first
    chapter_mgr = ChapterManager()
    cached = chapter_mgr.get_cached_memory(npc_id)
    if cached:
        return cached

    # Get graph data
    graphiti_mgr = GraphitiManager()
    graph_data = graphiti_mgr.get_graph_data(npc_id)

    if not graph_data.get('edges'):
        return None  # No memories yet

    # Format facts deterministically into organized sections
    formatted_facts = format_graph_for_context(graph_data['edges'][:30], npc_name)

    prompt = f"""You are compiling {npc_name}'s memories into a natural prose summary for their character context.

## Facts from Memory
{formatted_facts}

## Task
Convert these facts into a flowing, natural prose summary written in second person from {npc_name}'s perspective.

**Structure your response as follows:**
1. Start with the most important ongoing relationships (who they've spent time with, how they feel about them)
2. Then cover significant recent events or adventures
3. End with any unresolved situations or things still in progress

**Guidelines:**
- Write in second person: "You remember...", "You've been...", "You and [character]..."
- Keep the tone natural, as if {npc_name} is recalling their experiences
- Focus on plot-relevant information, not mundane details
- Maximum 200 words
- Do not include information not present in the facts
- If facts mention emotions or opinions, include those

**Example output style:**
"You've grown quite fond of the new fifth-year student who arrived under mysterious circumstances. Together you ventured to the ruins near Marunweem Lake, where you witnessed their impressive dueling skills against the Ashwinders. The rescue mission was cut short by a lock neither of you could open - Ferdinand Pratt's portrait remains stuck there, much to your private amusement. Back at the Three Broomsticks, you shared a laugh with Astoria about Ferdinand's predicament."

Write your summary now:"""

    model = memory_settings.get('prose_model', 'gemini-2.5-flash-lite')

    try:
        result = llm.chat(
            messages=[{"role": "user", "content": prompt}],
            model=model,
            temperature=0.7,
            max_tokens=4096,
            context="memory_prose"
        )

        if result:
            prose = f"## What You Remember\n\n{result.strip()}"
            # Cache the result
            chapter_mgr.cache_memory(npc_id, prose)
            return prose

    except Exception as e:
        print(f"[Memory] Prose compilation error: {e}")

    return None


# =============================================================================
# Public API for server.py
# =============================================================================

def get_npc_memory(npc_id: str, npc_name: str = None) -> Optional[str]:
    """
    Get the compiled memory block for an NPC to inject into their prompt.

    Args:
        npc_id: NPC's internal ID (e.g., "NellieOggspire")
        npc_name: NPC's display name for prose generation (optional)

    Returns:
        Memory prose string or None if no memories
    """
    settings = load_settings()
    if not settings.get('memory', {}).get('enabled', True):
        return None

    if not _graphiti_available:
        return None

    # Use npc_id as name if not provided
    if not npc_name:
        from .localization import get_display_name
        npc_name = get_display_name(npc_id)

    return compile_memory_prose(npc_id, npc_name)


def extract_search_query(player_message: str, npc_name: str, player_name: str = "the player") -> Optional[str]:
    """
    Extract a clean search query from player's conversational message.

    Uses a fast LLM to identify what the player is actually asking about,
    stripping conversational fluff and extracting key entities/topics.
    Resolves pronouns like "we", "us", "I" to actual entity names.

    Returns:
        Clean search query string, or None if no search needed (greetings, etc.)
    """
    import llm

    # Skip very short messages
    if len(player_message.strip()) < 5:
        return None

    prompt = f"""Extract a search query from this player message.

Player: {player_name}
Speaking to: {npc_name}
Message: "{player_message}"

Extract key entities/topics for searching the NPC's knowledge graph (2-6 words).
- EXCLUDE the NPC's name ({npc_name}) - we're already searching their graph
- Replace "I/me/my/we/us/our" with player name ({player_name}) if relevant
- Extract other names, places, events, objects mentioned
- If greeting/thanks/small talk, return NONE

Return ONLY the search terms or NONE. No explanation.

Examples:
- "do you remember what happened with Thomas?" → Thomas
- "what about that cave we explored?" → cave {player_name}
- "tell me about Professor Williams" → Professor Williams
- "{npc_name}, what do you think about the headmaster?" → headmaster
- "do you remember when {npc_name} helped with the potion?" → potion
- "what do you think of me?" → {player_name}
- "remember when we fought those bandits?" → bandits {player_name}
- "have you seen my wand?" → wand {player_name}
- "what about our adventure in the forest?" → adventure forest {player_name}
- "I met someone strange at Hogsmeade" → Hogsmeade {player_name}
- "do you know my friend Sebastian?" → Sebastian {player_name}
- "what did Poppy say about the beasts?" → Poppy beasts
- "have you spoken to Professor Fig lately?" → Professor Fig
- "did Sebastian mention the scriptorium?" → Sebastian scriptorium
- "what do you think of Ominis?" → Ominis
- "what happened at the tavern?" → tavern
- "hey how are you?" → NONE
- "thanks for your help" → NONE
- "that's interesting" → NONE
- "let's go" → NONE
- "tell me about yourself" → NONE"""

    settings = load_settings()
    # Use reranker model (small/fast) for intent extraction
    model = settings.get('memory', {}).get('reranker_model', 'meta-llama/llama-3.1-8b-instruct')

    try:
        _profiler.mark("search_intent start")
        result = llm.chat(
            messages=[{"role": "user", "content": prompt}],
            model=model,
            temperature=0,
            max_tokens=4096,
            context="search_intent"
        )
        _profiler.mark("search_intent done")

        if result:
            result = result.strip()
            # Normalize: remove quotes, asterisks, and other LLM formatting artifacts
            result = result.strip('"\'`*_')
            # Remove any remaining paired quotes inside
            if result.startswith('"') and result.endswith('"'):
                result = result[1:-1]
            if result.startswith("'") and result.endswith("'"):
                result = result[1:-1]
            result = result.strip()

            if result.upper() == "NONE" or len(result) < 3:
                return None
            print(f"[Memory] Search query extracted: '{result}' from '{player_message[:50]}...'")
            return result
    except Exception as e:
        print(f"[Memory] Search intent extraction failed: {e}")

    # Fallback: return original message
    return player_message


def search_relevant_facts(npc_id: str, query: str, npc_name: str = None,
                          player_name: str = None, center_node_name: str = None,
                          max_results: int = 50) -> Optional[List[str]]:
    """
    Search for facts relevant to a query, centered on a specific node.

    Uses a small LLM to extract search intent from conversational messages,
    then Graphiti's hybrid search with node distance reranking to find facts
    that are both semantically relevant AND close to the center node.

    Args:
        npc_id: NPC's group_id in the graph
        query: The player's message (will be processed to extract search query)
        npc_name: NPC's display name (for query extraction context)
        player_name: Player's character name (for resolving "we", "I", etc.)
        center_node_name: Node to center search on (default: NPC's name or "You")
        max_results: Maximum unique facts to return (after deduplication)

    Returns:
        List of unique fact strings, or None if search fails/no graph data
    """
    settings = load_settings()
    if not settings.get('memory', {}).get('enabled', True):
        return None

    if not _graphiti_available:
        return None

    # Extract clean search query from conversational message
    search_query = extract_search_query(query, npc_name or "the NPC", player_name or "the player")
    if not search_query:
        print(f"[Memory] No search needed for: '{query[:50]}...'")
        return None

    graphiti_mgr = GraphitiManager()
    if not graphiti_mgr.init_graphiti():
        return None

    # Default center node to NPC's name (for third-person facts) or "You" (for old data)
    if not center_node_name:
        center_node_name = npc_name if npc_name else "You"

    # Get center node UUID (optional - search works without it, just less targeted)
    center_uuid = graphiti_mgr.get_node_uuid(npc_id, center_node_name)

    # Search for more than we need to account for filtering and duplicates
    _profiler.mark("graph_lookup start")
    facts = graphiti_mgr.search_facts(
        npc_id=npc_id,
        query=search_query,
        center_node_uuid=center_uuid,
        max_results=max_results * 4  # Fetch extra to account for keyword filtering
    )
    _profiler.mark("graph_lookup done")

    if not facts:
        return None

    # Normalize text for matching: strip possessives, contractions, lowercase
    import re
    def normalize_for_match(text: str) -> str:
        text = text.lower()
        # Strip possessives and contractions: 's 't 'd 'll 've 're 'm
        text = re.sub(r"'(s|t|d|ll|ve|re|m)\b", "", text)
        text = re.sub(r"'(s|t|d|ll|ve|re|m)\b", "", text)  # Handle curly apostrophe
        return text

    # Extract keywords from query for filtering (skip common words)
    stopwords = {'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for',
                 'of', 'with', 'by', 'from', 'is', 'was', 'are', 'were', 'been', 'be',
                 'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
                 'should', 'may', 'might', 'must', 'shall', 'can', 'need', 'dare',
                 'that', 'this', 'these', 'those', 'it', 'its', 'they', 'them', 'their',
                 'we', 'us', 'our', 'you', 'your', 'i', 'me', 'my', 'he', 'him', 'his',
                 'she', 'her', 'what', 'which', 'who', 'whom', 'when', 'where', 'why',
                 'how', 'about', 'into', 'through', 'during', 'before', 'after', 'above',
                 'below', 'between', 'under', 'again', 'further', 'then', 'once', 'here',
                 'there', 'all', 'each', 'few', 'more', 'most', 'other', 'some', 'such',
                 'no', 'not', 'only', 'own', 'same', 'so', 'than', 'too', 'very', 'just',
                 'also', 'now', 'any', 'both', 'being', 'over', 'after', 'ever'}

    # Normalize and extract keywords
    normalized_query = normalize_for_match(search_query)
    query_words = [w for w in normalized_query.split() if w not in stopwords and len(w) > 2]

    # Separate facts into keyword-matched and semantic-only
    keyword_matched = []
    semantic_only = []
    seen = set()

    for fact_item in facts:
        # Handle both dict format (from search_facts) and string format
        fact_str = fact_item.get('fact', '') if isinstance(fact_item, dict) else str(fact_item)
        if not fact_str:
            continue

        fact_lower = fact_str.lower().strip()
        if fact_lower in seen:
            continue
        seen.add(fact_lower)

        # Normalize fact for keyword matching
        fact_normalized = normalize_for_match(fact_str)

        # Check if any query keyword appears in the normalized fact
        has_keyword = any(kw in fact_normalized for kw in query_words)
        if has_keyword:
            keyword_matched.append(fact_str)
        else:
            semantic_only.append(fact_str)

    # Prioritize keyword matches, then add semantic-only as fallback
    unique_facts = keyword_matched[:max_results]
    remaining = max_results - len(unique_facts)
    if remaining > 0:
        unique_facts.extend(semantic_only[:remaining])

    if unique_facts:
        print(f"[Memory] Search '{search_query}': {len(keyword_matched)} keyword matches, {len(semantic_only)} semantic-only")

    return unique_facts if unique_facts else None


def evaluate_chapter_boundary(npc_id: str, npc_name: str, dialogue_history: List[Dict],
                               current_location: str, game_date: str = "",
                               game_time: str = "", characters_in_earshot: List[str] = None) -> bool:
    """
    Evaluate if a chapter should close and handle the transition.
    Called when a conversation ends.

    Args:
        npc_id: NPC's internal ID
        npc_name: NPC's display name
        dialogue_history: Recent dialogue entries (already earshot-filtered)
        current_location: Current game location
        game_date: Current game date
        game_time: Current game time
        characters_in_earshot: List of character names this NPC knows about

    Returns:
        True if any chapters were closed and episodes added
    """
    settings = load_settings()
    if not settings.get('memory', {}).get('enabled', True):
        return False

    if not _graphiti_available:
        return False

    chapter_mgr = ChapterManager()
    graphiti_mgr = GraphitiManager()

    # Detect chapter boundary
    result = detect_chapter_boundary(npc_id, npc_name, dialogue_history, current_location, characters_in_earshot)

    chapters_closed = False

    # Handle current chapter action
    current_action = result.get('current_chapter_action', 'continue')
    additional_events = result.get('additional_events', [])

    if current_action == 'close':
        open_chapter = chapter_mgr.get_open_chapter(npc_id)
        if open_chapter:
            # Add any additional events to the chapter before closing
            if additional_events:
                existing_events = open_chapter.get('key_events', [])
                existing_events.extend(additional_events)
                open_chapter['key_events'] = existing_events

            # Get close timestamp
            close_at = result.get('close_at_timestamp')

            # Get chapter dialogue entries (up to close point)
            chapter_entries = []
            for entry in dialogue_history:
                if close_at and entry.get('timestamp', 0) > close_at:
                    break
                chapter_entries.append(entry)

            # Extract player name
            player_name = "the student"
            for entry in chapter_entries:
                if entry.get('isPlayer') and entry.get('speaker'):
                    player_name = entry['speaker']
                    break

            # Generate episode content with LLM
            episode_content = generate_episode_content(
                npc_name=npc_name,
                chapter_title=open_chapter['title'],
                chapter_summary=open_chapter.get('summary', 'No summary'),
                key_events=open_chapter.get('key_events', []),
                location=open_chapter.get('location', 'Unknown'),
                dialogue_entries=chapter_entries,
                player_name=player_name
            )

            # Add episode to Graphiti
            graphiti_mgr.add_episode(
                npc_id=npc_id,
                chapter_title=open_chapter['title'],
                content=episode_content,
                game_date=open_chapter.get('start_date', game_date),
                game_time=open_chapter.get('start_time', game_time)
            )

            # Mark chapter as closed (get end_timestamp from last chapter entry)
            last_entry_ts = chapter_entries[-1].get('timestamp') if chapter_entries else None
            chapter_mgr.close_chapter(
                npc_id,
                open_chapter.get('summary', ''),
                end_timestamp=last_entry_ts,
                key_events=open_chapter.get('key_events', [])
            )
            chapters_closed = True
            print(f"[Memory] Closed chapter '{open_chapter['title']}' for {npc_id}")

            # Trigger bio update (non-blocking, best-effort)
            try:
                update_bio_incremental(
                    npc_id=npc_id,
                    npc_name=npc_name,
                    chapter_start_ts=open_chapter.get('start_timestamp', 0),
                    chapter_end_ts=last_entry_ts or int(datetime.now().timestamp())
                )
            except Exception as e:
                print(f"[Bio] Update failed (non-fatal): {e}")

    elif current_action == 'continue' and additional_events:
        # Just add events to current chapter without closing
        chapter_mgr.add_events_to_chapter(npc_id, additional_events)

    # Process any new chapters from the result
    new_chapters = result.get('new_chapters', [])
    for new_chapter in new_chapters:
        chapter_title = new_chapter.get('title', 'New Chapter')
        chapter_status = new_chapter.get('status', 'open')
        chapter_summary = new_chapter.get('summary', '')
        chapter_events = new_chapter.get('key_events', [])
        start_ts = new_chapter.get('start_timestamp')
        end_ts = new_chapter.get('end_timestamp')

        # Validate timestamps - LLM sometimes returns garbage
        if start_ts and not is_valid_timestamp(start_ts):
            print(f"[Memory] Invalid start_timestamp {start_ts} for chapter '{chapter_title}' - skipping")
            continue
        if end_ts and not is_valid_timestamp(end_ts):
            print(f"[Memory] Invalid end_timestamp {end_ts} for chapter '{chapter_title}' - skipping")
            continue

        if chapter_status == 'closed' and start_ts and end_ts:
            # This is a fully closed chapter - add directly to Graphiti
            chapter_entries = [e for e in dialogue_history
                              if start_ts <= e.get('timestamp', 0) <= end_ts]

            # Extract player name
            player_name = "the student"
            for entry in chapter_entries:
                if entry.get('isPlayer') and entry.get('speaker'):
                    player_name = entry['speaker']
                    break

            # Generate episode content with LLM
            episode_content = generate_episode_content(
                npc_name=npc_name,
                chapter_title=chapter_title,
                chapter_summary=chapter_summary,
                key_events=chapter_events,
                location=current_location,
                dialogue_entries=chapter_entries,
                player_name=player_name
            )

            graphiti_mgr.add_episode(
                npc_id=npc_id,
                chapter_title=chapter_title,
                content=episode_content,
                game_date=game_date,
                game_time=game_time
            )
            chapters_closed = True
            print(f"[Memory] Added closed chapter '{chapter_title}' for {npc_id}")

            # Trigger bio update (non-blocking, best-effort)
            try:
                update_bio_incremental(
                    npc_id=npc_id,
                    npc_name=npc_name,
                    chapter_start_ts=start_ts,
                    chapter_end_ts=end_ts
                )
            except Exception as e:
                print(f"[Bio] Update failed (non-fatal): {e}")

        else:
            # Open chapter - start tracking it
            # Build context messages from recent dialogue
            context_messages = []
            for entry in dialogue_history[-15:]:
                context_messages.append({
                    'time': f"{entry.get('gameDate', '')} {entry.get('gameTime', '')}".strip(),
                    'speaker': entry.get('speaker', 'Unknown'),
                    'text': entry.get('text', '')[:200]  # Truncate long messages
                })

            chapter_mgr.start_chapter(
                npc_id, chapter_title, current_location, game_date, game_time,
                summary=chapter_summary,
                key_events=chapter_events,
                context_messages=context_messages
            )
            print(f"[Memory] Started new chapter '{chapter_title}' for {npc_id}")

    # If no chapters exist and none were created, start a default one
    if not chapter_mgr.get_open_chapter(npc_id) and not new_chapters:
        chapter_mgr.start_chapter(npc_id, "Beginning", current_location, game_date, game_time)

    # Update indexing meta with the latest entry timestamp
    # Only if we actually ran chapter detection (didn't skip)
    if not result.get('_skipped') and dialogue_history:
        max_ts = max(e.get('timestamp', 0) for e in dialogue_history)
        if max_ts > 0:
            chapter_mgr.update_indexing_meta(npc_id, max_ts)

    return chapters_closed


# =============================================================================
# LLM Helpers with Retry Logic
# =============================================================================

def call_llm_with_retry(prompt: str, model: str, max_retries: int = 3,
                        context: str = "memory") -> Optional[Dict]:
    """
    Call LLM and parse JSON response with retry logic.

    Args:
        prompt: The prompt to send
        model: Model name
        max_retries: Number of retries on failure
        context: Context label for logging

    Returns:
        Parsed JSON dict or None if all retries failed
    """
    import llm
    import time
    import re

    last_error = None

    for attempt in range(max_retries):
        try:
            result = llm.chat(
                messages=[{"role": "user", "content": prompt}],
                model=model,
                temperature=0.3,
                max_tokens=4096,
                context=context
            )

            if not result:
                last_error = "No LLM response"
                print(f"[Memory] Attempt {attempt + 1}/{max_retries}: No response")
                time.sleep(1)
                continue

            # Parse JSON response
            json_block_match = re.search(r'```json\s*([\s\S]*?)\s*```', result)
            if json_block_match:
                json_str = json_block_match.group(1)
            else:
                first_brace = result.find('{')
                last_brace = result.rfind('}')
                if first_brace != -1 and last_brace != -1:
                    json_str = result[first_brace:last_brace + 1]
                else:
                    json_str = result

            parsed = json.loads(json_str)
            return parsed

        except json.JSONDecodeError as e:
            last_error = f"JSON parse error: {e}"
            print(f"[Memory] Attempt {attempt + 1}/{max_retries}: {last_error}")

            # On retry, add hint to prompt about JSON formatting
            if attempt < max_retries - 1:
                prompt = prompt + "\n\nIMPORTANT: Your previous response had invalid JSON. Please return ONLY valid JSON with no extra text."
                time.sleep(1)

        except Exception as e:
            last_error = str(e)
            print(f"[Memory] Attempt {attempt + 1}/{max_retries}: {last_error}")
            time.sleep(1)

    print(f"[Memory] All {max_retries} attempts failed: {last_error}")
    return None


# =============================================================================
# Migration / Batch Processing
# =============================================================================

def migrate_npc_history(npc_id: str, npc_name: str, full_history: List[Dict],
                        batch_size: int = 50, progress_callback=None) -> Dict[str, Any]:
    """
    Migrate existing dialogue history into chapters/episodes for an NPC.
    Processes history in batches to handle large histories (1000+ entries).

    Args:
        npc_id: NPC's internal ID
        npc_name: NPC's display name
        full_history: Complete dialogue history (will be filtered to NPC's earshot)
        batch_size: Number of entries to process per batch
        progress_callback: Optional callback(current, total, message) for progress updates

    Returns:
        Dict with: chapters_created, episodes_added, entries_processed, errors
    """
    import llm

    settings = load_settings()
    if not settings.get('memory', {}).get('enabled', True):
        return {"error": "Memory system not enabled"}

    if not _graphiti_available:
        return {"error": "Graphiti not available"}

    # Filter to what this NPC witnessed, excluding insignificant NPCs
    from .text_utils import is_significant_npc
    npc_history = [e for e in full_history if
                   (e.get('voiceName') == npc_id or
                    npc_id in e.get('earshot', []))
                   and (e.get('isPlayer') or e.get('isAIResponse')
                        or is_significant_npc(e.get('voiceName', '')))]

    if not npc_history:
        return {"error": f"No history found for {npc_id}", "entries_processed": 0}

    # Sort by timestamp
    npc_history.sort(key=lambda x: x.get('timestamp', 0))

    total_entries = len(npc_history)
    print(f"[Migration] Processing {total_entries} entries for {npc_name}")

    if progress_callback:
        progress_callback(0, total_entries, f"Starting migration for {npc_name}")

    chapter_mgr = ChapterManager()
    graphiti_mgr = GraphitiManager()

    # Check if NPC already has chapters (already migrated)
    existing_chapters = chapter_mgr.get_all_chapters(npc_id)
    if existing_chapters.get('closed_chapters') or existing_chapters.get('open_chapter'):
        print(f"[Migration] {npc_name} already has chapters - skipping migration")
        return {
            "skipped": True,
            "reason": "Already migrated",
            "chapters_created": 0,
            "episodes_added": 0,
            "entries_processed": 0,
            "errors": []
        }

    # Initialize Graphiti
    if not graphiti_mgr.init_graphiti():
        return {"error": "Failed to initialize Graphiti/Kuzu"}

    results = {
        "chapters_created": 0,
        "episodes_added": 0,
        "entries_processed": 0,
        "errors": []
    }

    # Get all unique characters mentioned in history
    all_characters = set()
    for entry in npc_history:
        if entry.get('speaker') and not entry.get('isPlayer'):
            all_characters.add(entry.get('speaker'))
        for char in entry.get('earshot', []):
            all_characters.add(char)
    characters_list = list(all_characters)

    # Process in batches with sliding window
    current_pos = 0
    open_chapter = None

    while current_pos < total_entries:
        # Get batch with context overlap
        context_start = max(0, current_pos - 10)  # 10 entries overlap for context
        batch_end = min(total_entries, current_pos + batch_size)
        batch = npc_history[context_start:batch_end]

        # Determine location from batch
        current_location = "Unknown"
        for entry in reversed(batch):
            if entry.get('type') == 'location':
                current_location = entry.get('text', 'Unknown')
                break

        # Get game date/time from last entry
        last_entry = batch[-1] if batch else {}
        game_date = last_entry.get('gameDate', '')
        game_time = last_entry.get('gameTime', '')

        if progress_callback:
            progress_callback(current_pos, total_entries,
                            f"Processing entries {current_pos}-{batch_end}")

        try:
            # Build prompt for this batch
            prompt = build_chapter_prompt(
                npc_name, npc_id, batch, open_chapter, characters_list
            )

            model = settings.get('memory', {}).get('chapter_model', 'gemini-2.5-flash-lite')

            if progress_callback:
                progress_callback(current_pos, total_entries, "Detecting chapter boundaries...")

            # Use retry helper for robustness
            parsed = call_llm_with_retry(prompt, model, max_retries=3, context="migration_chapter")

            if not parsed:
                results["errors"].append(f"Failed after 3 retries at position {current_pos}")
                current_pos = batch_end
                continue

            # Handle current chapter action
            action = parsed.get('current_chapter_action', 'continue')

            if action == 'close' and open_chapter:
                # Close and save the chapter
                close_ts = parsed.get('close_at_timestamp', batch[-1].get('timestamp'))
                additional_events = parsed.get('additional_events', [])
                start_ts = open_chapter.get('start_timestamp', 0)

                # Add events
                if additional_events:
                    existing = open_chapter.get('key_events', [])
                    existing.extend(additional_events)
                    open_chapter['key_events'] = existing

                # Get chapter entries by timestamp range
                chapter_entries = [e for e in npc_history
                                   if start_ts <= e.get('timestamp', 0) <= close_ts]

                # Extract player name
                player_name = "the student"
                for entry in chapter_entries:
                    if entry.get('isPlayer') and entry.get('speaker'):
                        player_name = entry['speaker']
                        break

                # Generate episode content with LLM
                if progress_callback:
                    progress_callback(current_pos, total_entries,
                                    f"Generating episode: {open_chapter['title'][:30]}...")

                episode_content = generate_episode_content(
                    npc_name=npc_name,
                    chapter_title=open_chapter['title'],
                    chapter_summary=open_chapter.get('summary', ''),
                    key_events=open_chapter.get('key_events', []),
                    location=open_chapter.get('location', 'Unknown'),
                    dialogue_entries=chapter_entries,
                    player_name=player_name
                )

                # Add to Graphiti
                if progress_callback:
                    progress_callback(current_pos, total_entries,
                                    f"Indexing to graph: {open_chapter['title'][:30]}...")

                if graphiti_mgr.add_episode(
                    npc_id=npc_id,
                    chapter_title=open_chapter['title'],
                    content=episode_content,
                    game_date=open_chapter.get('start_date', game_date),
                    game_time=open_chapter.get('start_time', game_time)
                ):
                    results["episodes_added"] += 1

                # Save closed chapter (timestamps only)
                chapter_mgr.close_chapter(
                    npc_id=npc_id,
                    summary=open_chapter.get('summary', ''),
                    end_timestamp=close_ts,
                    key_events=open_chapter.get('key_events', [])
                )

                results["chapters_created"] += 1
                open_chapter = None
                print(f"[Migration] Closed chapter (ts {start_ts}-{close_ts})")

            # Process new chapters
            for new_chapter in parsed.get('new_chapters', []):
                chapter_status = new_chapter.get('status', 'open')

                if chapter_status == 'closed':
                    # Fully closed chapter - add directly
                    start_ts = new_chapter.get('start_timestamp', 0)
                    end_ts = new_chapter.get('end_timestamp', 0)

                    # Validate timestamps - LLM sometimes returns garbage
                    if not is_valid_timestamp(start_ts) or not is_valid_timestamp(end_ts):
                        print(f"[Migration] Invalid timestamps ({start_ts}, {end_ts}) for chapter '{new_chapter.get('title')}' - skipping")
                        continue

                    # Get chapter entries by timestamp range
                    chapter_entries = [e for e in npc_history
                                       if start_ts <= e.get('timestamp', 0) <= end_ts]

                    # Extract player name
                    player_name = "the student"
                    for entry in chapter_entries:
                        if entry.get('isPlayer') and entry.get('speaker'):
                            player_name = entry['speaker']
                            break

                    # Generate episode content with LLM
                    if progress_callback:
                        progress_callback(current_pos, total_entries,
                                        f"Generating episode: {new_chapter['title'][:30]}...")

                    episode_content = generate_episode_content(
                        npc_name=npc_name,
                        chapter_title=new_chapter['title'],
                        chapter_summary=new_chapter.get('summary', ''),
                        key_events=new_chapter.get('key_events', []),
                        location=current_location,
                        dialogue_entries=chapter_entries,
                        player_name=player_name
                    )

                    if progress_callback:
                        progress_callback(current_pos, total_entries,
                                        f"Indexing to graph: {new_chapter['title'][:30]}...")

                    if graphiti_mgr.add_episode(
                        npc_id=npc_id,
                        chapter_title=new_chapter['title'],
                        content=episode_content,
                        game_date=game_date,
                        game_time=game_time
                    ):
                        results["episodes_added"] += 1

                    # Save the closed chapter (start then immediately close)
                    chapter_mgr.start_chapter(
                        npc_id=npc_id,
                        title=new_chapter['title'],
                        location=current_location,
                        game_date=game_date,
                        game_time=game_time,
                        summary=new_chapter.get('summary', ''),
                        start_timestamp=start_ts,
                        key_events=new_chapter.get('key_events', [])
                    )
                    chapter_mgr.close_chapter(
                        npc_id=npc_id,
                        summary=new_chapter.get('summary', ''),
                        end_timestamp=end_ts,
                        key_events=new_chapter.get('key_events', [])
                    )

                    results["chapters_created"] += 1
                    print(f"[Migration] Added closed chapter '{new_chapter['title']}' (ts {start_ts}-{end_ts})")

                else:
                    # Open chapter - track it and save to ChapterManager
                    start_ts = new_chapter.get('start_timestamp', batch[0].get('timestamp', 0))

                    # Validate timestamp - use batch timestamp as fallback
                    if not is_valid_timestamp(start_ts):
                        fallback_ts = batch[0].get('timestamp', 0) if batch else 0
                        if is_valid_timestamp(fallback_ts):
                            print(f"[Migration] Invalid start_timestamp {start_ts}, using fallback {fallback_ts}")
                            start_ts = fallback_ts
                        else:
                            print(f"[Migration] Invalid timestamps for open chapter '{new_chapter.get('title')}' - skipping")
                            continue

                    open_chapter = {
                        "title": new_chapter.get('title', 'Chapter'),
                        "summary": new_chapter.get('summary', ''),
                        "location": current_location,
                        "start_date": game_date,
                        "start_time": game_time,
                        "start_timestamp": start_ts,
                        "key_events": new_chapter.get('key_events', [])
                    }

                    # Save to ChapterManager so it persists
                    chapter_mgr.start_chapter(
                        npc_id=npc_id,
                        title=open_chapter['title'],
                        location=current_location,
                        game_date=game_date,
                        game_time=game_time,
                        summary=new_chapter.get('summary', ''),
                        start_timestamp=start_ts,
                        key_events=new_chapter.get('key_events', [])
                    )

                    print(f"[Migration] Started chapter '{open_chapter['title']}' at ts {start_ts}")

        except json.JSONDecodeError as e:
            results["errors"].append(f"JSON parse error at {current_pos}: {e}")
        except Exception as e:
            results["errors"].append(f"Error at {current_pos}: {e}")

        results["entries_processed"] = batch_end
        current_pos = batch_end

    # Note: Open chapter is already saved to ChapterManager during processing
    if open_chapter:
        print(f"[Migration] Left open chapter '{open_chapter['title']}' at ts {open_chapter.get('start_timestamp', 0)}")

    # Mark this NPC as fully indexed so the memory queue doesn't reprocess them
    # Update BOTH the JSON indexing_meta AND the SQLite npc_state
    if npc_history:
        max_ts = max(e.get('timestamp', 0) for e in npc_history)
        if max_ts > 0:
            # Update JSON indexing_meta (legacy)
            chapter_mgr.update_indexing_meta(npc_id, max_ts)
            print(f"[Migration] Set indexing_meta for {npc_id} to timestamp {max_ts}")

            # CRITICAL: Also update SQLite npc_state directly
            # This is what the memory queue actually reads to determine what's processed
            try:
                from .memory_queue import get_max_entry_id_for_timestamp, get_connection, _ensure_initialized
                _ensure_initialized()

                max_entry_id = get_max_entry_id_for_timestamp(npc_id, max_ts)
                if max_entry_id > 0:
                    import time
                    now = int(time.time())
                    with get_connection() as conn:
                        # Use INSERT OR REPLACE to ensure the row exists
                        conn.execute("""
                            INSERT INTO npc_state (npc_id, is_processing, last_processed_entry_id, created_at, updated_at)
                            VALUES (?, 0, ?, ?, ?)
                            ON CONFLICT(npc_id) DO UPDATE SET
                                last_processed_entry_id = ?,
                                updated_at = ?
                        """, (npc_id, max_entry_id, now, now, max_entry_id, now))
                        conn.commit()
                    print(f"[Migration] Set SQLite last_processed_entry_id for {npc_id} to {max_entry_id}")
                else:
                    print(f"[Migration] WARNING: Could not find entry ID for timestamp {max_ts}")
            except Exception as e:
                print(f"[Migration] WARNING: Failed to update SQLite state: {e}")

    if progress_callback:
        progress_callback(total_entries, total_entries, "Migration complete")

    print(f"[Migration] Complete: {results['chapters_created']} chapters, "
          f"{results['episodes_added']} episodes, {len(results['errors'])} errors")

    return results


def migrate_all_npcs(full_history: List[Dict], progress_callback=None) -> Dict[str, Any]:
    """
    Migrate dialogue history for all NPCs that have history.

    Args:
        full_history: Complete dialogue history
        progress_callback: Optional callback(npc_id, npc_progress, message)

    Returns:
        Dict mapping npc_id -> migration results
    """
    from .localization import get_display_name

    # Find all unique significant NPCs in history (by voiceName and earshot)
    from .text_utils import is_significant_npc

    npc_entry_counts = {}
    for entry in full_history:
        voice_name = entry.get('voiceName')
        if voice_name and voice_name.lower() != 'player' and is_significant_npc(voice_name):
            npc_entry_counts[voice_name] = npc_entry_counts.get(voice_name, 0) + 1
        for char_id in entry.get('earshot', []):
            if char_id.lower() != 'player' and is_significant_npc(char_id):
                npc_entry_counts[char_id] = npc_entry_counts.get(char_id, 0) + 1

    # Filter to NPCs meeting minimum entry threshold (same as migration-status endpoint)
    settings = load_settings()
    min_entries = settings.get('memory', {}).get('chapter_entry_threshold', 30)
    npc_ids = {npc_id for npc_id, count in npc_entry_counts.items() if count >= min_entries}

    results = {}
    total_npcs = len(npc_ids)

    for i, npc_id in enumerate(npc_ids):
        npc_name = get_display_name(npc_id)

        if progress_callback:
            progress_callback(npc_id, f"{i+1}/{total_npcs}", f"Migrating {npc_name}")

        results[npc_id] = migrate_npc_history(npc_id, npc_name, full_history)

    return results


def is_memory_available() -> bool:
    """Check if the memory system is available (Graphiti installed)."""
    return _graphiti_available


def init_memory() -> bool:
    """
    Initialize the memory system (Graphiti + Kuzu).
    Call on server startup and after memory settings change.

    Returns:
        True if initialization succeeded, False otherwise.
    """
    if not _graphiti_available:
        print("[Memory] Graphiti not available - memory system disabled")
        return False

    settings = load_settings()
    memory_settings = settings.get('memory', {})

    if not memory_settings.get('enabled', True):
        print("[Memory] Memory system disabled in settings")
        return False

    graphiti_mgr = GraphitiManager()
    success = graphiti_mgr.init_graphiti()

    if success:
        print("[Memory] Memory system initialized successfully")
    else:
        print("[Memory] Memory system initialization failed")

    return success


def reset_memory_connection():
    """
    Reset the Graphiti connection. Call when memory settings change.
    Next init_memory() or graph operation will reconnect with new settings.
    """
    graphiti_mgr = GraphitiManager()
    graphiti_mgr._graphiti = None
    graphiti_mgr._loop = None
    print("[Memory] Memory connection reset - will reconnect on next use")
