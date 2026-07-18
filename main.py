"""Entry point: load a macro JSON and run it, with a global F12 panic key.

Usage:
    python main.py                      # runs macros/example.json
    python main.py macros/mine.json     # runs a specific macro

Press F12 at any time to hard-stop. Slam the mouse into a screen corner to
trigger pynput's own failsafe as a backup.
"""
from __future__ import annotations

import os
import sys
import threading

from pynput import keyboard

from core.dpi import set_dpi_aware
from core.engine import Engine, Macro

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "assets")
PANIC_KEY = keyboard.Key.f12


def main() -> int:
    set_dpi_aware()
    macro_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        HERE, "macros", "example.json"
    )
    if not os.path.exists(macro_path):
        print(f"Macro not found: {macro_path}")
        return 1

    macro = Macro.load(macro_path)

    # Anchor images for a macro live in assets/<macro-folder>/, but we point the
    # detector at assets/ and let macro steps name paths relative to it.
    panic = threading.Event()

    def on_press(key):
        if key == PANIC_KEY:
            panic.set()
            print("\n[main] F12 -> PANIC")
            return False  # stop the listener

    listener = keyboard.Listener(on_press=on_press)
    listener.start()

    print("=" * 56)
    print(f" Running: {macro.name}")
    print(" Press F12 to STOP at any time.")
    print("=" * 56)

    engine = Engine(assets_dir=ASSETS, panic=panic)
    engine.run(macro)

    listener.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
