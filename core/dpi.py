"""Make the process DPI-aware on Windows.

Without this, Windows lies to a process on scaled displays (e.g. 125%/150%):
it reports *logical* pixels while the screen capture sees *physical* pixels, so
clicks land in the wrong place. Declaring per-monitor awareness makes capture
(mss) and input (pynput) agree in physical pixels. No-op off Windows.

Call once, as early as possible, before creating windows or grabbing the screen.
"""
from __future__ import annotations

import sys

_done = False


def set_dpi_aware() -> None:
    global _done
    if _done or sys.platform != "win32":
        return
    _done = True
    import ctypes
    try:
        # -4 = DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 (Win10 1703+)
        ctypes.windll.user32.SetProcessDpiAwarenessContext(-4)
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor aware
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()  # system-DPI aware (older)
    except Exception:
        pass
