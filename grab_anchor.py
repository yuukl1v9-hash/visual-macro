"""Capture an anchor image by dragging a box over a screenshot.

This is the manual version of what the recorder will later do automatically.
Run it, and a still of your screen opens in a window: drag a box around the
button you want to detect, press ENTER, and it saves the crop.

Usage:
    python grab_anchor.py example/target_button
    # saves assets/example/target_button.png
"""
from __future__ import annotations

import os
import sys

import cv2

from core.capture import Capture

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "assets")


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python grab_anchor.py <name>   e.g. example/target_button")
        return 1
    name = sys.argv[1]
    out = os.path.join(ASSETS, name)
    if not out.lower().endswith(".png"):
        out += ".png"
    os.makedirs(os.path.dirname(out), exist_ok=True)

    print("Grabbing screen in 2 seconds -- switch to the target window...")
    cv2.waitKey(1)
    import time
    time.sleep(2)

    frame = Capture().grab()
    print("Drag a box around the button, then press ENTER (or C to cancel).")
    roi = cv2.selectROI("grab anchor - drag a box, ENTER to save", frame,
                        showCrosshair=False)
    cv2.destroyAllWindows()

    x, y, w, h = roi
    if w == 0 or h == 0:
        print("Cancelled (no region selected).")
        return 1

    crop = frame[y:y + h, x:x + w]
    cv2.imwrite(out, crop)
    print(f"Saved {out}  ({w}x{h})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
