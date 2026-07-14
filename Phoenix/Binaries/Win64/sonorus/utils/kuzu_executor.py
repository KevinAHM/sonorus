"""
Kuzu Database Executor - Thread-safe serialized access to kuzu/graphiti.

Kuzu (the graph database) is not thread-safe. This module provides a
serialized executor that ensures only one thread accesses kuzu at a time,
with proper queuing, retry logic, and logging.

Industry-standard pattern: Dedicated worker thread with work queue.
"""

import threading
import queue
import time
import traceback
from typing import Any, Callable, Optional, TypeVar
from dataclasses import dataclass
from enum import Enum
import functools

T = TypeVar('T')


class TaskPriority(Enum):
    """Priority levels for queued tasks."""
    HIGH = 0      # Critical operations (close_chapter, etc.)
    NORMAL = 1    # Standard operations (add_episode, search)
    LOW = 2       # Background operations (build_indices)


@dataclass
class TaskResult:
    """Result of a queued task."""
    success: bool
    value: Any = None
    error: Optional[Exception] = None
    retries: int = 0
    wait_time: float = 0.0  # Time spent waiting in queue


class _WorkItem:
    """Internal work item for the executor queue."""
    def __init__(self, func: Callable, args: tuple, kwargs: dict,
                 priority: TaskPriority, timeout: float, max_retries: int):
        self.func = func
        self.args = args
        self.kwargs = kwargs
        self.priority = priority
        self.timeout = timeout
        self.max_retries = max_retries
        self.result_event = threading.Event()
        self.result: Optional[TaskResult] = None
        self.enqueue_time = time.time()
        self.attempt = 0

    def __lt__(self, other):
        # For priority queue ordering
        return self.priority.value < other.priority.value


