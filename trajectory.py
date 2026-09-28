#!/usr/bin/env python3
"""Builds trajectory.jsonl and trajectory.html from raw/ data.

Pure offline processing: reads raw/events.jsonl and raw/terminal/*, merges
low-level events into actions, and writes trajectory.jsonl + trajectory.html
into <episode_dir>/traj/ (screenshots/recording.mp4 stay at the episode
root; trajectory.html's <img>/<video> tags point back up at them). Imports
nothing Windows-specific, so it runs the same way on macOS (for
development/testing) and on the Windows VM.

Usage:
    python trajectory.py output/<episode_id>
"""
import argparse
import base64
import html
import json
import re
import struct
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Local timezone used for the human-readable `timestamp` field. task.md's
# examples use +08:00; there is no spec for reading the real VM timezone,
# so this is fixed (see README "简化" section).
TZ = timezone(timedelta(hours=8))

DOUBLE_CLICK_MAX_GAP_MS = 500
DOUBLE_CLICK_MAX_DIST = 5
DRAG_MIN_DIST = 5
SCROLL_MERGE_GAP_MS = 400
TYPE_TEXT_MAX_GAP_MS = 1500
UIA_MATCH_WINDOW_MS = 2500

MODIFIER_KEYS = ("ctrl", "alt", "shift", "win")
HOTKEY_MODIFIERS = ("ctrl", "alt", "win")  # shift alone does not trigger a hotkey
MODIFIER_ORDER = ("ctrl", "alt", "shift", "win")

POWERSHELL_PROCESSES = {"powershell.exe", "pwsh.exe"}

# System/background processes are kept in raw/events.jsonl but dropped here.
BACKGROUND_PROCESS_NAMES = {
    "svchost.exe", "conhost.exe", "dwm.exe", "csrss.exe", "wininit.exe",
    "winlogon.exe", "services.exe", "lsass.exe", "fontdrvhost.exe",
    "smss.exe", "taskhostw.exe", "registry", "system", "system idle process",
}
SYSTEM_USERS = {"SYSTEM", "LOCAL SERVICE", "NETWORK SERVICE", "", None}


# --------------------------------------------------------------------------
# raw loading
# --------------------------------------------------------------------------

def load_raw_events(raw_dir: Path):
    path = raw_dir / "events.jsonl"
    events = []
    if not path.exists():
        return events
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    events.sort(key=lambda e: e["t_ms"])
    return events


def parse_transcripts(terminal_dir: Path):
    """Parses PowerShell transcripts into an ordered list of {command, output}.

    Transcript text has no reliable per-command timestamp (Start-Transcript
    only stamps file start/end, not every prompt), so ordering across files
    uses each file's start time, and commands within a file keep their
    textual order. Correlating a specific `shell_command` action with a
    transcript entry is therefore done by sequential consumption, not by
    matching timestamps (see README "简化" section).
    """
    if not terminal_dir.exists():
        return []

    files = []
    for path in sorted(terminal_dir.glob("*.txt")):
        text = path.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"^Start time:\s*(\d{14})", text, re.MULTILINE)
        start_key = m.group(1) if m else "0"
        files.append((start_key, path.name, text))
    files.sort(key=lambda x: (x[0], x[1]))

    commands = []
    prompt_re = re.compile(r"^PS [A-Za-z]:[^>]*>\s?(.*)$")
    for _, _, text in files:
        lines = text.splitlines()
        current_cmd = None
        current_output = []
        for line in lines:
            m = prompt_re.match(line)
            if m:
                if current_cmd is not None:
                    commands.append({
                        "command": current_cmd,
                        "output": "\n".join(current_output).strip("\n"),
                    })
                cmd_text = m.group(1).strip()
                if cmd_text:
                    current_cmd = cmd_text
                    current_output = []
                else:
                    current_cmd = None
                    current_output = []
            elif current_cmd is not None:
                current_output.append(line)
        if current_cmd is not None:
            commands.append({
                "command": current_cmd,
                "output": "\n".join(current_output).strip("\n"),
            })
    return commands


# --------------------------------------------------------------------------
# merging raw events into actions
# --------------------------------------------------------------------------

def _dist(x1, y1, x2, y2):
    return ((x1 - x2) ** 2 + (y1 - y2) ** 2) ** 0.5


def _find_uia_target(uia_queries, t_ms, x, y, used):
    best = None
    for i, ev in enumerate(uia_queries):
        if i in used:
            continue
        if 0 <= ev["t_ms"] - t_ms <= UIA_MATCH_WINDOW_MS:
            if best is None or ev["t_ms"] < uia_queries[best]["t_ms"]:
                best = i
    if best is None:
        return None
    used.add(best)
    return uia_queries[best]["target"]


def _window_at(window_events, t_ms):
    result = None
    for ev in window_events:
        if ev["t_ms"] <= t_ms:
            result = ev
        else:
            break
    return result


