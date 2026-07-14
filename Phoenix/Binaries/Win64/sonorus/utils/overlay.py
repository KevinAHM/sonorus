"""
Browser overlay utility — opens/closes Edge or Chrome windows in app mode
as frameless, topmost overlays. Supports multiple named overlays with
mutual exclusion (only one open at a time).
"""
import os
import subprocess
import ctypes
import ctypes.wintypes
import threading
import time

user32 = ctypes.windll.user32

# Win32 function signatures
user32.SetWindowPos.argtypes = [
    ctypes.wintypes.HWND, ctypes.wintypes.HWND,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ctypes.c_uint
]
user32.SetWindowPos.restype = ctypes.wintypes.BOOL
user32.GetWindowLongPtrW.argtypes = [ctypes.wintypes.HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_long
user32.SetWindowLongPtrW.argtypes = [ctypes.wintypes.HWND, ctypes.c_int, ctypes.c_long]
user32.SetWindowLongPtrW.restype = ctypes.c_long

# Constants
HWND_TOPMOST = ctypes.wintypes.HWND(-1)
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_SHOWWINDOW = 0x0040
SW_HIDE = 0
SW_SHOW = 5
GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080

WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)


def _find_browser():
    """Find Edge or Chrome executable."""
    paths = [
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
    ]
    for p in paths:
        if os.path.exists(p):
            return p
    return None


