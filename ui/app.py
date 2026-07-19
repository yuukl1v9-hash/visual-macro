"""visual-macro — desktop UI.

A CustomTkinter front end (modern dark sidebar) over the core engine + recorder.
Record a task, see the steps as an editable list, reorder or tweak them, then
play with an optional loop count / CSV data / a global hotkey. F12 stops a run
instantly. The step list is a dark-styled ttk.Treeview embedded in the CTk
window; dialogs (step editor, region picker, hotkeys) are themed ttk.

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

import customtkinter as ctk

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from core.engine import Engine, Macro, Step  # noqa: E402
from core.dpi import set_dpi_aware  # noqa: E402
import recorder as recorder_mod  # noqa: E402

from pynput import keyboard  # noqa: E402

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

ASSETS = os.path.join(ROOT, "assets")
MACROS = os.path.join(ROOT, "macros")
VERSION = "1.0.0"
REPO_URL = "https://github.com/yuukl1v9-hash/visual-macro"
HOTKEYS_FILE = os.path.join(ROOT, "hotkeys.json")
_MODS = {"ctrl", "alt", "shift", "win", "cmd"}

# Dark palette (Catppuccin-ish) used across the UI.
DARK = {
    "bg": "#1e1e2e", "surface": "#313244", "surface2": "#45475a",
    "stripe": "#2a2a3a",  # subtle alternate row shade
    "border": "#585b70", "text": "#cdd6f4", "subtext": "#a6adc8",
    "accent": "#89b4fa", "accent2": "#b4befe",
    "ok": "#a6e3a1", "warn": "#f9e2af", "err": "#f38ba8",
    "sidebar": "#181825", "card": "#26263a", "hover": "#3b3b52",
    "tip_bg": "#11111b", "tip_fg": "#cdd6f4",
}
LIGHT = {
    "bg": "#eff1f5", "surface": "#ffffff", "surface2": "#e6e9ef",
    "stripe": "#f5f6f9",
    "border": "#bcc0cc", "text": "#1e2030", "subtext": "#5c5f77",
    "accent": "#2563eb", "accent2": "#1d4ed8",
    "ok": "#2e7d32", "warn": "#a66300", "err": "#c0362c",
    "sidebar": "#e2e4ec", "card": "#ffffff", "hover": "#d6d9e4",
    "tip_bg": "#1e1e2e", "tip_fg": "#eff1f5",
}
# Active palette — mutated in place on toggle so every THEME[...] lookup (read
# at build/open time) picks up the new colours.
THEME = dict(DARK)
SETTINGS_FILE = os.path.join(ROOT, "settings.json")


def _macro_names() -> list[str]:
    """Saved macro names (filenames without .json) for the call-step dropdown."""
    try:
        return sorted(os.path.splitext(f)[0] for f in os.listdir(MACROS)
                      if f.lower().endswith(".json"))
    except OSError:
        return []


def _icon_path() -> str | None:
    """Locate icon.ico whether running from source or a PyInstaller bundle."""
    candidates = []
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidates += [os.path.join(base, "icon.ico"),
                       os.path.join(base, "ui", "icon.ico")]
    candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "icon.ico"))
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


def _dark_titlebar(win) -> None:
    """Ask Windows to paint this window's title bar dark (no-op elsewhere)."""
    try:
        import ctypes
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        val = ctypes.c_int(1)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE: 20 new, 19 older
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attr, ctypes.byref(val), ctypes.sizeof(val))
    except Exception:
        pass


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
    if a == "ask":
        msg = args.get("message", "")
        return "Ask", f'{step.get("target", "")} — "{msg}"'
    return a, str(step)


