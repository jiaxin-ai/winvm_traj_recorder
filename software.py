#!/usr/bin/env python3
"""V1.2: loads software adapters from adapters/ and calls them at the
Recorder's existing collection points. See task-v1.2.md and
adapters/software_trajectory_collector_specification.md.

Threads:
  - one worker thread per adapter: every method of that adapter runs
    there, one call at a time (COM needs a single thread). A call that
    overruns its budget is abandoned; if it hasn't started yet the worker
    skips it.
  - one dispatcher thread: makes the calls with their timeout budgets and
    appends the returned records to raw/. The collection points (window
    poll, input hooks, screenshot worker, file/process callbacks) only set
    a flag through notify_*() and return immediately, so nothing that
    records the main trajectory ever waits on an adapter.

Records are written to raw/ exactly as the adapter returned them. Only
records missing required fields or with a non-int t_ms are dropped (and
logged to raw/adapters.log).

Pure stdlib, nothing Windows-specific: runs as-is on macOS with the mock
adapter.
"""
import importlib.util
import json
import queue
import shutil
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

ADAPTERS_ROOT = Path(__file__).resolve().parent / "adapters"

ATTACH_TIMEOUT_S = 10
GET_STATE_TIMEOUT_S = 0.5
DRAIN_TIMEOUT_S = 0.2           # get_actions / get_events
DETACH_TIMEOUT_S = 5
ATTACH_RETRY_DELAY_S = 30
MAX_ATTACH_FAILURES = 3
MAX_CONSECUTIVE_FAILURES = 10
STOP_JOIN_TIMEOUT_S = 60

# Only loaded when named explicitly (--adapters mock): it treats notepad.exe
# as "the software" and would otherwise inject fake records into every real
# recording that happens to use Notepad.
NOT_AUTO_LOADED = {"mock"}

STATE_REQUIRED = ("t_ms", "software", "active_document", "selection", "mode")
RECORD_REQUIRED = ("t_ms", "software", "type", "name", "params", "source", "raw")


def parse_adapters_arg(value):
    """'auto' | 'none' | 'mock,autocad' -> 'auto' | 'none' | ['mock', 'autocad']"""
    value = value.strip()
    if value.lower() in ("auto", "none"):
        return value.lower()
    return [name.strip() for name in value.split(",") if name.strip()]


def _check_record(record, required):
    """Returns why a record must be dropped, or None if it's fine to write."""
    if not isinstance(record, dict):
        return f"not a dict ({type(record).__name__})"
    missing = [k for k in required if k not in record]
    if missing:
        return f"missing fields {missing}"
    t_ms = record["t_ms"]
    if not isinstance(t_ms, int) or isinstance(t_ms, bool):
        return f"t_ms is not an int ({t_ms!r})"
    return None


class AdapterContext:
    """The `ctx` passed to Adapter.attach()."""

    def __init__(self, episode_dir, log_fn):
        self.episode_dir = Path(episode_dir)
        self._log_fn = log_fn

    def log(self, msg):
        self._log_fn(str(msg))


class _Job:
    def __init__(self, fn):
        self.fn = fn
        self.done = threading.Event()
        self.abandoned = False
        self.result = None
        self.error = None


class _AdapterThread:
    """The dedicated thread one adapter's methods run on."""

    def __init__(self, name):
        self._q = queue.Queue()
        threading.Thread(target=self._run, args=(self._q,), daemon=True, name=f"adapter-{name}").start()

    @staticmethod
    def _run(q):
        while True:
            job = q.get()
            if job is None:
                return
            if job.abandoned:
                continue
            try:
                job.result = job.fn()
            except BaseException:
                job.error = traceback.format_exc()
            job.done.set()

    def call(self, fn, timeout_s):
        """Runs fn on this thread; returns ("ok", result), ("error",
        traceback) or ("timeout", None) -- the last one after at most
        timeout_s, whether or not fn ever finishes."""
        job = _Job(fn)
        self._q.put(job)
        if not job.done.wait(timeout_s):
            job.abandoned = True
            return "timeout", None
        if job.error is not None:
            return "error", job.error
        return "ok", job.result

    def close(self):
        """Lets the thread exit once whatever it's running returns. There's
        no way to kill a Python thread, so a thread stuck inside an adapter
        call is simply abandoned (it's a daemon)."""
        self._q.put(None)


