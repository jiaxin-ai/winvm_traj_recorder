#!/usr/bin/env python3
"""AutoCAD adapter. Design: task-adapter-autocad.md (same directory);
interface and record format: adapters/software_trajectory_collector_specification.md.

Connects to an already running AutoCAD (acad.exe) through COM, read-only:
  - get_state(): ActiveDocument properties + GetVariable, queried on the
    Recorder's adapter thread.
  - get_actions()/get_events(): COM events (one Application sink + one sink
    per open Document). The adapter thread is initialized as MTA, so AutoCAD's
    event calls arrive on COM's RPC threads without this thread pumping
    messages; the callbacks only stamp t_ms and append to a deque. Plus the
    official command-line log (LOGFILEMODE=1, set by the operator
    beforehand) for cancels and errors.

Never calls into AutoCAD from inside an event callback, never sets a system
variable, runs a command, changes the selection or writes a file.

Only stdlib at module level: software.py imports this file on the
Recorder's main thread (and on macOS during development). pywin32 is
imported inside attach(), on the adapter's own thread, because the first
`import pythoncom` initializes COM on the importing thread.

Loaded by file path (software.py), so no relative imports.

Debug entry (spec 3.3), from the project root:
    python -m adapters.autocad --probe [--repeat N]
    python -m adapters.autocad --watch
"""
import argparse
import codecs
import collections
import json
import math
import ntpath
import os
import re
import sys
import time
import traceback
import types

NAME = "AutoCAD"


def _now_ms():
    return time.time_ns() // 1_000_000


# -- tunables ----------------------------------------------------------------
PROGIDS = ("AutoCAD.Application.25.1", "AutoCAD.Application", "AutoCAD.Application.25",
           "AutoCAD.Application.24.3", "AutoCAD.Application.24.2", "AutoCAD.Application.24.1",
           "AutoCAD.Application.24")
SELECTION_LIMIT = 50            # state.selection items read per get_state()
STATE_BUDGET_S = 0.35           # stop enumerating the selection after this (Recorder budget 500 ms)
DRAIN_BUDGET_S = 0.15           # own budget inside get_actions/get_events (Recorder budget 200 ms)
PROBE_TIMEOUT_MS = 100          # WM_NULL responsiveness probe
MAX_PER_CALL = 200              # spec 6: records per get_events()/get_actions()
QUEUE_LIMIT = 2000              # spec 6: aggregate beyond this
OBJECT_CACHE_LIMIT = 100_000    # ObjectID -> (Handle, ObjectName), for ObjectErased
NEW_DRAWING_PAIR_MS = 10_000    # NewDrawing timestamps not paired with a new document by then are dropped
STALE_UNRESOLVED_MS = 5_000     # unresolved object events emitted without handle when AutoCAD stays busy
ARCHIVE_KEEP_MS = 60_000        # info of closed documents kept for late callbacks
SELF_CHECK_MISSES = 2           # distinct running commands seen without any BeginCommand -> events broken
LOG_READ_MAX_BYTES = 256 * 1024
ERROR_LOG_INTERVAL_S = 60
SAVE_EXTENSIONS = (".dwg", ".dxf", ".dwt", ".dws")
SYSVAR_IGNORE = frozenset()     # fill from acceptance runs; keep capabilities.yaml/README in sync
INITIAL_SYSVARS = ("CLAYER", "CTAB", "OSMODE", "ORTHOMODE", "SNAPMODE", "GRIDMODE", "POLARMODE", "DYNMODE")
COALESCE_TYPES = frozenset({"object_modified", "selection_changed", "sysvar_changed"})

APP_EVENTS = frozenset({"BeginCommand", "EndCommand", "BeginLisp", "EndLisp", "LispCancelled",
                        "NewDrawing", "EndOpen", "EndSave", "SysVarChanged"})
DOC_EVENTS = frozenset({"ObjectAdded", "ObjectModified", "ObjectErased", "SelectionChanged",
                        "BeginClose", "Activate", "LayoutSwitched"})

# -- HRESULTs ------------------------------------------------------------------
RPC_E_CHANGED_MODE = -2147417850            # 0x80010106
RPC_E_CALL_REJECTED = -2147418111           # 0x80010001
RPC_E_SERVERCALL_RETRYLATER = -2147417846   # 0x8001010A
RPC_S_SERVER_UNAVAILABLE = -2147023174      # 0x800706BA
RPC_E_DISCONNECTED = -2147417848            # 0x80010108
CO_E_OBJNOTCONNECTED = -2147221251          # 0x800401FD
DISP_E_MEMBERNOTFOUND = -2147352573         # 0x80020003
DISP_E_UNKNOWNNAME = -2147352570            # 0x80020006
BUSY_HRESULTS = frozenset({RPC_E_CALL_REJECTED, RPC_E_SERVERCALL_RETRYLATER})
DEAD_HRESULTS = frozenset({RPC_S_SERVER_UNAVAILABLE, RPC_E_DISCONNECTED, CO_E_OBJNOTCONNECTED})

IMPLTYPEFLAG_FDEFAULT = 1
IMPLTYPEFLAG_FSOURCE = 2
WM_NULL = 0
SMTO_ABORTIFHUNG = 2

# -- value mappings ------------------------------------------------------------
INSUNITS_NAMES = {
    0: "unitless", 1: "inches", 2: "feet", 3: "miles", 4: "millimeters", 5: "centimeters",
    6: "meters", 7: "kilometers", 8: "microinches", 9: "mils", 10: "yards", 11: "angstroms",
    12: "nanometers", 13: "microns", 14: "decimeters", 15: "dekameters", 16: "hectometers",
    17: "gigameters", 18: "astronomical_units", 19: "light_years", 20: "parsecs",
    21: "us_survey_feet",
}
# CMDACTIVE bits in priority order (16 = DDE is not mapped on purpose)
CMDACTIVE_MODES = ((8, "dialog"), (4, "script"), (2, "transparent_command"), (1, "command_active"),
                   (64, "arx_command"), (32, "lisp"))

STATE_FIELDS = ("active_document", "selection", "selection_count", "selection_truncated", "mode",
                "active_command", "active_layer", "active_layout", "ucs", "units", "entity_count",
                "document_count", "unsaved_changes", "read_only", "view_center", "view_height")

