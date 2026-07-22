"""
Player context management for per-player save isolation.

Provides a singleton PlayerContext that orchestrates switching between players:
name normalization, module registration with close/reinit callbacks,
legacy data migration, and background worker lifecycle management.

DB modules register themselves here; this module never imports from them
(avoiding circular imports).
"""

import os
import re
import shutil
import logging
import threading

from .settings import DATA_DIR

log = logging.getLogger("PlayerContext")

PLAYERS_DIR = os.path.join(DATA_DIR, "players")

# ── Filesystem-unsafe characters: \ / : * ? " < > | ──
_UNSAFE_CHARS_RE = re.compile(r'[\\/:*?"<>|]')

# Control characters (U+0000–U+001F, U+007F)
_CONTROL_CHARS_RE = re.compile(r'[\x00-\x1f\x7f]')

# Names that the game uses as placeholders before the player picks a real name
_PLACEHOLDER_NAMES = {"firstname lastname"}

# Per-player files to migrate from data/ root into data/players/{name}/
_SQLITE_DBS = [
    "dialogue_history.db",
    "commitments.db",
    "owl_post.db",
    "memory_queue.db",
]
_SQLITE_SIBLINGS = ["-wal", "-shm"]

_KUZU_DB = "memory.kuzu"
_KUZU_SIBLINGS = [".wal"]

_DIRECTORIES = [
    "cognis",
    "chapters",
    "chapter_content",
    "npc_bios",
    "graph_backups",
    "tts",
]


# ── Name normalization ──────────────────────────────────────────────────────

def normalize_player_name(raw_name):
    """Convert a raw player name to a filesystem-safe folder name.

    Rules:
    - Preserve Unicode letters (no ASCII transliteration)
    - Strip filesystem-unsafe characters: \\ / : * ? \" < > |
    - Strip control characters (U+0000–U+001F, U+007F)
    - Use str.casefold() for locale-safe Unicode case folding
    - Strip leading/trailing whitespace and dots (Windows NTFS restriction)
    - Return "player_unknown" if result is empty or input is None
    """
    if raw_name is None:
        return "player_unknown"

    name = str(raw_name)
    name = _UNSAFE_CHARS_RE.sub("", name)
    name = _CONTROL_CHARS_RE.sub("", name)
    name = name.casefold()
    name = name.strip().strip(".")

    return name if name else "player_unknown"


# ── Registered module descriptor ────────────────────────────────────────────

class _RegisteredModule:
    """Tracks a DB module's close/reinit callbacks."""

    __slots__ = ("name", "close_fn", "reinit_fn")

    def __init__(self, name, close_fn, reinit_fn):
        self.name = name
        self.close_fn = close_fn
        self.reinit_fn = reinit_fn

    def __repr__(self):
        return f"_RegisteredModule({self.name!r})"


# ── Singleton PlayerContext ─────────────────────────────────────────────────

