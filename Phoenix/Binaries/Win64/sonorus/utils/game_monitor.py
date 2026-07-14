"""
Game process monitoring for Sonorus.
Detects when Hogwarts Legacy closes and shuts down the server.
"""

import os
import sys
import time
import threading
import ctypes

from constants import GAME_WINDOW_TITLE

GAME_PROCESS_NAME = "HogwartsLegacy.exe"
_game_check_interval = 5.0  # Check every 5 seconds
_game_monitor_running = False


def _find_window_by_title(title):
    """Check if a window containing the given title exists."""
    found = [False]

    def enum_callback(hwnd, _):
        try:
            length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buffer = ctypes.create_unicode_buffer(length + 1)
                ctypes.windll.user32.GetWindowTextW(hwnd, buffer, length + 1)
                if buffer.value.strip() == title:
                    found[0] = True
                    return False  # Stop enumeration
        except:
            pass
        return True  # Continue enumeration

    try:
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        ctypes.windll.user32.EnumWindows(WNDENUMPROC(enum_callback), 0)
        return found[0]
    except Exception:
        return True  # Assume exists on error to avoid false shutdown


def is_game_running():
    """Check if Hogwarts Legacy is running by looking for window first (fast), then process."""
    # Fast check: look for game window
    if not _find_window_by_title(GAME_WINDOW_TITLE):
        print(f"[GameMonitor] Window '{GAME_WINDOW_TITLE}' not found - game closed")
        return False

    # Window exists, game is running
    return True


def _kill_process_tree():
    """Force kill the entire process tree using taskkill."""
    import subprocess
    pid = os.getpid()
    print(f"[GameMonitor] Killing process tree (PID {pid})")
    sys.stdout.flush()
    # taskkill /F = force, /T = tree (kill children too), /PID = process ID
    # This kills cmd.exe, python, and any child processes
    subprocess.Popen(
        f'taskkill /F /T /PID {pid}',
        shell=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )


def start_game_monitor():
    """Start background thread that monitors if game is running."""
    global _game_monitor_running

    if _game_monitor_running:
        return

    # Debug mode - skip game monitoring entirely
    if os.environ.get("SONORUS_DEBUG"):
        print("[GameMonitor] Debug mode - game monitoring disabled")
        return

    # Initial check - don't start server if game isn't running
    if not is_game_running():
        print(f"[GameMonitor] {GAME_PROCESS_NAME} not detected - server will not start")
        print("[GameMonitor] Please start Hogwarts Legacy first")
        sys.exit(1)

    _game_monitor_running = True

    def monitor_loop():
        global _game_monitor_running
        check_count = 0

        while _game_monitor_running:
            time.sleep(_game_check_interval)
            check_count += 1

            # Debug output every 12 checks (~60 seconds) to confirm monitor is running
            if check_count % 12 == 0:
                print(f"[GameMonitor] Still monitoring (check #{check_count})")

            if not is_game_running():
                # Force flush to ensure logs are written before any termination
                import datetime
                timestamp = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
                print(f"\n[GameMonitor {timestamp}] {GAME_PROCESS_NAME} no longer running")
                print(f"[GameMonitor {timestamp}] Shutting down server...")
                sys.stdout.flush()
                sys.stderr.flush()
                _game_monitor_running = False

                # Gracefully shutdown memory processing before exit
                # Wait up to 15 minutes for graphiti operations (add_episode can take 5-10 min)
                try:
                    from .memory_queue import graceful_shutdown, is_processing
                    memory_shutdown_ok = graceful_shutdown(max_wait=900.0)  # 15 minutes
                except Exception as e:
                    memory_shutdown_ok = False
                    print(f"[GameMonitor] Error during graceful shutdown: {e}")

                if not memory_shutdown_ok:
                    # Check if still actively processing - if so, wait longer
                    try:
                        if is_processing():
                            print("[GameMonitor] Graphiti still processing - waiting for completion before exit...")
                            extra_wait = 0
                            while is_processing() and extra_wait < 600:  # up to 10 more minutes
                                time.sleep(2.0)
                                extra_wait += 2
                                if extra_wait % 30 == 0:
                                    print(f"[GameMonitor] Still waiting for graphiti... ({extra_wait}s extra)")
                            if not is_processing():
                                print("[GameMonitor] Graphiti operations completed")
                                # Close connections now that processing is done
                                try:
                                    graceful_shutdown(max_wait=30.0)
                                except Exception:
                                    pass
                            else:
                                print("[GameMonitor] Graphiti still running after extended wait - forcing exit")
                        else:
                            print("[GameMonitor] Memory shutdown incomplete but no active processing")
                    except Exception as e:
                        print(f"[GameMonitor] Error checking processing state: {e}")

                # Delete lock files so Lua doesn't wait 60s thinking server is starting
                script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                for lock_file in ["server.lock", "server.heartbeat"]:
                    try:
                        lock_path = os.path.join(script_dir, lock_file)
                        if os.path.exists(lock_path):
                            os.remove(lock_path)
                            print(f"[GameMonitor] Deleted {lock_file}")
                    except Exception as e:
                        print(f"[GameMonitor] Could not delete {lock_file}: {e}")

                # Force flush before termination
                sys.stdout.flush()
                sys.stderr.flush()

                # Kill entire process tree via taskkill
                _kill_process_tree()

                # Give taskkill a moment to work, then force exit
                time.sleep(0.5)
                os._exit(0)

    monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
    monitor_thread.start()
    print(f"[GameMonitor] Monitoring {GAME_PROCESS_NAME} (check every {_game_check_interval}s)")
