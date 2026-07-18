"""Input actuation: move the mouse, click, type.

Uses pynput. Two habits baked in that make macros reliable:
  * click the CENTER of the matched box, not a corner;
  * move-then-pause-then-click, because instant teleport-clicks get dropped by
    some UIs.
"""
from __future__ import annotations

import time

from pynput.keyboard import Controller as KeyController
from pynput.keyboard import Key
from pynput.mouse import Button
from pynput.mouse import Controller as MouseController


class Actuator:
    def __init__(self, move_settle: float = 0.04):
        self._mouse = MouseController()
        self._kbd = KeyController()
        self._move_settle = move_settle  # pause between arriving and clicking

    def move(self, x: int, y: int) -> None:
        self._mouse.position = (int(x), int(y))

    def click(self, x: int, y: int, button: str = "left", clicks: int = 1) -> None:
        self.move(x, y)
        time.sleep(self._move_settle)  # let the target register the hover
        btn = Button.right if button == "right" else Button.left
        for _ in range(clicks):
            self._mouse.click(btn, 1)
            time.sleep(0.03)

    def type_text(self, text: str, per_char: float = 0.01) -> None:
        for ch in text:
            self._kbd.type(ch)
            if per_char:
                time.sleep(per_char)

    def hotkey(self, *keys: str) -> None:
        """Press a chord, e.g. hotkey('ctrl', 's'). Named keys map to pynput Key."""
        resolved = [self._resolve(k) for k in keys]
        for k in resolved:
            self._kbd.press(k)
        for k in reversed(resolved):
            self._kbd.release(k)

    @staticmethod
    def _resolve(k: str):
        k = k.lower()
        special = {
            "ctrl": Key.ctrl, "control": Key.ctrl,
            "alt": Key.alt, "shift": Key.shift,
            "win": Key.cmd, "cmd": Key.cmd,
            "enter": Key.enter, "return": Key.enter,
            "tab": Key.tab, "esc": Key.esc, "escape": Key.esc,
            "space": Key.space, "backspace": Key.backspace,
            "delete": Key.delete, "up": Key.up, "down": Key.down,
            "left": Key.left, "right": Key.right,
        }
        if k in special:
            return special[k]
        if k.startswith("f") and k[1:].isdigit():
            return getattr(Key, k)  # f1..f12
        return k  # a literal character