class KuzuExecutor:
    """
    Thread-safe executor for kuzu/graphiti operations.

    All operations are serialized through a single worker thread,
    ensuring kuzu is never accessed concurrently.

    Features:
    - Priority queue (HIGH/NORMAL/LOW)
    - Configurable timeouts
    - Automatic retry with exponential backoff
    - Detailed logging of contention and retries
    - Graceful shutdown with drain
    """

    _instance: Optional['KuzuExecutor'] = None
    _lock = threading.Lock()

    def __new__(cls) -> 'KuzuExecutor':
        """Singleton pattern - only one executor instance."""
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self._queue: queue.PriorityQueue = queue.PriorityQueue()
        self._worker_thread: Optional[threading.Thread] = None
        self._running = False
        self._current_task: Optional[str] = None
        self._active_tasks = 0
        self._state_lock = threading.Lock()
        self._idle_event = threading.Event()
        self._idle_event.set()
        self._stats = {
            'total_tasks': 0,
            'successful_tasks': 0,
            'failed_tasks': 0,
            'total_retries': 0,
            'total_wait_time': 0.0,
            'max_queue_depth': 0,
            'contentions': 0,  # Times a task had to wait because another was running
        }
        self._stats_lock = threading.Lock()
        self._initialized = True

        # Start worker thread
        self._start_worker()

    def _start_worker(self):
        """Start the worker thread."""
        if self._worker_thread is not None and self._worker_thread.is_alive():
            return

        self._running = True
        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            name="KuzuExecutor-Worker",
            daemon=True
        )
        self._worker_thread.start()
        print("[KuzuExecutor] Worker thread started")

    def _worker_loop(self):
        """Main worker loop - processes tasks from queue."""
        while self._running or not self._queue.empty() or self._has_active_tasks():
            try:
                # Wait for work with timeout (allows checking _running flag)
                try:
                    _, work_item = self._queue.get(timeout=1.0)
                except queue.Empty:
                    self._update_idle_state()
                    continue

                # Track queue depth
                current_depth = self._queue.qsize()
                with self._stats_lock:
                    if current_depth > self._stats['max_queue_depth']:
                        self._stats['max_queue_depth'] = current_depth

                # Process the work item
                self._process_work_item(work_item)
                self._queue.task_done()

            except Exception as e:
                print(f"[KuzuExecutor] Worker loop error: {e}")
                traceback.print_exc()

        print("[KuzuExecutor] Worker thread exiting")

    def _process_work_item(self, work_item: _WorkItem):
        """Process a single work item with retry logic."""
        func_name = getattr(work_item.func, '__name__', str(work_item.func))
        wait_time = time.time() - work_item.enqueue_time

        # Log if task had to wait (contention)
        if wait_time > 0.1:  # More than 100ms wait
            with self._stats_lock:
                self._stats['contentions'] += 1
            print(f"[KuzuExecutor] Task '{func_name}' waited {wait_time:.2f}s in queue")

        self._mark_task_started(func_name)
        try:
            for attempt in range(work_item.max_retries + 1):
                work_item.attempt = attempt

                if attempt > 0:
                    # Exponential backoff: 0.5s, 1s, 2s, 4s, ...
                    backoff = min(0.5 * (2 ** (attempt - 1)), 10.0)
                    print(f"[KuzuExecutor] Retry {attempt}/{work_item.max_retries} for '{func_name}' after {backoff:.1f}s")
                    with self._stats_lock:
                        self._stats['total_retries'] += 1
                    time.sleep(backoff)

                try:
                    start_time = time.time()
                    result = work_item.func(*work_item.args, **work_item.kwargs)
                    elapsed = time.time() - start_time

                    # Success
                    work_item.result = TaskResult(
                        success=True,
                        value=result,
                        retries=attempt,
                        wait_time=wait_time
                    )

                    with self._stats_lock:
                        self._stats['total_tasks'] += 1
                        self._stats['successful_tasks'] += 1
                        self._stats['total_wait_time'] += wait_time

                    if attempt > 0:
                        print(f"[KuzuExecutor] Task '{func_name}' succeeded after {attempt} retries ({elapsed:.2f}s)")

                    break

                except Exception as e:
                    error_str = str(e)

                    # Check if error is retryable
                    retryable = self._is_retryable_error(e)

                    if not retryable or attempt >= work_item.max_retries:
                        # Final failure
                        print(f"[KuzuExecutor] Task '{func_name}' failed: {error_str}")
                        if attempt > 0:
                            print(f"[KuzuExecutor] Failed after {attempt} retries")
                        traceback.print_exc()

                        work_item.result = TaskResult(
                            success=False,
                            error=e,
                            retries=attempt,
                            wait_time=wait_time
                        )

                        with self._stats_lock:
                            self._stats['total_tasks'] += 1
                            self._stats['failed_tasks'] += 1
                            self._stats['total_wait_time'] += wait_time

                        break
                    else:
                        print(f"[KuzuExecutor] Task '{func_name}' failed (retryable): {error_str}")
        finally:
            work_item.result_event.set()
            self._mark_task_finished()

    def _is_retryable_error(self, error: Exception) -> bool:
        """Determine if an error is worth retrying."""
        error_str = str(error).lower()

        # Retryable errors
        retryable_patterns = [
            'timeout',
            'connection',
            'busy',
            'locked',
            'deadlock',
            'temporary',
            'unavailable',
        ]

        # Non-retryable errors (logic errors, etc.)
        non_retryable_patterns = [
            'syntax error',
            'invalid',
            'not found',
            'does not exist',
            'already exists',
            'permission denied',
        ]

        for pattern in non_retryable_patterns:
            if pattern in error_str:
                return False

        for pattern in retryable_patterns:
            if pattern in error_str:
                return True

        # Default: retry on unknown errors (could be transient)
        return True

    def submit(self, func: Callable[..., T], *args,
               priority: TaskPriority = TaskPriority.NORMAL,
               timeout: float = 60.0,
               max_retries: int = 3,
               **kwargs) -> TaskResult:
        """
        Submit a task for execution and wait for result.

        Args:
            func: The function to execute
            *args: Positional arguments for func
            priority: Task priority (HIGH, NORMAL, LOW)
            timeout: Max time to wait for result (seconds)
            max_retries: Number of retries on failure
            **kwargs: Keyword arguments for func

        Returns:
            TaskResult with success status and value/error
        """
        if not self._running:
            self._start_worker()

        work_item = _WorkItem(func, args, kwargs, priority, timeout, max_retries)
        self._idle_event.clear()

        # Add to queue (priority, work_item) - lower priority value = higher priority
        self._queue.put((priority.value, work_item))

        func_name = getattr(func, '__name__', str(func))

        # Wait for result
        if not work_item.result_event.wait(timeout=timeout):
            # Timeout
            print(f"[KuzuExecutor] Task '{func_name}' timed out after {timeout}s")
            return TaskResult(
                success=False,
                error=TimeoutError(f"Task timed out after {timeout}s"),
                wait_time=timeout
            )

        return work_item.result

    def submit_async(self, func: Callable[..., T], *args,
                     priority: TaskPriority = TaskPriority.NORMAL,
                     timeout: float = 60.0,
                     max_retries: int = 3,
                     callback: Optional[Callable[[TaskResult], None]] = None,
                     **kwargs) -> None:
        """
        Submit a task for execution without waiting (fire-and-forget with optional callback).

        Args:
            func: The function to execute
            *args: Positional arguments for func
            priority: Task priority (HIGH, NORMAL, LOW)
            timeout: Max time for execution (not wait time)
            max_retries: Number of retries on failure
            callback: Optional callback to receive TaskResult
            **kwargs: Keyword arguments for func
        """
        if not self._running:
            self._start_worker()

        def wrapped_with_callback():
            result = func(*args, **kwargs)
            return result

        work_item = _WorkItem(wrapped_with_callback, (), {}, priority, timeout, max_retries)
        self._idle_event.clear()

        if callback:
            # Start a thread to wait for result and call callback
            def wait_and_callback():
                work_item.result_event.wait(timeout=timeout)
                if work_item.result:
                    callback(work_item.result)
            threading.Thread(target=wait_and_callback, daemon=True).start()

        self._queue.put((priority.value, work_item))

    def get_stats(self) -> dict:
        """Get executor statistics."""
        with self._stats_lock:
            stats = self._stats.copy()
        stats['queue_depth'] = self._queue.qsize()
        stats['current_task'] = self._current_task
        stats['active_tasks'] = self._active_tasks
        stats['worker_alive'] = self._worker_thread.is_alive() if self._worker_thread else False
        return stats

    def _has_active_tasks(self) -> bool:
        with self._state_lock:
            return self._active_tasks > 0

    def _update_idle_state(self) -> None:
        with self._state_lock:
            is_idle = self._active_tasks == 0 and self._queue.empty()
        if is_idle:
            self._idle_event.set()
        else:
            self._idle_event.clear()

    def _mark_task_started(self, func_name: str) -> None:
        with self._state_lock:
            self._active_tasks += 1
            self._current_task = func_name
            self._idle_event.clear()

    def _mark_task_finished(self) -> None:
        with self._state_lock:
            if self._active_tasks > 0:
                self._active_tasks -= 1
            self._current_task = None
        self._update_idle_state()

    def shutdown(self, wait: bool = True, timeout: float = 30.0) -> bool:
        """
        Shutdown the executor.

        Args:
            wait: If True, wait for pending tasks to complete
            timeout: Max time to wait for pending tasks

        Returns:
            True if shutdown cleanly, False if timed out
        """
        print(f"[KuzuExecutor] Shutdown requested (wait={wait}, timeout={timeout})")

        if wait:
            if not self._idle_event.wait(timeout=timeout):
                remaining = self._queue.qsize()
                active = self._active_tasks
                print(f"[KuzuExecutor] Shutdown timeout with {remaining} queued and {active} active tasks")

        self._running = False

        if self._worker_thread and self._worker_thread.is_alive():
            join_timeout = timeout if wait else 5.0
            self._worker_thread.join(timeout=join_timeout)

        stats = self.get_stats()
        print(f"[KuzuExecutor] Shutdown complete. Stats: {stats['total_tasks']} tasks, "
              f"{stats['failed_tasks']} failed, {stats['total_retries']} retries, "
              f"{stats['contentions']} contentions")

        return self._queue.empty() and self._active_tasks == 0 and not stats['worker_alive']


