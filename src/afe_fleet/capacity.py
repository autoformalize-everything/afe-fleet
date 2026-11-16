"""Spare capacity: claim only while the agent's subscription usage runs behind the clock.

For every rate-limit window the subscription reports (a 5-hour session, a week, a per-model
week), `used` is the fraction of the limit spent and `elapsed` the fraction of the window
passed. A worker with `spare_capacity = L` claims only while, in every window,

    used < L * elapsed + ease_in * (1 - elapsed) + allowance

The pace line `L * elapsed` alone is `used / elapsed < L`: at L = 1 the worker never spends
faster than the clock refills, and below 1 it leaves headroom for whoever else uses the account.
It is unforgiving right after a reset, when `elapsed` is near 0, so `ease_in` grants a head
start that fades linearly to nothing at the window's end, and `allowance` a constant margin
(both fractions of the limit, default 0). A window with nothing used never blocks; a reported
hard limit always does; usage it cannot read counts as no spare capacity. Running attempts are
never interrupted: the check gates claims.

Usage is read through the agent CLI itself, without a model request: Claude Code answers a
`/usage` message in stream-json mode with a `usage_report` (`rate_limits.limits[]` with `group`,
`percent`, `resets_at`); the Codex app-server answers `account/rateLimits/read` (`primary` and
`secondary` windows with `usedPercent`, `windowDurationMins`, `resetsAt`). `[agent]
usage_command`, a command printing that same JSON, replaces the built-in reader.
"""

from __future__ import annotations

import json
import logging
import math
import queue
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

log = logging.getLogger(__name__)

# Claude's report gives each window's reset time but not its length.
CLAUDE_WINDOW_S = {"session": 5 * 3600, "weekly": 7 * 86400}


class UsageUnknown(Exception):
    """The usage could not be read or understood."""


@dataclass(frozen=True)
class Window:
    name: str
    used: float  # fraction of the limit spent
    elapsed: float  # fraction of the window passed, 0..1

    def budget(self, limit: float, ease_in: float = 0.0, allowance: float = 0.0) -> float:
        """The fraction of the limit this window may have used by now."""
        return limit * self.elapsed + ease_in * (1 - self.elapsed) + allowance

    def spare(self, limit: float, ease_in: float = 0.0, allowance: float = 0.0) -> bool:
        return self.used <= 0 or self.used < self.budget(limit, ease_in, allowance)


def _elapsed(resets_at: float, length: float, now: float) -> float:
    return min(1.0, max(0.0, 1.0 - (resets_at - now) / length))