class App(tk.Tk):
    def __init__(self):
        set_dpi_aware()  # correct click coords on scaled displays
        super().__init__()
        self.title("visual-macro")
        self.geometry("940x620")
        self.minsize(860, 560)
        self._icon = _icon_path()
        if self._icon:
            try:
                self.iconbitmap(self._icon)
            except tk.TclError:
                pass

        self.steps: list[dict] = []
        self.macro_name = "untitled"
        self.region = None
        self._dirty = False

        self._log_q: "queue.Queue[str]" = queue.Queue()
        self._cmd_q: "queue.Queue" = queue.Queue()  # UI callbacks from workers
        self.panic = threading.Event()
        self._running = False
        self._recording = False

        self._settings = self._read_settings()
        self._appearance = self._settings.get("appearance", "dark")
        if self._appearance not in ("dark", "light"):
            self._appearance = "dark"
        geo = self._settings.get("geometry")
        if geo:                                  # restore saved size + position
            try:
                self.geometry(geo)
            except tk.TclError:
                pass
        THEME.clear()
        THEME.update(DARK if self._appearance == "dark" else LIGHT)
        ctk.set_appearance_mode(self._appearance)
        self._apply_theme()
        self._build_ui()
        _dark_titlebar(self)
        self._start_panic_listener()
        self._hotkeys: dict[str, str] = {}   # "ctrl+alt+1" -> macro file path
        self._hk_listener = None
        self._load_hotkeys()
        self._restart_hotkeys()
        self.after(80, self._drain_log)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._bind_shortcuts()
        self.refresh()  # shows the empty-state hint on first launch
        self.log("Ready. Record a task or open a macro.")

    # -- appearance (light / dark) ----------------------------------------
    def _read_settings(self) -> dict:
        try:
            import json
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_settings(self) -> None:
        import json
        self._settings["appearance"] = self._appearance
        try:
            self._settings["geometry"] = self.geometry()  # "WxH+X+Y"
        except tk.TclError:
            pass
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(self._settings, f, indent=2)
        except OSError:
            pass

    def _set_appearance(self, mode: str) -> None:
        if mode == self._appearance:
            return
        self._appearance = mode
        THEME.clear()
        THEME.update(DARK if mode == "dark" else LIGHT)
        ctk.set_appearance_mode(mode)
        self._apply_theme()
        self._container.destroy()   # rebuild content with the new palette
        self._build_ui()
        self.refresh()
        self._save_settings()
        self.log(f"Switched to {mode} mode.")

    def _bind_shortcuts(self) -> None:
        self.bind("<Control-s>", lambda e: self.on_save())
        self.bind("<Control-o>", lambda e: self.on_open())
        self.bind("<Control-n>", lambda e: self.on_new())
        self.bind("<F5>", lambda e: self.on_play())
        self.bind("<Delete>", self._on_delete_key)

    def _on_delete_key(self, _e) -> None:
        # don't hijack Delete while typing in a field
        w = self.focus_get()
        if isinstance(w, (ttk.Entry, tk.Entry, ttk.Spinbox, tk.Spinbox, tk.Text)):
            return
        self.on_delete()

    # -- theme -------------------------------------------------------------
    def _apply_theme(self) -> None:
        """Dark ttk styling for the embedded Treeview and the ttk dialogs
        (StepEditor / HotkeyDialog / RegionPicker). The main window is CTk."""
        c = THEME
        st = ttk.Style(self)
        st.theme_use("clam")
        st.configure(".", background=c["bg"], foreground=c["text"],
                     fieldbackground=c["surface"], bordercolor=c["border"],
                     focuscolor=c["accent"], font=("Segoe UI", 10))
        st.configure("TFrame", background=c["bg"])
        st.configure("TLabel", background=c["bg"], foreground=c["text"])
        st.configure("Dim.TLabel", foreground=c["subtext"])
        st.configure("Status.TLabel", background=c["surface"], foreground=c["subtext"])
        st.configure("TButton", background=c["surface2"], foreground=c["text"],
                     bordercolor=c["border"], focusthickness=0, padding=(8, 4),
                     relief="flat")
        st.map("TButton",
               background=[("active", c["accent"]), ("pressed", c["accent"]),
                           ("disabled", c["surface"])],
               foreground=[("active", c["bg"]), ("disabled", c["subtext"])])
        st.configure("Accent.TButton", background=c["accent"], foreground=c["bg"])
        st.map("Accent.TButton", background=[("active", c["accent2"])])
        st.configure("TSpinbox", fieldbackground=c["surface"], foreground=c["text"],
                     arrowcolor=c["text"], bordercolor=c["border"])
        st.configure("TEntry", fieldbackground=c["surface"], foreground=c["text"],
                     bordercolor=c["border"], insertcolor=c["text"])
        st.configure("TCombobox", fieldbackground=c["surface"], foreground=c["text"],
                     background=c["surface2"], arrowcolor=c["text"],
                     bordercolor=c["border"], insertcolor=c["text"])
        st.map("TCombobox", fieldbackground=[("readonly", c["surface"])],
               foreground=[("readonly", c["text"])])
        # the dropdown popup is a classic Listbox — theme it via the option db
        self.option_add("*TCombobox*Listbox.background", c["surface"])
        self.option_add("*TCombobox*Listbox.foreground", c["text"])
        self.option_add("*TCombobox*Listbox.selectBackground", c["accent"])
        self.option_add("*TCombobox*Listbox.selectForeground", c["bg"])
        st.configure("Treeview", background=c["surface"], fieldbackground=c["surface"],
                     foreground=c["text"], borderwidth=0, rowheight=24)
        st.configure("Treeview.Heading", background=c["surface2"],
                     foreground=c["subtext"], relief="flat")
        st.map("Treeview.Heading", background=[("active", c["surface2"])])
        st.map("Treeview", background=[("selected", c["accent"])],
               foreground=[("selected", c["bg"])])
        st.configure("Vertical.TScrollbar", background=c["surface2"],
                     troughcolor=c["bg"], bordercolor=c["bg"], arrowcolor=c["text"])

    # -- layout ------------------------------------------------------------
    def _sidebar_btn(self, parent, text, cmd, accent=False, tip=""):
        b = ctk.CTkButton(
            parent, text=text, command=cmd, height=38, corner_radius=8,
            anchor="w", font=ctk.CTkFont(size=13),
            fg_color=THEME["accent"] if accent else "transparent",
            text_color=THEME["bg"] if accent else THEME["text"],
            hover_color=THEME["accent2"] if accent else THEME["hover"])
        b.pack(fill="x", padx=12, pady=3)
        if tip:
            Tooltip(b, tip)
        return b

    def _build_ui(self) -> None:
        c = THEME
        if not hasattr(self, "loop_var"):      # preserve value across re-theme
            self.loop_var = tk.StringVar(value="1")
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        # everything lives in a container so a theme switch can rebuild it
        self._container = ctk.CTkFrame(self, corner_radius=0, fg_color=c["bg"])
        self._container.grid(row=0, column=0, sticky="nsew")
        self._container.grid_columnconfigure(1, weight=1)
        self._container.grid_rowconfigure(0, weight=1)

        # ---- sidebar -----------------------------------------------------
        side = ctk.CTkFrame(self._container, width=200, corner_radius=0,
                            fg_color=c["sidebar"])
        side.grid(row=0, column=0, sticky="nsw")
        side.grid_propagate(False)
        ctk.CTkLabel(side, text="⚡  visual-macro",
                     font=ctk.CTkFont(size=18, weight="bold"),
                     text_color=c["accent"]).pack(pady=(20, 4), padx=12, anchor="w")
        ctk.CTkLabel(side, text="RUN", font=ctk.CTkFont(size=11, weight="bold"),
                     text_color=c["subtext"]).pack(pady=(14, 2), padx=14, anchor="w")
        self.btn_record = self._sidebar_btn(
            side, "  ●  Record", self.on_record,
            tip="Do a task by hand once — it records the steps. F10 to stop.")
        self.btn_play = self._sidebar_btn(
            side, "  ▶  Play", self.on_play, accent=True,
            tip="Run the macro. Loop sets how many times.")
        self.btn_data = self._sidebar_btn(
            side, "  ▶  Play with data…", self.on_play_data,
            tip="Run once per CSV row; columns become ${variables}.")
        self.btn_stop = self._sidebar_btn(
            side, "  ■  Stop", self.on_stop, tip="Stop a run (or press F12).")
        self.btn_stop.configure(state="disabled")

        ctk.CTkLabel(side, text="MACRO", font=ctk.CTkFont(size=11, weight="bold"),
                     text_color=c["subtext"]).pack(pady=(16, 2), padx=14, anchor="w")
        self._sidebar_btn(side, "  ✚  New", self.on_new)
        self._sidebar_btn(side, "  📂  Open…", self.on_open)
        self._sidebar_btn(side, "  💾  Save…", self.on_save)

        ctk.CTkLabel(side, text="TOOLS", font=ctk.CTkFont(size=11, weight="bold"),
                     text_color=c["subtext"]).pack(pady=(16, 2), padx=14, anchor="w")
        self._sidebar_btn(side, "  ⌨  Hotkeys…", self.on_hotkeys,
                          tip="Bind a global hotkey to a saved macro.")
        self._sidebar_btn(side, "  ⓘ  About", self.on_about)
        ctk.CTkLabel(side, text=f"v{VERSION}", text_color=c["subtext"],
                     font=ctk.CTkFont(size=11)).pack(side="bottom", pady=(4, 12))
        theme_seg = ctk.CTkSegmentedButton(
            side, values=["🌙 Dark", "☀ Light"],
            command=lambda v: self._set_appearance(
                "dark" if v.startswith("🌙") else "light"))
        theme_seg.set("🌙 Dark" if self._appearance == "dark" else "☀ Light")
        theme_seg.pack(side="bottom", padx=12, pady=(0, 4), fill="x")

        # ---- main --------------------------------------------------------
        main = ctk.CTkFrame(self._container, fg_color="transparent")
        main.grid(row=0, column=1, sticky="nsew", padx=18, pady=16)
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(2, weight=1)

        header = ctk.CTkFrame(main, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(0, weight=1)
        self.name_lbl = ctk.CTkLabel(header, text="untitled",
                                     font=ctk.CTkFont(size=20, weight="bold"))
        self.name_lbl.grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(header, text="Loop").grid(row=0, column=1, padx=(0, 4))
        ctk.CTkEntry(header, width=56, textvariable=self.loop_var).grid(row=0, column=2)
        ctk.CTkLabel(header, text="0 = ∞", text_color=c["subtext"]).grid(
            row=0, column=3, padx=(6, 0))
        self.status = ctk.CTkLabel(header, text="", fg_color=c["card"],
                                   corner_radius=12, text_color=c["subtext"],
                                   font=ctk.CTkFont(size=12), height=28, width=120)
        self.status.grid(row=0, column=4, padx=(12, 0), ipadx=6)

        # step-edit toolbar
        toolbar = ctk.CTkFrame(main, fg_color="transparent")
        toolbar.grid(row=1, column=0, sticky="ew", pady=(12, 8))
        for text, cmd, tip in [
            ("✚ Add", self.on_add, "Add a step (plain-English menu)"),
            ("Edit", self.on_edit, "Edit the selected step (or double-click)"),
            ("Delete", self.on_delete, "Delete the selected step"),
            ("▲", self.on_up, "Move up"), ("▼", self.on_down, "Move down"),
            ("🔍 Test", self.on_test, "Preview where the step would click — no click"),
        ]:
            b = ctk.CTkButton(toolbar, text=text, command=cmd, width=64, height=30,
                              corner_radius=8, fg_color=c["card"],
                              hover_color=c["hover"], text_color=c["text"])
            b.pack(side="left", padx=(0, 6))
            Tooltip(b, tip)

        # step list (ttk.Treeview inside a rounded card)
        card = ctk.CTkFrame(main, corner_radius=10, fg_color=c["surface"])
        card.grid(row=2, column=0, sticky="nsew")
        card.grid_columnconfigure(0, weight=1)
        card.grid_rowconfigure(0, weight=1)
        cols = ("num", "action", "detail")
        self.tree = ttk.Treeview(card, columns=cols, show="headings")
        self.tree.heading("num", text="#")
        self.tree.heading("action", text="Action")
        self.tree.heading("detail", text="Detail")
        self.tree.column("num", width=40, anchor="center", stretch=False)
        self.tree.column("action", width=150, stretch=False)
        self.tree.column("detail", width=420)
        self.tree.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)
        self.tree.bind("<Double-1>", lambda e: self.on_edit())
        self.tree.tag_configure("odd", background=c["surface"])
        self.tree.tag_configure("even", background=c["stripe"])
        self.empty_hint = tk.Label(
            self.tree, bg=c["surface"], fg=c["subtext"], font=("Segoe UI", 11),
            justify="center",
            text="No steps yet.\n\nClick  ●  Record  to capture a task,\n"
                 "or  ✚ Add  to build one step at a time.")
        csb = ctk.CTkScrollbar(card, command=self.tree.yview)
        csb.grid(row=0, column=1, sticky="ns", padx=(0, 4), pady=6)
        self.tree.configure(yscrollcommand=csb.set)

        # log (dark tk.Text inside a card — keeps coloured tags)
        logcard = ctk.CTkFrame(main, corner_radius=10, fg_color=c["card"], height=150)
        logcard.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        logcard.grid_propagate(False)
        logcard.grid_columnconfigure(0, weight=1)
        logcard.grid_rowconfigure(0, weight=1)
        self.log_txt = tk.Text(logcard, height=7, state="disabled", wrap="word",
                               font=("Consolas", 9), bg=c["card"], fg=c["subtext"],
                               insertbackground=c["text"], relief="flat",
                               highlightthickness=0, padx=10, pady=8, borderwidth=0)
        self.log_txt.grid(row=0, column=0, sticky="nsew")
        self.log_txt.tag_configure("ok", foreground=c["ok"])
        self.log_txt.tag_configure("warn", foreground=c["warn"])
        self.log_txt.tag_configure("err", foreground=c["err"])

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
                low = msg.lower()
                tag = ""
                if "ambiguous" in low or "not found" in low or "error" in low \
                        or "fail" in low or "panic" in low:
                    tag = "warn" if "ambiguous" in low or "not found" in low else "err"
                elif "found (" in low or "saved" in low or "done" in low \
                        or "✓" in msg:
                    tag = "ok"
                self.log_txt.configure(state="normal")
                self.log_txt.insert("end", msg + "\n", tag)
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
            tag = "even" if (i - 1) % 2 else "odd"
            self.tree.insert("", "end", iid=str(i - 1),
                             values=(i, action, detail), tags=(tag,))
            if act in ("if", "loop"):
                depth += 1
        if keep is not None and self.steps:
            iid = str(min(keep, len(self.steps) - 1))
            self.tree.selection_set(iid)
            self.tree.focus(iid)
        self.name_lbl.configure(
            text=self.macro_name + ("  ●(unsaved)" if self._dirty else ""))
        self.set_status(f"{len(self.steps)} steps")
        if self.steps:
            self.empty_hint.place_forget()
        else:
            self.empty_hint.place(relx=0.5, rely=0.42, anchor="center")

    def _selected_index(self) -> int | None:
        sel = self.tree.selection()
        return int(sel[0]) if sel else None

    # -- record ------------------------------------------------------------
    def on_record(self) -> None:
        if self._running or self._recording:
            return
        name = ctk_ask(self, "Record macro", "Name for this macro:", "my-macro")
        if not name:
            return
        self._recording = True
        self._set_busy(True)
        self._rec_name = name
        self.set_status("Get ready — recording starts after the countdown…")
        self._show_rec_banner()
        self.iconify()  # get the UI out of the way so clicks don't hit it
        self._countdown(self.COUNTDOWN_START)  # time to switch to your app

    COUNTDOWN_START = 3

    def _countdown(self, n: int) -> None:
        if not self._recording:  # cancelled (e.g. window closed)
            return
        if n <= 0:
            self._start_recording()
            return
        self._set_banner("countdown", n)
        self.after(1000, lambda: self._countdown(n - 1))

    def _start_recording(self) -> None:
        if not self._recording:
            return
        name = self._rec_name
        self._set_banner("recording")
        self.log(f"Recording '{name}' — do your task, press F10 to stop.")
        self.set_status("Recording…  (window minimized; press F10 to stop)")

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

    def _show_rec_banner(self) -> None:
        """A floating badge that stays visible while the main window is
        minimized — shows the countdown, then the live recording state."""
        b = tk.Toplevel(self)
        b.overrideredirect(True)
        b.attributes("-topmost", True)
        try:
            b.attributes("-alpha", 0.94)
        except tk.TclError:
            pass
        f = tk.Frame(b, bg="#11111b", padx=18, pady=10)
        f.pack()
        self._rec_dot = tk.Label(f, text="●", bg="#11111b", fg="#f9e2af",
                                 font=("Segoe UI", 15, "bold"))
        self._rec_dot.pack(side="left")
        self._rec_text = tk.Label(f, text="", bg="#11111b", fg="#cdd6f4",
                                   font=("Segoe UI", 12, "bold"))
        self._rec_text.pack(side="left")
        self._rec_banner = b
        # initial text/position is set by the first _countdown tick, which
        # runs synchronously right after this in on_record()

    def _set_banner(self, mode: str, n: int = 0) -> None:
        b = getattr(self, "_rec_banner", None)
        if b is None:
            return
        if mode == "countdown":
            self._rec_dot.config(fg="#f9e2af")  # amber while waiting
            self._rec_text.config(
                text=f"  Recording in {n}…   switch to your app")
        else:
            self._rec_dot.config(fg="#f38ba8")  # red = live
            self._rec_text.config(text="  Recording  ·  press  F10  to stop")
        b.update_idletasks()
        sw = b.winfo_screenwidth()
        b.geometry(f"+{(sw - b.winfo_width()) // 2}+22")

    def _hide_rec_banner(self) -> None:
        b = getattr(self, "_rec_banner", None)
        if b is not None:
            try:
                b.destroy()
            except tk.TclError:
                pass
            self._rec_banner = None

    def _after_record(self) -> None:
        self._recording = False
        self._set_busy(False)
        self._hide_rec_banner()
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
        self._run_macro(macro, "Playing…  (F12 or Stop to abort)")

    def _run_macro(self, macro, status: str) -> None:
        """Shared play path used by ▶ Play and by global hotkeys."""
        self.panic.clear()
        self._running = True
        self._set_busy(True)
        self.btn_stop.configure(state="normal")
        self.set_status(status)

        def worker():
            try:
                engine = Engine(assets_dir=ASSETS, panic=self.panic, log=self.log,
                                ask_fn=self._ask)
                engine.run(macro)
            except Exception as e:
                self.post(lambda e=e: self.log(f"Play error: {e}"))
            finally:
                self.post(self._after_play)

        threading.Thread(target=worker, daemon=True).start()

    def _ask(self, prompt: str, default: str = "") -> "str | None":
        """Called from the play worker thread: show an input dialog on the Tk
        main thread and block the worker until the user answers (or F12)."""
        result = {}
        done = threading.Event()

        def show():
            try:
                result["v"] = ctk_ask(self, "Macro input", prompt, default)
            finally:
                done.set()

        self.post(show)
        while not done.wait(0.2):        # yield so F12 can break the wait
            if self.panic.is_set():
                return None
        return result.get("v")

    def _after_play(self) -> None:
        self._running = False
        self._set_busy(False)
        self.btn_stop.configure(state="disabled")
        self.set_status(f"{len(self.steps)} steps")

    # -- global hotkeys ----------------------------------------------------
    @staticmethod
    def _to_pynput_hotkey(hk: str) -> str:
        """'ctrl+alt+1' -> '<ctrl>+<alt>+1' (pynput GlobalHotKeys format)."""
        parts = []
        for tok in hk.lower().replace(" ", "").split("+"):
            if not tok:
                continue
            if tok in _MODS or (tok.startswith("f") and tok[1:].isdigit()) \
                    or len(tok) > 1:
                parts.append(f"<{tok}>")
            else:
                parts.append(tok)
        return "+".join(parts)

    def _load_hotkeys(self) -> None:
        try:
            import json
            with open(HOTKEYS_FILE, "r", encoding="utf-8") as f:
                self._hotkeys = dict(json.load(f))
        except (OSError, ValueError):
            self._hotkeys = {}

    def _save_hotkeys(self) -> None:
        import json
        try:
            with open(HOTKEYS_FILE, "w", encoding="utf-8") as f:
                json.dump(self._hotkeys, f, indent=2)
        except OSError as e:
            self.log(f"Could not save hotkeys: {e}")

    def _restart_hotkeys(self) -> None:
        """(Re)start the background global-hotkey listener from self._hotkeys."""
        if self._hk_listener is not None:
            try:
                self._hk_listener.stop()
            except Exception:
                pass
            self._hk_listener = None
        if not self._hotkeys:
            return
        mapping = {}
        for hk, path in self._hotkeys.items():
            try:
                combo = self._to_pynput_hotkey(hk)
                mapping[combo] = (lambda p=path: self.post(
                    lambda: self._hotkey_fire(p)))
            except Exception as e:
                self.log(f"Bad hotkey '{hk}': {e}")
        if not mapping:
            return
        try:
            self._hk_listener = keyboard.GlobalHotKeys(mapping)
            self._hk_listener.daemon = True
            self._hk_listener.start()
            self.log(f"Global hotkeys active: {', '.join(self._hotkeys)}")
        except Exception as e:
            self.log(f"Could not start hotkeys: {e}")

    def _hotkey_fire(self, path: str) -> None:
        """A bound hotkey was pressed — play that macro file (if not busy)."""
        if self._running or self._recording:
            self.log("Hotkey ignored — already running.")
            return
        if not os.path.exists(path):
            self.log(f"Hotkey macro missing: {path}")
            return
        try:
            macro = Macro.load(path)
        except Exception as e:
            self.log(f"Hotkey load failed: {e}")
            return
        name = os.path.basename(path)
        self.log(f"[hotkey] playing {name}")
        self._run_macro(macro, f"Playing {name} (hotkey)…  (F12 to abort)")

    def on_hotkeys(self) -> None:
        HotkeyDialog(self)

    def on_play_data(self) -> None:
        """Run the macro once per row of a CSV; each column becomes a ${var}."""
        if self._running or self._recording:
            return
        if not self.steps:
            messagebox.showinfo("Nothing to play", "This macro has no steps yet.")
            return
        path = filedialog.askopenfilename(
            title="Choose a CSV — one run per row, columns become ${variables}",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
        if not path:
            return
        try:
            import csv
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))
        except Exception as e:
            messagebox.showerror("Could not read CSV", str(e))
            return
        if not rows:
            messagebox.showinfo("Empty", "That CSV has no data rows.")
            return
        cols = ", ".join(f"${{{c}}}" for c in rows[0].keys() if c)
        if not messagebox.askyesno(
                "Run with data",
                f"Run '{self.macro_name}' {len(rows)} times — once per row?\n\n"
                f"Columns available as variables:\n{cols}\n\n"
                "Make sure your target app is ready. F12 stops."):
            return

        try:
            repeat = int(self.loop_var.get())
        except ValueError:
            repeat = 1
        region = tuple(self.region) if self.region else None
        step_dicts = self.steps

        self.panic.clear()
        self._running = True
        self._set_busy(True)
        self.btn_stop.configure(state="normal")
        self.set_status(f"Playing data ×{len(rows)}…  (F12 or Stop to abort)")

        def worker():
            done = 0
            try:
                engine = Engine(assets_dir=ASSETS, panic=self.panic, log=self.log,
                                ask_fn=self._ask)
                macro = Macro(name=self.macro_name,
                              steps=[Step(**s) for s in step_dicts],
                              repeat=repeat, region=region)
                for i, row in enumerate(rows, 1):
                    if self.panic.is_set():
                        break
                    self.log(f"[data] row {i}/{len(rows)}: {dict(row)}")
                    engine.run(macro, initial_vars=row)
                    done = i
            except Exception as e:
                self.post(lambda e=e: self.log(f"Data run error: {e}"))
            finally:
                self.post(lambda: self.log(f"[data] finished {done}/{len(rows)} rows."))
                self.post(self._after_play)

        threading.Thread(target=worker, daemon=True).start()

    def on_stop(self) -> None:
        if self._running:
            self.panic.set()
            self.log("Stop requested.")

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self.btn_record.configure(state=state)
        self.btn_play.configure(state=state)
        self.btn_data.configure(state=state)

    # -- edit operations ---------------------------------------------------
    def on_up(self) -> None:
        i = self._selected_index()
        if i is None or i == 0:
            return
        self.steps[i - 1], self.steps[i] = self.steps[i], self.steps[i - 1]
        self._dirty = True
        self.refresh()
        self.tree.selection_set(str(i - 1))

    def on_down(self) -> None:
        i = self._selected_index()
        if i is None or i >= len(self.steps) - 1:
            return
        self.steps[i + 1], self.steps[i] = self.steps[i], self.steps[i + 1]
        self._dirty = True
        self.refresh()
        self.tree.selection_set(str(i + 1))

    def on_delete(self) -> None:
        i = self._selected_index()
        if i is None:
            return
        del self.steps[i]
        self._dirty = True
        self.refresh()

    def on_add(self) -> None:
        action = _choose_action(self)
        if not action:
            return
        step = _default_step(action)
        self.steps.append(step)
        self._dirty = True
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
            self._dirty = True
            self.refresh()

    # -- about -------------------------------------------------------------
    def on_about(self) -> None:
        dlg = ctk.CTkToplevel(self)
        dlg.title("About visual-macro")
        dlg.geometry("420x290")
        dlg.transient(self)
        dlg.resizable(False, False)
        dlg.grab_set()
        _dialog_icon(dlg)
        ctk.CTkLabel(dlg, text="⚡ visual-macro",
                     font=ctk.CTkFont(size=20, weight="bold"),
                     text_color=THEME["accent"]).pack(padx=28, pady=(22, 0))
        ctk.CTkLabel(dlg, text=f"version {VERSION}",
                     text_color=THEME["subtext"]).pack()
        ctk.CTkLabel(dlg, justify="center",
                     text="Record & replay desktop tasks.\nSteps are found by "
                          "image, text (OCR) or an AI model —\nnot fixed "
                          "coordinates.").pack(padx=28, pady=(14, 8))
        ctk.CTkLabel(dlg, text=REPO_URL, text_color=THEME["accent"]).pack()
        ctk.CTkLabel(dlg, text="MIT License",
                     text_color=THEME["subtext"]).pack(pady=(6, 0))

        def copy_link():
            self.clipboard_clear()
            self.clipboard_append(REPO_URL)
            self.log("Repo link copied to clipboard.")

        row = ctk.CTkFrame(dlg, fg_color="transparent")
        row.pack(pady=16)
        ctk.CTkButton(row, text="Copy link", command=copy_link, width=100,
                      fg_color=THEME["card"], hover_color=THEME["hover"]).pack(
            side="left", padx=6)
        ctk.CTkButton(row, text="Close", command=dlg.destroy, width=100).pack(
            side="left", padx=6)

    # -- test a single detection step -------------------------------------
    DETECTION = {"find_click", "wait_for", "find_text_click", "wait_for_text",
                 "find_object_click", "wait_for_object"}

    def on_test(self) -> None:
        i = self._selected_index()
        if i is None:
            messagebox.showinfo("Test", "Select a step to test.")
            return
        step_dict = self.steps[i]
        if step_dict.get("action") not in self.DETECTION:
            messagebox.showinfo("Test", "Pick a Click-image / text / object step "
                                        "— those are the ones that search the screen.")
            return
        if self._running or self._recording:
            return
        self.log(f"[test] step {i + 1}: searching the screen…")
        self.iconify()  # get out of the way so we test the real screen behind us

        def worker():
            try:
                eng = Engine(assets_dir=ASSETS, log=self.log)
                res = eng.test_step(Step(**step_dict),
                                    tuple(self.region) if self.region else None)
            except Exception as e:
                res = {"error": str(e)}
            self.post(lambda: self._show_test_result(res))

        threading.Thread(target=worker, daemon=True).start()

    def _show_test_result(self, res: dict) -> None:
        self.deiconify(); self.lift()
        if res.get("error"):
            self.log(f"[test] error: {res['error']}")
            return
        if not res.get("found"):
            self.log(f"[test] NOT found (best conf={res.get('confidence', 0):.2f}). "
                     f"Lower the threshold, re-capture the anchor, or check the region.")
            return
        conf = res.get("confidence", 0.0)
        if res.get("ambiguous"):
            self.log(f"[test] AMBIGUOUS — best={conf:.2f} vs look-alike="
                     f"{res.get('second', 0):.2f}. This is your wrong-click risk: "
                     f"lock a region or use a more distinctive anchor.")
        else:
            self.log(f"[test] found (conf={conf:.2f}) — it would click {res.get('center')}")
        box = res.get("box")
        if box:
            self._flash_box(box, ok=not res.get("ambiguous"))
        elif res.get("center"):
            cx, cy = res["center"]
            self._flash_box((cx - 40, cy - 20, cx + 40, cy + 20),
                            ok=not res.get("ambiguous"))

    def _flash_box(self, box, ok=True) -> None:
        """Briefly draw a highlight rectangle on screen where it would click."""
        x1, y1, x2, y2 = box
        w, h = max(1, x2 - x1), max(1, y2 - y1)
        color = THEME["ok"] if ok else THEME["warn"]
        try:
            ov = tk.Toplevel(self)
            ov.overrideredirect(True)
            ov.attributes("-topmost", True)
            ov.attributes("-alpha", 0.35)
            ov.geometry(f"{w}x{h}+{x1}+{y1}")
            ov.configure(bg=color)
            ov.after(1400, ov.destroy)
        except Exception as e:
            self.log(f"[test] (could not draw highlight: {e})")

    # -- file --------------------------------------------------------------
    def _confirm_discard(self) -> bool:
        """Return True if it's OK to throw away the current macro."""
        if not self._dirty or not self.steps:
            return True
        ans = messagebox.askyesnocancel(
            "Unsaved changes",
            "Save changes to this macro before continuing?")
        if ans is None:        # Cancel
            return False
        if ans:                # Yes -> save; proceed only if it actually saved
            return self.on_save()
        return True            # No -> discard

    def on_new(self) -> None:
        if not self._confirm_discard():
            return
        self.steps = []
        self.macro_name = "untitled"
        self.region = None
        self._dirty = False
        self.refresh()

    def on_open(self) -> None:
        if not self._confirm_discard():
            return
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
        self._dirty = False
        self.refresh()
        self.log(f"Opened {os.path.basename(path)} ({len(self.steps)} steps).")

    def on_save(self) -> bool:
        """Returns True if the macro was written, False if cancelled."""
        import json
        default = os.path.join(MACROS, f"{self.macro_name}.json")
        os.makedirs(MACROS, exist_ok=True)
        path = filedialog.asksaveasfilename(
            initialdir=MACROS, initialfile=os.path.basename(default),
            defaultextension=".json", filetypes=[("Macro JSON", "*.json")])
        if not path:
            return False
        try:
            repeat = int(self.loop_var.get())
        except ValueError:
            repeat = 1
        self.macro_name = os.path.splitext(os.path.basename(path))[0]
        data = {"name": self.macro_name, "repeat": repeat,
                "region": self.region, "steps": self.steps}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        self._dirty = False
        self.refresh()
        self.log(f"Saved {os.path.basename(path)}.")
        return True

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
        if self._running or self._recording:
            if not messagebox.askyesno(
                    "Quit", "A macro is still running. Quit anyway?"):
                return
        elif not self._confirm_discard():
            return
        self._save_settings()   # remember window size/position + theme
        self.panic.set()
        for lst in (getattr(self, "_klistener", None),
                    getattr(self, "_hk_listener", None)):
            try:
                if lst is not None:
                    lst.stop()
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
    if action == "ask":
        return {"action": "ask", "target": "",
                "args": {"message": "Enter a value:", "default": ""}}
    if action == "read_text":
        return {"action": "read_text", "target": "", "threshold": 0.30}
    return {"action": "wait", "args": {"seconds": 1.0}}


