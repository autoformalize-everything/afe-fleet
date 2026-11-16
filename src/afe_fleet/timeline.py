"""Timelines from the event log (events.py): a Chrome trace and a summary of where time went.

    afe-fleet timeline [--fleet NAME] [--since TIME] [--until TIME] [-o trace.json]

The trace (Trace Event format, complete events) opens in https://ui.perfetto.dev,
chrome://tracing or speedscope: one row per worker slot (and one for the admin), with spans
nested by time: attempt › agent › turn › tool, beside verify, push and the rest; idle spans
carry their reason. The summary, on standard output, gives each worker's busy and idle time
(by reason), the time per phase, the slowest tools, and every attempt.

A span still open at the end of the log (a running worker, or one that died) is drawn up to
the worker's last event and marked `open`.
"""

from __future__ import annotations

import gzip
import json
import time
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

PHASES = ("fetch", "worktree", "merge-base", "container-start", "prepare", "agent", "verify", "push", "pr",
          "report", "review-post", "cleanup", "container-stop")


@dataclass
class Span:
    w: str
    name: str
    start: float
    end: float
    args: dict = field(default_factory=dict)
    open: bool = False

    @property
    def dur(self) -> float:
        return self.end - self.start


def read_events(directory: Path, since: float | None = None, until: float | None = None) -> list[dict]:
    out = []
    for f in sorted(directory.glob("*.jsonl*")):
        opener = gzip.open if f.suffix == ".gz" else open
        with opener(f, "rt") as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(e, dict) and isinstance(e.get("t"), (int, float)):
                    if (since is None or e["t"] >= since) and (until is None or e["t"] <= until):
                        out.append(e)
    out.sort(key=lambda e: e["t"])
    return out


def spans(events: Iterable[dict]) -> tuple[list[Span], list[dict]]:
    """Pairs each `E` with the latest open `B` of the same worker, span name and id."""
    stacks: dict[tuple, list[dict]] = defaultdict(list)
    done: list[Span] = []
    instants: list[dict] = []
    last_t: dict[str, float] = {}
    for e in events:
        w = e.get("w", "?")
        last_t[w] = e["t"]
        key = (w, e.get("span"), e.get("id"))
        if e.get("ph") == "B":
            stacks[key].append(e)
        elif e.get("ph") == "E":
            if stacks[key]:
                b = stacks[key].pop()
                args = {k: v for k, v in b.items() if k not in ("t", "w", "ph", "span")}
                args.update({k: v for k, v in e.items() if k not in ("t", "w", "ph", "span")})
                done.append(Span(w, e["span"], b["t"], e["t"], args))
        elif e.get("ph") == "i":
            instants.append(e)
    for (w, name, _), opened in stacks.items():  # still running, or the process died
        for b in opened:
            args = {k: v for k, v in b.items() if k not in ("t", "w", "ph", "span")}
            done.append(Span(w, name, b["t"], max(last_t.get(w, b["t"]), b["t"]), args, open=True))
    done.sort(key=lambda s: (s.start, -s.dur))
    return done, instants


def label(s: Span) -> str:
    a = s.args
    if s.name == "attempt":
        return f"{a.get('kind', '')} {a.get('task', '')} e{a.get('epoch', '')}"
    if s.name == "tool":
        return f"{a.get('name', 'tool')}: {a.get('detail', '')}"[:120]
    if s.name == "idle":
        return f"idle ({a.get('reason', '')})"
    if s.name == "turn":
        return "turn"
    return s.name