def _cursor_at_or_before(mouse_position_events, t_ms):
    """Where the mouse cursor physically was, last known, at or before
    t_ms — from mouse_down/mouse_up/scroll events (the only raw events
    that carry x/y). Used to mark the cursor on observation.screenshot;
    this is present-tense info (where the mouse already is), not a leak of
    where the upcoming action will click."""
    result = None
    for ev in mouse_position_events:
        if ev["t_ms"] <= t_ms:
            result = [ev["x"], ev["y"]]
        else:
            break
    return result


def _is_powershell(window_ev):
    if not window_ev:
        return False
    return window_ev.get("process", "").lower() in POWERSHELL_PROCESSES


def merge_mouse_actions(events, window_events):
    """Pairs mouse_down/up into click/double_click/drag actions."""
    downs = [e for e in events if e["type"] == "mouse_down"]
    ups = [e for e in events if e["type"] == "mouse_up"]
    uia_queries = [e for e in events if e["type"] == "uia_query"]
    used_uia = set()

    # Pair each down with the next up of the same button (single-button
    # interactions only; simultaneous multi-button drags are out of scope).
    pairs = []
    ups_by_button = {}
    for e in ups:
        ups_by_button.setdefault(e["button"], []).append(e)
    for down in downs:
        btn = down["button"]
        candidates = ups_by_button.get(btn, [])
        match = None
        for up in candidates:
            if up["t_ms"] >= down["t_ms"] and up not in [p[1] for p in pairs]:
                match = up
                break
        if match:
            pairs.append((down, match))
    pairs.sort(key=lambda p: p[0]["t_ms"])

    raw_clicks = []
    for down, up in pairs:
        dist = _dist(down["x"], down["y"], up["x"], up["y"])
        target = _find_uia_target(uia_queries, down["t_ms"], down["x"], down["y"], used_uia)
        if dist > DRAG_MIN_DIST:
            raw_clicks.append({
                "kind": "drag",
                "start_t_ms": down["t_ms"],
                "end_t_ms": up["t_ms"],
                "button": down["button"],
                "start": [down["x"], down["y"]],
                "end": [up["x"], up["y"]],
                "target": target,
            })
        else:
            raw_clicks.append({
                "kind": "click",
                "start_t_ms": down["t_ms"],
                "end_t_ms": up["t_ms"],
                "button": down["button"],
                "position": [down["x"], down["y"]],
                "target": target,
            })

    # Merge consecutive same-button clicks within the double-click window.
    actions = []
    i = 0
    while i < len(raw_clicks):
        c = raw_clicks[i]
        if (
            c["kind"] == "click"
            and i + 1 < len(raw_clicks)
            and raw_clicks[i + 1]["kind"] == "click"
            and raw_clicks[i + 1]["button"] == c["button"]
            and raw_clicks[i + 1]["start_t_ms"] - c["end_t_ms"] <= DOUBLE_CLICK_MAX_GAP_MS
            and _dist(*c["position"], *raw_clicks[i + 1]["position"]) <= DOUBLE_CLICK_MAX_DIST
        ):
            n = raw_clicks[i + 1]
            actions.append({
                "start_t_ms": c["start_t_ms"],
                "end_t_ms": n["end_t_ms"],
                "action": {
                    "type": "mouse_double_click",
                    "button": c["button"],
                    "position": c["position"],
                    "target": c["target"] or n["target"],
                },
            })
            i += 2
            continue
        if c["kind"] == "click":
            actions.append({
                "start_t_ms": c["start_t_ms"],
                "end_t_ms": c["end_t_ms"],
                "action": {
                    "type": "mouse_click",
                    "button": c["button"],
                    "position": c["position"],
                    "target": c["target"],
                },
            })
        else:
            actions.append({
                "start_t_ms": c["start_t_ms"],
                "end_t_ms": c["end_t_ms"],
                "action": {
                    "type": "mouse_drag",
                    "button": c["button"],
                    "start": c["start"],
                    "end": c["end"],
                    "target": c["target"],
                },
            })
        i += 1
    return actions


def merge_scroll_actions(events):
    scrolls = [e for e in events if e["type"] == "scroll"]
    actions = []
    i = 0
    while i < len(scrolls):
        group = [scrolls[i]]
        j = i + 1
        while j < len(scrolls) and scrolls[j]["t_ms"] - group[-1]["t_ms"] <= SCROLL_MERGE_GAP_MS:
            group.append(scrolls[j])
            j += 1
        first = group[0]
        dx = sum(e["dx"] for e in group)
        dy = sum(e["dy"] for e in group)
        actions.append({
            "start_t_ms": first["t_ms"],
            "end_t_ms": group[-1]["t_ms"],
            "action": {
                "type": "scroll",
                "position": [first["x"], first["y"]],
                "dx": dx,
                "dy": dy,
                "target": None,
            },
        })
        i = j
    return actions


PRINTABLE_RE = re.compile(r"^[\x20-\x7e]$|^[一-鿿]$")


