"""visual-macro — desktop UI.

A Tkinter front end over the core engine + recorder. No extra installs (tkinter
ships with Python). Record a task, see the steps as an editable list, reorder or
tweak them, then play with an optional loop count. F12 stops a run instantly.

Run:
    python ui/app.py
"""
from __future__ import annotations

import os
import queue
import sys
import threading

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from core.engine import Engine, Macro, Step  # noqa: E402
import recorder as recorder_mod  # noqa: E402

from pynput import keyboard  # noqa: E402

ASSETS = os.path.join(ROOT, "assets")
MACROS = os.path.join(ROOT, "macros")


def _describe_cond(cond: dict) -> str:
    cond = cond or {}
    neg = "NOT " if cond.get("negate") else ""
    ctype = cond.get("type", "image")
    if ctype == "var":
        name = cond.get("name") or cond.get("target", "")
        op = cond.get("op", "set")
        if op in ("set", "empty"):
            return f"{neg}var {name} {op}"
        return f'{neg}var {name} {op} "{cond.get("value", "")}"'
    return f'{neg}{ctype} "{cond.get("target", "")}" on screen'


def describe(step: dict) -> tuple[str, str]:
    """Return (action label, human detail) for a step dict."""
    a = step.get("action", "?")
    args = step.get("args", {}) or {}
    if a == "find_click":
        btn = args.get("button", "left")
        extra = "" if btn == "left" else f" ({btn})"
        return "Click image", f"{step.get('target', '')}{extra}"
    if a == "wait_for":
        return "Wait for image", step.get("target", "")
    if a == "find_text_click":
        btn = args.get("button", "left")
        extra = "" if btn == "left" else f" ({btn})"
        return "Click text", f'"{step.get("target", "")}"{extra}'
    if a == "wait_for_text":
        return "Wait for text", f'"{step.get("target", "")}"'
    if a == "find_object_click":
        btn = args.get("button", "left")
        extra = "" if btn == "left" else f" ({btn})"
        return "Click object", f'"{step.get("target", "")}"{extra}'
    if a == "wait_for_object":
        return "Wait for object", f'"{step.get("target", "")}"'
    if a == "click":
        return "Click at", f"({args.get('x')}, {args.get('y')})"
    if a == "type":
        t = step.get("target", "")
        return "Type", t if len(t) <= 40 else t[:37] + "..."
    if a == "hotkey":
        return "Press", "+".join(args.get("keys", []))
    if a == "wait":
        return "Wait", f"{args.get('seconds', 1)}s"
    if a == "if":
        return "If", _describe_cond(args.get("cond", {}))
    if a == "else":
        return "Else", ""
    if a == "end_if":
        return "End if", ""
    if a == "loop":
        cnt = args.get("count", 0)
        mode = args.get("mode", "")
        cond = args.get("cond", {}) or {}
        parts = []
        if mode in ("while", "until") and (cond.get("target") or cond.get("name")):
            parts.append(f"{mode} {_describe_cond(cond)}")
        try:
            if int(cnt) > 0:
                parts.append(f"max {cnt}x")
        except (TypeError, ValueError):
            pass
        return "Loop", ", ".join(parts) if parts else "forever (until break)"
    if a == "end_loop":
        return "End loop", ""
    if a == "break":
        return "Break", ""
    if a == "continue":
        return "Continue", ""
    if a == "call":
        rep = args.get("repeat", 1)
        suffix = f" (x{rep})" if rep and rep != 1 else ""
        return "Call macro", f'{step.get("target", "")}{suffix}'
    if a == "set":
        op = args.get("op", "assign")
        name = step.get("target", "")
        val = args.get("value", "")
        return "Set var", (f"{name} = {val}" if op == "assign"
                           else f"{name} {op} {val}")
    if a == "read_text":
        return "Read text", f'-> {step.get("target", "")}'
    return a, str(step)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("visual-macro")
        self.geometry("640x560")
        self.minsize(560, 460)

        self.steps: list[dict] = []
        self.macro_name = "untitled"
        self.region = None

        self._log_q: "queue.Queue[str]" = queue.Queue()
        self._cmd_q: "queue.Queue" = queue.Queue()  # UI callbacks from workers
        self.panic = threading.Event()
        self._running = False
        self._recording = False

        self._build_ui()
        self._start_panic_listener()
        self.after(80, self._drain_log)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.log("Ready. Record a task or open a macro.")

    # -- layout ------------------------------------------------------------
    def _build_ui(self) -> None:
        bar = ttk.Frame(self, padding=(8, 6))
        bar.pack(fill="x")

        self.btn_record = ttk.Button(bar, text="● Record", command=self.on_record)
        self.btn_play = ttk.Button(bar, text="▶ Play", command=self.on_play)
        self.btn_stop = ttk.Button(bar, text="■ Stop", command=self.on_stop,
                                   state="disabled")
        self.btn_record.pack(side="left")
        self.btn_play.pack(side="left", padx=(6, 0))
        self.btn_stop.pack(side="left", padx=(6, 12))

        ttk.Label(bar, text="Loop:").pack(side="left")
        self.loop_var = tk.StringVar(value="1")
        ttk.Spinbox(bar, from_=0, to=9999, width=5, textvariable=self.loop_var).pack(
            side="left", padx=(2, 0))
        ttk.Label(bar, text="(0 = forever)").pack(side="left", padx=(4, 0))

        filebar = ttk.Frame(self, padding=(8, 0))
        filebar.pack(fill="x")
        ttk.Button(filebar, text="New", command=self.on_new).pack(side="left")
        ttk.Button(filebar, text="Open…", command=self.on_open).pack(
            side="left", padx=(6, 0))
        ttk.Button(filebar, text="Save…", command=self.on_save).pack(
            side="left", padx=(6, 0))
        self.name_lbl = ttk.Label(filebar, text="untitled")
        self.name_lbl.pack(side="right")

        # step list
        mid = ttk.Frame(self, padding=(8, 6))
        mid.pack(fill="both", expand=True)

        cols = ("num", "action", "detail")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", height=12)
        self.tree.heading("num", text="#")
        self.tree.heading("action", text="Action")
        self.tree.heading("detail", text="Detail")
        self.tree.column("num", width=36, anchor="center", stretch=False)
        self.tree.column("action", width=130, stretch=False)
        self.tree.column("detail", width=380)
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda e: self.on_edit())

        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        sb.pack(side="left", fill="y")
        self.tree.configure(yscrollcommand=sb.set)

        side = ttk.Frame(mid, padding=(6, 0))
        side.pack(side="left", fill="y")
        for text, cmd in [
            ("▲ Up", self.on_up), ("▼ Down", self.on_down),
            ("Edit", self.on_edit), ("Delete", self.on_delete),
            ("+ Add", self.on_add),
        ]:
            ttk.Button(side, text=text, width=9, command=cmd).pack(pady=2)

        # log
        logframe = ttk.Frame(self, padding=(8, 4))
        logframe.pack(fill="both")
        self.log_txt = tk.Text(logframe, height=8, state="disabled", wrap="word",
                               font=("Consolas", 9))
        self.log_txt.pack(fill="both", expand=True)

        self.status = ttk.Label(self, text="", anchor="w", padding=(8, 2),
                                relief="sunken")
        self.status.pack(fill="x")

    # -- logging (thread-safe) --------------------------------------------
    def log(self, msg: str) -> None:
        self._log_q.put(str(msg))

    def post(self, fn) -> None:
        """Schedule a callable to run on the Tk main thread (from any thread)."""
        self._cmd_q.put(fn)

    def _drain_log(self) -> None:
        try:
            while True:
                msg = self._log_q.get_nowait()
                self.log_txt.configure(state="normal")
                self.log_txt.insert("end", msg + "\n")
                self.log_txt.see("end")
                self.log_txt.configure(state="disabled")
        except queue.Empty:
            pass
        try:
            while True:
                fn = self._cmd_q.get_nowait()
                try:
                    fn()
                except Exception as e:
                    self.log(f"UI error: {e}")
        except queue.Empty:
            pass
        self.after(80, self._drain_log)

    def set_status(self, text: str) -> None:
        self.status.configure(text=text)

    # -- step list rendering ----------------------------------------------
    def refresh(self) -> None:
        sel = self.tree.selection()
        keep = self.tree.index(sel[0]) if sel else None
        self.tree.delete(*self.tree.get_children())
        depth = 0
        for i, step in enumerate(self.steps, 1):
            act = step.get("action")
            action, detail = describe(step)
            # indent to show if/else/end_if and loop/end_loop nesting
            if act in ("end_if", "end_loop"):
                depth = max(0, depth - 1)
            level = depth - 1 if (act == "else" and depth > 0) else depth
            action = ("    " * level) + action
            self.tree.insert("", "end", iid=str(i - 1),
                             values=(i, action, detail))
            if act in ("if", "loop"):
                depth += 1
        if keep is not None and self.steps:
            iid = str(min(keep, len(self.steps) - 1))
            self.tree.selection_set(iid)
            self.tree.focus(iid)
        self.name_lbl.configure(text=self.macro_name)
        self.set_status(f"{len(self.steps)} steps")

    def _selected_index(self) -> int | None:
        sel = self.tree.selection()
        return int(sel[0]) if sel else None

    # -- record ------------------------------------------------------------
    def on_record(self) -> None:
        if self._running or self._recording:
            return
        name = simpledialog.askstring(
            "Record macro", "Name for this macro:", initialvalue="my-macro",
            parent=self)
        if not name:
            return
        self._recording = True
        self._set_busy(True)
        self.log(f"Recording '{name}' — do your task, press F10 to stop.")
        self.set_status("Recording…  (window minimized; press F10 to stop)")
        self.iconify()  # get the UI out of the way so clicks don't hit it

        def worker():
            try:
                rec = recorder_mod.Recorder(name, log=self.log)
                rec.start()  # blocks until F10
                path = os.path.join(MACROS, f"{rec.name}.json")
                self.post(lambda: self._load_macro(path))
            except Exception as e:
                self.post(lambda e=e: self.log(f"Record error: {e}"))
            finally:
                self.post(self._after_record)

        threading.Thread(target=worker, daemon=True).start()

    def _after_record(self) -> None:
        self._recording = False
        self._set_busy(False)
        self.deiconify()
        self.lift()

    # -- play / stop -------------------------------------------------------
    def on_play(self) -> None:
        if self._running or self._recording:
            return
        if not self.steps:
            messagebox.showinfo("Nothing to play", "This macro has no steps yet.")
            return
        try:
            repeat = int(self.loop_var.get())
        except ValueError:
            repeat = 1

        macro = Macro(
            name=self.macro_name,
            steps=[Step(**s) for s in self.steps],
            repeat=repeat,
            region=tuple(self.region) if self.region else None,
        )
        self.panic.clear()
        self._running = True
        self._set_busy(True)
        self.btn_stop.configure(state="normal")
        self.set_status("Playing…  (F12 or Stop to abort)")

        def worker():
            try:
                engine = Engine(assets_dir=ASSETS, panic=self.panic, log=self.log)
                engine.run(macro)
            except Exception as e:
                self.post(lambda e=e: self.log(f"Play error: {e}"))
            finally:
                self.post(self._after_play)

        threading.Thread(target=worker, daemon=True).start()

    def _after_play(self) -> None:
        self._running = False
        self._set_busy(False)
        self.btn_stop.configure(state="disabled")
        self.set_status(f"{len(self.steps)} steps")

    def on_stop(self) -> None:
        if self._running:
            self.panic.set()
            self.log("Stop requested.")

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self.btn_record.configure(state=state)
        self.btn_play.configure(state=state)

    # -- edit operations ---------------------------------------------------
    def on_up(self) -> None:
        i = self._selected_index()
        if i is None or i == 0:
            return
        self.steps[i - 1], self.steps[i] = self.steps[i], self.steps[i - 1]
        self.refresh()
        self.tree.selection_set(str(i - 1))

    def on_down(self) -> None:
        i = self._selected_index()
        if i is None or i >= len(self.steps) - 1:
            return
        self.steps[i + 1], self.steps[i] = self.steps[i], self.steps[i + 1]
        self.refresh()
        self.tree.selection_set(str(i + 1))

    def on_delete(self) -> None:
        i = self._selected_index()
        if i is None:
            return
        del self.steps[i]
        self.refresh()

    def on_add(self) -> None:
        action = _choose_action(self)
        if not action:
            return
        step = _default_step(action)
        self.steps.append(step)
        self.refresh()
        self.tree.selection_set(str(len(self.steps) - 1))
        # markers have nothing to edit; everything else opens the editor
        if action not in ("else", "end_if", "end_loop", "break", "continue"):
            self.on_edit()

    def on_edit(self) -> None:
        i = self._selected_index()
        if i is None:
            return
        edited = StepEditor(self, self.steps[i]).result
        if edited is not None:
            self.steps[i] = edited
            self.refresh()

    # -- file --------------------------------------------------------------
    def on_new(self) -> None:
        if self.steps and not messagebox.askyesno(
                "New macro", "Discard the current steps?"):
            return
        self.steps = []
        self.macro_name = "untitled"
        self.region = None
        self.refresh()

    def on_open(self) -> None:
        path = filedialog.askopenfilename(
            initialdir=MACROS, filetypes=[("Macro JSON", "*.json")])
        if path:
            self._load_macro(path)

    def _load_macro(self, path: str) -> None:
        try:
            m = Macro.load(path)
        except Exception as e:
            messagebox.showerror("Open failed", str(e))
            return
        self.macro_name = m.name
        self.region = list(m.region) if m.region else None
        self.loop_var.set(str(m.repeat))
        # keep steps as dicts for editing
        self.steps = [
            {k: v for k, v in vars(s).items() if not _is_default(k, v)}
            for s in m.steps
        ]
        self.refresh()
        self.log(f"Opened {os.path.basename(path)} ({len(self.steps)} steps).")

    def on_save(self) -> None:
        import json
        default = os.path.join(MACROS, f"{self.macro_name}.json")
        os.makedirs(MACROS, exist_ok=True)
        path = filedialog.asksaveasfilename(
            initialdir=MACROS, initialfile=os.path.basename(default),
            defaultextension=".json", filetypes=[("Macro JSON", "*.json")])
        if not path:
            return
        try:
            repeat = int(self.loop_var.get())
        except ValueError:
            repeat = 1
        self.macro_name = os.path.splitext(os.path.basename(path))[0]
        data = {"name": self.macro_name, "repeat": repeat,
                "region": self.region, "steps": self.steps}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        self.refresh()
        self.log(f"Saved {os.path.basename(path)}.")

    # -- panic key + shutdown ---------------------------------------------
    def _start_panic_listener(self) -> None:
        def on_press(key):
            if key == keyboard.Key.f12:
                self.panic.set()
                self.log("F12 → panic.")
        self._klistener = keyboard.Listener(on_press=on_press)
        self._klistener.daemon = True
        self._klistener.start()

    def _on_close(self) -> None:
        self.panic.set()
        try:
            self._klistener.stop()
        except Exception:
            pass
        self.destroy()


