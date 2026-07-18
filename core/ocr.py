"""Text detection via OCR.

Finds where a piece of text appears on screen so a macro can click it even when
it moves -- the text equivalent of detector.py's image matching. Uses RapidOCR
(onnxruntime), which is pip-installable with no external binary:

    pip install rapidocr-onnxruntime

The engine is imported by everything, so the heavy OCR model is loaded lazily --
only the first time a text step actually runs.
"""
from __future__ import annotations

import difflib

import numpy as np

from .detector import Match


def _norm(s: str) -> str:
    return " ".join(s.lower().split())


class TextFinder:
    def __init__(self):
        self._engine = None

    def _ensure(self) -> None:
        if self._engine is not None:
            return
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as e:
            raise RuntimeError(
                "OCR text steps need RapidOCR. Install it with:\n"
                "    pip install rapidocr-onnxruntime"
            ) from e
        self._engine = RapidOCR()

    def find(
        self,
        frame_bgr: np.ndarray,
        text: str,
        min_conf: float = 0.5,
        fuzzy: float = 0.80,
    ) -> Match:
        """Locate `text` in the frame.

        A detected line matches if the target is a substring of it (case- and
        space-insensitive) or a fuzzy ratio clears `fuzzy`. Returns the best
        matching line whose OCR confidence >= min_conf.
        """
        self._ensure()
        target = _norm(text)
        if not target:
            return Match(False, (0, 0), 0.0)

        result, _elapse = self._engine(frame_bgr)
        if not result:
            return Match(False, (0, 0), 0.0)

        best = None  # (score, center)
        for box, line, score in result:
            det = _norm(line)
            hit = target in det or (
                difflib.SequenceMatcher(None, target, det).ratio() >= fuzzy
            )
            if not hit or score < min_conf:
                continue
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            center = (int(sum(xs) / 4), int(sum(ys) / 4))
            if best is None or score > best[0]:
                best = (score, center)

        if best is None:
            return Match(False, (0, 0), 0.0)
        return Match(True, best[1], float(best[0]))

    def read(self, frame_bgr: np.ndarray, min_conf: float = 0.3) -> str:
        """Return all text found in the frame, in reading order (top-to-bottom,
        then left-to-right), space-joined. Used to capture a value into a var."""
        self._ensure()
        result, _elapse = self._engine(frame_bgr)
        if not result:
            return ""
        lines = []
        for box, line, score in result:
            if score < min_conf:
                continue
            ys = [p[1] for p in box]
            xs = [p[0] for p in box]
            lines.append((min(ys), min(xs), line))
        lines.sort(key=lambda t: (round(t[0] / 10), t[1]))  # rough row grouping
        return " ".join(t[2] for t in lines).strip()