def _is_default(key: str, val) -> bool:
    # trim Step defaults so saved JSON stays clean
    defaults = {"target": "", "args": {}, "timeout": 5.0,
                "threshold": 0.80, "on_fail": "abort"}
    return key in defaults and val == defaults[key]


# Plain-English step menu (label -> engine action; None = section header).
FRIENDLY_STEPS = [
    ("—  Click something  —", None),
    ("Click an image on screen", "find_click"),
    ("Click text (reads the screen)", "find_text_click"),
    ("Click an object (AI model)", "find_object_click"),
    ("Click a fixed X, Y spot", "click"),
    ("—  Wait  —", None),
    ("Wait for an image to appear", "wait_for"),
    ("Wait for text to appear", "wait_for_text"),
    ("Wait for an object to appear", "wait_for_object"),
    ("Wait a set amount of time", "wait"),
    ("—  Type  —", None),
    ("Type some text", "type"),
    ("Press keys (e.g. Ctrl+S)", "hotkey"),
    ("—  Only-if / repeat  —", None),
    ("If found… (run steps only when on screen)", "if"),
    ("Else", "else"),
    ("End if", "end_if"),
    ("Repeat a block (loop)…", "loop"),
    ("End loop", "end_loop"),
    ("Break out of the loop", "break"),
    ("Skip to the next loop pass", "continue"),
    ("—  Advanced  —", None),
    ("Set a variable", "set"),
    ("Ask me for a value (prompt)", "ask"),
    ("Read screen text into a variable", "read_text"),
    ("Run another saved macro", "call"),
]


