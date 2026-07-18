"""Record a task by doing it once.

Run it, do the task by hand, press F10 to stop. It watches your real mouse and
keyboard globally and writes a macro:

  * each CLICK  -> grabs a small screenshot AROUND the click and saves it as an
                   anchor image, then emits a `find_click` step. On playback the
                   macro finds that image wherever it is -- this is the whole
                   point vs TinyTask's fixed coordinates.
  * TYPING      -> merged into a single `type` step.
  * ctrl/alt/win + key -> a `hotkey` step (e.g. ctrl+s).
  * enter/tab/esc/etc.  -> a `hotkey` step.
  * long pauses -> a `wait` step, so playback keeps your natural pacing.

Usage:
    python recorder.py my-macro
    # writes macros/my-macro.json  and  assets/my-macro/step_NN.png

Controls while recording:
    F10  = stop and save         (this key is never recorded)
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time

import cv2

from pynput import keyboard, mouse
from pynput.keyboard import Key, KeyCode

from core.capture import Capture
from core.dpi import set_dpi_aware

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "assets")
MACROS = os.path.join(HERE, "macros")

STOP_KEY = Key.f10
BOX_W, BOX_H = 180, 70          # anchor box captured around each click (pixels)
WAIT_THRESHOLD = 0.75          # gaps longer than this become a `wait` step
WAIT_MAX = 15.0                # never emit a wait longer than this

_MODIFIERS = {
    Key.ctrl: "ctrl", Key.ctrl_l: "ctrl", Key.ctrl_r: "ctrl",
    Key.alt: "alt", Key.alt_l: "alt", Key.alt_r: "alt", Key.alt_gr: "alt",
    Key.shift: "shift", Key.shift_l: "shift", Key.shift_r: "shift",
    Key.cmd: "win", Key.cmd_l: "win", Key.cmd_r: "win",
}


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_") or "macro"


class Recorder:
    def __init__(self, name: str, log=print):
        set_dpi_aware()
        self.log = log
        self.name = _safe(name)
        self.asset_dir = os.path.join(ASSETS, self.name)
        os.makedirs(self.asset_dir, exist_ok=True)

        self.capture = Capture()
        self._ox, self._oy = self.capture.region_origin(None)  # screen->frame offset

        self.steps: list[dict] = []
        self._held: set[str] = set()
        self._text_buf = ""
        self._text_start_t: float | None = None
        self._last_t: float | None = None
        self._click_idx = 0

        self._stop = threading.Event()
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        self.log("=" * 56)
        self.log(f" Recording macro: {self.name}")
        self.log(" Do your task now. Press F10 to STOP and save.")
        self.log("=" * 56)

        m_listener = mouse.Listener(on_click=self._on_click)
        k_listener = keyboard.Listener(
            on_press=self._on_press, on_release=self._on_release
        )
        m_listener.start()
        k_listener.start()

        self._stop.wait()  # blocks until F10

        m_listener.stop()
        k_listener.stop()
        self._flush_text()
        self._save()

    # -- mouse -------------------------------------------------------------
    def _on_click(self, x: int, y: int, button, pressed: bool) -> None:
        if not pressed or self._stop.is_set():
            return
        with self._lock:
            self._flush_text()
            t = time.perf_counter()
            self._maybe_wait(t)

            anchor = self._grab_anchor(x, y)
            if anchor is None:
                # Fall back to a fixed-coordinate click if the grab failed.
                self._append({
                    "action": "click",
                    "args": {"x": int(x), "y": int(y),
                             "button": "right" if button == mouse.Button.right else "left"},
                })
                self._last_t = t
                return

            self._click_idx += 1
            fname = f"step_{self._click_idx:02d}.png"
            cv2.imwrite(os.path.join(self.asset_dir, fname), anchor)
            target = f"{self.name}/{fname}"
            self._append({
                "action": "find_click",
                "target": target,
                "timeout": 10.0,
                "threshold": 0.80,
                "on_fail": "abort",
                "args": {"button": "right" if button == mouse.Button.right else "left"},
            })
            self._last_t = t
            self.log(f"  + click  -> {target}")

    def _grab_anchor(self, x: int, y: int):
        try:
            frame = self.capture.grab()  # full screen, BGR
        except Exception as e:
            self.log(f"  ! anchor grab failed: {e}")
            return None
        h, w = frame.shape[:2]
        # screen coords -> frame-local coords
        fx, fy = x - self._ox, y - self._oy
        x0 = max(0, fx - BOX_W // 2)
        y0 = max(0, fy - BOX_H // 2)
        x1 = min(w, fx + BOX_W // 2)
        y1 = min(h, fy + BOX_H // 2)
        if x1 - x0 < 8 or y1 - y0 < 8:
            return None
        return frame[y0:y1, x0:x1].copy()

    # -- keyboard ----------------------------------------------------------
    def _on_press(self, key):
        if key == STOP_KEY:
            self._stop.set()
            return False  # stop the keyboard listener
        with self._lock:
            mod = _MODIFIERS.get(key)
            if mod:
                self._held.add(mod)
                return

            t = time.perf_counter()
            active = self._held - {"shift"}  # shift is folded into the char

            if active:  # a real chord: ctrl/alt/win + something
                self._flush_text()
                self._maybe_wait(t)
                combo = sorted(active) + [self._token(key)]
                self._append({"action": "hotkey", "args": {"keys": combo}})
                self._last_t = t
                self.log(f"  + hotkey -> {'+'.join(combo)}")
                return

            ch = getattr(key, "char", None)
            if ch is not None and len(ch) == 1 and ch.isprintable():
                if not self._text_buf:
                    self._text_start_t = t
                self._text_buf += ch
                return

            if key == Key.space:
                if not self._text_buf:
                    self._text_start_t = t
                self._text_buf += " "
                return

            # any other special key -> its own hotkey step
            self._flush_text()
            self._maybe_wait(t)
            self._append({"action": "hotkey", "args": {"keys": [self._token(key)]}})
            self._last_t = t
            self.log(f"  + key    -> {self._token(key)}")

    def _on_release(self, key):
        mod = _MODIFIERS.get(key)
        if mod:
            with self._lock:
                self._held.discard(mod)

    @staticmethod
    def _token(key) -> str:
        if isinstance(key, KeyCode):
            if key.char is not None and len(key.char) == 1:
                c = key.char
                if ord(c) < 32:            # ctrl+letter arrives as a control char
                    return chr(ord(c) + 96)  # \x13 -> 's'
                return c.lower()
            if key.vk is not None and 32 <= key.vk < 127:
                return chr(key.vk).lower()
            return "?"
        return str(key).replace("Key.", "")

    # -- step assembly -----------------------------------------------------
    def _flush_text(self) -> None:
        if not self._text_buf:
            return
        text = self._text_buf
        self._text_buf = ""
        self._maybe_wait(self._text_start_t)
        self._append({"action": "type", "target": text})
        self._last_t = self._text_start_t
        preview = text if len(text) <= 30 else text[:27] + "..."
        self.log(f"  + type   -> {preview!r}")

    def _maybe_wait(self, t: float | None) -> None:
        if t is None or self._last_t is None:
            return
        gap = t - self._last_t
        if gap > WAIT_THRESHOLD:
            self._append({"action": "wait",
                          "args": {"seconds": round(min(gap, WAIT_MAX), 1)}})

    def _append(self, step: dict) -> None:
        self.steps.append(step)

    # -- output ------------------------------------------------------------
    def _save(self) -> None:
        path = os.path.join(MACROS, f"{self.name}.json")
        os.makedirs(MACROS, exist_ok=True)
        macro = {"name": self.name, "repeat": 1, "region": None, "steps": self.steps}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(macro, f, indent=2)
        n_clicks = sum(1 for s in self.steps if s["action"] == "find_click")
        self.log("=" * 56)
        self.log(f" Saved {len(self.steps)} steps ({n_clicks} clicks) -> {path}")
        self.log(f" Anchor images -> {self.asset_dir}")
        self.log(f" Play it with:  python main.py macros/{self.name}.json")
        self.log("=" * 56)


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python recorder.py <macro-name>")
        return 1
    Recorder(sys.argv[1]).start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