def _number(v: object, what: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
        raise UsageUnknown(f"{what} is not a usable number: {v!r}")
    return float(v)


def parse_claude(report: dict, now: float) -> list[Window]:
    limits = (report.get("rate_limits") or {}).get("limits") if isinstance(report, dict) else None
    if not isinstance(limits, list) or not limits:
        raise UsageUnknown("no rate_limits.limits in the Claude usage report")
    out = []
    for lim in limits:
        if not isinstance(lim, dict):
            raise UsageUnknown(f"malformed limit {lim!r}")
        group = lim.get("group") or lim.get("kind")
        scope = ((lim.get("scope") or {}).get("model") or {}).get("display_name") if lim.get("scope") else None
        name = f"{lim.get('kind') or group}{f' ({scope})' if scope else ''}"
        used = _number(lim.get("percent"), f"{name} percent") / 100
        length = CLAUDE_WINDOW_S.get(str(group))
        if lim.get("resets_at") is None:
            if used > 0:
                raise UsageUnknown(f"{name}: {used:.0%} used but no reset time")
            out.append(Window(name, 0.0, 0.0))
            continue
        if length is None:
            raise UsageUnknown(f"{name}: unknown window group {group!r}")
        try:
            resets = datetime.fromisoformat(str(lim["resets_at"])).timestamp()
        except ValueError:
            raise UsageUnknown(f"{name}: bad resets_at {lim['resets_at']!r}") from None
        out.append(Window(name, used, _elapsed(resets, length, now)))
    return out


def parse_codex(result: dict, now: float) -> list[Window]:
    if not isinstance(result, dict) or not isinstance(result.get("rateLimits"), dict):
        raise UsageUnknown("no rateLimits in the Codex usage report")
    rl = result["rateLimits"]
    if result.get("ordinaryUsageAllowed") is False or rl.get("rateLimitReachedType"):
        why = rl.get("rateLimitReachedType") or "usage not allowed"
        return [Window(f"codex limit reached ({why})", math.inf, 0.0)]  # no allowance passes a hard limit
    out = []
    for key in ("primary", "secondary"):
        w = rl.get(key)
        if w is None:
            continue  # not applicable to this plan
        if not isinstance(w, dict):
            raise UsageUnknown(f"malformed {key} window {w!r}")
        mins = _number(w.get("windowDurationMins"), f"{key} windowDurationMins")
        name = f"{int(mins // 60)}h" if mins < 24 * 60 else f"{int(mins // 1440)}d"
        used = _number(w.get("usedPercent"), f"{key} usedPercent") / 100
        if w.get("resetsAt") is None:
            if used > 0:
                raise UsageUnknown(f"{name}: {used:.0%} used but no reset time")
            out.append(Window(name, 0.0, 0.0))
            continue
        if mins <= 0:
            raise UsageUnknown(f"{name}: window of {mins} minutes")
        out.append(Window(name, used, _elapsed(_number(w["resetsAt"], f"{key} resetsAt"), mins * 60, now)))
    if not out:
        raise UsageUnknown("the Codex usage report has no windows")
    return out


PARSERS = {"claude": parse_claude, "codex": parse_codex}


def verdict(windows: list[Window], limit: float, ease_in: float = 0.0, allowance: float = 0.0) -> tuple[bool, str]:
    """Whether there is spare capacity, and the window that decides it: the first one over its
    budget, else the one closest to it."""
    if not windows:
        return False, "no usage windows"
    over = [w for w in windows if not w.spare(limit, ease_in, allowance)]
    used_any = [w for w in windows if w.used > 0] or windows
    worst = over[0] if over else min(used_any, key=lambda w: w.budget(limit, ease_in, allowance) - w.used)
    if math.isinf(worst.used):
        return False, worst.name
    budget = worst.budget(limit, ease_in, allowance)
    rel = "≥" if over else "<"
    return not over, (f"{worst.name} {worst.used:.0%} used {rel} budget {budget:.0%} "
                      f"at {worst.elapsed:.0%} of its window")


def read_claude(claude: str = "claude", timeout: float = 120) -> dict:
    """Claude Code's usage report: `/usage` sent in stream-json mode, run outside any project."""
    message = json.dumps({"type": "user", "message": {"role": "user", "content": "/usage"}})
    argv = [claude, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]
    try:
        with tempfile.TemporaryDirectory(prefix="afe-usage-") as cwd:
            p = subprocess.run(argv, input=message + "\n", capture_output=True, text=True, timeout=timeout, cwd=cwd)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise UsageUnknown(f"{claude} /usage: {e}") from None
    for line in p.stdout.splitlines():
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(msg, dict) and isinstance(msg.get("usage_report"), dict):
            return msg["usage_report"]
    raise UsageUnknown(f"{claude} /usage gave no usage report (exit {p.returncode}): "
                       f"{(p.stderr or p.stdout).strip()[-300:]}")


def read_codex(codex: str = "codex", timeout: float = 60) -> dict:
    """The Codex app-server's `account/rateLimits/read` result."""
    try:
        proc = subprocess.Popen([codex, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, bufsize=1)
    except OSError as e:
        raise UsageUnknown(f"{codex} app-server: {e}") from None
    lines: queue.Queue[str | None] = queue.Queue()

    def reader() -> None:
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=reader, daemon=True).start()
    try:
        for req in ({"method": "initialize", "id": 1,
                     "params": {"clientInfo": {"name": "afe_fleet", "title": "afe-fleet", "version": "1"}}},
                    {"method": "initialized", "params": {}},
                    {"method": "account/rateLimits/read", "id": 2}):
            proc.stdin.write(json.dumps(req) + "\n")
        proc.stdin.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                line = lines.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                raise UsageUnknown(f"{codex} app-server: no rate limits within {timeout:.0f}s") from None
            if line is None:
                raise UsageUnknown(f"{codex} app-server exited before answering")
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(msg, dict) and msg.get("id") == 2:
                if "error" in msg:
                    raise UsageUnknown(f"{codex} app-server: {msg['error']}")
                return msg.get("result") or {}
    except OSError as e:
        raise UsageUnknown(f"{codex} app-server: {e}") from None
    finally:
        proc.kill()
        proc.wait(10)


READERS = {"claude": read_claude, "codex": read_codex}


def run_command(argv: list[str], timeout: float = 120) -> dict:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise UsageUnknown(f"{argv[0]}: {e}") from None
    if p.returncode != 0:
        raise UsageUnknown(f"{argv[0]} exited {p.returncode}: {(p.stderr or p.stdout).strip()[-300:]}")
    lines = [ln for ln in p.stdout.splitlines() if ln.strip()]
    try:
        return json.loads(lines[-1]) if lines else {}
    except json.JSONDecodeError:
        raise UsageUnknown(f"{argv[0]} did not print JSON: {lines[-1][:200]!r}") from None


class SpareCapacity:
    """The spare-capacity check for one agent CLI, shared by a worker's slots; the usage reading
    is cached for `ttl` seconds. `exe` is the CLI's executable; `command`, if given, a command that
    prints the usage JSON instead."""

    def __init__(self, cli: str, limit: float, command: list[str] | None = None, *, exe: str | None = None,
                 ease_in: float = 0.0, allowance: float = 0.0, ttl: float = 300,
                 clock: Callable[[], float] = time.time, reader: Callable[[], dict] | None = None):
        if cli not in PARSERS:
            raise ValueError(f"no usage parser for agent CLI {cli!r}")
        self.cli, self.limit, self.ttl, self.clock = cli, limit, ttl, clock
        self.ease_in, self.allowance = ease_in, allowance
        if reader is not None:
            self.read = reader
        elif command:
            self.read = lambda: run_command(list(command))
        else:
            self.read = lambda: READERS[cli](exe or cli)
        self._lock = threading.Lock()
        self._read_at: float | None = None
        self._windows: list[Window] | None = None
        self._error: str | None = None

    def check(self) -> tuple[bool, str]:
        with self._lock:
            now = self.clock()
            if self._read_at is None or now - self._read_at >= self.ttl:
                self._read_at = now
                try:
                    self._windows, self._error = PARSERS[self.cli](self.read(), now), None
                except UsageUnknown as e:
                    self._windows, self._error = None, str(e)
            if self._windows is None:
                return False, f"usage unknown ({self._error})"
            return verdict(self._windows, self.limit, self.ease_in, self.allowance)