class _LoadedAdapter:
    def __init__(self, dir_name, cls, manager):
        self.dir_name = dir_name
        self.cls = cls
        self.name = str(cls.NAME)
        self.ctx = AdapterContext(manager.episode_dir, lambda msg: manager.log(self.name, msg))
        self.process_names = [str(p).lower() for p in cls.PROCESS_NAMES]
        self.spec_version = str(cls.SPEC_VERSION)
        self.thread = _AdapterThread(self.name)
        self.instance = None           # created on self.thread by the first attach()
        self.attached = False
        self.ever_attached = False
        self.attach_failures = 0
        self.retry_at = None           # time.monotonic() after which a failed attach may be retried
        self.gave_up = False
        self.disabled = False
        self.timeouts = 0
        self.errors = 0
        self.consecutive_failures = 0

    def matches(self, process_name):
        return bool(process_name) and process_name.lower() in self.process_names

    def live(self):
        return self.attached and not self.disabled


class SoftwareManager:
    def __init__(self, episode_dir, mode, adapters_root=ADAPTERS_ROOT):
        self.episode_dir = Path(episode_dir)
        self.raw_dir = self.episode_dir / "raw"
        self.mode = mode
        self.adapters_root = Path(adapters_root)
        self.adapters = []
        self._log_lock = threading.Lock()
        self._cv = threading.Condition()
        self._foreground = None
        self._foreground_changed = False
        self._exited = []
        self._want_state = False
        self._want_drain = False
        self._stopping = False
        self._thread = None

    @property
    def enabled(self):
        return self.mode != "none"

    # -- logging -----------------------------------------------------------
    def log(self, source, msg):
        line = f"{datetime.now().isoformat(timespec='milliseconds')} [{source}] {msg}\n"
        with self._log_lock:
            self.raw_dir.mkdir(parents=True, exist_ok=True)
            with (self.raw_dir / "adapters.log").open("a", encoding="utf-8") as f:
                f.write(line)

    def _warn(self, msg):
        print(f"[software] {msg}", file=sys.stderr)
        self.log("recorder", msg)

    # -- loading -----------------------------------------------------------
    def load(self):
        """Imports every adapter to use this session. A missing adapters/
        directory or a broken adapter only produces a warning."""
        if not self.enabled:
            return
        if not self.adapters_root.is_dir():
            self._warn(f"adapters 目录不存在,不加载任何适配器: {self.adapters_root}")
            return
        wanted = None if self.mode == "auto" else {n.lower() for n in self.mode}
        seen = set()
        for d in sorted(self.adapters_root.iterdir()):
            if not d.is_dir() or d.name.startswith((".", "_")):
                continue
            key = d.name.lower()
            if wanted is None and key in NOT_AUTO_LOADED:
                continue
            if wanted is not None and key not in wanted:
                continue
            seen.add(key)
            try:
                cls = self._import_adapter_class(d)
            except (Exception, SystemExit):
                self._warn(f"跳过适配器 {d.name}: 导入失败\n{traceback.format_exc()}")
                continue
            adapter = _LoadedAdapter(d.name, cls, self)
            self.adapters.append(adapter)
            self._copy_capabilities(d)
            self.log("recorder", f"已加载适配器 {d.name}: NAME={adapter.name} "
                                 f"PROCESS_NAMES={adapter.process_names} SPEC_VERSION={adapter.spec_version}")
        if wanted is not None:
            for name in sorted(wanted - seen):
                self._warn(f"--adapters 指定的适配器不存在: {name}(应为 {self.adapters_root / name})")

    @staticmethod
    def _import_adapter_class(adapter_dir):
        path = adapter_dir / "adapter.py"
        if not path.is_file():
            raise FileNotFoundError(f"{path} 不存在")
        spec = importlib.util.spec_from_file_location(f"trajrec_adapter_{adapter_dir.name}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cls = module.Adapter
        for attr in ("NAME", "PROCESS_NAMES", "SPEC_VERSION"):
            getattr(cls, attr)
        if not isinstance(cls.PROCESS_NAMES, (list, tuple)):
            raise TypeError("PROCESS_NAMES 必须是列表")
        return cls

    def _copy_capabilities(self, adapter_dir):
        src = adapter_dir / "capabilities.yaml"
        if not src.is_file():
            self._warn(f"适配器 {adapter_dir.name} 缺少 capabilities.yaml")
            return
        dst_dir = self.raw_dir / "adapters"
        dst_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst_dir / f"{adapter_dir.name}.capabilities.yaml")

    def status(self):
        """The `adapters` list written into meta.json."""
        return [{
            "name": a.name, "spec_version": a.spec_version, "attached": a.ever_attached,
            "timeouts": a.timeouts, "errors": a.errors, "disabled": a.disabled,
            "process_names": a.process_names,
        } for a in self.adapters]

    # -- notifications from the collection points (all non-blocking) ------
    def start(self):
        if not self.adapters:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True, name="software-dispatcher")
        self._thread.start()

    def notify_foreground(self, process_name):
        if self._thread is None:
            return
        with self._cv:
            self._foreground = process_name
            self._foreground_changed = True
            self._cv.notify()

    def notify_process_exit(self, process_name):
        if self._thread is None:
            return
        with self._cv:
            self._exited.append(process_name)
            # The latest window report may still name the now-dead process;
            # don't let it trigger a re-attach. Done here, at arrival time,
            # so a re-open reported after this exit still counts.
            if self._foreground and self._foreground.lower() == str(process_name).lower():
                self._foreground = None
            self._cv.notify()

    def notify_observation(self):
        """Same moment as an observation screenshot -> get_state()."""
        if self._thread is None:
            return
        with self._cv:
            self._want_state = True
            self._cv.notify()

    def notify_drain(self):
        """Same moment as an action or event is collected -> get_actions() + get_events()."""
        if self._thread is None:
            return
        with self._cv:
            self._want_drain = True
            self._cv.notify()

    def stop(self):
        """Final get_actions()/get_events(), then detach() everything. Blocks
        until done (bounded by the per-call budgets)."""
        if self._thread is None:
            return
        with self._cv:
            self._stopping = True
            self._cv.notify()
        self._thread.join(timeout=STOP_JOIN_TIMEOUT_S)

    # -- dispatcher ----------------------------------------------------------
    def _has_work(self):
        return (self._foreground_changed or self._exited or self._want_state
                or self._want_drain or self._stopping)

    def _loop(self):
        while True:
            with self._cv:
                if not self._has_work():
                    self._cv.wait(timeout=1.0)  # wakes periodically only to check attach retries
                exited, self._exited = self._exited, []
            for process_name in exited:
                self._on_process_exit(process_name)
            with self._cv:
                foreground = self._foreground
                foreground_changed, self._foreground_changed = self._foreground_changed, False
                want_state, self._want_state = self._want_state, False
                want_drain, self._want_drain = self._want_drain, False
                stopping = self._stopping
            self._attach_if_needed(foreground, foreground_changed)
            if want_state:
                for a in self.adapters:
                    if a.live():
                        self._get_state(a)
            if want_drain:
                for a in self.adapters:
                    self._drain(a)
            if stopping:
                with self._cv:
                    if self._want_state or self._want_drain or self._exited:
                        continue  # a notification raced in; handle it before the final drain
                self._shutdown()
                return

    def _on_process_exit(self, process_name):
        for a in self.adapters:
            if a.live() and a.matches(process_name):
                self.log(a.name, f"进程 {process_name} 退出,detach")
                self._detach(a)

    def _attach_if_needed(self, foreground, foreground_changed):
        now = time.monotonic()
        for a in self.adapters:
            if a.disabled or a.attached or a.gave_up or not a.matches(foreground):
                continue
            if a.retry_at is not None:
                if now < a.retry_at:
                    continue
            elif not foreground_changed:
                continue
            self._attach(a)

    def _attach(self, a):
        def do_attach():
            if a.instance is None:
                a.instance = a.cls()
            return a.instance.attach(a.ctx)

        status, result = a.thread.call(do_attach, ATTACH_TIMEOUT_S)
        self._count(a, "attach", status, result)
        if a.disabled:
            return
        if status == "ok" and result:
            a.attached = a.ever_attached = True
            a.attach_failures = 0
            a.retry_at = None
            self.log(a.name, "attach 成功")
            self._get_state(a)
            return
        a.attach_failures += 1
        if status == "ok":
            self.log(a.name, f"attach 返回 {result!r}")
        if a.attach_failures >= MAX_ATTACH_FAILURES:
            a.gave_up = True
            self._warn(f"{a.name}: attach 连续失败 {a.attach_failures} 次,本次录制不再重试")
        else:
            a.retry_at = time.monotonic() + ATTACH_RETRY_DELAY_S
            self.log(a.name, f"attach 失败({a.attach_failures}/{MAX_ATTACH_FAILURES}),{ATTACH_RETRY_DELAY_S}s 后重试")

    def _get_state(self, a):
        status, result = a.thread.call(lambda: a.instance.get_state(), GET_STATE_TIMEOUT_S)
        self._count(a, "get_state", status, result)
        if status == "ok" and result is not None:
            self._write(a, "get_state", "software_state.jsonl", [result], STATE_REQUIRED)

    def _drain(self, a):
        for method, filename in (("get_actions", "software_actions.jsonl"),
                                 ("get_events", "software_events.jsonl")):
            if not a.live():
                return
            status, result = a.thread.call(lambda m=method: getattr(a.instance, m)(), DRAIN_TIMEOUT_S)
            self._count(a, method, status, result)
            if status != "ok":
                continue
            if not isinstance(result, list):
                self.log(a.name, f"{method} 返回了 {type(result).__name__},应为 list,本次结果丢弃")
                continue
            self._write(a, method, filename, result, RECORD_REQUIRED)

    def _detach(self, a):
        status, result = a.thread.call(lambda: a.instance.detach(), DETACH_TIMEOUT_S)
        a.attached = False
        self._count(a, "detach", status, result)
        if status == "timeout":
            self.log(a.name, "detach 超时,放弃该线程;之后如需重新 attach 会换新线程和新实例")
            a.thread.close()
            a.thread = _AdapterThread(a.name)
            a.instance = None
        else:
            self.log(a.name, "detach 完成")

    def _shutdown(self):
        for a in self.adapters:
            self._drain(a)
        for a in self.adapters:
            if a.live():
                self._detach(a)
        for a in self.adapters:
            a.thread.close()

    def _count(self, a, method, status, detail):
        if status == "ok":
            a.consecutive_failures = 0
            return
        if status == "timeout":
            a.timeouts += 1
            self.log(a.name, f"{method} 超时")
        else:
            a.errors += 1
            self.log(a.name, f"{method} 抛异常:\n{detail.rstrip()}")
        a.consecutive_failures += 1
        if a.consecutive_failures >= MAX_CONSECUTIVE_FAILURES and not a.disabled:
            a.disabled = True
            a.attached = False
            self._warn(f"{a.name}: 连续 {MAX_CONSECUTIVE_FAILURES} 次超时或异常,本次录制停用该适配器")

    def _write(self, a, method, filename, records, required):
        lines, dropped = [], []
        for record in records:
            reason = _check_record(record, required)
            if reason is None:
                try:
                    lines.append(json.dumps(record, ensure_ascii=False, allow_nan=False))
                except (TypeError, ValueError) as exc:
                    reason = f"无法序列化为 JSON: {exc}"
            if reason is not None:
                dropped.append(reason)
        if dropped:
            self.log(a.name, f"{method}: 丢弃 {len(dropped)} 条非法记录,第一条原因: {dropped[0]}")
        if lines:
            with (self.raw_dir / filename).open("a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