def _is_printable(key):
    return bool(PRINTABLE_RE.match(key)) if len(key) == 1 else False


def merge_key_actions(events, window_events, transcript_commands):
    """Merges key_down/up into hotkey / type_text / key_press / shell_command.

    Also returns, per shell_command action, the transcript output that must
    be attached to the *next* step's observation.terminal.
    """
    key_stream = [e for e in events if e["type"] in ("key_down", "key_up")]
    key_stream.sort(key=lambda e: e["t_ms"])
    uia_focused = [e for e in events if e["type"] == "uia_focused"]
    used_focus = set()

    actions = []
    pending_output_by_start_t = {}
    transcript_iter = iter(transcript_commands)

    held_modifiers = set()
    type_buffer = []  # list of (t_ms, char) currently being accumulated
    type_start_t = None
    last_key_t = None

    def flush_type_text():
        nonlocal type_buffer, type_start_t
        if type_buffer:
            text = "".join(c for _, c in type_buffer)
            # uia_focused is queried asynchronously right as the burst
            # starts, so its t_ms is always slightly AFTER type_start_t
            # (same reason screenshots need an at-or-after match, not
            # at-or-before) — find the first one at or after, not before.
            target = None
            best_i = None
            for i, ev in enumerate(uia_focused):
                if i in used_focus:
                    continue
                if 0 <= ev["t_ms"] - type_start_t <= UIA_MATCH_WINDOW_MS:
                    if best_i is None or ev["t_ms"] < uia_focused[best_i]["t_ms"]:
                        best_i = i
            if best_i is not None:
                used_focus.add(best_i)
                target = uia_focused[best_i]["target"]
            actions.append({
                "start_t_ms": type_start_t,
                "end_t_ms": type_buffer[-1][0],
                "action": {"type": "type_text", "text": text, "target": target},
            })
        type_buffer = []
        type_start_t = None

    for ev in key_stream:
        key = ev["key"]
        t_ms = ev["t_ms"]

        if ev["type"] == "key_up":
            held_modifiers.discard(key)
            continue

        win_ev = _window_at(window_events, t_ms)
        in_powershell = _is_powershell(win_ev)

        if key in MODIFIER_KEYS:
            held_modifiers.add(key)
            continue

        active_mods = [m for m in MODIFIER_ORDER if m in held_modifiers]
        hotkey_mods = [m for m in active_mods if m in HOTKEY_MODIFIERS]

        if in_powershell:
            if key == "enter":
                flush_type_text()
                try:
                    entry = next(transcript_iter)
                    command = entry["command"]
                    output = entry["output"]
                except StopIteration:
                    command = ""
                    output = ""
                actions.append({
                    "start_t_ms": t_ms,
                    "end_t_ms": t_ms,
                    "action": {"type": "shell_command", "command": command},
                })
                pending_output_by_start_t[t_ms] = output
            # Any other key in a PowerShell window produces no
            # type_text/key_press per task.md; it only feeds the transcript.
            continue

        if hotkey_mods:
            flush_type_text()
            keys = hotkey_mods + [key]
            actions.append({
                "start_t_ms": t_ms,
                "end_t_ms": t_ms,
                "action": {"type": "hotkey", "keys": keys},
            })
            continue

        if key == "backspace" and type_buffer:
            type_buffer.pop()
            last_key_t = t_ms
            continue

        if _is_printable(key) and not active_mods:
            if type_start_t is not None and last_key_t is not None and t_ms - last_key_t > TYPE_TEXT_MAX_GAP_MS:
                flush_type_text()
            if type_start_t is None:
                type_start_t = t_ms
            type_buffer.append((t_ms, key))
            last_key_t = t_ms
            continue

        # Non-printable, non-hotkey key (enter/tab/esc/f5/up/...) outside
        # PowerShell: ends any in-progress type_text, then stands alone.
        flush_type_text()
        actions.append({
            "start_t_ms": t_ms,
            "end_t_ms": t_ms,
            "action": {"type": "key_press", "key": key},
        })

    flush_type_text()
    actions.sort(key=lambda a: a["start_t_ms"])
    return actions, pending_output_by_start_t


# --------------------------------------------------------------------------
# steps
# --------------------------------------------------------------------------

def filter_process_events(events):
    """Keeps only current-user, non-background process events.

    An `exit` event may not carry `user`/full info (the process is already
    gone by the time it's detected), so a pid accepted at `start` is kept at
    `exit` too rather than being re-filtered on possibly-missing fields.
    """
    def passes(e):
        name = (e.get("name") or "").lower()
        user = e.get("user")
        return name not in BACKGROUND_PROCESS_NAMES and user not in SYSTEM_USERS

    out = []
    accepted_pids = set()
    for e in events:
        if e["type"] != "process_event":
            continue
        if e["op"] == "start":
            if passes(e):
                accepted_pids.add(e["pid"])
                out.append(e)
        else:  # exit
            if e["pid"] in accepted_pids or passes(e):
                out.append(e)
    return out