def chrome_trace(all_spans: list[Span], instants: list[dict]) -> dict:
    workers = sorted({s.w for s in all_spans} | {e.get("w", "?") for e in instants})
    tid = {w: i + 1 for i, w in enumerate(workers)}
    ev = [{"ph": "M", "name": "process_name", "pid": 1, "args": {"name": "afe-fleet"}}]
    ev += [{"ph": "M", "name": "thread_name", "pid": 1, "tid": tid[w], "args": {"name": w}} for w in workers]
    for s in all_spans:
        ev.append({"ph": "X", "name": label(s), "cat": s.name, "pid": 1, "tid": tid[s.w],
                   "ts": round(s.start * 1e6), "dur": max(1, round(s.dur * 1e6)),
                   "args": {**s.args, **({"open": True} if s.open else {})}})
    for e in instants:
        ev.append({"ph": "i", "s": "t", "name": e.get("text") or e.get("span"), "cat": e.get("span"), "pid": 1,
                   "tid": tid[e.get("w", "?")], "ts": round(e["t"] * 1e6),
                   "args": {k: v for k, v in e.items() if k not in ("t", "w", "ph")}})
    return {"traceEvents": ev, "displayTimeUnit": "ms"}


def _fmt(sec: float) -> str:
    if sec < 60:
        return f"{sec:.1f}s" if sec >= 0.1 else f"{sec * 1000:.0f}ms"
    sec = round(sec)
    return f"{sec // 3600}h{sec % 3600 // 60:02d}m" if sec >= 3600 else f"{sec // 60}m{sec % 60:02d}s"


def summary(all_spans: list[Span]) -> str:
    lines = []
    by_w: dict[str, list[Span]] = defaultdict(list)
    for s in all_spans:
        by_w[s.w].append(s)
    lines.append("Workers (wall time from first to last event):")
    for w, ss in sorted(by_w.items()):
        wall = max(s.end for s in ss) - min(s.start for s in ss)
        busy = sum(s.dur for s in ss if s.name in ("attempt", "sweep"))
        idle: dict[str, float] = defaultdict(float)
        for s in ss:
            if s.name == "idle":
                idle[s.args.get("reason", "?")] += s.dur
        claim = sum(s.dur for s in ss if s.name == "claim")
        idle_txt = ", ".join(f"{r} {_fmt(d)}" for r, d in sorted(idle.items(), key=lambda x: -x[1])) or "none"
        pct = 100 * busy / wall if wall else 0
        lines.append(f"  {w}: {_fmt(wall)} wall, busy {_fmt(busy)} ({pct:.0f}%), claiming {_fmt(claim)}, "
                     f"idle: {idle_txt}")
    work = [s for s in all_spans if s.name in PHASES]
    if work:
        lines.append("\nPhases (total, count, mean, max):")
        agg: dict[str, list[float]] = defaultdict(list)
        for s in work:
            agg[s.name].append(s.dur)
        for name in sorted(agg, key=lambda n: -sum(agg[n])):
            d = agg[name]
            lines.append(f"  {name:16} {_fmt(sum(d)):>8} {len(d):5}  {_fmt(sum(d) / len(d)):>8} {_fmt(max(d)):>8}")
    tools = [s for s in all_spans if s.name == "tool"]
    if tools:
        lines.append("\nTools (total time by kind; the slowest calls):")
        agg = defaultdict(list)
        for s in tools:
            agg[s.args.get("name", "?")].append(s.dur)
        for name in sorted(agg, key=lambda n: -sum(agg[n])):
            lines.append(f"  {name:20} {_fmt(sum(agg[name])):>8} in {len(agg[name])} calls")
        for s in sorted(tools, key=lambda s: -s.dur)[:8]:
            lines.append(f"  {_fmt(s.dur):>8}  {s.w}  {label(s)}")
    attempts = [s for s in all_spans if s.name == "attempt"]
    if attempts:
        lines.append("\nAttempts:")
        for s in attempts:
            when = time.strftime("%H:%M:%S", time.gmtime(s.start))
            lines.append(f"  {when}  {_fmt(s.dur):>8}  {s.w:28} {label(s):48} "
                         f"{s.args.get('outcome', 'running' if s.open else '?')}")
    return "\n".join(lines)


def parse_time(v: str | None) -> float | None:
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