# --- step editing helpers ------------------------------------------------
_ACTIONS = ["find_click", "wait_for", "find_text_click", "wait_for_text",
            "find_object_click", "wait_for_object",
            "click", "type", "hotkey", "wait", "if", "else", "end_if",
            "loop", "end_loop", "break", "continue", "call",
            "set", "read_text"]


def _default_step(action: str) -> dict:
    if action in ("find_click", "wait_for"):
        return {"action": action, "target": "", "threshold": 0.80,
                "timeout": 10.0, "on_fail": "abort"}
    if action in ("find_text_click", "wait_for_text"):
        # OCR confidence runs lower than template match, so a gentler threshold.
        return {"action": action, "target": "", "threshold": 0.50,
                "timeout": 10.0, "on_fail": "abort"}
    if action in ("find_object_click", "wait_for_object"):
        return {"action": action, "target": "button", "threshold": 0.50,
                "timeout": 10.0, "on_fail": "abort"}
    if action == "click":
        return {"action": "click", "args": {"x": 0, "y": 0, "button": "left"}}
    if action == "type":
        return {"action": "type", "target": ""}
    if action == "hotkey":
        return {"action": "hotkey", "args": {"keys": ["enter"]}}
    if action == "if":
        return {"action": "if", "args": {"cond": {
            "type": "image", "target": "", "threshold": 0.80,
            "timeout": 2.0, "negate": False}}}
    if action in ("else", "end_if"):
        return {"action": action}
    if action == "loop":
        return {"action": "loop", "args": {
            "count": 10, "mode": "", "cond": {
                "type": "image", "target": "", "threshold": 0.80,
                "timeout": 2.0}}}
    if action in ("end_loop", "break", "continue"):
        return {"action": action}
    if action == "call":
        return {"action": "call", "target": "", "args": {"repeat": 1}}
    if action == "set":
        return {"action": "set", "target": "", "args": {"op": "assign", "value": ""}}
    if action == "read_text":
        return {"action": "read_text", "target": "", "threshold": 0.30}
    return {"action": "wait", "args": {"seconds": 1.0}}