def _screenshot_at_or_before(screenshots, t_ms):
    """The observation for a step must show the state AFTER the previous
    action's effects settled, not the state at the instant this action
    fires (by then the mouse has already moved to its target). So
    input_recorder.py takes each screenshot once input goes quiet for a
    short debounce window — i.e. after an action (or a whole burst)
    finishes, not when it starts. This picks the most recent one of those
    settled screenshots before this action begins, which naturally also
    covers step 0 (only the initial screenshot precedes it) and the final
    step (only the explicit stop-time screenshot precedes recording_end),
    with no special-casing needed."""
    result = None
    for ev in screenshots:
        if ev["t_ms"] <= t_ms:
            result = ev
        else:
            break
    return result


def _first_screenshot_at_or_after(screenshots, t_ms):
    """action.screenshot is a picture of the exact moment the action fired
    (mouse_down / first scroll tick / key_down), captured asynchronously
    a few ms afterward — so its t_ms is always slightly larger than the
    action's own start_t_ms. This finds that "trigger" screenshot."""
    for ev in screenshots:
        if ev["t_ms"] >= t_ms:
            return ev
    return None


def build_steps(raw_events, transcript_commands, snapshot_events=None, snapshot_enabled=False):
    window_events = [e for e in raw_events if e["type"] == "window_active"]
    all_screenshots = [e for e in raw_events if e["type"] == "screenshot"]
    # Screenshots written before this field existed are treated as
    # "settle" (that was the only kind at the time).
    screenshots = [e for e in all_screenshots if e.get("kind") != "trigger"]
    trigger_screenshots = [e for e in all_screenshots if e.get("kind") == "trigger"]
    mouse_position_events = [e for e in raw_events if e["type"] in ("mouse_down", "mouse_up", "scroll")]
    file_events = [e for e in raw_events if e["type"] == "file_event"]
    process_events = filter_process_events(raw_events)

    recording_start_events = [e for e in raw_events if e["type"] == "recording_start"]
    recording_end_events = [e for e in raw_events if e["type"] == "recording_end"]
    video_t0 = recording_start_events[0]["t_ms"] if recording_start_events else (
        raw_events[0]["t_ms"] if raw_events else 0
    )
    recording_end_t_ms = recording_end_events[-1]["t_ms"] if recording_end_events else (
        raw_events[-1]["t_ms"] if raw_events else video_t0
    )
    recording_start_t_ms = recording_start_events[0]["t_ms"] if recording_start_events else video_t0

    mouse_actions = merge_mouse_actions(raw_events, window_events)
    scroll_actions = merge_scroll_actions(raw_events)
    key_actions, terminal_output_by_start_t = merge_key_actions(raw_events, window_events, transcript_commands)

    actions = sorted(mouse_actions + scroll_actions + key_actions, key=lambda a: a["start_t_ms"])

    def make_observation(shot):
        # Window state is looked up at the matched screenshot's own time
        # (not the action's start_t_ms) so both fields in one observation
        # describe the same instant.
        win_t_ms = shot["t_ms"] if shot else recording_start_t_ms
        win = _window_at(window_events, win_t_ms)
        obs = {
            "screenshot": shot["path"] if shot else None,
            "video_time": round((shot["t_ms"] - video_t0) / 1000, 3) if shot else None,
            "cursor": _cursor_at_or_before(mouse_position_events, win_t_ms),
            "window": {"title": win["title"], "process": win["process"]} if win else None,
            "terminal": None,
        }
        return obs

    def events_between(left_t_ms, right_t_ms, left_inclusive=False):
        files = []
        seen_paths = {}
        for e in file_events:
            in_range = (left_t_ms < e["t_ms"] <= right_t_ms) if not left_inclusive else (
                left_t_ms <= e["t_ms"] <= right_t_ms
            )
            if not in_range:
                continue
            entry = {"op": e["op"], "path": e["path"]}
            if e["op"] == "renamed":
                entry["new_path"] = e["new_path"]
            elif e["op"] != "deleted":
                entry["size"] = e.get("size")
            if e["op"] == "modified":
                seen_paths[e["path"]] = entry
                continue
            files.append(entry)
        files.extend(seen_paths.values())
        files.sort(key=lambda x: x.get("path", ""))

        procs = []
        for e in process_events:
            in_range = (left_t_ms < e["t_ms"] <= right_t_ms) if not left_inclusive else (
                left_t_ms <= e["t_ms"] <= right_t_ms
            )
            if not in_range:
                continue
            entry = {"op": e["op"], "name": e["name"], "pid": e["pid"]}
            if e.get("cmdline") is not None:
                entry["cmdline"] = e["cmdline"]
            procs.append(entry)
        return {"files": files, "processes": procs}

    steps = []
    pending_terminal_stdout = None

    for idx, act in enumerate(actions):
        shot = _screenshot_at_or_before(screenshots, act["start_t_ms"])
        obs = make_observation(shot)
        if pending_terminal_stdout is not None:
            obs["terminal"] = {"stdout": pending_terminal_stdout, "stderr": None}
            pending_terminal_stdout = None

        trigger_shot = _first_screenshot_at_or_after(trigger_screenshots, act["start_t_ms"])
        act["action"]["screenshot"] = trigger_shot["path"] if trigger_shot else None

        # step 0's window covers [recording_start, next action) instead of
        # (this action's own end, next action) per task.md's step-0 rule.
        left = recording_start_t_ms if idx == 0 else act["end_t_ms"]
        right = actions[idx + 1]["start_t_ms"] if idx + 1 < len(actions) else recording_end_t_ms
        ev = events_between(left, right, left_inclusive=(idx == 0))

        steps.append({
            "step_id": idx,
            "t_ms": act["start_t_ms"],
            "observation": obs,
            "action": act["action"],
            "events": ev,
        })

        if act["action"]["type"] == "shell_command":
            out = terminal_output_by_start_t.get(act["start_t_ms"])
            if out is not None:
                pending_terminal_stdout = out

    # final step: action = null, observation = the explicit screenshot
    # main.py takes at stop time (the most recent one at/before recording_end).
    final_t_ms = recording_end_t_ms
    final_shot = _screenshot_at_or_before(screenshots, final_t_ms)
    obs = make_observation(final_shot)
    if pending_terminal_stdout is not None:
        obs["terminal"] = {"stdout": pending_terminal_stdout, "stderr": None}
    last_action_end = actions[-1]["end_t_ms"] if actions else recording_start_t_ms
    ev = events_between(last_action_end, recording_end_t_ms)
    steps.append({
        "step_id": len(actions),
        "t_ms": final_t_ms,
        "observation": obs,
        "action": None,
        "events": ev,
    })

    for s in steps:
        s["timestamp"] = datetime.fromtimestamp(s["t_ms"] / 1000, tz=TZ).isoformat(timespec="milliseconds")

    _assign_snapshots(steps, snapshot_events or [], snapshot_enabled)

    return steps


