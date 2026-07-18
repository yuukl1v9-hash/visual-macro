"""ML object detection (learned button/UI detector).

Runs a YOLOv8-style ONNX object-detection model over a screen frame and returns
where a requested class (e.g. "button", "checkbox") appears. Unlike template
matching, this finds elements that *change appearance* -- different labels,
themes, sizes -- as long as the model was trained to recognize the class.

Model-agnostic: point it at any YOLOv8 `.onnx` export. Class names come from a
sidecar text file next to the model (one name per line) if present; otherwise
targets are matched by numeric class index.

Dependencies (optional, installed only if you use object steps):
    pip install onnxruntime      # (already present if you installed OCR)

See models/README.md for how to get or train a model.
"""
from __future__ import annotations

import os

import cv2
import numpy as np

from .detector import Match


class ObjectDetector:
    def __init__(self, model_path: str, imgsz: int = 640, iou: float = 0.45):
        self.model_path = model_path
        self.imgsz = imgsz
        self.iou = iou
        self._sess = None
        self._input_name = None
        self._names: list[str] | None = None

    # -- setup -------------------------------------------------------------
    def _ensure(self) -> None:
        if self._sess is not None:
            return
        if not os.path.exists(self.model_path):
            raise RuntimeError(f"Model not found: {self.model_path}")
        try:
            import onnxruntime as ort
        except ImportError as e:
            raise RuntimeError(
                "Object steps need onnxruntime. Install it with:\n"
                "    pip install onnxruntime"
            ) from e
        self._sess = ort.InferenceSession(
            self.model_path, providers=["CPUExecutionProvider"])
        self._input_name = self._sess.get_inputs()[0].name
        self._names = self._load_names()

    def _load_names(self) -> list[str] | None:
        base = os.path.splitext(self.model_path)[0]
        for cand in (base + ".names", base + ".txt",
                     os.path.join(os.path.dirname(self.model_path), "classes.txt")):
            if os.path.exists(cand):
                with open(cand, "r", encoding="utf-8") as f:
                    return [ln.strip() for ln in f if ln.strip()]
        return None

    # -- inference ---------------------------------------------------------
    def _letterbox(self, img: np.ndarray):
        h, w = img.shape[:2]
        r = min(self.imgsz / h, self.imgsz / w)
        nh, nw = int(round(h * r)), int(round(w * r))
        resized = cv2.resize(img, (nw, nh))
        canvas = np.full((self.imgsz, self.imgsz, 3), 114, dtype=np.uint8)
        top, left = (self.imgsz - nh) // 2, (self.imgsz - nw) // 2
        canvas[top:top + nh, left:left + nw] = resized
        return canvas, r, left, top

    def detect(self, frame_bgr: np.ndarray, min_conf: float):
        """Return a list of (class_idx, name, confidence, (x1,y1,x2,y2)) in the
        frame's own coordinate space."""
        self._ensure()
        canvas, r, left, top = self._letterbox(frame_bgr)
        blob = canvas[:, :, ::-1].transpose(2, 0, 1)[None].astype(np.float32) / 255.0
        out = self._sess.run(None, {self._input_name: blob})[0]

        preds = out[0]
        if preds.shape[0] < preds.shape[1]:  # (C, N) -> (N, C)
            preds = preds.T
        boxes = preds[:, :4]
        scores_all = preds[:, 4:]
        class_ids = scores_all.argmax(1)
        confs = scores_all.max(1)

        keep = confs >= min_conf
        boxes, class_ids, confs = boxes[keep], class_ids[keep], confs[keep]
        if len(boxes) == 0:
            return []

        # cx,cy,w,h (letterbox space) -> xywh top-left for NMS
        xywh = []
        for cx, cy, w, h in boxes:
            xywh.append([cx - w / 2, cy - h / 2, w, h])
        idxs = cv2.dnn.NMSBoxes(xywh, confs.tolist(), min_conf, self.iou)
        if len(idxs) == 0:
            return []
        idxs = np.array(idxs).flatten()

        results = []
        for i in idxs:
            cx, cy, w, h = boxes[i]
            # undo letterbox back to original frame coords
            x1 = (cx - w / 2 - left) / r
            y1 = (cy - h / 2 - top) / r
            x2 = (cx + w / 2 - left) / r
            y2 = (cy + h / 2 - top) / r
            ci = int(class_ids[i])
            name = self._names[ci] if self._names and ci < len(self._names) else str(ci)
            results.append((ci, name, float(confs[i]),
                            (int(x1), int(y1), int(x2), int(y2))))
        return results

    # -- query -------------------------------------------------------------
    def _match_class(self, target: str):
        """Return the class index to look for, None for 'any class', or -1 for
        an unknown name (matches nothing)."""
        t = (target or "").strip().lower()
        if t in ("", "*", "any"):
            return None
        if t.isdigit():
            return int(t)
        if self._names:
            for i, n in enumerate(self._names):
                if n.strip().lower() == t:
                    return i
        return -1

    def find(self, frame_bgr: np.ndarray, target: str, min_conf: float = 0.5) -> Match:
        """Find the highest-confidence detection whose class matches `target`
        and return the center of its box."""
        self._ensure()
        want = self._match_class(target)
        if want == -1:
            return Match(False, (0, 0), 0.0)  # named a class the model doesn't have
        best = None
        for ci, _name, conf, (x1, y1, x2, y2) in self.detect(frame_bgr, min_conf):
            if want is not None and ci != want:
                continue
            if best is None or conf > best[0]:
                best = (conf, ((x1 + x2) // 2, (y1 + y2) // 2))
        if best is None:
            return Match(False, (0, 0), 0.0)
        return Match(True, best[1], best[0])