# -- command-line log patterns (task-adapter-autocad.md 6.3) -------------------
# Chinese prompt/cancellation escapes checked against a Windows log sample.
# Unknown-command and LISP-error patterns still require live validation.
_C = "[:：]"
LOG_PATTERNS = {
    "CHS": {
        "command": re.compile(rf"^命令\s*{_C}\s*(\S+)\s*$"),
        "idle": re.compile(rf"^命令\s*{_C}\s*$"),
        "cancel": re.compile(r"\*取消\*\s*$"),        # searched: usually follows a prompt on the same line
        "unknown": re.compile(r"^未知命令\s*[“\"](.+?)[”\"]"),
        "lisp_error": re.compile(rf"^;\s*错误\s*{_C}\s*(.*)$"),
        "result": (re.compile(r"找到\s*\d+\s*个"), re.compile(r"总计\s*\d+\s*个")),
        "input": re.compile(rf"^(?P<prompt>(?:指定|输入|选择)[^:：]*?(?:\[[^\]]*\])?\s*(?:<[^>]*>)?)\s*{_C}\s*(?P<input>\S.*)$"),
    },
    "ENU": {
        "command": re.compile(r"^Command:\s*(\S+)\s*$"),
        "idle": re.compile(r"^Command:\s*$"),
        "cancel": re.compile(r"\*Cancel\*\s*$"),
        "unknown": re.compile(r'^Unknown command "(.+)"\.'),
        "lisp_error": re.compile(r"^; error:\s*(.*)$"),
        "result": (re.compile(r"^\d+ found"), re.compile(r"^\d+ found, \d+ total"),
                   re.compile(r"^\d+ (was|were) not in current space")),
        "input": re.compile(r"^(?P<prompt>[A-Z][^:]*?(?:\[[^\]]*\])?\s*(?:<[^>]*>)?):\s*(?P<input>\S.*)$"),
    },
}
# "prompt: input" lines can misclassify software output as user input, so
# they stay off until real log samples confirm the patterns above.
LOG_INPUT_RULE = {"CHS": False, "ENU": False}
# Log files without a BOM that aren't valid UTF-8 are decoded with the code
# page of AutoCAD's UI language, not the Windows system code page (the two
# can differ, e.g. Chinese AutoCAD on an English Windows).
LOG_ANSI_ENCODING = {"CHS": "gbk", "ENU": "cp1252"}
# Observed EN installation writes Chinese escaped prompts and cp1252 quotes.
# Parse prompt language from each line rather than assuming EN means English.
LOG_PATTERNS["EN"] = LOG_PATTERNS["ENU"]
LOG_ANSI_ENCODING["EN"] = "cp1252"
LOG_INPUT_RULE["EN"] = False


def decode_log_escapes(line):
    return re.sub(r"\\U\+([0-9A-Fa-f]{4})", lambda m: chr(int(m.group(1), 16)), line)


# -- pure helpers (no pywin32; checked on macOS) -------------------------------
def mode_from_cmdactive(value):
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    for bit, name in CMDACTIVE_MODES:
        if v & bit:
            return name
    return "idle"


def units_name(value):
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    return INSUNITS_NAMES.get(v, f"insunits_{v}")


def ucs_value(ucsname, worlducs):
    if ucsname:
        return str(ucsname)
    if worlducs is None:
        return None
    return "WORLD" if int(worlducs) == 1 else "UNNAMED"


def jsonable(v):
    """COM VARIANT -> something json.dumps(allow_nan=False) accepts."""
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, (tuple, list)):
        return [jsonable(x) for x in v]
    return str(v)


def normalize_command(name):
    """'_line' / '.LINE' / "'zoom" -> 'LINE' / 'LINE' / 'ZOOM' (for matching only)."""
    return str(name or "").strip().lstrip("_.'").upper()


def lisp_action(first_line):
    """BeginLisp(FirstLine) -> (type, name)."""
    expr = str(first_line or "")
    m = re.match(r"^\(\s*C:([^\s()]+)\s*\)\s*$", expr, re.IGNORECASE)
    if m:
        return "command", m.group(1).upper()
    m = re.match(r"^\(\s*([^\s()]+)", expr)
    return "api_call", (m.group(1) if m else "AutoLISP")


def make_record(t_ms, type_, name, params, source, raw):
    return {"t_ms": t_ms, "software": NAME, "type": type_, "name": name,
            "params": params, "source": source, "raw": raw}


class RecordQueue:
    """Pending records of one kind (actions or events). Implements spec 6:
    at most MAX_PER_CALL per take(), consecutive identical records of
    COALESCE_TYPES merged with params.repeat, and beyond QUEUE_LIMIT the
    newest records aggregated per type."""

    def __init__(self, log):
        self.items = collections.deque()
        self._log = log
        self.coalesced = 0
        self.aggregated = 0

    def __len__(self):
        return len(self.items)

    def push(self, record):
        self.items.append(record)

    def enforce_limit(self):
        if len(self.items) <= QUEUE_LIMIT:
            return
        excess = []
        while len(self.items) > QUEUE_LIMIT:
            excess.append(self.items.pop())
        excess.reverse()
        groups = collections.OrderedDict()
        for r in excess:
            g = groups.setdefault(r["type"], {"n": 0, "first": r["t_ms"], "last": r["t_ms"], "source": r["source"]})
            g["n"] += 1
            g["first"] = min(g["first"], r["t_ms"])
            g["last"] = max(g["last"], r["t_ms"])
        for type_, g in groups.items():
            self.items.append(make_record(
                g["first"], type_, type_,
                {"aggregated": g["n"], "first_t_ms": g["first"], "last_t_ms": g["last"]},
                g["source"], f"aggregated {g['n']} {type_} records"))
        self.aggregated += len(excess)
        self._log(f"队列超过 {QUEUE_LIMIT} 条,{len(excess)} 条记录按类型聚合为 {len(groups)} 条: "
                  + ", ".join(f"{t}×{g['n']}" for t, g in groups.items()))

    @staticmethod
    def _key(r):
        params = {k: v for k, v in r["params"].items() if k != "repeat"}
        return (r["type"], r["name"], r["source"], json.dumps(params, sort_keys=True, default=str))

    def take(self, limit=MAX_PER_CALL):
        out = []
        last_key = None
        while self.items:
            r = self.items[0]
            key = self._key(r) if r["type"] in COALESCE_TYPES and "aggregated" not in r["params"] else None
            if key is not None and key == last_key:
                self.items.popleft()
                prev = out[-1]
                prev["params"] = dict(prev["params"])
                prev["params"]["repeat"] = prev["params"].get("repeat", 1) + r["params"].get("repeat", 1)
                self.coalesced += 1
                continue
            if len(out) >= limit:
                break
            out.append(self.items.popleft())
            last_key = key
        return out


class LogTail:
    """Incrementally reads one AutoCAD command-line log file. Opens, reads
    and closes on every call (never keeps a handle), so AutoCAD's writes
    are not disturbed."""

    def __init__(self, path, offset, ansi_encoding):
        self.path = path
        self.offset = offset
        self.ansi_encoding = ansi_encoding
        self.encoding = None       # decided from the BOM or the first non-ASCII bytes
        self._bom_checked = False
        self._decoder = None
        self._pending = ""
        self.disabled = False
        self.parser = None

    def _set_encoding(self, encoding):
        self.encoding = encoding
        self._decoder = codecs.getincrementaldecoder(encoding)(errors="replace")

    def read_lines(self, max_bytes=LOG_READ_MAX_BYTES):
        """New complete lines since the last call. Raises OSError."""
        size = os.path.getsize(self.path)
        reset = False
        if size < self.offset:
            self.offset, self._pending, self._bom_checked = 0, "", False
            self.encoding = self._decoder = None
            reset = True
        if size == self.offset:
            return [], reset
        with open(self.path, "rb") as f:
            if not self._bom_checked:
                head = f.read(3)
                self._bom_checked = True
                if head[:2] == b"\xff\xfe":
                    self._set_encoding("utf-16-le")
                    self.offset = max(self.offset, 2)
                elif head[:3] == b"\xef\xbb\xbf":
                    self._set_encoding("utf-8")
                    self.offset = max(self.offset, 3)
                if self.encoding == "utf-16-le" and self.offset % 2:
                    self.offset -= 1
            f.seek(self.offset)
            data = f.read(max_bytes)
        self.offset += len(data)
        if self.encoding is None:
            if data.isascii():
                text = data.decode("ascii")
            else:
                probe = codecs.getincrementaldecoder("utf-8")(errors="strict")
                try:
                    probe.decode(data, final=False)
                    self._set_encoding("utf-8")
                except UnicodeDecodeError:
                    self._set_encoding(self.ansi_encoding)
                text = self._decoder.decode(data)
        else:
            text = self._decoder.decode(data)
        parts = (self._pending + text).split("\n")
        self._pending = parts.pop()
        return [p.rstrip("\r") for p in parts], reset