# --------------------------------------------------------------------------
# snapshot field (task-v1.1.md)
# --------------------------------------------------------------------------

def _step_id_for_t_ms(steps, t_ms):
    """The step whose window [step.t_ms, next_step.t_ms) contains t_ms.
    A t_ms before the first step's own t_ms (e.g. the episode_start
    snapshot, committed before the first user action) still resolves to
    step 0 -- there's nothing earlier for it to belong to."""
    if not steps:
        return None
    result = steps[0]["step_id"]
    for s in steps:
        if s["t_ms"] <= t_ms:
            result = s["step_id"]
        else:
            break
    return result


def _assign_snapshots(steps, snapshot_events, snapshot_enabled):
    """Sets each step's `snapshot` field per task-v1.1.md section 3: None
    outright when the feature was off; otherwise the file_ckpt of the last
    snapshot event whose requested_t_ms falls in that step's window,
    carried forward from the previous step when its own window has none."""
    if not snapshot_enabled:
        for s in steps:
            s["snapshot"] = None
        return

    last_event_by_step = {}
    for e in sorted(snapshot_events, key=lambda ev: ev["requested_t_ms"]):
        sid = _step_id_for_t_ms(steps, e["requested_t_ms"])
        if sid is not None:
            last_event_by_step[sid] = e  # later events win -- "窗口内有多个时取最后一个"

    carry = None
    for s in steps:
        if s["step_id"] in last_event_by_step:
            carry = last_event_by_step[s["step_id"]]
        s["snapshot"] = {
            "file_ckpt": carry["commit"] if carry else None,
            "name": carry["name"] if carry else None,
            "vm_ckpt": None,
        }


def build_snapshot_index(steps, snapshot_events, watch_dir, snapshot_enabled):
    """Builds the structure written to snapshots/index.json: every commit
    that was actually made, each tagged with the step it falls under."""
    checkpoints = []
    for e in sorted(snapshot_events, key=lambda ev: ev["requested_t_ms"]):
        checkpoints.append({
            "commit": e["commit"],
            "requested_t_ms": e["requested_t_ms"],
            "committed_t_ms": e["committed_t_ms"],
            "reason": e["reason"],
            "name": e["name"],
            "step_id": _step_id_for_t_ms(steps, e["requested_t_ms"]),
            "files_changed": e["files_changed"],
        })
    return {"work_dir": watch_dir, "enabled": snapshot_enabled, "checkpoints": checkpoints}


# --------------------------------------------------------------------------
# HTML rendering
# --------------------------------------------------------------------------

def _png_size(path: Path):
    try:
        data = path.read_bytes()
        if data[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        width, height = struct.unpack(">II", data[16:24])
        return width, height
    except Exception:
        return None


def _load_cursor_icon():
    """Embeds cursor.png (if present next to this script) as a base64 data
    URI, so trajectory.html stays self-contained — no need to copy the icon
    into every episode directory. The arrow's tip sits at the image's
    top-left corner, so the marker div is positioned with no centering
    offset (unlike the plain dot it replaces)."""
    path = Path(__file__).resolve().parent / "cursor.png"
    size = _png_size(path)
    if not size:
        return None
    data_uri = "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")
    return {"data_uri": data_uri, "width": size[0], "height": size[1]}


