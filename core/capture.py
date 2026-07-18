"""Screen capture.

Grabs the screen (or a sub-region) as a BGR numpy array that OpenCV can use
directly. Prefers `dxcam` if it's installed (fast, Windows-only) and falls back
to `mss` (slower but zero-fuss and cross-platform).

A macro does not need 240Hz -- mss at ~30-60 grabs/sec is plenty. Install dxcam
later only if the histogram tells you capture is your bottleneck.
"""
from __future__ import annotations

import numpy as np


class Capture:
    def __init__(self, monitor: int | None = None):
        """monitor: None (default) = the whole virtual desktop (all monitors),
        so frame coordinates line up 1:1 with pynput's click coordinates and
        multi-monitor setups work without per-screen offset math. Pass an int to
        grab a specific mss monitor index instead. dxcam uses output 0."""
        self._monitor = monitor
        self._backend = None
        self._dxcam = None
        self._mss = None
        self._mss_mon = None
        self._init_backend()

    def _init_backend(self) -> None:
        # Try the fast path first.
        try:
            import dxcam  # type: ignore

            self._dxcam = dxcam.create(output_idx=0, output_color="BGR")
            if self._dxcam is not None:
                self._backend = "dxcam"
                return
        except Exception:
            self._dxcam = None

        # Fall back to mss.
        import mss  # imported lazily so dxcam-only setups don't need it

        self._mss = mss.mss()
        # mss.monitors[0] is the "all monitors" virtual screen; [1..] are the
        # individual displays. Default (monitor=None) grabs the full virtual
        # desktop so frame coords match pynput's virtual-screen coords exactly.
        idx = 0 if self._monitor is None else self._monitor
        self._mss_mon = self._mss.monitors[idx]
        self._backend = "mss"

    @property
    def backend(self) -> str:
        return self._backend or "none"

    def grab(self, region: tuple[int, int, int, int] | None = None) -> np.ndarray:
        """Return a BGR frame.

        region = (left, top, width, height) in screen pixels, or None for the
        whole monitor. Returning None from the backend (no new frame yet) is
        retried transparently.
        """
        if self._backend == "dxcam":
            frame = None
            # dxcam returns None if no NEW frame since last grab; spin briefly.
            for _ in range(64):
                if region is not None:
                    left, top, w, h = region
                    frame = self._dxcam.grab(region=(left, top, left + w, top + h))
                else:
                    frame = self._dxcam.grab()
                if frame is not None:
                    break
            if frame is None:
                raise RuntimeError("dxcam returned no frame")
            return np.ascontiguousarray(frame)

        # mss path
        if region is not None:
            left, top, w, h = region
            mon = {"left": left, "top": top, "width": w, "height": h}
        else:
            mon = self._mss_mon
        raw = self._mss.grab(mon)
        # mss gives BGRA; drop alpha -> BGR
        return np.array(raw)[:, :, :3]

    def region_origin(
        self, region: tuple[int, int, int, int] | None
    ) -> tuple[int, int]:
        """Screen-space (x, y) offset of a captured region, so match coords
        inside a cropped grab can be converted back to absolute click points."""
        if region is not None:
            return region[0], region[1]
        if self._backend == "mss" and self._mss_mon is not None:
            return self._mss_mon["left"], self._mss_mon["top"]
        return 0, 0