class LogParser:
    """Classifies log lines (task-adapter-autocad.md 6.3). Keeps the command
    currently echoed in this log file as context."""

    def __init__(self, patterns):
        self.p = patterns
        self.command = None

    def parse(self, line):
        s = decode_log_escapes(line).strip()
        if not s:
            return None
        p = self.p
        # LOCALE can describe a different language than the actual log text.
        for candidate in (LOG_PATTERNS["CHS"], LOG_PATTERNS["ENU"]):
            if any(candidate[k].search(s) for k in ("command", "idle", "cancel", "unknown", "lisp_error")):
                p = candidate
                self.p = p
                break
        # Check cancellation first: "命令: *取消*" also matches command.
        if p["cancel"].search(s):
            cmd, self.command = self.command, None
            return ("cancel", cmd)
        m = p["command"].match(s)
        if m:
            self.command = normalize_command(m.group(1))
            return ("command", self.command)
        if p["idle"].match(s):
            self.command = None
            return None
        m = p["unknown"].match(s)
        if m:
            return ("unknown_command", m.group(1).strip())
        m = p["lisp_error"].match(s)
        if m:
            return ("lisp_error", m.group(1).strip())
        m = p["input"].match(s)
        if m and self.command:
            value = m.group("input").strip()
            if not any(r.search(value) for r in p["result"]):
                return ("input", m.group("prompt").strip(), value)
        return None