def _esc(value):
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _kv_table(pairs):
    rows = "".join(f'<tr><td class="k">{_esc(k)}</td><td>{v_html}</td></tr>' for k, v_html in pairs)
    return f'<table class="kv">{rows}</table>'


def _render_target_html(target):
    if not target:
        return '<p class="empty">无</p>'
    pairs = [
        ("name", _esc(target.get("name"))),
        ("control_type", _esc(target.get("control_type"))),
        ("automation_id", _esc(target.get("automation_id"))),
        ("window", _esc(target.get("window"))),
        ("bounding_rect", _esc(json.dumps(target.get("bounding_rect")))),
    ]
    return _kv_table(pairs)


def _render_action_html(action, sizes, asset_prefix):
    if action is None:
        return '<p class="empty">(结束,无动作)</p>'
    pairs = [("type", f"<b>{_esc(action['type'])}</b>")]
    for k, v in action.items():
        if k in ("type", "target", "screenshot"):
            continue
        pairs.append((k, _esc(json.dumps(v, ensure_ascii=False))))
    parts = [_kv_table(pairs)]
    if "target" in action:
        parts.append('<div class="subhead">UIA target</div>')
        parts.append(_render_target_html(action["target"]))
    shot = action.get("screenshot")
    parts.append('<div class="subhead">Screenshot(动作触发瞬间)</div>')
    if shot:
        size = sizes.get(shot)
        overlay = _overlay_html(action, size) if size else ""
        parts.append(f'<div class="shot-wrap"><img src="{_esc(asset_prefix + shot)}" loading="lazy">{overlay}</div>')
    else:
        parts.append('<p class="empty">无截图</p>')
    return "".join(parts)


def _render_file_events_html(files):
    if not files:
        return '<p class="empty">无</p>'
    items = []
    for f in files:
        if f["op"] == "renamed":
            extra = f' → {_esc(f.get("new_path"))}'
        elif "size" in f:
            extra = f' ({_esc(f.get("size"))} bytes)'
        else:
            extra = ""
        items.append(f'<li>[{_esc(f["op"])}] {_esc(f["path"])}{extra}</li>')
    return "<ul>" + "".join(items) + "</ul>"


def _render_process_events_html(procs):
    if not procs:
        return '<p class="empty">无</p>'
    items = []
    for p in procs:
        extra = f' — {_esc(p["cmdline"])}' if p.get("cmdline") else ""
        items.append(f'<li>[{_esc(p["op"])}] {_esc(p["name"])} (pid {_esc(p["pid"])}){extra}</li>')
    return "<ul>" + "".join(items) + "</ul>"


def _overlay_html(action, img_size):
    """Click marker + UIA bounding-rect box, positioned as % of the
    screenshot's pixel size so it lines up regardless of display width."""
    if not img_size or not action:
        return ""
    iw, ih = img_size
    pos = None
    if action["type"] in ("mouse_click", "mouse_double_click", "scroll"):
        pos = action.get("position")
    elif action["type"] == "mouse_drag":
        pos = action.get("start")
    rect = None
    target = action.get("target")
    if target and target.get("bounding_rect"):
        rect = target["bounding_rect"]

    parts = []
    if pos:
        parts.append(f'<div class="marker" style="left:{pos[0] / iw * 100:.3f}%; top:{pos[1] / ih * 100:.3f}%;"></div>')
    if rect:
        x1, y1, x2, y2 = rect
        parts.append(
            f'<div class="rect" style="left:{x1 / iw * 100:.3f}%; top:{y1 / ih * 100:.3f}%; '
            f'width:{(x2 - x1) / iw * 100:.3f}%; height:{(y2 - y1) / ih * 100:.3f}%;"></div>'
        )
    return "".join(parts)


def _cursor_marker_html(cursor, img_size):
    """Just the cursor dot, no bounding-rect box — for
    observation.screenshot, which shows where the mouse already is, not
    what's about to be clicked."""
    if not cursor or not img_size:
        return ""
    iw, ih = img_size
    x, y = cursor
    return f'<div class="marker" style="left:{x / iw * 100:.3f}%; top:{y / ih * 100:.3f}%;"></div>'


