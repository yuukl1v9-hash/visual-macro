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


class Detector:
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
        self, frame_bgr: np.ndarray, template_name: str, threshold: float = 0.80
    ) -> Match:
        """Locate template_name inside frame_bgr.

        Returns the best match; found=True only if confidence >= threshold.
        Grayscale + TM_CCOEFF_NORMED: fast and robust to minor color shifts.
        """
        tmpl = self._load_template(template_name)
        frame_gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)

        th, tw = tmpl.shape[:2]
        fh, fw = frame_gray.shape[:2]
        if th > fh or tw > fw:
            return Match(False, (0, 0), 0.0)

        res = cv2.matchTemplate(frame_gray, tmpl, cv2.TM_CCOEFF_NORMED)
        _min_val, max_val, _min_loc, max_loc = cv2.minMaxLoc(res)

        cx = max_loc[0] + tw // 2
        cy = max_loc[1] + th // 2
        return Match(max_val >= threshold, (cx, cy), float(max_val))