class Tooltip:
    """Tiny hover tooltip for a widget."""

    def __init__(self, widget, text):
        self.widget, self.text, self.tip = widget, text, None
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)

    def _show(self, _):
        if self.tip:
            return
        x = self.widget.winfo_rootx() + 8
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip = tk.Toplevel(self.widget)
        self.tip.overrideredirect(True)
        self.tip.attributes("-topmost", True)
        self.tip.geometry(f"+{x}+{y}")
        tk.Label(self.tip, text=self.text, bg=THEME["tip_bg"], fg=THEME["tip_fg"],
                 font=("Segoe UI", 9), padx=6, pady=3, relief="solid", bd=1,
                 justify="left").pack()

    def _hide(self, _):
        if self.tip:
            self.tip.destroy()
            self.tip = None


def _dialog_icon(win) -> None:
    """Apply the app icon to a CTkToplevel (needs a small delay on CTk)."""
    ic = _icon_path()
    if ic:
        win.after(250, lambda: win.winfo_exists() and win.iconbitmap(ic))


def ctk_ask(parent, title: str, prompt: str, default: str = "") -> "str | None":
    """A dark CTk text-input dialog. Returns the string, or None if cancelled."""
    win = ctk.CTkToplevel(parent)
    win.title(title)
    win.transient(parent)
    win.geometry("400x180")
    win.grab_set()
    _dialog_icon(win)
    out = {"v": None}
    ctk.CTkLabel(win, text=prompt, wraplength=360, justify="left").pack(
        padx=20, pady=(20, 8), anchor="w")
    var = tk.StringVar(value=default)
    ent = ctk.CTkEntry(win, textvariable=var, width=360)
    ent.pack(padx=20)
    ent.focus_set()

    def ok(*_):
        out["v"] = var.get()
        win.destroy()

    row = ctk.CTkFrame(win, fg_color="transparent")
    row.pack(pady=18)
    ctk.CTkButton(row, text="OK", command=ok, width=90).pack(side="left", padx=6)
    ctk.CTkButton(row, text="Cancel", command=win.destroy, width=90,
                  fg_color=THEME["card"], hover_color=THEME["hover"]).pack(side="left")
    ent.bind("<Return>", ok)
    win.bind("<Escape>", lambda e: win.destroy())
    parent.wait_window(win)
    return out["v"]