def _find_matching_windows(title_substring):
    """Find all visible window handles matching a title substring."""
    result = []
    def callback(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            if title_substring.lower() in buf.value.lower():
                result.append(hwnd)
        return True
    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return set(result)


def _find_new_window(title_substring, exclude_hwnds, retries=30, delay=0.25):
    """Find a new visible window matching title that wasn't in exclude_hwnds."""
    for _ in range(retries):
        current = _find_matching_windows(title_substring)
        new_windows = current - exclude_hwnds
        if new_windows:
            return next(iter(new_windows))
        time.sleep(delay)
    return None


class BrowserOverlay:
    """Manages a browser overlay window that can be toggled open/closed."""

    def __init__(self, url, title_match, profile_name="sonorus_overlay",
                 width_pct=0.5, height_pct=0.75):
        """
        Args:
            url: URL to open in app mode
            title_match: Substring to match in window title for finding the window
            profile_name: Isolated browser profile directory name
            width_pct: Window width as fraction of screen (0.0-1.0)
            height_pct: Window height as fraction of screen (0.0-1.0)
        """
        self.url = url
        self.title_match = title_match
        self.profile_name = profile_name
        self.width_pct = width_pct
        self.height_pct = height_pct

        self._proc = None
        self._hwnd = None
        self._lock = threading.Lock()
        self._browser_exe = _find_browser()

    @property
    def is_open(self):
        """Check if the overlay is currently open."""
        if self._proc is None:
            return False
        if self._proc.poll() is not None:
            # Process exited
            self._proc = None
            self._hwnd = None
            return False
        return True

    def toggle(self):
        """Toggle the overlay open or closed."""
        with self._lock:
            if self.is_open:
                self.close()
            else:
                self.open()

    def open(self):
        """Open the overlay window."""
        if self.is_open:
            # Already open — bring to front
            if self._hwnd:
                user32.SetForegroundWindow(self._hwnd)
            return

        if not self._browser_exe:
            print("[Overlay] No Chromium browser found")
            return

        # Snapshot existing windows with matching title before launching
        existing_hwnds = _find_matching_windows(self.title_match)

        screen_w = user32.GetSystemMetrics(0)
        screen_h = user32.GetSystemMetrics(1)
        width = int(screen_w * self.width_pct)
        height = int(screen_h * self.height_pct)
        x = (screen_w - width) // 2
        y = (screen_h - height) // 2

        profile_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data", self.profile_name
        )

        self._proc = subprocess.Popen([
            self._browser_exe,
            f"--app={self.url}",
            f"--window-size={width},{height}",
            f"--window-position={x},{y}",
            f"--user-data-dir={profile_dir}",
            "--disable-extensions",
            "--new-window",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=msEdgeSidebarV2",
            "--disk-cache-size=52428800",
        ])

        print(f"[Overlay] Launched PID {self._proc.pid}, finding window...")

        # Find and style the window in a background thread
        threading.Thread(target=self._style_window, args=(existing_hwnds,), daemon=True).start()

    def _style_window(self, existing_hwnds):
        """Find the overlay window and apply topmost + hide-from-taskbar styles."""
        hwnd = _find_new_window(self.title_match, existing_hwnds)
        if not hwnd:
            print("[Overlay] Could not find window")
            return

        with self._lock:
            # Check process is still alive before assigning hwnd
            if self._proc is None or self._proc.poll() is not None:
                return
            self._hwnd = hwnd

        self._apply_styles(hwnd)

        # Re-apply after a delay — Edge can overwrite styles during its own init
        # Only refresh styles (no hide/show) to avoid a visible flash
        time.sleep(1.0)
        if self._proc and self._proc.poll() is None:
            self._refresh_styles(hwnd)

    def _apply_styles(self, hwnd):
        """Apply topmost + hide-from-taskbar styles to a window handle."""
        user32.ShowWindow(hwnd, SW_HIDE)
        ex_style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex_style | WS_EX_TOPMOST | WS_EX_TOOLWINDOW)
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        user32.ShowWindow(hwnd, SW_SHOW)

        # Give the window a moment to be fully ready before interacting
        time.sleep(0.15)

        # Release the game's cursor clip so the mouse can interact with the overlay
        ctypes.windll.user32.ClipCursor(None)

        # Move cursor to top-left corner of the window (safe empty area)
        # and simulate a click to steal focus from the game
        rect = ctypes.wintypes.RECT()
        if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            user32.SetCursorPos(rect.left + 5, rect.top + 5)

        user32.SetForegroundWindow(hwnd)

        MOUSEEVENTF_LEFTDOWN = 0x0002
        MOUSEEVENTF_LEFTUP = 0x0004
        user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

        # Move cursor back to center after the click registers
        time.sleep(0.1)
        if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            user32.SetCursorPos((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)

        ex_style2 = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        print(f"[Overlay] Styles applied (hwnd={hwnd}, topmost={bool(ex_style2 & WS_EX_TOPMOST)}, hidden_taskbar={bool(ex_style2 & WS_EX_TOOLWINDOW)})")

    def _refresh_styles(self, hwnd):
        """Re-apply extended styles and topmost without hiding the window."""
        ex_style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex_style | WS_EX_TOPMOST | WS_EX_TOOLWINDOW)
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)

    def close(self):
        """Close the overlay window."""
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None
            self._hwnd = None
            print("[Overlay] Closed")


class OverlayManager:
    """Coordinates multiple browser overlays — only one can be open at a time."""

    def __init__(self):
        self._overlays = {}  # name -> BrowserOverlay
        self._lock = threading.Lock()

    def register(self, name, overlay):
        """Register a named overlay."""
        self._overlays[name] = overlay

    def toggle(self, name):
        """Toggle a named overlay. Closes any other open overlay first."""
        with self._lock:
            target = self._overlays.get(name)
            if not target:
                return

            if target.is_open:
                target.close()
            else:
                # Close any other open overlay first
                for other_name, other in self._overlays.items():
                    if other_name != name and other.is_open:
                        other.close()
                target.open()

    def close_all(self):
        """Close all open overlays."""
        for overlay in self._overlays.values():
            if overlay.is_open:
                overlay.close()

    def get(self, name):
        """Get a named overlay."""
        return self._overlays.get(name)

    def toggler(self, name):
        """Return a proxy object with a .toggle() method for a named overlay."""
        return _OverlayToggler(self, name)


class _OverlayToggler:
    """Proxy that calls OverlayManager.toggle(name) via .toggle()."""

    def __init__(self, manager, name):
        self._manager = manager
        self._name = name

    def toggle(self):
        self._manager.toggle(self._name)