def _render_step_html(step, sizes, asset_prefix):
    obs = step["observation"] or {}
    action = step["action"]
    events = step["events"] or {"files": [], "processes": []}
    shot = obs.get("screenshot")
    vtime = obs.get("video_time")

    summary_bits = [f"Step {step['step_id']}", _esc(step["timestamp"]), _esc(action["type"] if action else "结束")]
    if vtime is not None:
        summary_bits.append(f'<span class="vtime" onclick="event.preventDefault(); seek({vtime});">video {vtime}s</span>')
    summary = " · ".join(summary_bits)

    # Cursor dot only here (no bounding-rect box, no click marker for an
    # upcoming click): this screenshot shows the state BEFORE the action,
    # so only "where the mouse currently is" is legitimate to show. The
    # click-position + UIA-rect overlay belongs on action.screenshot.
    if shot:
        cursor_html = _cursor_marker_html(obs.get("cursor"), sizes.get(shot))
        screenshot_html = f'<div class="shot-wrap"><img src="{_esc(asset_prefix + shot)}" loading="lazy">{cursor_html}</div>'
    else:
        screenshot_html = '<p class="empty">无截图</p>'

    win = obs.get("window")
    window_html = (
        f'<span class="badge">{_esc(win["process"])}</span> {_esc(win["title"])}'
        if win else '<p class="empty">无</p>'
    )

    term = obs.get("terminal")
    terminal_html = f'<pre>{_esc(term["stdout"])}</pre>' if term else '<p class="empty">无</p>'

    return f'''<details class="step">
<summary>{summary}</summary>
<div class="step-body">
  <details class="section" open>
    <summary>Observation</summary>
    <div class="section-body">
      <details open><summary>Screenshot</summary>{screenshot_html}</details>
      <details open><summary>Window</summary>{window_html}</details>
      <details open><summary>Terminal</summary>{terminal_html}</details>
    </div>
  </details>
  <details class="section" open>
    <summary>Action</summary>
    <div class="section-body">{_render_action_html(action, sizes, asset_prefix)}</div>
  </details>
  <details class="section" open>
    <summary>Events</summary>
    <div class="section-body">
      <details open><summary>File events</summary>{_render_file_events_html(events.get("files"))}</details>
      <details open><summary>Process events</summary>{_render_process_events_html(events.get("processes"))}</details>
    </div>
  </details>
</div>
</details>'''


def _render_task_card_html(episode_dir):
    path = episode_dir / "task.json"
    if not path.exists():
        return ""
    try:
        task = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    title = task.get("title", "")
    instruction = task.get("task_instruction", "")
    extra_pairs = [(k, _esc(v)) for k, v in task.items() if k not in ("title", "task_instruction")]
    extra_html = _kv_table(extra_pairs) if extra_pairs else ""
    return f'''<div class="task-card">
  <h1>{_esc(title)}</h1>
  <p>{_esc(instruction)}</p>
  {extra_html}
</div>'''


