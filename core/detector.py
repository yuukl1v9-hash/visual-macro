"""Image detection via OpenCV template matching.

Given an anchor image (a small PNG grabbed around a click) and a screen frame,
find where the anchor currently is and how confident the match is. This is what
makes the macro resilient: TinyTask replays a fixed (x, y); we find the button
wherever it moved to.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class Match:
    found: bool
    center: tuple[int, int]  # (x, y) in the coordinate space of the frame passed in
    confidence: float
    second: float = 0.0      # score of the next-best, far-away match
    ambiguous: bool = False  # a look-alike scored nearly as high -> risky click
    box: tuple[int, int, int, int] | None = None  # (x1, y1, x2, y2) of best match


class Detector:
    # If the runner-up scores within this margin of the winner, the match is
    # "ambiguous" -- there's a look-alike and clicking the best is risky.
    AMBIGUITY_MARGIN = 0.05

    def __init__(self, assets_dir: str):
        self.assets_dir = assets_dir
        self._cache: dict[str, np.ndarray] = {}

    def _load_template(self, name: str) -> np.ndarray:
        if name in self._cache:
            return self._cache[name]
        path = name if os.path.isabs(name) else os.path.join(self.assets_dir, name)
        if not path.lower().endswith((".png", ".jpg", ".jpeg", ".bmp")):
            path += ".png"
        img = cv2.imread(path, cv2.IMREAD_COLOR)
        if img is None:
            raise FileNotFoundError(f"Anchor image not found: {path}")
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        self._cache[name] = gray
        return gray

    def find(
        self,
        frame_bgr: np.ndarray,
        template_name: str,
        threshold: float = 0.80,
        multi_scale: bool = False,
        edges: bool = False,
    ) -> Match:
        """Locate template_name inside frame_bgr.

        Returns the best match; found=True only if confidence >= threshold.
        Also reports the next-best far-away match so the caller can tell a
        confident, unique hit from an ambiguous one (two look-alikes).
        Grayscale + TM_CCOEFF_NORMED: fast and robust to minor color shifts.
        multi_scale retries a few template sizes for DPI/resolution changes.
        edges matches on Canny outlines instead of pixels -- robust to theme /
        colour changes (dark vs light mode) where the shape stays the same.
        """
        tmpl = self._load_template(template_name)
        frame_gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
        if edges:
            frame_gray = cv2.Canny(frame_gray, 50, 150)
        fh, fw = frame_gray.shape[:2]

        scales = (1.0,) if not multi_scale else (1.0, 0.9, 1.1, 0.8, 1.25, 0.67, 1.5)
        best = None  # (score, loc, tw, th, result_map)
        for s in scales:
            t = tmpl
            if s != 1.0:
                nw, nh = max(8, int(tmpl.shape[1] * s)), max(8, int(tmpl.shape[0] * s))
                t = cv2.resize(tmpl, (nw, nh))
            if edges:
                t = cv2.Canny(t, 50, 150)
            th, tw = t.shape[:2]
            if th > fh or tw > fw:
                continue
            res = cv2.matchTemplate(frame_gray, t, cv2.TM_CCOEFF_NORMED)
            _mn, mx, _ml, mloc = cv2.minMaxLoc(res)
            if best is None or mx > best[0]:
                best = (mx, mloc, tw, th, res)

        if best is None:
            return Match(False, (0, 0), 0.0)

        score, loc, tw, th, res = best
        cx, cy = loc[0] + tw // 2, loc[1] + th // 2
        box = (loc[0], loc[1], loc[0] + tw, loc[1] + th)

        # Second-best: blank out a template-sized area around the winner, re-peak.
        second = 0.0
        x0 = max(0, loc[0] - tw // 2)
        y0 = max(0, loc[1] - th // 2)
        x1 = min(res.shape[1], loc[0] + tw // 2 + 1)
        y1 = min(res.shape[0], loc[1] + th // 2 + 1)
        masked = res.copy()
        masked[y0:y1, x0:x1] = -1.0
        if masked.size:
            second = float(masked.max())

        ambiguous = (score >= threshold and second >= threshold
                     and (score - second) < self.AMBIGUITY_MARGIN)
        return Match(score >= threshold, (cx, cy), float(score),
                     second=second, ambiguous=ambiguous, box=box)