# Global executor instance
_executor: Optional[KuzuExecutor] = None


def get_executor() -> KuzuExecutor:
    """Get the global KuzuExecutor instance."""
    global _executor
    if _executor is None:
        _executor = KuzuExecutor()
    return _executor


def kuzu_operation(priority: TaskPriority = TaskPriority.NORMAL,
                   timeout: float = 60.0,
                   max_retries: int = 3):
    """
    Decorator to wrap a function for serialized kuzu execution.

    Usage:
        @kuzu_operation(priority=TaskPriority.HIGH)
        def my_db_function(arg1, arg2):
            # This will be executed in the kuzu worker thread
            return result
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> T:
            executor = get_executor()
            result = executor.submit(func, *args, priority=priority,
                                    timeout=timeout, max_retries=max_retries, **kwargs)
            if result.success:
                return result.value
            elif result.error:
                raise result.error
            else:
                return None
        return wrapper
    return decorator


def shutdown_executor(wait: bool = True, timeout: float = 30.0) -> bool:
    """Shutdown the global executor."""
    global _executor
    if _executor:
        result = _executor.shutdown(wait=wait, timeout=timeout)
        # Clear both module-level and class-level singleton references
        # so executor can be restarted if needed
        _executor = None
        KuzuExecutor._instance = None
        return result
    return True