def _is_default(key: str, val) -> bool:
    # trim Step defaults so saved JSON stays clean
    defaults = {"target": "", "args": {}, "timeout": 5.0,
                "threshold": 0.80, "on_fail": "abort"}
    return key in defaults and val == defaults[key]


def _choose_action(parent) -> str | None:
    dlg = tk.Toplevel(parent)
    dlg.title("Add step")
    dlg.transient(parent)
    dlg.grab_set()
    ttk.Label(dlg, text="Step type:", padding=8).pack()
    var = tk.StringVar(value=_ACTIONS[0])
    ttk.Combobox(dlg, textvariable=var, values=_ACTIONS, state="readonly").pack(
        padx=10)
    out = {"v": None}
    def ok():
        out["v"] = var.get()
        dlg.destroy()
    ttk.Button(dlg, text="Add", command=ok).pack(pady=8)
    parent.wait_window(dlg)
    return out["v"]


class StepEditor(tk.Toplevel):
    """Modal editor whose fields depend on the step's action."""

    def __init__(self, parent, step: dict):
        super().__init__(parent)
        self.title("Edit step")
        self.transient(parent)
        self.grab_set()
        self.result: dict | None = None
        self._step = dict(step)
        self._vars: dict[str, tk.StringVar] = {}

        action = self._step.get("action", "wait")
        args = self._step.get("args", {}) or {}
        ttk.Label(self, text=f"Action: {action}", padding=(10, 8)).grid(
            row=0, column=0, columnspan=2, sticky="w")

        fields = self._fields_for(action, args)
        for r, (label, key, val) in enumerate(fields, start=1):
            ttk.Label(self, text=label, padding=(10, 2)).grid(
                row=r, column=0, sticky="e")
            var = tk.StringVar(value=str(val))
            self._vars[key] = var
            ttk.Entry(self, textvariable=var, width=34).grid(
                row=r, column=1, padx=(0, 10), pady=2)

        btns = ttk.Frame(self, padding=8)
        btns.grid(row=len(fields) + 1, column=0, columnspan=2)
        ttk.Button(btns, text="OK", command=self._ok).pack(side="left", padx=4)
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="left")
        parent.wait_window(self)

    def _fields_for(self, action, args):
        if action in ("find_click", "wait_for"):
            return [("Image (target)", "target", self._step.get("target", "")),
                    ("Threshold 0-1", "threshold", self._step.get("threshold", 0.80)),
                    ("Timeout (s)", "timeout", self._step.get("timeout", 10.0)),
                    ("Button", "button", args.get("button", "left")),
                    ("On fail", "on_fail", self._step.get("on_fail", "abort"))]
        if action in ("find_text_click", "wait_for_text"):
            return [("Text (target)", "target", self._step.get("target", "")),
                    ("Min conf 0-1", "threshold", self._step.get("threshold", 0.50)),
                    ("Timeout (s)", "timeout", self._step.get("timeout", 10.0)),
                    ("Button", "button", args.get("button", "left")),
                    ("Region l,t,w,h", "region",
                     ",".join(str(v) for v in args.get("region", [])) ),
                    ("On fail", "on_fail", self._step.get("on_fail", "abort"))]
        if action in ("find_object_click", "wait_for_object"):
            return [("Class label", "target", self._step.get("target", "button")),
                    ("Min conf 0-1", "threshold", self._step.get("threshold", 0.50)),
                    ("Timeout (s)", "timeout", self._step.get("timeout", 10.0)),
                    ("Button", "button", args.get("button", "left")),
                    ("Model (blank=auto)", "model", args.get("model", "")),
                    ("Region l,t,w,h", "region",
                     ",".join(str(v) for v in args.get("region", [])) ),
                    ("On fail", "on_fail", self._step.get("on_fail", "abort"))]
        if action == "click":
            return [("X", "x", args.get("x", 0)), ("Y", "y", args.get("y", 0)),
                    ("Button", "button", args.get("button", "left"))]
        if action == "type":
            return [("Text", "target", self._step.get("target", ""))]
        if action == "hotkey":
            return [("Keys (comma sep)", "keys",
                     ",".join(args.get("keys", ["enter"])))]
        if action == "if":
            cond = (self._step.get("args", {}) or {}).get("cond", {}) or {}
            return [("Type (image/text/var)", "ctype", cond.get("type", "image")),
                    ("Target (image or text)", "target", cond.get("target", "")),
                    ("Threshold/min conf", "threshold", cond.get("threshold", 0.80)),
                    ("Timeout (s)", "timeout", cond.get("timeout", 2.0)),
                    ("Region l,t,w,h", "region",
                     ",".join(str(v) for v in cond.get("region", []))),
                    ("[var] name", "vname", cond.get("name", "")),
                    ("[var] op eq/ne/contains/gt/lt/ge/le/set/empty", "vop",
                     cond.get("op", "eq")),
                    ("[var] value", "vvalue", cond.get("value", "")),
                    ("Negate (true/false)", "negate", str(cond.get("negate", False)))]
        if action == "loop":
            cond = args.get("cond", {}) or {}
            return [("Max iterations (0=∞)", "count", args.get("count", 0)),
                    ("Mode (while/until/blank)", "mode", args.get("mode", "")),
                    ("Cond type (image/text/var)", "ctype", cond.get("type", "image")),
                    ("Cond target", "target", cond.get("target", "")),
                    ("Cond thr/conf", "threshold", cond.get("threshold", 0.80)),
                    ("Cond timeout (s)", "timeout", cond.get("timeout", 2.0)),
                    ("[var] name", "vname", cond.get("name", "")),
                    ("[var] op (eq/ne/gt/lt/...)", "vop", cond.get("op", "lt")),
                    ("[var] value", "vvalue", cond.get("value", ""))]
        if action == "call":
            return [("Macro name", "target", self._step.get("target", "")),
                    ("Repeat", "repeat", args.get("repeat", 1))]
        if action == "set":
            return [("Variable name", "target", self._step.get("target", "")),
                    ("Op (assign/add/sub/mul/div)", "op", args.get("op", "assign")),
                    ("Value (supports ${var})", "value", args.get("value", ""))]
        if action == "read_text":
            return [("Store in variable", "target", self._step.get("target", "")),
                    ("Min conf 0-1", "threshold", self._step.get("threshold", 0.30)),
                    ("Region l,t,w,h", "region",
                     ",".join(str(v) for v in args.get("region", [])))]
        if action in ("else", "end_if", "end_loop", "break", "continue"):
            return []
        return [("Seconds", "seconds", args.get("seconds", 1.0))]

    @staticmethod
    def _cond_from_vars(v: dict) -> dict:
        """Build a condition dict from editor fields (image / text / var)."""
        ctype = (v.get("ctype", "image").strip().lower() or "image")
        if ctype not in ("image", "text", "var"):
            raise ValueError("Type must be image, text, or var")
        if ctype == "var":
            return {"type": "var", "name": v.get("vname", "").strip(),
                    "op": (v.get("vop", "eq").strip().lower() or "eq"),
                    "value": v.get("vvalue", "")}
        cond = {"type": ctype, "target": v.get("target", ""),
                "threshold": float(v.get("threshold", 0.8) or 0.8),
                "timeout": float(v.get("timeout", 2.0) or 2.0)}
        region_str = v.get("region", "").strip()
        if region_str:
            parts = [int(p) for p in region_str.split(",")]
            if len(parts) != 4:
                raise ValueError("Region must be 4 numbers: l,t,w,h")
            cond["region"] = parts
        return cond

    def _ok(self):
        action = self._step.get("action", "wait")
        v = {k: var.get() for k, var in self._vars.items()}
        out: dict = {"action": action}
        try:
            if action in ("find_click", "wait_for"):
                out["target"] = v["target"]
                out["threshold"] = float(v["threshold"])
                out["timeout"] = float(v["timeout"])
                out["on_fail"] = v["on_fail"] or "abort"
                if v.get("button", "left") != "left":
                    out["args"] = {"button": v["button"]}
            elif action in ("find_text_click", "wait_for_text"):
                out["target"] = v["target"]
                out["threshold"] = float(v["threshold"])
                out["timeout"] = float(v["timeout"])
                out["on_fail"] = v["on_fail"] or "abort"
                args_out = {}
                if v.get("button", "left") != "left":
                    args_out["button"] = v["button"]
                region_str = v.get("region", "").strip()
                if region_str:
                    parts = [int(p) for p in region_str.split(",")]
                    if len(parts) != 4:
                        raise ValueError("Region must be 4 numbers: l,t,w,h")
                    args_out["region"] = parts
                if args_out:
                    out["args"] = args_out
            elif action in ("find_object_click", "wait_for_object"):
                out["target"] = v["target"]
                out["threshold"] = float(v["threshold"])
                out["timeout"] = float(v["timeout"])
                out["on_fail"] = v["on_fail"] or "abort"
                args_out = {}
                if v.get("button", "left") != "left":
                    args_out["button"] = v["button"]
                if v.get("model", "").strip():
                    args_out["model"] = v["model"].strip()
                region_str = v.get("region", "").strip()
                if region_str:
                    parts = [int(p) for p in region_str.split(",")]
                    if len(parts) != 4:
                        raise ValueError("Region must be 4 numbers: l,t,w,h")
                    args_out["region"] = parts
                if args_out:
                    out["args"] = args_out
            elif action == "click":
                out["args"] = {"x": int(v["x"]), "y": int(v["y"]),
                               "button": v.get("button", "left")}
            elif action == "type":
                out["target"] = v["target"]
            elif action == "hotkey":
                keys = [k.strip() for k in v["keys"].split(",") if k.strip()]
                out["args"] = {"keys": keys}
            elif action == "if":
                cond = self._cond_from_vars(v)
                if v.get("negate", "false").strip().lower() in ("true", "1", "yes"):
                    cond["negate"] = True
                out["args"] = {"cond": cond}
            elif action == "loop":
                args_out = {}
                cnt = str(v.get("count", "0")).strip()
                args_out["count"] = int(cnt) if cnt else 0
                mode = v.get("mode", "").strip().lower()
                if mode in ("while", "until"):
                    args_out["mode"] = mode
                    args_out["cond"] = self._cond_from_vars(v)
                out["args"] = args_out
            elif action == "call":
                out["target"] = v.get("target", "").strip()
                rep = str(v.get("repeat", "1")).strip()
                out["args"] = {"repeat": int(rep) if rep else 1}
            elif action == "set":
                out["target"] = v.get("target", "").strip()
                op = (v.get("op", "assign").strip().lower() or "assign")
                if op not in ("assign", "add", "sub", "mul", "div"):
                    raise ValueError("Op must be assign/add/sub/mul/div")
                out["args"] = {"op": op, "value": v.get("value", "")}
            elif action == "read_text":
                out["target"] = v.get("target", "").strip()
                out["threshold"] = float(v.get("threshold", 0.30) or 0.30)
                region_str = v.get("region", "").strip()
                if region_str:
                    parts = [int(p) for p in region_str.split(",")]
                    if len(parts) != 4:
                        raise ValueError("Region must be 4 numbers: l,t,w,h")
                    out["args"] = {"region": parts}
            elif action in ("else", "end_if", "end_loop", "break", "continue"):
                pass  # markers carry no data
            else:
                out["args"] = {"seconds": float(v["seconds"])}
        except ValueError as e:
            messagebox.showerror("Invalid value", str(e), parent=self)
            return
        self.result = out
        self.destroy()


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