def _choose_action(parent) -> str | None:
    dlg = ctk.CTkToplevel(parent)
    dlg.title("Add a step")
    dlg.geometry("380x540")
    dlg.transient(parent)
    dlg.grab_set()
    _dialog_icon(dlg)
    ctk.CTkLabel(dlg, text="What should this step do?",
                 font=ctk.CTkFont(size=15, weight="bold")).pack(
        padx=16, pady=(14, 4), anchor="w")
    out = {"v": None}

    def pick(act):
        out["v"] = act
        dlg.destroy()

    frame = ctk.CTkScrollableFrame(dlg, fg_color="transparent")
    frame.pack(fill="both", expand=True, padx=10, pady=(0, 6))
    for label, act in FRIENDLY_STEPS:
        if act is None:
            ctk.CTkLabel(frame, text=label.strip(" —").upper(),
                         font=ctk.CTkFont(size=11, weight="bold"),
                         text_color=THEME["subtext"]).pack(
                anchor="w", pady=(10, 2), padx=4)
        else:
            ctk.CTkButton(frame, text=label, anchor="w", height=30, corner_radius=6,
                          fg_color=THEME["card"], hover_color=THEME["hover"],
                          text_color=THEME["text"],
                          command=lambda a=act: pick(a)).pack(fill="x", pady=2)
    ctk.CTkButton(dlg, text="Cancel", command=dlg.destroy, width=100,
                  fg_color=THEME["card"], hover_color=THEME["hover"]).pack(pady=(0, 12))
    parent.wait_window(dlg)
    return out["v"]


