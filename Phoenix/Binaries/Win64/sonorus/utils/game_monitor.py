"""
Game process monitoring for Sonorus.
Detects when Hogwarts Legacy closes and shuts down the server.
"""

import os
import sys
import time
import subprocess
import threading

GAME_PROCESS_NAME = "HogwartsLegacy.exe"
_game_check_interval = 5.0  # Check every 5 seconds
_game_monitor_running = False


def is_game_running():
    """Check if Hogwarts Legacy is running by looking for the process."""
    try:
        # Use tasklist on Windows (works regardless of game language)
        result = subprocess.run(
            ['tasklist', '/FI', f'IMAGENAME eq {GAME_PROCESS_NAME}', '/NH'],
            capture_output=True,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
        )
        # tasklist returns the process name if found
        return GAME_PROCESS_NAME.lower() in result.stdout.lower()
    except Exception as e:
        print(f"[GameMonitor] Error checking process: {e}")
        # If we can't check, assume game is running to avoid false shutdowns
        return True


def start_game_monitor():
    """Start background thread that monitors if game is running."""
    global _game_monitor_running

    if _game_monitor_running:
        return

    # Initial check - don't start server if game isn't running
    if not is_game_running():
        print(f"[GameMonitor] {GAME_PROCESS_NAME} not detected - server will not start")
        print("[GameMonitor] Please start Hogwarts Legacy first")
        sys.exit(1)

    _game_monitor_running = True

    def monitor_loop():
        global _game_monitor_running
        consecutive_failures = 0

        while _game_monitor_running:
            time.sleep(_game_check_interval)

            if not is_game_running():
                consecutive_failures += 1
                if consecutive_failures >= 2:  # Require 2 consecutive failures to avoid false positives
                    print(f"\n[GameMonitor] {GAME_PROCESS_NAME} no longer running")
                    print("[GameMonitor] Shutting down server...")
                    _game_monitor_running = False
                    os._exit(0)
            else:
                consecutive_failures = 0

    monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
    monitor_thread.start()
    print(f"[GameMonitor] Monitoring {GAME_PROCESS_NAME} (check every {_game_check_interval}s)")
