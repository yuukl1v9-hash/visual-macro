"""The macro runner: turns a list of Steps into actions.

A Macro is plain data (loaded from JSON), so macros are portable files anyone
can edit or share -- no code. The runner walks the steps with a
sense -> act -> verify loop and checks a panic Event before every step so F12
can stop it instantly, even mid-loop.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable

_VAR_RE = re.compile(r"\$\{(\w+)\}")

from .actuator import Actuator
from .capture import Capture
from .detector import Detector


@dataclass
class Step:
    action: str                       # see _dispatch below for the verbs
    target: str = ""                  # anchor image name, or text to type
    args: dict[str, Any] = field(default_factory=dict)
    timeout: float = 5.0              # for find_click / wait_for
    threshold: float = 0.80           # match confidence required
    on_fail: str = "abort"            # "abort" | "skip" | "retry"


@dataclass
class Macro:
    name: str
    steps: list[Step]
    repeat: int = 1                   # how many times to run the whole list
    region: tuple[int, int, int, int] | None = None  # (l,t,w,h) search area

    @staticmethod
    def load(path: str) -> "Macro":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        steps = []
        for s in data.get("steps", []):
            s = dict(s)
            # tolerate a top-level `cond` on `if` steps -> fold into args
            if "cond" in s:
                s.setdefault("args", {})["cond"] = s.pop("cond")
            steps.append(Step(**s))
        region = data.get("region")
        return Macro(
            name=data.get("name", os.path.basename(path)),
            steps=steps,
            repeat=int(data.get("repeat", 1)),
            region=tuple(region) if region else None,
        )


class Engine:
    def __init__(
        self,
        assets_dir: str,
        panic: threading.Event | None = None,
        log: Callable[[str], None] = print,
        models_dir: str | None = None,
    ):
        self.capture = Capture()
        self.detector = Detector(assets_dir)
        self.actuator = Actuator()
        self.panic = panic or threading.Event()
        self.log = log
        # models/ and macros/ live beside assets/ by default
        root = os.path.dirname(os.path.abspath(assets_dir))
        self.models_dir = models_dir or os.path.join(root, "models")
        self.macros_dir = os.path.join(root, "macros")
        self._text_finder = None  # lazy: OCR model loads on first text step
        self._obj_detectors: dict[str, Any] = {}  # lazy, keyed by model file
        self._submacros: dict[str, tuple] = {}  # cache: path -> (program, region)
        self._call_stack: list[str] = []  # for recursion / cycle detection
        self._vars: dict[str, str] = {}  # variable store
        self._step_no = 1

    # -- public ------------------------------------------------------------
    def run(self, macro: Macro, initial_vars: dict | None = None) -> bool:
        self.log(f"[engine] running '{macro.name}' (capture={self.capture.backend})")
        # fresh variables per run (persist across repeats); a data-driven run
        # seeds them from the current CSV row so ${column} substitutes per row
        self._vars = {str(k): str(v) for k, v in (initial_vars or {}).items()}
        # Compile the flat if/else/end_if step list into nested blocks once.
        program, _ = self._parse(macro.steps, 0, set())
        loops = macro.repeat if macro.repeat > 0 else 1_000_000
        for i in range(loops):
            if self.panic.is_set():
                self.log("[engine] PANIC -- aborting.")
                return False
            if macro.repeat != 1:
                self.log(f"[engine] --- iteration {i + 1} ---")
            self._step_no = 1
            if self._run_nodes(program, macro.region) == "abort":
                return False
        self.log("[engine] done.")
        return True

    # -- control flow ------------------------------------------------------
    def _parse(self, steps, i, stop):
        """Recursive-descent parse of a flat step list into nodes. `if` and
        `loop` become dict nodes with child lists; every other step stays a
        Step. `stop` is the set of marker actions that end this level."""
        nodes: list = []
        while i < len(steps):
            act = steps[i].action
            if act in stop:
                return nodes, i
            if act == "if":
                cond = (steps[i].args or {}).get("cond", {})
                then_nodes, j = self._parse(steps, i + 1, {"else", "end_if"})
                else_nodes: list = []
                if j < len(steps) and steps[j].action == "else":
                    else_nodes, j = self._parse(steps, j + 1, {"end_if"})
                if j < len(steps) and steps[j].action == "end_if":
                    j += 1  # consume the matching end_if
                nodes.append({"action": "if", "cond": cond,
                              "then": then_nodes, "else": else_nodes})
                i = j
            elif act == "loop":
                body, j = self._parse(steps, i + 1, {"end_loop"})
                if j < len(steps) and steps[j].action == "end_loop":
                    j += 1  # consume the matching end_loop
                nodes.append({"action": "loop",
                              "args": (steps[i].args or {}), "body": body})
                i = j
            elif act in ("else", "end_if", "end_loop"):
                self.log(f"[engine] warning: stray '{act}' ignored")
                i += 1
            else:
                nodes.append(steps[i])
                i += 1
        return nodes, i

    def _run_nodes(self, nodes, region) -> str:
        """Execute a node list. Returns a control status:
        'ok' (fell through), 'abort' (stop macro), or 'break'/'continue'
        (propagate up to the nearest enclosing loop)."""
        for node in nodes:
            if self.panic.is_set():
                self.log("[engine] PANIC -- aborting.")
                return "abort"
            if isinstance(node, dict):
                act = node.get("action")
                if act == "if":
                    take = self._eval_cond(node["cond"], region)
                    self.log(f"[if] condition -> {take}")
                    branch = node["then"] if take else node["else"]
                    status = self._run_nodes(branch, region)
                    if status != "ok":
                        return status  # abort/break/continue bubble up
                    continue
                if act == "loop":
                    if self._run_loop(node, region) == "abort":
                        return "abort"
                    continue  # break/continue don't escape the loop
            # leaf Step
            step = node
            if step.action == "break":
                self.log("[loop] break")
                return "break"
            if step.action == "continue":
                self.log("[loop] continue")
                return "continue"
            if step.action == "call":
                if self._run_call(step, region) == "abort":
                    return "abort"
                continue  # break/continue don't escape a sub-macro
            n = self._step_no
            self._step_no += 1
            ok = self._run_step(step, region, n)
            if not ok and step.on_fail == "abort":
                self.log(f"[engine] step {n} failed; aborting macro.")
                return "abort"
        return "ok"

    def _run_loop(self, node, region) -> str:
        """Run a loop body until its cap, its while/until condition, a break,
        or a panic. Returns 'abort' or 'ok'."""
        args = node.get("args", {}) or {}
        body = node["body"]
        try:
            count = int(args.get("count", 0) or 0)  # 0 = unlimited
        except (TypeError, ValueError):
            count = 0
        cond = args.get("cond")
        mode = args.get("mode")  # "while" | "until" | None
        it = 0
        while True:
            if self.panic.is_set():
                return "abort"
            if count > 0 and it >= count:
                self.log(f"[loop] reached max {count} iteration(s)")
                return "ok"
            if cond and mode in ("while", "until"):
                present = self._eval_cond(cond, region)
                keep_going = present if mode == "while" else (not present)
                if not keep_going:
                    self.log(f"[loop] {mode}-condition ended the loop")
                    return "ok"
            it += 1
            self.log(f"[loop] iteration {it}")
            status = self._run_nodes(body, region)
            if status == "abort":
                return "abort"
            if status == "break":
                return "ok"
            # "continue" and "ok" both just proceed to the next iteration

    # -- sub-macros --------------------------------------------------------
    _MAX_CALL_DEPTH = 20

    def _resolve_macro_path(self, name: str) -> str:
        path = name if os.path.isabs(name) else os.path.join(self.macros_dir, name)
        if not path.lower().endswith(".json"):
            path += ".json"
        return path

    def _load_submacro(self, path: str):
        """Load + parse a macro file once; cache (program, region) by path."""
        if path in self._submacros:
            return self._submacros[path]
        macro = Macro.load(path)
        program, _ = self._parse(macro.steps, 0, set())
        self._submacros[path] = (program, macro.region)
        return self._submacros[path]

    def _run_call(self, step: Step, region) -> str:
        """Run another macro as a subroutine. Returns 'abort' or 'ok'.
        break/continue inside the sub-macro do not escape to the caller."""
        name = (step.target or "").strip()
        if not name:
            self.log("[call] no macro name given; skipping")
            return "ok"
        path = self._resolve_macro_path(name)

        if path in self._call_stack:
            self.log(f"[call] recursion cycle on '{name}'; skipping")
            return "ok"
        if len(self._call_stack) >= self._MAX_CALL_DEPTH:
            self.log(f"[call] max call depth {self._MAX_CALL_DEPTH} reached; skipping")
            return "ok"

        try:
            program, sub_region = self._load_submacro(path)
        except Exception as e:
            self.log(f"[call] could not load '{name}': {e}")
            return "ok"

        try:
            repeat = int((step.args or {}).get("repeat", 1) or 1)
        except (TypeError, ValueError):
            repeat = 1

        eff_region = sub_region or region  # sub-macro's own region wins
        self.log(f"[call] -> {os.path.basename(path)} (x{repeat})")
        self._call_stack.append(path)
        try:
            for _ in range(repeat if repeat > 0 else 1_000_000):
                if self.panic.is_set():
                    return "abort"
                if self._run_nodes(program, eff_region) == "abort":
                    return "abort"
        finally:
            self._call_stack.pop()
        return "ok"

    def _eval_cond(self, cond: dict, region) -> bool:
        """A condition is 'is this image/text on screen?' or a variable test,
        with optional negate."""
        ctype = cond.get("type", "image")
        if ctype == "var":
            result = self._eval_var_cond(cond)
            return (not result) if cond.get("negate") else result
        target = self._expand(cond.get("target", ""))
        default_thr = 0.80 if ctype == "image" else 0.50
        probe = Step(
            action="_probe",
            target=target,
            threshold=float(cond.get("threshold", default_thr)),
            timeout=float(cond.get("timeout", 2.0)),  # short: it's a check
        )
        eff = cond.get("region") or region
        eff = tuple(eff) if eff else None
        if ctype == "text":
            present = self._locate_text(probe, eff) is not None
        else:
            present = self._locate(probe, eff) is not None
        return (not present) if cond.get("negate") else present

    def _eval_var_cond(self, cond: dict) -> bool:
        """Test a variable: eq/ne/contains/gt/lt/ge/le/set/empty."""
        name = cond.get("name") or cond.get("target", "")
        op = cond.get("op", "set")
        raw = self._vars.get(name)
        val = self._expand(str(cond.get("value", "")))
        if op == "set":
            return raw not in (None, "")
        if op == "empty":
            return raw in (None, "")
        cur = raw if raw is not None else ""
        if op == "eq":
            return cur == val
        if op == "ne":
            return cur != val
        if op == "contains":
            return val in cur
        if op in ("gt", "lt", "ge", "le"):
            a, b = self._to_num(cur), self._to_num(val)
            return {"gt": a > b, "lt": a < b, "ge": a >= b, "le": a <= b}[op]
        self.log(f"[if] unknown var op '{op}'")
        return False

    # -- variable helpers --------------------------------------------------
    def _expand(self, s):
        """Replace ${name} with variable values (empty if unset)."""
        if not isinstance(s, str):
            return s
        return _VAR_RE.sub(lambda m: str(self._vars.get(m.group(1), "")), s)

    @staticmethod
    def _to_num(s) -> float:
        try:
            return float(s)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _num_str(x: float) -> str:
        return str(int(x)) if float(x).is_integer() else str(x)

    def _set_var(self, name: str, op: str, val: str) -> None:
        if op in ("add", "sub", "mul", "div"):
            cur, v = self._to_num(self._vars.get(name, "0")), self._to_num(val)
            if op == "add":
                cur += v
            elif op == "sub":
                cur -= v
            elif op == "mul":
                cur *= v
            elif op == "div":
                cur = cur / v if v else cur
            self._vars[name] = self._num_str(cur)
        else:  # assign
            self._vars[name] = val
        self.log(f"[set] {name} = {self._vars[name]!r}")

    # -- internals ---------------------------------------------------------
    def _run_step(self, step: Step, region, n: int) -> bool:
        self.log(f"[step {n}] {step.action} {step.target!r}")
        try:
            return self._dispatch(step, region)
        except Exception as e:  # a bad step should not crash the whole run
            self.log(f"[step {n}] error: {e}")
            return False

    def _dispatch(self, step: Step, region) -> bool:
        a = step.args
        if step.action == "wait":
            time.sleep(float(a.get("seconds", 1.0)))
            return True

        if step.action == "set":  # target = var name (NOT expanded)
            self._set_var(step.target, a.get("op", "assign"),
                          self._expand(str(a.get("value", ""))))
            return True

        if step.action == "read_text":  # OCR a region into a variable
            eff = a.get("region") or region
            eff = tuple(eff) if eff else None
            self._vars[step.target] = self._read_text(step, eff)
            self.log(f"[read_text] {step.target} = {self._vars[step.target]!r}")
            return True

        if step.action == "type":
            self.actuator.type_text(self._expand(step.target))
            return True

        if step.action == "hotkey":
            self.actuator.hotkey(*[self._expand(k) for k in a.get("keys", [])])
            return True

        if step.action == "click":  # fixed coordinates (fallback mode)
            self.actuator.click(int(a["x"]), int(a["y"]),
                                a.get("button", "left"), int(a.get("clicks", 1)))
            return True

        # detection steps: expand ${vars} in the target (image name / text / class)
        if step.target:
            step = replace(step, target=self._expand(step.target))

        if step.action in ("find_click", "wait_for"):
            match = self._locate(step, region)
            if not match:
                return False
            if step.action == "find_click":
                ox, oy = self.capture.region_origin(region)
                self.actuator.click(match[0] + ox, match[1] + oy,
                                    a.get("button", "left"), int(a.get("clicks", 1)))
            return True

        if step.action in ("find_text_click", "wait_for_text"):
            # OCR is slow full-screen, so allow a per-step region override.
            eff = a.get("region") or region
            eff = tuple(eff) if eff else None
            match = self._locate_text(step, eff)
            if not match:
                return False
            if step.action == "find_text_click":
                ox, oy = self.capture.region_origin(eff)
                self.actuator.click(match[0] + ox, match[1] + oy,
                                    a.get("button", "left"), int(a.get("clicks", 1)))
            return True

        if step.action in ("find_object_click", "wait_for_object"):
            eff = a.get("region") or region
            eff = tuple(eff) if eff else None
            match = self._locate_object(step, eff)
            if not match:
                return False
            if step.action == "find_object_click":
                ox, oy = self.capture.region_origin(eff)
                self.actuator.click(match[0] + ox, match[1] + oy,
                                    a.get("button", "left"), int(a.get("clicks", 1)))
            return True

        self.log(f"[engine] unknown action: {step.action}")
        return False

    def _locate(self, step: Step, region) -> tuple[int, int] | None:
        """Poll for the anchor until found or timeout. Returns center in frame
        coords, or None.

        Refuses an *ambiguous* match (a look-alike scored nearly as high) unless
        the step opts in with args.allow_ambiguous -- this is the guard against
        clicking the wrong button. args.multi_scale enables DPI/size-tolerant
        matching."""
        a = step.args or {}
        multi = bool(a.get("multi_scale", False))
        edges = bool(a.get("edges", False))
        allow_ambig = bool(a.get("allow_ambiguous", False))
        deadline = time.perf_counter() + step.timeout
        best = 0.0
        prev_sig = None
        while time.perf_counter() < deadline:
            if self.panic.is_set():
                return None
            frame = self.capture.grab(region)
            # change-gate: if the screen hasn't changed since the last check,
            # skip the (comparatively costly) match entirely.
            sig = frame[::16, ::16].tobytes()
            if sig == prev_sig:
                time.sleep(0.05)
                continue
            prev_sig = sig
            m = self.detector.find(frame, step.target, step.threshold, multi, edges)
            best = max(best, m.confidence)
            if m.found and m.ambiguous and not allow_ambig:
                self.log(f"          AMBIGUOUS: best={m.confidence:.2f} vs "
                         f"look-alike={m.second:.2f} — refusing to click. "
                         f"Lock a region or use a more distinctive anchor.")
                time.sleep(0.05)
                continue
            if m.found:
                self.log(f"          found (conf={m.confidence:.2f}) at {m.center}")
                return m.center
            time.sleep(0.05)  # ~20 checks/sec is plenty for UI
        self.log(f"          NOT found (best conf={best:.2f}, need {step.threshold})")
        return None

    def test_step(self, step: Step, region) -> dict:
        """Run a single detection step WITHOUT clicking, for the UI 'Test'
        button. Returns a result dict incl. the match box in SCREEN coords."""
        a = step.args or {}
        eff = a.get("region") or region
        eff = tuple(eff) if eff else None
        step = replace(step, target=self._expand(step.target))
        ox, oy = self.capture.region_origin(eff)
        frame = self.capture.grab(eff)
        out = {"action": step.action, "found": False, "confidence": 0.0,
               "ambiguous": False, "second": 0.0, "box": None, "center": None}
        try:
            if step.action in ("find_click", "wait_for"):
                m = self.detector.find(frame, step.target, step.threshold,
                                       bool(a.get("multi_scale", False)),
                                       bool(a.get("edges", False)))
                out.update(found=m.found, confidence=m.confidence,
                           ambiguous=m.ambiguous, second=m.second)
                if m.box:
                    x1, y1, x2, y2 = m.box
                    out["box"] = (x1 + ox, y1 + oy, x2 + ox, y2 + oy)
                    out["center"] = (m.center[0] + ox, m.center[1] + oy)
            elif step.action in ("find_text_click", "wait_for_text"):
                if self._text_finder is None:
                    from .ocr import TextFinder
                    self._text_finder = TextFinder()
                m = self._text_finder.find(frame, step.target, step.threshold)
                out.update(found=m.found, confidence=m.confidence)
                if m.found:
                    out["center"] = (m.center[0] + ox, m.center[1] + oy)
            elif step.action in ("find_object_click", "wait_for_object"):
                det = self._get_object_detector(a.get("model"))
                if det:
                    m = det.find(frame, step.target, step.threshold)
                    out.update(found=m.found, confidence=m.confidence)
                    if m.found:
                        out["center"] = (m.center[0] + ox, m.center[1] + oy)
            else:
                out["error"] = "not a detection step"
        except Exception as e:
            out["error"] = str(e)
        return out

    def _locate_text(self, step: Step, region) -> tuple[int, int] | None:
        """Poll OCR for step.target text until found or timeout."""
        if self._text_finder is None:
            from .ocr import TextFinder
            try:
                self._text_finder = TextFinder()
            except Exception as e:
                self.log(f"          OCR unavailable: {e}")
                return None
        deadline = time.perf_counter() + step.timeout
        best = 0.0
        # OCR is heavier than template match; poll a bit slower.
        while time.perf_counter() < deadline:
            if self.panic.is_set():
                return None
            frame = self.capture.grab(region)
            try:
                m = self._text_finder.find(frame, step.target, step.threshold)
            except Exception as e:
                self.log(f"          OCR error: {e}")
                return None
            best = max(best, m.confidence)
            if m.found:
                self.log(f"          text found (conf={m.confidence:.2f}) "
                         f"at {m.center}")
                return m.center
            time.sleep(0.15)
        self.log(f"          text NOT found (best conf={best:.2f})")
        return None

    def _read_text(self, step: Step, region) -> str:
        """OCR a region and return the text found (for read_text steps)."""
        if self._text_finder is None:
            from .ocr import TextFinder
            try:
                self._text_finder = TextFinder()
            except Exception as e:
                self.log(f"          OCR unavailable: {e}")
                return ""
        try:
            frame = self.capture.grab(region)
            return self._text_finder.read(frame, step.threshold)
        except Exception as e:
            self.log(f"          OCR read error: {e}")
            return ""

    def _get_object_detector(self, model_name: str | None):
        """Lazily build (and cache) an ObjectDetector for a model file.
        model_name is a filename in models_dir; if omitted, the first *.onnx
        found there is used. Returns None with a logged reason on failure."""
        import glob as _glob

        if not model_name:
            found = sorted(_glob.glob(os.path.join(self.models_dir, "*.onnx")))
            if not found:
                self.log(f"          no .onnx model in {self.models_dir} "
                         f"(see models/README.md)")
                return None
            model_path = found[0]
        else:
            model_path = (model_name if os.path.isabs(model_name)
                          else os.path.join(self.models_dir, model_name))

        if model_path in self._obj_detectors:
            return self._obj_detectors[model_path]
        from .ml_detector import ObjectDetector
        try:
            det = ObjectDetector(model_path)
            det._ensure()  # surface load errors now, not mid-loop
        except Exception as e:
            self.log(f"          object detector unavailable: {e}")
            return None
        self._obj_detectors[model_path] = det
        self.log(f"          loaded model: {os.path.basename(model_path)}")
        return det

    def _locate_object(self, step: Step, region) -> tuple[int, int] | None:
        """Poll the ML detector for step.target (a class label) until found."""
        det = self._get_object_detector((step.args or {}).get("model"))
        if det is None:
            return None
        deadline = time.perf_counter() + step.timeout
        best = 0.0
        while time.perf_counter() < deadline:
            if self.panic.is_set():
                return None
            frame = self.capture.grab(region)
            try:
                m = det.find(frame, step.target, step.threshold)
            except Exception as e:
                self.log(f"          object detect error: {e}")
                return None
            best = max(best, m.confidence)
            if m.found:
                self.log(f"          object '{step.target}' found "
                         f"(conf={m.confidence:.2f}) at {m.center}")
                return m.center
            time.sleep(0.12)
        self.log(f"          object '{step.target}' NOT found "
                 f"(best conf={best:.2f})")
        return None