class StepEditor(ctk.CTkToplevel):
    """Modal editor whose fields depend on the step's action."""

    def __init__(self, parent, step: dict):
        super().__init__(parent)
        self.title("Edit step")
        self.transient(parent)
        self.grab_set()
        self.result: dict | None = None
        self._step = dict(step)
        self._vars: dict[str, tk.StringVar] = {}
        self._parent = parent
        _dialog_icon(self)

        action = self._step.get("action", "wait")
        args = self._step.get("args", {}) or {}
        ctk.CTkLabel(self, text=f"Edit  ·  {action}",
                     font=ctk.CTkFont(size=15, weight="bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", padx=16, pady=(14, 8))

        fields = self._fields_for(action, args)
        for r, (label, key, val) in enumerate(fields, start=1):
            ctk.CTkLabel(self, text=label, text_color=THEME["subtext"]).grid(
                row=r, column=0, sticky="e", padx=(14, 8), pady=4)
            var = tk.StringVar(value=str(val))
            self._vars[key] = var
            if action == "call" and key == "target":
                # dropdown of saved macros (editable — you can still type one
                # you haven't saved yet)
                ctk.CTkComboBox(self, values=_macro_names(), variable=var,
                                width=240).grid(row=r, column=1, pady=4)
            else:
                ctk.CTkEntry(self, textvariable=var, width=240).grid(
                    row=r, column=1, pady=4)
            if key == "region":  # drag-a-box picker
                ctk.CTkButton(self, text="Pick ▢", width=64,
                              fg_color=THEME["card"], hover_color=THEME["hover"],
                              command=lambda v=var: self._pick_region(v)).grid(
                    row=r, column=2, padx=(6, 14))

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.grid(row=len(fields) + 1, column=0, columnspan=3, pady=14)
        ctk.CTkButton(btns, text="OK", command=self._ok, width=90).pack(
            side="left", padx=6)
        ctk.CTkButton(btns, text="Cancel", command=self.destroy, width=90,
                      fg_color=THEME["card"], hover_color=THEME["hover"]).pack(
            side="left")
        parent.wait_window(self)

    def _pick_region(self, var: tk.StringVar) -> None:
        self.withdraw()
        self._parent.withdraw()
        try:
            box = RegionPicker(self._parent).result
        finally:
            self._parent.deiconify()
            self.deiconify(); self.lift(); self.grab_set()
        if box:
            var.set(",".join(str(v) for v in box))

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
        if action == "ask":
            return [("Store in variable", "target", self._step.get("target", "")),
                    ("Prompt message", "message",
                     args.get("message", "Enter a value:")),
                    ("Default (optional)", "default", args.get("default", ""))]
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
            elif action == "ask":
                out["target"] = v.get("target", "").strip()
                args_out = {"message": v.get("message", "").strip()
                            or "Enter a value:"}
                if v.get("default", "").strip():
                    args_out["default"] = v["default"]
                out["args"] = args_out
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


class RegionPicker(tk.Toplevel):
    """Full virtual-desktop overlay: drag a box, get (left, top, width, height)
    in screen coordinates (the same space the engine's regions use)."""

    def __init__(self, parent):
        super().__init__(parent)
        self.result = None
        import mss
        with mss.mss() as s:
            mon = s.monitors[0]  # bounding box of all monitors
        self._ox, self._oy = mon["left"], mon["top"]
        self.overrideredirect(True)
        self.geometry(f'{mon["width"]}x{mon["height"]}+{mon["left"]}+{mon["top"]}')
        self.attributes("-topmost", True)
        try:
            self.attributes("-alpha", 0.30)
        except tk.TclError:
            pass
        self.canvas = tk.Canvas(self, bg="#101018", highlightthickness=0,
                                cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_text(mon["width"] // 2, 40, fill="#cdd6f4",
                                font=("Segoe UI", 16),
                                text="Drag a box around the area to search  ·  "
                                     "Esc to cancel")
        self._sx = self._sy = 0
        self._rect = None
        self.canvas.bind("<ButtonPress-1>", self._down)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._up)
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()
        self.focus_force()
        parent.wait_window(self)

    def _down(self, e):
        self._sx, self._sy = e.x, e.y
        self._rect = self.canvas.create_rectangle(e.x, e.y, e.x, e.y,
                                                  outline="#89b4fa", width=2)

    def _drag(self, e):
        if self._rect:
            self.canvas.coords(self._rect, self._sx, self._sy, e.x, e.y)

    def _up(self, e):
        x1, y1 = min(self._sx, e.x), min(self._sy, e.y)
        x2, y2 = max(self._sx, e.x), max(self._sy, e.y)
        if (x2 - x1) >= 5 and (y2 - y1) >= 5:
            self.result = (x1 + self._ox, y1 + self._oy, x2 - x1, y2 - y1)
        self.destroy()


class HotkeyDialog(ctk.CTkToplevel):
    """Bind global hotkeys to saved macros. Persists to hotkeys.json and
    restarts the app's background listener on every change."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Global hotkeys")
        self.geometry("500x430")
        self.transient(app)
        self.grab_set()
        _dialog_icon(self)
        self._pick = tk.StringVar(value="")  # chosen macro path

        ctk.CTkLabel(self, text="Press a hotkey from any app to run a macro.",
                     font=ctk.CTkFont(size=14, weight="bold")).pack(
            padx=16, pady=(14, 8), anchor="w")

        self.lb = tk.Listbox(self, height=8, activestyle="none",
                             bg=THEME["surface"], fg=THEME["text"],
                             selectbackground=THEME["accent"],
                             selectforeground=THEME["bg"], relief="flat",
                             highlightthickness=0, font=("Consolas", 10))
        self.lb.pack(fill="both", expand=True, padx=16)

        ctk.CTkButton(self, text="Remove selected", command=self._remove,
                      width=150, fg_color=THEME["card"],
                      hover_color=THEME["hover"]).pack(pady=8)

        form = ctk.CTkFrame(self, fg_color="transparent")
        form.pack(fill="x", padx=16)
        ctk.CTkLabel(form, text="Hotkey").pack(side="left")
        self.hk_var = tk.StringVar(value="ctrl+alt+1")
        ctk.CTkEntry(form, textvariable=self.hk_var, width=130).pack(
            side="left", padx=8)
        ctk.CTkButton(form, text="Pick macro…", command=self._choose, width=120,
                      fg_color=THEME["card"], hover_color=THEME["hover"]).pack(
            side="left")
        self.pick_lbl = ctk.CTkLabel(self, text="(no macro chosen)",
                                     text_color=THEME["subtext"])
        self.pick_lbl.pack(anchor="w", padx=16, pady=(6, 0))

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(pady=14)
        ctk.CTkButton(btns, text="Add binding", command=self._add,
                      width=120).pack(side="left", padx=6)
        ctk.CTkButton(btns, text="Close", command=self.destroy, width=90,
                      fg_color=THEME["card"], hover_color=THEME["hover"]).pack(
            side="left")
        self._refresh()

    def _refresh(self):
        self.lb.delete(0, "end")
        self._keys = list(self.app._hotkeys.keys())
        for hk in self._keys:
            self.lb.insert("end", f"{hk:<18}  →  "
                           f"{os.path.basename(self.app._hotkeys[hk])}")
        if not self._keys:
            self.lb.insert("end", "(no hotkeys yet)")

    def _choose(self):
        p = filedialog.askopenfilename(
            initialdir=MACROS, filetypes=[("Macro JSON", "*.json")], parent=self)
        if p:
            self._pick.set(p)
            self.pick_lbl.configure(text=os.path.basename(p))

    def _add(self):
        hk = self.hk_var.get().strip().lower()
        path = self._pick.get()
        if not hk:
            messagebox.showerror("Hotkey", "Type a hotkey, e.g. ctrl+alt+1.",
                                 parent=self)
            return
        if not path:
            messagebox.showerror("Hotkey", "Pick a macro first.", parent=self)
            return
        self.app._hotkeys[hk] = path
        self.app._save_hotkeys()
        self.app._restart_hotkeys()
        self._pick.set("")
        self.pick_lbl.configure(text="(no macro chosen)")
        self._refresh()

    def _remove(self):
        sel = self.lb.curselection()
        if not sel or not self._keys:
            return
        hk = self._keys[sel[0]]
        self.app._hotkeys.pop(hk, None)
        self.app._save_hotkeys()
        self.app._restart_hotkeys()
        self._refresh()


def main():
    if "--selftest" in sys.argv:
        # smoke test (used to verify a packaged build): build the window,
        # pump the event loop briefly, then exit 0.
        app = App()
        app.update_idletasks()
        app.update()
        app.destroy()
        print("selftest OK")
        return
    App().mainloop()


if __name__ == "__main__":
    main()
