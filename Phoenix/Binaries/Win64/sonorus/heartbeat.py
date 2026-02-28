"""Heartbeat for server startup - keeps server.lock fresh until Python server takes over."""
import os
import time
import datetime

LOCK_FILE = "server.lock"
STOP_FILE = "server.lock.stop"
HEARTBEAT_FILE = "server.heartbeat"

while True:
    if os.path.exists(STOP_FILE) or os.path.exists(HEARTBEAT_FILE):
        break
    try:
        # Write in Windows time format (HH:MM:SS.xx) to match what Lua expects
        now = datetime.datetime.now()
        time_str = now.strftime("%H:%M:%S.%f")[:11]
        with open(LOCK_FILE, "w") as f:
            f.write(time_str)
    except:
        pass  # Silently continue - lock file is best-effort
    time.sleep(5)