def render_html(steps, episode_dir: Path, html_dir: Path):
    """Writes trajectory.html into html_dir. Every path stored in a step
    (screenshot paths, etc.) is relative to episode_dir, per the existing
    trajectory.jsonl schema -- that doesn't change here. Since V1.1,
    html_dir is a subdirectory of episode_dir (traj/), not episode_dir
    itself, so asset_prefix is prepended to each <img>/<video> src the
    HTML actually embeds, to point back at episode_dir/screenshots and
    episode_dir/recording.mp4 from the new location."""
    asset_prefix = "../" * len(html_dir.relative_to(episode_dir).parts)

    sizes = {}
    for s in steps:
        shots = [s["observation"].get("screenshot")]
        if s["action"]:
            shots.append(s["action"].get("screenshot"))
        for shot in shots:
            if shot and shot not in sizes:
                size = _png_size(episode_dir / shot)
                if size:
                    sizes[shot] = size

    video_path = "recording.mp4"
    has_video = (episode_dir / video_path).exists()
    video_section_html = (
        f'<video id="video" src="{asset_prefix + video_path}" controls></video>' if has_video
        else '<p class="empty">recording.mp4 not found</p>'
    )

    steps_html = "\n".join(_render_step_html(s, sizes, asset_prefix) for s in steps)
    task_card_html = _render_task_card_html(episode_dir)

    cursor_icon = _load_cursor_icon()
    if cursor_icon:
        # No centering offset: the arrow's tip is at the icon's own
        # top-left corner, which is where we want it to line up with the
        # actual x/y — unlike the plain-dot fallback, which needs to be
        # recentered on the point.
        marker_css = (
            f".marker {{ position: absolute; width: {cursor_icon['width']}px; height: {cursor_icon['height']}px; "
            f"background-image: url('{cursor_icon['data_uri']}'); background-size: contain; "
            f"background-repeat: no-repeat; pointer-events: none; }}"
        )
    else:
        marker_css = (
            ".marker { position: absolute; width: 10px; height: 10px; margin: -5px; border-radius: 50%; "
            "background: rgba(255,60,60,0.9); border: 1px solid #fff; }"
        )

    html_doc = f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>trajectory</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, sans-serif; margin: 0; background: #1e1f22; color: #e6e6e6; }}
  header {{ padding: 20px 24px; background: #26272b; border-bottom: 1px solid #333; }}
  header h2 {{ margin: 0 0 12px 0; }}
  .task-card h1 {{ margin: 0 0 6px 0; font-size: 20px; }}
  .task-card p {{ margin: 0 0 8px 0; color: #cfd2d6; }}
  #steps {{ padding: 12px 24px; }}
  details.step {{ border: 1px solid #333; border-radius: 6px; margin-bottom: 10px; background: #232427; }}
  details.step > summary {{ padding: 10px 14px; font-weight: 600; cursor: pointer; list-style: revert; }}
  details.step > summary:hover {{ background: #2a2b2f; }}
  .step-body {{ padding: 4px 16px 14px 16px; border-top: 1px solid #333; }}
  details.section {{ margin: 10px 0; border-left: 3px solid #3a3d42; padding-left: 10px; }}
  details.section > summary {{ font-weight: 600; cursor: pointer; color: #cfd2d6; padding: 4px 0; }}
  .section-body {{ padding: 4px 0 4px 4px; }}
  .section-body > details {{ margin: 8px 0; }}
  .section-body > details > summary {{ cursor: pointer; color: #9aa0a6; font-size: 13px; padding: 2px 0; }}
  .subhead {{ color: #9aa0a6; font-size: 12px; margin: 8px 0 2px 0; }}
  video {{ max-width: 720px; width: 100%; display: block; }}
  #recording {{ padding: 20px 24px; border-top: 1px solid #333; }}
  #recording h2 {{ margin: 0 0 12px 0; }}
  .shot-wrap {{ position: relative; display: inline-block; max-width: 640px; width: 100%; }}
  .shot-wrap img {{ width: 100%; display: block; border: 1px solid #444; }}
  {marker_css}
  .rect {{ position: absolute; border: 2px solid #4ea1ff; background: rgba(78,161,255,0.15); }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 4px; background: #3a3d42; font-size: 12px; margin-right: 6px; }}
  pre {{ white-space: pre-wrap; word-break: break-all; background: #17181a; padding: 8px; border-radius: 4px; font-size: 12px; max-height: 200px; overflow: auto; margin: 0; }}
  .vtime {{ cursor: pointer; color: #4ea1ff; text-decoration: underline; }}
  table.kv {{ border-collapse: collapse; font-size: 12px; width: 100%; }}
  table.kv td {{ padding: 2px 6px; vertical-align: top; word-break: break-word; }}
  table.kv td.k {{ color: #9aa0a6; white-space: nowrap; width: 110px; }}
  .empty {{ color: #666; font-style: italic; margin: 2px 0; }}
  ul {{ margin: 4px 0; padding-left: 18px; font-size: 12px; }}
</style>
</head>
<body>
<header>
  <h2>trajectory</h2>
  {task_card_html}
</header>
<div id="steps">
{steps_html}
</div>
<section id="recording">
  <h2>Recording</h2>
  {video_section_html}
</section>
<script>
function seek(t) {{
  const v = document.getElementById('video');
  if (!v) return;
  v.currentTime = t;
  v.play();
  v.scrollIntoView({{behavior: 'smooth', block: 'center'}});
}}
</script>
</body>
</html>
"""
    html_dir.mkdir(parents=True, exist_ok=True)
    (html_dir / "trajectory.html").write_text(html_doc, encoding="utf-8")


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def _load_meta(episode_dir: Path):
    path = episode_dir / "meta.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def generate(episode_dir: Path):
    episode_dir = Path(episode_dir)
    raw_dir = episode_dir / "raw"
    raw_events = load_raw_events(raw_dir)
    transcript_commands = parse_transcripts(raw_dir / "terminal")

    meta = _load_meta(episode_dir)
    snapshot_meta = meta.get("snapshot") or {}
    snapshot_enabled = bool(snapshot_meta.get("enabled"))
    watch_dir = meta.get("watch_dir")
    snapshot_events = [e for e in raw_events if e["type"] == "snapshot"]

    steps = build_steps(raw_events, transcript_commands, snapshot_events, snapshot_enabled)

    traj_dir = episode_dir / "traj"
    traj_dir.mkdir(parents=True, exist_ok=True)
    out_path = traj_dir / "trajectory.jsonl"
    with out_path.open("w", encoding="utf-8") as f:
        for s in steps:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")

    index = build_snapshot_index(steps, snapshot_events, watch_dir, snapshot_enabled)
    snapshots_dir = episode_dir / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    (snapshots_dir / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")

    render_html(steps, episode_dir, traj_dir)
    return steps


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("episode_dir", help="episode 目录,如 output/20260922_103215")
    args = parser.parse_args()

    episode_dir = Path(args.episode_dir)
    if not episode_dir.exists():
        print(f"episode 目录不存在: {episode_dir}", file=sys.stderr)
        sys.exit(1)

    steps = generate(episode_dir)
    traj_dir = episode_dir / "traj"
    print(f"[trajectory] {len(steps)} steps -> {traj_dir / 'trajectory.jsonl'}, {traj_dir / 'trajectory.html'}")


if __name__ == "__main__":
    main()