# -- COM event sink ------------------------------------------------------------
def _make_handler(raw, event_name, doc_key):
    def handler(*args):
        # Runs on a COM RPC thread while AutoCAD waits: stamp, enqueue, return.
        raw.append((time.time_ns() // 1_000_000, event_name, args, doc_key))
    return handler


class _Sink:
    """Wrapped with win32com.server.util.wrap (DesignatedWrapPolicy): every
    dispid of the event interface maps to a method, so AutoCAD never gets
    DISP_E_MEMBERNOTFOUND; events we don't record go to _noop. Arguments
    arrive raw (PyIDispatch for objects); nothing here calls AutoCAD."""

    def __init__(self, iid, dispid_names, handled, raw, doc_key):
        self._event_iid = iid
        self._com_interfaces_ = []
        self._public_methods_ = []
        self._dispid_to_func_ = {}
        for dispid, event_name in dispid_names.items():
            if event_name in handled:
                func = "On" + event_name
                setattr(self, func, _make_handler(raw, event_name, doc_key))
            else:
                func = "_noop"
            self._dispid_to_func_[dispid] = func

    def _query_interface_(self, iid):
        # Match pywin32 genpy's event sink: return an IDispatch gateway for
        # the custom dispinterface, not a native gateway for its unknown IID.
        if iid == self._event_iid:
            from win32com.server.util import wrap
            return wrap(self)
        return None

    def _noop(self, *args):
        return None


class _DocInfo:
    def __init__(self, key, disp, name, path):
        self.key = key
        self.disp = disp
        self.name = name
        self.path = path          # None for a never-saved drawing
        self.sink = None          # (IConnectionPoint, cookie)
        self.logfile = None


class _Busy(Exception):
    """AutoCAD rejected the call or its main thread doesn't respond."""


class _Dead(Exception):
    """AutoCAD is gone."""


def _hresult(exc):
    hr = getattr(exc, "hresult", None)
    if hr is None and getattr(exc, "args", None):
        hr = exc.args[0]
    return hr if isinstance(hr, int) else None


def _member(obj, name, index):
    """TYPEATTR / FUNCDESC field: pywin32 exposes them as attributes and as
    sequence items (TYPEATTR order: iid, lcid, memidConstructor,
    memidDestructor, cbSizeInstance, typekind, cFuncs, cVars, cImplTypes, ...)."""
    try:
        return getattr(obj, name)
    except AttributeError:
        return obj[index]


def _process_name(pid):
    try:
        import psutil
        return psutil.Process(pid).name()
    except Exception:
        return None


def _import_pywin32():
    """Imports pywin32 on the calling thread. sys.coinit_flags is read only
    when pythoncom is imported for the first time in the process; setting
    it to MTA here keeps that import from making this thread STA."""
    had = hasattr(sys, "coinit_flags")
    old = getattr(sys, "coinit_flags", None)
    sys.coinit_flags = 0  # COINIT_MULTITHREADED
    try:
        import pywintypes
        import pythoncom
        import win32gui
        import win32process
        import win32com.server.util
    finally:
        if had:
            sys.coinit_flags = old
        else:
            del sys.coinit_flags
    return types.SimpleNamespace(pywintypes=pywintypes, pythoncom=pythoncom, win32gui=win32gui,
                                 win32process=win32process, server_util=win32com.server.util)


class Adapter:
    NAME = NAME
    PROCESS_NAMES = ["acad.exe"]
    SPEC_VERSION = "1"

    def __init__(self):
        self._ctx = None
        self._reset()

    def _reset(self):
        self._m = None
        self._pc = None
        self._com_inited = False
        self._app = None
        self._hwnd = None
        self._pid = None
        self._dead = False
        self._events_on = False
        self._events_off_reason = None
        self._app_sink = None
        self._doc_iface = None
        self._docs = {}
        self._archive = {}
        self._active_key = None
        self._rescan_needed = False
        self._pending_new = collections.deque()   # NewDrawing t_ms not yet paired with a document
        self._closing = {}                          # doc key -> BeginClose t_ms, until the document is gone
        self._raw = collections.deque()
        self._actions = RecordQueue(self._log)
        self._events = RecordQueue(self._log)
        self._unresolved = collections.deque()   # [record, PyIDispatch, native event name]
        self._cmd_stack = []                      # [normalized name, t_ms, object events seen]
        self._last_lisp = None                    # (name, expression)
        self._sysvars = {}
        self._objects = collections.OrderedDict()
        self._dispids = {}
        self._seen_begin_command = False
        self._selfcheck_seen = set()
        self._selfcheck_misses = 0
        self._log_decided = False
        self._log_on = False
        self._log_reason = "尚无打开的图纸,未读取 LOGFILEMODE"
        self._locale = None
        self._logs = {}
        self._timing = None
        self._error_last = {}
        self.stats = collections.Counter()

    # -- logging ------------------------------------------------------------
    def _log(self, msg):
        if self._ctx is not None:
            try:
                self._ctx.log(msg)
            except Exception:
                pass

    def _log_error(self, key, msg):
        """At most one line per key per ERROR_LOG_INTERVAL_S."""
        self.stats[f"error:{key}"] += 1
        now = time.monotonic()
        if now - self._error_last.get(key, -1e9) >= ERROR_LOG_INTERVAL_S:
            self._error_last[key] = now
            self._log(f"{msg}(同类错误每 {ERROR_LOG_INTERVAL_S}s 最多记一次,累计 {self.stats[f'error:{key}']} 次)")

    # -- spec 3.1 methods ----------------------------------------------------
    def attach(self, ctx):
        self._ctx = ctx
        self._reset()
        try:
            return self._attach()
        except Exception:
            self._log(f"attach 异常:\n{traceback.format_exc().rstrip()}")
            self._teardown()
            return False

    def detach(self):
        try:
            self._teardown()
        except Exception:
            self._log(f"detach 异常:\n{traceback.format_exc().rstrip()}")

    def get_state(self):
        try:
            return self._get_state()
        except Exception:
            self._log_error("get_state", f"get_state 异常:\n{traceback.format_exc().rstrip()}")
            return None

    def get_actions(self):
        try:
            if self._app is None and not self._actions:
                return []
            deadline = time.perf_counter() + DRAIN_BUDGET_S
            self._pump_raw()
            self._maintain(deadline)
            self._read_logs(deadline)
            return self._actions.take()
        except Exception:
            self._log_error("get_actions", f"get_actions 异常:\n{traceback.format_exc().rstrip()}")
            return []

    def get_events(self):
        try:
            if self._app is None and not self._events:
                return []
            deadline = time.perf_counter() + DRAIN_BUDGET_S
            self._pump_raw()
            self._maintain(deadline)
            self._resolve(deadline)
            return self._events.take()
        except Exception:
            self._log_error("get_events", f"get_events 异常:\n{traceback.format_exc().rstrip()}")
            return []

    # -- attach / detach -----------------------------------------------------
    def _attach(self):
        if sys.platform != "win32":
            self._log("非 Windows 平台,AutoCAD 适配器不可用")
            return False
        try:
            m = _import_pywin32()
        except ImportError as exc:
            self._log(f"导入 pywin32 失败: {exc}")
            return False
        self._m, self._pc = m, m.pythoncom
        pc = self._pc

        mta = True
        try:
            pc.CoInitializeEx(pc.COINIT_MULTITHREADED)
            self._com_inited = True
        except pc.com_error as exc:
            if _hresult(exc) != RPC_E_CHANGED_MODE:
                raise
            mta = False
            self._events_off_reason = "适配器线程已是 STA(RPC_E_CHANGED_MODE),STA 下接收事件会卡住 AutoCAD"

        errors = []
        for progid in PROGIDS:
            try:
                clsid = m.pywintypes.IID(progid)
                self._app = pc.GetActiveObject(clsid).QueryInterface(pc.IID_IDispatch)
                break
            except m.pywintypes.com_error as exc:
                errors.append(f"{progid}: {_hresult(exc)}")
        if self._app is None:
            self._log("未找到运行中的 AutoCAD(" + "; ".join(errors) + ")。常见原因: AutoCAD 仍在启动;"
                      "AutoCAD 与 Recorder 权限不同(一个以管理员运行);AutoCAD LT 不支持 COM")
            self._teardown()
            return False

        self._hwnd = int(self._prop(self._app, "app", "HWND"))
        _, self._pid = m.win32process.GetWindowThreadProcessId(self._hwnd)
        pname = _process_name(self._pid)
        if pname is not None and pname.lower() != "acad.exe":
            self._log(f"COM 返回的 AutoCAD 进程是 {pname}(PID {self._pid}),不是 acad.exe")
            self._teardown()
            return False
        try:
            _, fg_pid = m.win32process.GetWindowThreadProcessId(m.win32gui.GetForegroundWindow())
        except Exception:
            fg_pid = None
        if fg_pid and fg_pid != self._pid and (_process_name(fg_pid) or "").lower() == "acad.exe":
            self._log(f"前台 acad.exe(PID {fg_pid})不是 COM 能连接到的实例(PID {self._pid});"
                      "多实例时只能连接 ROT 中登记的那个,本次不连接")
            self._teardown()
            return False
        version = self._prop(self._app, "app", "Version")

        if mta:
            try:
                self._setup_events()
            except self._pc.com_error as exc:
                if _hresult(exc) in DEAD_HRESULTS:
                    raise
                self._events_off_reason = f"订阅 COM 事件失败: {exc}"
                self._log(traceback.format_exc().rstrip())
            except Exception as exc:
                self._events_off_reason = f"订阅 COM 事件失败: {exc!r}"
                self._log(traceback.format_exc().rstrip())
        self._rescan(initial=True)
        self._init_active_doc_caches()

        level = "A" if self._events_on else "B"
        self._log(f"attach 成功: AutoCAD {version} PID {self._pid} level={level} "
                  f"events={'on' if self._events_on else 'off(' + str(self._events_off_reason) + ')'} "
                  f"log={'on' if self._log_on else 'off(' + str(self._log_reason) + ')'} "
                  f"locale={self._locale} documents={len(self._docs)}")
        return True

    def _setup_events(self):
        pc = self._pc
        tlb, _ = self._app.GetTypeInfo().GetContainingTypeLib()
        try:
            # The same type library loaded in this process: walking it through
            # the remote proxy would cost one cross-process call per member.
            guid, lcid, _syskind, major, minor = tuple(tlb.GetLibAttr())[:5]
            tlb = pc.LoadRegTypeLib(guid, major, minor, lcid)
        except Exception as exc:
            self._log(f"本地加载 AutoCAD 类型库失败,改为经 COM 读取(较慢): {exc!r}")
        ifaces = self._find_source_interfaces(tlb, ("AcadApplication", "AcadDocument"))
        if len(ifaces) != 2:
            raise RuntimeError(f"类型库中找不到 AcadApplication / AcadDocument 的默认事件接口(找到 {sorted(ifaces)})")
        self._app_sink = self._advise(self._app, ifaces["AcadApplication"], APP_EVENTS, None)
        self._doc_iface = ifaces["AcadDocument"]
        self._events_on = True

    def _find_source_interfaces(self, tlb, coclass_names):
        """-> {coclass name: (iid, {dispid: event name})} of each coclass's
        default source (event) interface."""
        pc = self._pc
        found = {}
        for i in range(tlb.GetTypeInfoCount()):
            if tlb.GetTypeInfoType(i) != pc.TKIND_COCLASS:
                continue
            coclass = tlb.GetDocumentation(i)[0]
            if coclass not in coclass_names:
                continue
            ti = tlb.GetTypeInfo(i)
            for j in range(_member(ti.GetTypeAttr(), "cImplTypes", 8)):
                flags = ti.GetImplTypeFlags(j)
                if flags & IMPLTYPEFLAG_FSOURCE and flags & IMPLTYPEFLAG_FDEFAULT:
                    src = ti.GetRefTypeInfo(ti.GetRefTypeOfImplType(j))
                    attr = src.GetTypeAttr()
                    names = {}
                    for k in range(_member(attr, "cFuncs", 6)):
                        memid = _member(src.GetFuncDesc(k), "memid", 0)
                        names[memid] = src.GetNames(memid)[0]
                    found[coclass] = (_member(attr, "iid", 0), names)
                    break
            if len(found) == len(coclass_names):
                break
        return found

    def _advise(self, disp, iface, handled, doc_key):
        iid, names = iface
        stage = "wrap sink"
        try:
            wrapped = self._m.server_util.wrap(_Sink(iid, names, handled, self._raw, doc_key))
            stage = "QueryInterface(IConnectionPointContainer)"
            container = disp.QueryInterface(self._pc.IID_IConnectionPointContainer)
            stage = "FindConnectionPoint"
            cp = container.FindConnectionPoint(iid)
            stage = "Advise"
            return cp, cp.Advise(wrapped)
        except Exception:
            self._log(f"COM 订阅失败 stage={stage} iid={iid} document={doc_key!r}\n"
                      + traceback.format_exc().rstrip())
            raise

    def _unadvise(self, sink):
        if sink is None or self._dead:
            return
        cp, cookie = sink
        try:
            cp.Unadvise(cookie)
        except Exception:
            pass

    def _disable_events(self, reason):
        if not self._events_on:
            return
        self._events_on = False
        self._events_off_reason = reason
        self._unadvise(self._app_sink)
        self._app_sink = None
        for info in self._docs.values():
            self._unadvise(info.sink)
            info.sink = None
        self._log(f"COM 事件判定不可用,切换到 level B: {reason}")

    def _init_active_doc_caches(self):
        """Initial sysvar values (the 'from' of later change events) and the
        running command at attach time (for the event self-check)."""
        try:
            doc = self._prop(self._app, "app", "ActiveDocument")
        except (_Busy, _Dead):
            raise
        except Exception:
            return  # no document open
        for name in INITIAL_SYSVARS:
            try:
                self._sysvars[name] = jsonable(self._call(doc, "doc", "GetVariable", name))
            except (_Busy, _Dead):
                raise
            except Exception:
                pass
        try:
            names = self._call(doc, "doc", "GetVariable", "CMDNAMES")
            if names:
                self._selfcheck_seen.add(str(names))
        except (_Busy, _Dead):
            raise
        except Exception:
            pass

    def _teardown(self):
        if not self._dead:
            self._unadvise(self._app_sink)
            for info in self._docs.values():
                self._unadvise(info.sink)
        self._app_sink = None
        self._docs.clear()
        self._archive.clear()
        self._unresolved.clear()
        self._raw.clear()
        self._app = None
        if self._com_inited:
            try:
                self._pc.CoUninitialize()
            except Exception:
                pass
            self._com_inited = False

    # -- COM access (adapter thread only) ------------------------------------
    def _dispid(self, disp, kind, name):
        key = (kind, name)
        dispid = self._dispids.get(key)
        if dispid is None:
            dispid = disp.GetIDsOfNames(name)
            self._dispids[key] = dispid
        return dispid

    def _call(self, disp, kind, name, *args):
        """Property get or method call through IDispatch::Invoke. dispids are
        cached per (object kind, member name)."""
        pc = self._pc
        flags = pc.DISPATCH_METHOD | pc.DISPATCH_PROPERTYGET
        for attempt in (0, 1):
            try:
                return disp.Invoke(self._dispid(disp, kind, name), 0, flags, True, *args)
            except pc.com_error as exc:
                hr = _hresult(exc)
                if hr in DEAD_HRESULTS:
                    self._mark_dead()
                    raise _Dead() from exc
                if hr in BUSY_HRESULTS:
                    self.stats["busy_rejected"] += 1
                    raise _Busy() from exc
                if attempt == 0 and hr in (DISP_E_MEMBERNOTFOUND, DISP_E_UNKNOWNNAME) \
                        and (kind, name) in self._dispids:
                    del self._dispids[(kind, name)]
                    continue
                raise

    _prop = _call

    def _mark_dead(self):
        if self._dead:
            return
        self._dead = True
        self._log("AutoCAD 已断开(进程退出或 COM 连接失效),之后 get_state 返回 None")
        while self._unresolved:
            self._finalize_unresolved(self._unresolved.popleft())

    def _responsive(self):
        """False when AutoCAD is gone or its main thread doesn't answer an
        empty WM_NULL message within PROBE_TIMEOUT_MS."""
        if self._dead or self._app is None:
            return False
        w = self._m.win32gui
        try:
            if not w.IsWindow(self._hwnd):
                self._mark_dead()
                return False
            rc = w.SendMessageTimeout(self._hwnd, WM_NULL, 0, 0, SMTO_ABORTIFHUNG, PROBE_TIMEOUT_MS)
            if isinstance(rc, tuple) and rc and rc[0] == 0:
                raise OSError("SendMessageTimeout returned 0")
            return True
        except Exception:
            self.stats["busy_probe"] += 1
            return False

    # -- get_state -------------------------------------------------------------
    def _get_state(self):
        if self._app is None or self._dead:
            return None
        t_ms = _now_ms()
        start = time.perf_counter()
        self._pump_raw()
        state = {"t_ms": t_ms, "software": NAME}
        state.update({f: None for f in STATE_FIELDS})
        if not self._responsive():
            if self._dead:
                return None
            state["mode"] = "busy"
            return state
        cmdactive = None
        try:
            cmdactive = self._fill_state(state, start)
        except _Dead:
            return None
        except _Busy:
            state.update({f: None for f in STATE_FIELDS})
            state["mode"] = "busy"
            return state
        if cmdactive == 0:
            self._cmd_stack = [e for e in self._cmd_stack if e[1] > t_ms]
        self._self_check(state.get("active_command"))
        return state

    def _field(self, label, fn):
        """fn() for one state field: None on ordinary errors, _Busy/_Dead propagate."""
        t0 = time.perf_counter()
        try:
            return fn()
        except (_Busy, _Dead):
            raise
        except Exception as exc:
            self._log_error(f"state:{label}", f"get_state 读取 {label} 失败: {exc!r}")
            return None
        finally:
            if self._timing is not None:
                self._timing[label] = round((time.perf_counter() - t0) * 1000, 2)

    def _fill_state(self, st, start):
        """Returns the raw CMDACTIVE value (or None)."""
        app = self._app
        docs = self._prop(app, "app", "Documents")
        count = self._field("document_count", lambda: int(self._prop(docs, "documents", "Count")))
        st["document_count"] = count
        if count is not None and count != len(self._docs):
            self._rescan_needed = True
        if count == 0:
            st.update(mode="no_document", selection=[], selection_count=0, selection_truncated=False)
            return None
        doc = self._field("ActiveDocument", lambda: self._prop(app, "app", "ActiveDocument"))
        if doc is None:
            return None
        full = self._field("FullName", lambda: str(self._prop(doc, "doc", "FullName") or ""))
        if full:
            st["active_document"] = full
        elif full is not None:
            st["active_document"] = self._field("Name", lambda: str(self._prop(doc, "doc", "Name")))
        st["read_only"] = self._field("read_only", lambda: bool(self._prop(doc, "doc", "ReadOnly")))

        def gv(name):
            return self._field(name, lambda: self._call(doc, "doc", "GetVariable", name))

        cmdactive = gv("CMDACTIVE")
        try:
            cmdactive = int(cmdactive) if cmdactive is not None else None
        except (TypeError, ValueError):
            cmdactive = None
        st["mode"] = mode_from_cmdactive(cmdactive)
        names = gv("CMDNAMES")
        st["active_command"] = None if names is None else str(names)
        layer = gv("CLAYER")
        st["active_layer"] = None if layer is None else str(layer)
        layout = gv("CTAB")
        st["active_layout"] = None if layout is None else str(layout)
        ucsname, worlducs = gv("UCSNAME"), gv("WORLDUCS")
        st["ucs"] = ucs_value(ucsname, worlducs) if (ucsname or worlducs is not None) else None
        st["units"] = units_name(gv("INSUNITS"))
        dbmod = gv("DBMOD")
        st["unsaved_changes"] = None if dbmod is None else int(dbmod) != 0
        viewctr = jsonable(gv("VIEWCTR"))
        st["view_center"] = viewctr if isinstance(viewctr, list) else None
        viewsize = jsonable(gv("VIEWSIZE"))
        st["view_height"] = viewsize if isinstance(viewsize, (int, float)) and not isinstance(viewsize, bool) else None
        st["entity_count"] = self._field(
            "entity_count", lambda: int(self._prop(self._prop(doc, "doc", "ModelSpace"), "block", "Count")))
        sel = self._field("selection", lambda: self._read_selection(doc, start))
        if sel is not None:
            st["selection"], st["selection_count"], st["selection_truncated"] = sel
        return cmdactive

    def _read_selection(self, doc, start):
        ss = self._prop(doc, "doc", "PickfirstSelectionSet")
        count = int(self._prop(ss, "selset", "Count"))
        items = []
        for i in range(min(count, SELECTION_LIMIT)):
            if time.perf_counter() - start > STATE_BUDGET_S:
                break
            ent = self._call(ss, "selset", "Item", i)
            object_name = str(self._prop(ent, "object", "ObjectName"))
            handle = str(self._prop(ent, "object", "Handle"))
            try:
                self._cache_object(int(self._prop(ent, "object", "ObjectID")), handle, object_name)
            except (_Busy, _Dead):
                raise
            except Exception:
                pass
            items.append(f"{object_name}({handle})")
        return items, count, count > len(items)

    def _self_check(self, active_command):
        """Events advised but never delivered: running commands observed
        without a single BeginCommand since attach."""
        if not self._events_on or self._seen_begin_command or not active_command:
            return
        if active_command in self._selfcheck_seen:
            return
        self._selfcheck_seen.add(active_command)
        self._selfcheck_misses += 1
        if self._selfcheck_misses >= SELF_CHECK_MISSES:
            self._disable_events(f"attach 后观察到 {self._selfcheck_misses} 个新的运行中命令"
                                 f"(CMDNAMES 见过: {', '.join(sorted(self._selfcheck_seen))}),"
                                 "但从未收到 BeginCommand 事件")

    # -- event pump (adapter thread, no COM calls) ---------------------------
    def _pump_raw(self):
        while self._raw:
            t_ms, event_name, args, doc_key = self._raw.popleft()
            self.stats[f"cb:{event_name}"] += 1
            handler = getattr(self, "_ev_" + event_name, None)
            if handler is None:
                continue
            try:
                handler(t_ms, args, doc_key)
            except Exception:
                self._log_error(f"handle:{event_name}", f"处理 {event_name} 失败:\n{traceback.format_exc().rstrip()}")
        self._actions.enforce_limit()
        self._events.enforce_limit()
        if len(self._unresolved) > QUEUE_LIMIT:
            overflow = []
            while len(self._unresolved) > QUEUE_LIMIT:
                overflow.append(self._unresolved.pop())
            for slot in reversed(overflow):
                self._finalize_unresolved(slot)
            self._events.enforce_limit()

    def _event(self, t_ms, type_, name, params, raw):
        self._events.push(make_record(t_ms, type_, name, params, "com_event", raw))

    def _action(self, t_ms, type_, name, params, raw, source="com_event"):
        self._actions.push(make_record(t_ms, type_, name, params, source, raw))

    def _current_command(self):
        return self._cmd_stack[-1][0] if self._cmd_stack else None

    def _doc_info(self, key):
        info = self._docs.get(key)
        if info is None and key in self._archive:
            info = self._archive[key][0]
        return info

    @staticmethod
    def _arg(args, i):
        return args[i] if len(args) > i else None

    def _ev_BeginCommand(self, t_ms, args, _key):
        self._seen_begin_command = True
        name = str(self._arg(args, 0) or "")
        self._cmd_stack.append([normalize_command(name), t_ms, 0])
        self._action(t_ms, "command", name, {}, f"BeginCommand(CommandName={name!r})")

    def _ev_EndCommand(self, t_ms, args, _key):
        name = str(self._arg(args, 0) or "")
        norm = normalize_command(name)
        entry = None
        for i in range(len(self._cmd_stack) - 1, -1, -1):
            if self._cmd_stack[i][0] == norm:
                entry = self._cmd_stack[i]
                del self._cmd_stack[i:]
                break
        raw = f"EndCommand(CommandName={name!r})"
        self._event(t_ms, "command_executed", name,
                    {"command": name, "duration_ms": (t_ms - entry[1]) if entry else None}, raw)
        if norm == "U" or (norm == "UNDO" and entry is not None and entry[2] > 0):
            self._event(t_ms, "undo", name, {"command": name}, raw)
        elif norm in ("REDO", "MREDO"):
            self._event(t_ms, "redo", name, {"command": name}, raw)

    def _ev_BeginLisp(self, t_ms, args, _key):
        expr = str(self._arg(args, 0) or "")
        type_, name = lisp_action(expr)
        self._last_lisp = (name, expr)
        self._action(t_ms, type_, name, {"expression": expr}, f"BeginLisp(FirstLine={expr!r})")

    def _lisp_end(self, t_ms, type_, native):
        name, expr = self._last_lisp or ("AutoLISP", None)
        self._last_lisp = None
        self._event(t_ms, type_, name, {"expression": expr}, f"{native}()")

    def _ev_EndLisp(self, t_ms, _args, _key):
        self._lisp_end(t_ms, "lisp_ended", "EndLisp")

    def _ev_LispCancelled(self, t_ms, _args, _key):
        self._lisp_end(t_ms, "lisp_cancelled", "LispCancelled")

    def _ev_NewDrawing(self, t_ms, _args, _key):
        self._pending_new.append(t_ms)
        self._rescan_needed = True

    def _ev_EndOpen(self, t_ms, args, _key):
        path = str(self._arg(args, 0) or "")
        self._event(t_ms, "document_opened", ntpath.basename(path) or path, {"path": path},
                    f"EndOpen(FileName={path!r})")
        self._rescan_needed = True

    def _ev_EndSave(self, t_ms, args, _key):
        path = str(self._arg(args, 0) or "")
        self._rescan_needed = True
        if not path.lower().endswith(SAVE_EXTENSIONS):
            self._log_error("endsave_skipped", f"EndSave 路径不是图纸文件,不作为 document_saved 返回: {path}")
            return
        self._event(t_ms, "document_saved", ntpath.basename(path), {"path": path}, f"EndSave(FileName={path!r})")

    def _ev_SysVarChanged(self, t_ms, args, _key):
        name = str(self._arg(args, 0) or "").upper()
        value = jsonable(self._arg(args, 1))
        if not name or name in SYSVAR_IGNORE:
            return
        prev = self._sysvars.get(name)
        self._sysvars[name] = value
        raw = f"SysVarChanged({name}, {value!r})"
        if name == "CLAYER":
            self._event(t_ms, "layer_changed", str(value), {"from": prev, "to": value}, raw)
        elif name != "CTAB":  # layout changes come from LayoutSwitched
            self._event(t_ms, "sysvar_changed", name, {"name": name, "from": prev, "to": value}, raw)

    def _on_object(self, t_ms, type_, native, obj):
        cmd = self._current_command()
        if self._cmd_stack:
            self._cmd_stack[-1][2] += 1
        rec = make_record(t_ms, type_, "unresolved",
                          {"handle": None, "object_type": None, "object_id": None, "command": cmd},
                          "com_event", f"{native}(unresolved)")
        self._unresolved.append([rec, obj, native])

    def _ev_ObjectAdded(self, t_ms, args, _key):
        self._on_object(t_ms, "object_created", "ObjectAdded", self._arg(args, 0))

    def _ev_ObjectModified(self, t_ms, args, _key):
        self._on_object(t_ms, "object_modified", "ObjectModified", self._arg(args, 0))

    def _ev_ObjectErased(self, t_ms, args, _key):
        # Queued behind the object events still being resolved, so an object
        # created and erased in quick succession is in the cache by the time
        # this one is completed (_complete_erased).
        if self._cmd_stack:
            self._cmd_stack[-1][2] += 1
        try:
            object_id = int(self._arg(args, 0))
        except (TypeError, ValueError):
            object_id = None
        rec = make_record(t_ms, "object_deleted", f"ObjectID({object_id})",
                          {"object_id": object_id, "handle": None, "object_type": None,
                           "command": self._current_command()},
                          "com_event", f"ObjectErased(ObjectID={object_id})")
        self._unresolved.append([rec, None, "ObjectErased"])

    def _ev_SelectionChanged(self, t_ms, _args, key):
        info = self._doc_info(key)
        self._event(t_ms, "selection_changed", info.name if info else "unknown", {}, "SelectionChanged()")

    def _ev_BeginClose(self, t_ms, _args, key):
        info = self._doc_info(key)
        name, path = (info.name, info.path) if info else ("unknown", None)
        self._event(t_ms, "document_closed", name, {"name": name, "path": path}, f"BeginClose() [{name}]")
        self._closing[key] = t_ms
        self._rescan_needed = True

    def _ev_Activate(self, t_ms, _args, key):
        info = self._doc_info(key)
        name, path = (info.name, info.path) if info else ("unknown", None)
        self._active_key = key
        self._event(t_ms, "document_activated", name, {"name": name, "path": path}, f"Activate() [{name}]")

    def _ev_LayoutSwitched(self, t_ms, args, _key):
        layout = str(self._arg(args, 0) or "")
        prev = self._sysvars.get("CTAB")
        self._sysvars["CTAB"] = layout
        self._event(t_ms, "layout_switched", layout, {"from": prev, "to": layout},
                    f"LayoutSwitched(LayoutName={layout!r})")

    # -- object resolution ---------------------------------------------------
    def _cache_object(self, object_id, handle, object_type):
        self._objects[object_id] = (handle, object_type)
        self._objects.move_to_end(object_id)
        while len(self._objects) > OBJECT_CACHE_LIMIT:
            self._objects.popitem(last=False)

    def _complete_erased(self, rec):
        params = rec["params"]
        handle, object_type = self._objects.get(params["object_id"], (None, None))
        if handle and object_type:
            rec["params"] = dict(params, handle=handle, object_type=object_type)
            rec["name"] = f"{object_type}({handle})"
        else:
            self.stats["erased_unknown"] += 1

    def _finalize_unresolved(self, slot):
        """Emits a queued object record without asking AutoCAD."""
        rec = slot[0]
        if slot[2] == "ObjectErased":
            self._complete_erased(rec)
        else:
            slot[1] = None
            self.stats["unresolved"] += 1
        self._events.push(rec)

    def _resolve(self, deadline):
        """Completes queued object records in order: ObjectName/Handle/ObjectID
        for ObjectAdded/ObjectModified (COM, within the deadline), cache
        lookup for ObjectErased."""
        responsive = None
        while self._unresolved:
            slot = self._unresolved[0]
            if slot[2] == "ObjectErased" or self._dead:
                self._finalize_unresolved(self._unresolved.popleft())
                continue
            if time.perf_counter() >= deadline:
                break
            if responsive is None:
                responsive = self._responsive()
            if not responsive:
                if _now_ms() - slot[0]["t_ms"] > STALE_UNRESOLVED_MS:
                    self._finalize_unresolved(self._unresolved.popleft())
                    continue
                break
            slot = self._unresolved.popleft()
            rec, obj, native = slot
            values = {}
            try:
                for member in ("ObjectName", "Handle", "ObjectID"):
                    try:
                        values[member] = self._prop(obj, "object", member)
                    except (_Busy, _Dead):
                        raise
                    except Exception:
                        values[member] = None  # typically erased before we got to it
            except _Busy:
                # e.g. a modal dialog: the window answers but COM calls are rejected
                if _now_ms() - rec["t_ms"] > STALE_UNRESOLVED_MS:
                    self._finalize_unresolved(slot)
                    continue
                self._unresolved.appendleft(slot)
                break
            except _Dead:
                self._finalize_unresolved(slot)
                break
            slot[1] = None
            object_type = None if values["ObjectName"] is None else str(values["ObjectName"])
            handle = None if values["Handle"] is None else str(values["Handle"])
            try:
                object_id = int(values["ObjectID"]) if values["ObjectID"] is not None else None
            except (TypeError, ValueError):
                object_id = None
            params = dict(rec["params"], handle=handle, object_type=object_type, object_id=object_id)
            rec["params"] = params
            if object_type and handle:
                rec["name"] = f"{object_type}({handle})"
            elif object_type:
                rec["name"] = object_type
            rec["raw"] = f"{native}({object_type}, {handle})"
            if object_id is not None and handle and object_type:
                self._cache_object(object_id, handle, object_type)
            if handle is None:
                self.stats["unresolved"] += 1
            self._events.push(rec)

    # -- documents -----------------------------------------------------------
    def _maintain(self, deadline):
        if self._app is None or self._dead:
            return
        now = _now_ms()
        for key in [k for k, (_, t) in self._archive.items() if now - t > ARCHIVE_KEEP_MS]:
            del self._archive[key]
        if self._rescan_needed and time.perf_counter() < deadline and self._responsive():
            try:
                self._rescan()
            except (_Busy, _Dead):
                pass

    def _rescan(self, initial=False):
        """Enumerates open documents: advises sinks on new ones, pairs
        NewDrawing timestamps with new unsaved ones (document_created),
        unadvises closed ones, refreshes names/paths (SAVEAS)."""
        app = self._app
        docs = self._prop(app, "app", "Documents")
        seen = collections.OrderedDict()
        for i in range(int(self._prop(docs, "documents", "Count"))):
            d = self._call(docs, "documents", "Item", i)
            name = str(self._prop(d, "doc", "Name") or "")
            full = str(self._prop(d, "doc", "FullName") or "")
            try:
                key = int(self._prop(d, "doc", "HWND"))
            except (_Busy, _Dead):
                raise
            except Exception:
                key = f"{name}|{full}"
            seen[key] = (d, name, full)
        now = _now_ms()
        while self._pending_new and now - self._pending_new[0] > NEW_DRAWING_PAIR_MS:
            self._log(f"NewDrawing({self._pending_new.popleft()}) 在 {NEW_DRAWING_PAIR_MS} ms 内未找到对应的新图纸,丢弃")
        for key, (d, name, full) in seen.items():
            info = self._docs.get(key)
            if info is not None:
                info.disp, info.name, info.path = d, name, full or None
            else:
                info = _DocInfo(key, d, name, full or None)
                self._docs[key] = info
                if not initial and not full and self._pending_new:
                    t_ms = self._pending_new.popleft()
                    self._event(t_ms, "document_created", name, {"name": name}, f"NewDrawing() -> {name}")
                if self._events_on:
                    try:
                        info.sink = self._advise(d, self._doc_iface, DOC_EVENTS, key)
                    except (_Busy, _Dead):
                        raise
                    except Exception as exc:
                        self._log(f"图纸 {name} 订阅 Document 事件失败,该图纸无对象/选择/关闭事件: {exc!r}")
            self._ensure_log_settings(d)
            if self._log_on and info.logfile is None:
                self._track_log(info)
        for key in [k for k in self._docs if k not in seen]:
            info = self._docs.pop(key)
            self._unadvise(info.sink)
            info.sink, info.disp = None, None
            self._archive[key] = (info, now)
        for key, t_close in list(self._closing.items()):
            if key not in seen or now - t_close > NEW_DRAWING_PAIR_MS:  # gone, or the close was cancelled
                del self._closing[key]
        try:
            active = self._prop(app, "app", "ActiveDocument")
            self._active_key = int(self._prop(active, "doc", "HWND"))
        except (_Busy, _Dead):
            raise
        except Exception:
            pass
        # NewDrawing fires before the document exists and BeginClose before it
        # is gone: keep rescanning until they show up (or time out).
        # Log setup skipped because AutoCAD was busy is retried the same way.
        self._rescan_needed = bool(self._pending_new or self._closing
                                   or (self._docs and not self._log_decided)
                                   or (self._log_on and any(i.logfile is None for i in self._docs.values())))

    # -- command-line log ----------------------------------------------------
    def _ensure_log_settings(self, doc):
        if self._log_decided:
            return
        try:
            mode = self._call(doc, "doc", "GetVariable", "LOGFILEMODE")
            self._locale = str(self._call(doc, "doc", "GetVariable", "LOCALE") or "").upper() or None
        except _Busy:
            return  # decided on a later rescan
        except _Dead:
            raise
        except Exception as exc:
            self._log_reason = f"读取 LOGFILEMODE/LOCALE 失败: {exc!r}"
            self._log_decided = True
            return
        self._log_decided = True
        try:
            mode = int(mode)
        except (TypeError, ValueError):
            pass
        if mode != 1:
            self._log_reason = f"LOGFILEMODE={mode},操作者未打开命令行日志"
        elif self._locale not in LOG_PATTERNS:
            self._log_reason = f"LOCALE={self._locale} 没有对应的日志解析规则"
        else:
            self._log_on = True
            self._log_reason = None

    def _track_log(self, info):
        """Starts following this document's log file from its current end.
        Retried on later rescans until LOGFILENAME is known."""
        try:
            path = str(self._call(info.disp, "doc", "GetVariable", "LOGFILENAME") or "")
        except _Busy:
            return
        except _Dead:
            raise
        except Exception as exc:
            info.logfile = ""  # don't retry
            self._log(f"图纸 {info.name} 读取 LOGFILENAME 失败: {exc!r}")
            return
        info.logfile = path
        if not path:
            return
        if path in self._logs:
            return
        try:
            offset = os.path.getsize(path)
        except OSError:
            offset = 0
        tail = LogTail(path, offset, LOG_ANSI_ENCODING[self._locale])
        tail.parser = LogParser(LOG_PATTERNS[self._locale])
        self._logs[path] = tail

    def _read_logs(self, deadline):
        if not self._log_on:
            return
        for tail in self._logs.values():
            if tail.disabled or time.perf_counter() >= deadline:
                continue
            try:
                lines, reset = tail.read_lines()
            except FileNotFoundError:
                continue
            except OSError as exc:
                tail.disabled = True
                self._log(f"日志文件无法读取,停止跟踪 {tail.path}: {exc!r}")
                continue
            if reset:
                self._log(f"日志文件变短,从头读取: {tail.path}")
            t_ms = _now_ms()
            for line in lines:
                parsed = tail.parser.parse(line)
                if parsed is not None:
                    self._on_log_line(t_ms, parsed, tail.parser, line.strip())

    def _on_log_line(self, t_ms, parsed, parser, line):
        kind = parsed[0]
        if kind == "command":
            if not self._events_on:
                self._action(t_ms, "command", parsed[1], {}, line, source="log")
        elif kind == "cancel":
            cmd = parsed[1] or self._current_command() or "unknown"
            for i in range(len(self._cmd_stack) - 1, -1, -1):
                if self._cmd_stack[i][0] == normalize_command(cmd):
                    del self._cmd_stack[i:]
                    break
            self._events.push(make_record(t_ms, "command_cancelled", cmd, {"command": cmd}, "log", line))
        elif kind == "unknown_command":
            self._events.push(make_record(t_ms, "error_raised", "unknown_command",
                                          {"kind": "unknown_command", "message": line, "command": parsed[1]},
                                          "log", line))
        elif kind == "lisp_error":
            self._events.push(make_record(t_ms, "error_raised", "lisp_error",
                                          {"kind": "lisp_error", "message": line}, "log", line))
        elif kind == "input" and LOG_INPUT_RULE.get(self._locale):
            self._action(t_ms, "command", parser.command, {"prompt": parsed[1], "input": parsed[2]},
                         line, source="log")

    # -- debug helpers ---------------------------------------------------------
    def _debug_counts(self):
        """SelectionSets.Count and DBMOD of the active document, for --probe's read-only check."""
        try:
            doc = self._prop(self._app, "app", "ActiveDocument")
            sets = int(self._prop(self._prop(doc, "doc", "SelectionSets"), "selsets", "Count"))
            dbmod = self._call(doc, "doc", "GetVariable", "DBMOD")
            return {"SelectionSets.Count": sets, "DBMOD": dbmod}
        except Exception as exc:
            return {"error": repr(exc)}

    def summary(self):
        return {"level": "A" if self._events_on else ("B" if self._app is not None else "C"),
                "events": "on" if self._events_on else f"off({self._events_off_reason})",
                "log": "on" if self._log_on else f"off({self._log_reason})",
                "locale": self._locale, "pid": self._pid, "documents": len(self._docs),
                "log_files": sorted(self._logs)}


def main(argv=None):
    # Keep redirected output printable even with Windows' cp1252 default.
    # PowerShell must also decode UTF-8 (see README).
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description="AutoCAD adapter debug entry")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--probe", action="store_true", help="attach, print one get_state() and its duration")
    mode.add_argument("--watch", action="store_true", help="print get_actions()/get_events() every 500 ms")
    parser.add_argument("--repeat", type=int, default=1,
                        help="--probe only: call get_state() N times (read-only check: SelectionSets.Count/DBMOD)")
    args = parser.parse_args(argv)

    class _Ctx:
        episode_dir = None

        def log(self, msg):
            print(f"[ctx.log] {msg}", flush=True)

    adapter = Adapter()
    t0 = time.perf_counter()
    ok = adapter.attach(_Ctx())
    print(f"attach -> {ok} ({(time.perf_counter() - t0) * 1000:.0f} ms)")
    if not ok:
        return 1
    try:
        print(json.dumps(adapter.summary(), ensure_ascii=False))
        if args.probe:
            before = adapter._debug_counts()
            durations = []
            state = None
            for _ in range(max(1, args.repeat)):
                adapter._timing = {}
                t0 = time.perf_counter()
                state = adapter.get_state()
                durations.append((time.perf_counter() - t0) * 1000)
            print(f"get_state ({durations[-1]:.1f} ms):")
            print(json.dumps(state, ensure_ascii=False, indent=2))
            print("per-field ms: " + json.dumps(adapter._timing, ensure_ascii=False))
            if len(durations) > 1:
                print(f"{len(durations)} calls: min {min(durations):.1f} ms, max {max(durations):.1f} ms")
            print(f"read-only check: before {before}, after {adapter._debug_counts()}")
        else:
            last_state = 0.0
            while True:
                for record in adapter.get_actions() + adapter.get_events():
                    print(f"lag={_now_ms() - record['t_ms']}ms {json.dumps(record, ensure_ascii=False)}", flush=True)
                if time.monotonic() - last_state >= 5:
                    last_state = time.monotonic()
                    st = adapter.get_state()
                    if st is None:
                        print("[state] None", flush=True)
                    else:
                        print(f"[state] mode={st['mode']} active_command={st['active_command']!r} "
                              f"selection_count={st['selection_count']} document={st['active_document']!r} "
                              f"level={adapter.summary()['level']}", flush=True)
                time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        if args.watch:
            stats = dict(adapter.stats)
            stats["coalesced"] = adapter._events.coalesced + adapter._actions.coalesced
            stats["aggregated"] = adapter._events.aggregated + adapter._actions.aggregated
            print("stats: " + json.dumps(stats, ensure_ascii=False, sort_keys=True))
        adapter.detach()
    return 0


if __name__ == "__main__":
    sys.exit(main())