class PlayerContext:
    """Singleton that orchestrates per-player data directory switching.

    Usage:
        ctx = PlayerContext()
        ctx.register("dialogue_db", close_fn, reinit_fn)
        ctx.set_worker_lifecycle(stop_fn, start_fn)
        ctx.switch("Adri")          # first handshake
        ctx.switch("Adri")          # same player → no-op
        ctx.switch("Éloïse")       # different player → full switch
    """

    _instance = None
    _creation_lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._creation_lock:
                if cls._instance is None:
                    inst = super().__new__(cls)
                    inst._initialized = False
                    cls._instance = inst
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self._lock = threading.Lock()
        self._modules: list[_RegisteredModule] = []
        self._current_player_name: str | None = None
        self._player_data_dir: str | None = None
        self._ready = False

        # Worker lifecycle callbacks (optional)
        self._stop_workers_fn = None
        self._start_workers_fn = None

        self._initialized = True

    # ── Properties ──────────────────────────────────────────────────────

    @property
    def current_player_name(self):
        """The normalized name of the currently active player, or None."""
        return self._current_player_name

    @property
    def player_data_dir(self):
        """Absolute path to the current player's data directory, or None."""
        return self._player_data_dir

    def is_ready(self):
        """True once a player has been successfully switched in."""
        return self._ready

    # ── Registration ────────────────────────────────────────────────────

    def register(self, name, close_fn, reinit_fn):
        """Register a DB module's close/reinit callbacks.

        Args:
            name: Human-readable module name (for logging).
            close_fn: Callable with no args — close all DB connections.
            reinit_fn: Callable(new_data_dir) — reopen with new paths.
        """
        self._modules.append(_RegisteredModule(name, close_fn, reinit_fn))
        log.info("Registered module: %s", name)

    def set_worker_lifecycle(self, stop_fn, start_fn):
        """Register background worker stop/start callbacks.

        Args:
            stop_fn: Callable with no args — signal graceful stop to workers.
            start_fn: Callable with no args — restart workers after switch.
        """
        should_start_now = False
        with self._lock:
            self._stop_workers_fn = stop_fn
            self._start_workers_fn = start_fn
            should_start_now = self._ready and self._current_player_name is not None and start_fn is not None
        log.info("Worker lifecycle callbacks registered")

        # The first Lua player handshake can arrive before the server finishes
        # registering these callbacks. If a player is already active, start the
        # background workers immediately instead of waiting for a future switch.
        if should_start_now:
            log.info("Player already ready; starting background workers immediately")
            try:
                start_fn()
            except Exception:
                log.exception("Error starting workers")

    # ── Core switch orchestration ───────────────────────────────────────

    def switch(self, raw_player_name):
        """Switch to a (possibly new) player.

        Thread-safe. If the normalized name matches the current player,
        this is a no-op (common case: fast travel).

        Args:
            raw_player_name: The player name string from the game handshake.

        Raises:
            ValueError: If the name is a known placeholder (e.g. during character creation).
        """
        normalized = normalize_player_name(raw_player_name)

        if normalized in _PLACEHOLDER_NAMES:
            log.warning("Rejecting placeholder player name: '%s'", raw_player_name)
            raise ValueError(f"Placeholder player name rejected: '{raw_player_name}'")

        # Same-player handshake → no-op
        if normalized == self._current_player_name:
            log.debug("Same player '%s' — no-op", normalized)
            return normalized

        with self._lock:
            # Double-check under lock (another thread may have switched)
            if normalized == self._current_player_name:
                log.debug("Same player '%s' (after lock) — no-op", normalized)
                return normalized

            log.info("Switching player: %s → %s",
                     self._current_player_name or "(none)", normalized)

            is_first_switch = self._current_player_name is None

            # 1. Stop background workers
            if not is_first_switch and self._stop_workers_fn:
                log.info("Stopping background workers...")
                try:
                    self._stop_workers_fn()
                except Exception:
                    log.exception("Error stopping workers")

            # 2. Close all registered modules
            if not is_first_switch:
                for mod in self._modules:
                    log.info("Closing module: %s", mod.name)
                    try:
                        mod.close_fn()
                    except Exception:
                        log.exception("Error closing module %s", mod.name)

            # 3. Compute new player_data_dir
            new_data_dir = os.path.join(PLAYERS_DIR, normalized)

            # 4. Migrate legacy data if needed
            if not os.path.exists(new_data_dir):
                self._migrate_legacy_data(new_data_dir)

            # 5. Create folder if needed
            os.makedirs(new_data_dir, exist_ok=True)

            # 6. Set player_data_dir
            self._player_data_dir = new_data_dir

            # 7. Reinit all registered modules
            for mod in self._modules:
                log.info("Reinitializing module: %s → %s", mod.name, new_data_dir)
                try:
                    mod.reinit_fn(new_data_dir)
                except Exception:
                    log.exception("Error reinitializing module %s", mod.name)

            # 8. Restart background workers
            if self._start_workers_fn:
                log.info("Starting background workers...")
                try:
                    self._start_workers_fn()
                except Exception:
                    log.exception("Error starting workers")

            # 9. Finalize
            self._ready = True
            self._current_player_name = normalized
            log.info("Player switch complete: %s (dir: %s)",
                     normalized, new_data_dir)
            return normalized

    # ── Legacy data migration ───────────────────────────────────────────

    def _migrate_legacy_data(self, new_data_dir):
        """Move legacy per-player files from data/ root into a player folder.

        Only runs when the player folder doesn't yet exist AND there are
        legacy files in data/ root to migrate.
        """
        # Check if any legacy files exist
        legacy_files_exist = False

        for db_name in _SQLITE_DBS:
            if os.path.exists(os.path.join(DATA_DIR, db_name)):
                legacy_files_exist = True
                break

        if not legacy_files_exist and os.path.exists(os.path.join(DATA_DIR, _KUZU_DB)):
            legacy_files_exist = True

        if not legacy_files_exist:
            for dir_name in _DIRECTORIES:
                if os.path.exists(os.path.join(DATA_DIR, dir_name)):
                    legacy_files_exist = True
                    break

        if not legacy_files_exist:
            log.info("No legacy files found in data/ root — skipping migration")
            return

        log.info("Migrating legacy data → %s", new_data_dir)
        os.makedirs(new_data_dir, exist_ok=True)

        # SQLite databases + WAL/SHM siblings
        for db_name in _SQLITE_DBS:
            src = os.path.join(DATA_DIR, db_name)
            if os.path.exists(src):
                shutil.move(src, os.path.join(new_data_dir, db_name))
                log.info("  Moved %s", db_name)
            for suffix in _SQLITE_SIBLINGS:
                sibling = db_name + suffix
                src_sib = os.path.join(DATA_DIR, sibling)
                if os.path.exists(src_sib):
                    shutil.move(src_sib, os.path.join(new_data_dir, sibling))
                    log.info("  Moved %s", sibling)

        # Kuzu database + WAL sibling
        kuzu_src = os.path.join(DATA_DIR, _KUZU_DB)
        if os.path.exists(kuzu_src):
            shutil.move(kuzu_src, os.path.join(new_data_dir, _KUZU_DB))
            log.info("  Moved %s", _KUZU_DB)
        for suffix in _KUZU_SIBLINGS:
            sibling = _KUZU_DB + suffix
            src_sib = os.path.join(DATA_DIR, sibling)
            if os.path.exists(src_sib):
                shutil.move(src_sib, os.path.join(new_data_dir, sibling))
                log.info("  Moved %s", sibling)

        # Directories
        for dir_name in _DIRECTORIES:
            src_dir = os.path.join(DATA_DIR, dir_name)
            if os.path.exists(src_dir):
                shutil.move(src_dir, os.path.join(new_data_dir, dir_name))
                log.info("  Moved %s/", dir_name)

        log.info("Migration complete")

    # ── Reset (for testing) ─────────────────────────────────────────────

    @classmethod
    def _reset(cls):
        """Reset the singleton. For testing only."""
        cls._instance = None


# ── Module-level convenience functions ──────────────────────────────────────

def get_context():
    """Return the PlayerContext singleton."""
    return PlayerContext()


def register(name, close_fn, reinit_fn):
    """Register a DB module with the PlayerContext."""
    PlayerContext().register(name, close_fn, reinit_fn)


def is_ready():
    """True once a player has been successfully switched in."""
    return PlayerContext().is_ready()


def switch(raw_player_name):
    """Switch to a (possibly new) player. Returns normalized name."""
    return PlayerContext().switch(raw_player_name)


def get_player_data_dir():
    """Return the current player's data directory, or None."""
    return PlayerContext().player_data_dir
