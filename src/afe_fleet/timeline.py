"""Timelines from the event log (events.py): a Chrome trace and a summary of where time went.

    afe-fleet timeline [--fleet NAME] [--since TIME] [--until TIME] [-o trace.json]

The trace (Trace Event format, complete events) opens in https://ui.perfetto.dev,
chrome://tracing or speedscope: one row per worker slot (and one for the admin), with spans
nested by time: attempt › agent › turn › tool, beside verify, push and the rest; idle spans
carry their reason. The summary, on standard output, gives each worker's busy and idle time
(by reason), the time per phase, the slowest tools, and every attempt.

A span still open at the end of the log (a running worker, or one that died) is drawn up to
the worker's last event and marked `open`.

With `sudo afe-fleet trace` running (trace.py), the log also has a `proc` span for every program
run in the workers' containers. Each worker gets a group of tracks for them beside its own row,
packed into a few lanes (`lanes`): a child sits under its parent, as in a flame graph, and a
lane is added only for processes that run side by side, so an hour of tens of thousands of
processes takes a dozen or two tracks. The containers' own processes (init, keep-alive loop,
Codex's exec server) are left out (`is_infra`). The summary adds the programs that took the
most time.
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
        elif e.get("ph") == "X" and isinstance(e.get("dur"), (int, float)):  # complete: trace.py's processes
            args = {k: v for k, v in e.items() if k not in ("t", "w", "ph", "span", "dur")}
            done.append(Span(w, e.get("span", "?"), e["t"], e["t"] + e["dur"], args, open=bool(e.get("open"))))
    for (w, name, _), opened in stacks.items():  # still running, or the process died
        for b in opened:
            args = {k: v for k, v in b.items() if k not in ("t", "w", "ph", "span")}
            done.append(Span(w, name, b["t"], max(last_t.get(w, b["t"]), b["t"]), args, open=True))
    done.sort(key=lambda s: (s.start, -s.dur))
    owners = {s.args["container"]: s.w for s in done if s.name == "container-start" and s.args.get("container")}
    infra: set[tuple[str, int]] = set()
    for s in done:  # a container's processes belong to the worker that started it
        if s.name == "proc":
            c = s.args.get("container", s.w)
            s.w = owners.get(c) or owners.get(c.removesuffix("-verify")) or "other containers"
            if is_infra(s, (c, s.args.get("ppid")) in infra):
                s.args["infra"] = True
                if not str(s.args.get("cmd", "")).startswith("fork of "):  # a fork may exec real work
                    infra.add((c, s.args.get("pid")))
    return done, instants


INFRA_PROGRAMS = ("docker-init", "codex-exec-server")


def is_infra(s: Span, parent_infra: bool) -> bool:
    """The container's own processes, there for its whole life: the init, the keep-alive loop
    (agent.KEEPALIVE) and its sleeps, Codex's exec server. Not what the agent ran."""
    cmd = str(s.args.get("cmd", ""))
    return (Path(cmd).name in INFRA_PROGRAMS or "sleep 3600 & wait" in str(s.args.get("args", ""))
            or (parent_infra and (cmd.startswith("fork of ") or Path(cmd).name == "sleep")))


def lanes(procs: list[Span]) -> list[int]:
    """Packs one worker's processes into few tracks: each lane holds properly nested spans (a
    stack), a child goes into its parent's lane when it fits under it, and a new lane opens only
    for processes that really run side by side. Returns each span's lane, in input order."""
    order = sorted(range(len(procs)), key=lambda i: (procs[i].start, -procs[i].dur))
    stacks: list[list[float]] = []  # per lane, the ends of the open spans, innermost last
    lane_of_pid: dict[object, int] = {}
    out = [0] * len(procs)

    def fits(k: int, s: Span) -> bool:
        st = stacks[k]
        while st and st[-1] <= s.start:
            st.pop()
        return not st or s.end <= st[-1]

    for i in order:
        s = procs[i]
        parent = lane_of_pid.get(s.args.get("ppid"))
        k = parent if parent is not None and fits(parent, s) else next(
            (k for k in range(len(stacks)) if fits(k, s)), None)
        if k is None:
            stacks.append([])
            k = len(stacks) - 1
        stacks[k].append(s.end)
        lane_of_pid[s.args.get("pid")] = k
        out[i] = k
    return out


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
    if s.name == "proc":
        name = Path(str(a.get("cmd") or "?").removeprefix("fork of ")).name
        args = str(a.get("args") or "")
        return (args if args.split(" ", 1)[0].endswith(name) else f"{name}: {args}")[:120]
    return s.name


def chrome_trace(all_spans: list[Span], instants: list[dict]) -> dict:
    workers = sorted({s.w for s in all_spans if s.name != "proc"}
                     | {e.get("w", "?") for e in instants if e.get("span") != "usage"})
    tid = {w: i + 1 for i, w in enumerate(workers)}
    ev = [{"ph": "M", "name": "process_name", "pid": 1, "args": {"name": "afe-fleet"}}]
    ev += [{"ph": "M", "name": "thread_name", "pid": 1, "tid": tid[w], "args": {"name": w}} for w in workers]
    # a trace "process" per worker for what ran in its containers, packed into a few lanes
    by_worker: dict[str, list[Span]] = defaultdict(list)
    for s in all_spans:
        if s.name == "proc" and not s.args.get("infra"):
            by_worker[s.w].append(s)
    for g, (w, ps) in enumerate(sorted(by_worker.items()), start=2):
        lane = lanes(ps)
        n = max(lane) + 1
        ev.append({"ph": "M", "name": "process_name", "pid": g, "args": {"name": f"{w}: processes"}})
        ev += [{"ph": "M", "name": "thread_name", "pid": g, "tid": k + 1, "args": {"name": f"lane {k + 1}"}}
               for k in range(n)]
        for s, k in zip(ps, lane):
            ts = round(s.start * 1e6)  # start and end rounded alike, so nesting survives rounding
            ev.append({"ph": "X", "name": label(s), "cat": "proc", "pid": g, "tid": k + 1,
                       "ts": ts, "dur": round(s.end * 1e6) - ts,
                       "args": {**s.args, **({"open": True} if s.open else {})}})
    for s in all_spans:
        if s.name == "proc":
            continue
        ev.append({"ph": "X", "name": label(s), "cat": s.name, "pid": 1, "tid": tid[s.w],
                   "ts": round(s.start * 1e6), "dur": max(1, round(s.dur * 1e6)),
                   "args": {**s.args, **({"open": True} if s.open else {})}})
    for e in instants:
        if e.get("span") == "usage":
            # a counter track per agent CLI, one line per window: % of the limit used
            vals = {w["name"]: round(100 * w["used"], 1) if w.get("used") is not None else 100.0
                    for w in e.get("windows") or [] if isinstance(w, dict) and w.get("name")}
            if vals:
                ev.append({"ph": "C", "name": f"{e.get('cli', 'agent')} usage %", "pid": 1,
                           "ts": round(e["t"] * 1e6), "args": vals})
            continue
        ev.append({"ph": "i", "s": "t", "name": e.get("text") or e.get("span"), "cat": e.get("span"), "pid": 1,
                   "tid": tid[e.get("w", "?")], "ts": round(e["t"] * 1e6),
                   "args": {k: v for k, v in e.items() if k not in ("t", "w", "ph")}})
    return {"traceEvents": ev, "displayTimeUnit": "ms"}


def _fmt(sec: float) -> str:
    if sec < 60:
        return f"{sec:.1f}s" if sec >= 0.1 else f"{sec * 1000:.0f}ms"
    sec = round(sec)
    return f"{sec // 3600}h{sec % 3600 // 60:02d}m" if sec >= 3600 else f"{sec // 60}m{sec % 60:02d}s"


def summary(all_spans: list[Span], instants: list[dict] | None = None) -> str:
    lines = []
    by_w: dict[str, list[Span]] = defaultdict(list)
    for s in all_spans:
        if s.name != "proc":
            by_w[s.w].append(s)
    if by_w:
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
    procs = [s for s in all_spans if s.name == "proc" and not s.args.get("infra")
             and not str(s.args.get("cmd", "")).startswith("fork of ")]
    if procs:
        lines.append("\nPrograms in the containers (wall total, CPU total, runs, slowest run):")
        agg_p: dict[str, list[Span]] = defaultdict(list)
        for s in procs:
            agg_p[Path(str(s.args.get("cmd", "?"))).name].append(s)
        for name in sorted(agg_p, key=lambda n: -sum(s.dur for s in agg_p[n]))[:12]:
            ss = agg_p[name]
            cpu = sum(s.args.get("cpu") or 0 for s in ss)
            lines.append(f"  {name:20} {_fmt(sum(s.dur for s in ss)):>8} {_fmt(cpu):>8} {len(ss):6}  "
                         f"{_fmt(max(s.dur for s in ss)):>8}")
        failed = [s for s in procs if s.args.get("exit") not in (None, 0) or s.args.get("signal")]
        lines.append(f"  {len(procs)} program runs, {len(failed)} failed or killed; the slowest:")
        for s in sorted(procs, key=lambda s: -s.dur)[:6]:
            lines.append(f"  {_fmt(s.dur):>8}  {s.w}  {label(s)[:90]}")
    turns = [s for s in all_spans if s.name == "turn"]
    if any(s.args.get("tokens") or s.args.get("cost") for s in turns):
        lines.append("\nTokens and cost (agent turns):")
        tot: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        for s in turns:
            for k, v in (s.args.get("tokens") or {}).items():
                tot[s.w][k] += v or 0
            tot[s.w]["cost"] += s.args.get("cost") or 0
        for w, t in sorted(tot.items()):
            toks = ", ".join(f"{k} {int(v):,}" for k, v in t.items() if k != "cost" and v)
            cost = f", ${t['cost']:.2f}" if t["cost"] else ""
            lines.append(f"  {w}: {toks}{cost}")
    usage = [e for e in instants or [] if e.get("span") == "usage" and e.get("windows")]
    if usage:
        latest: dict[tuple[str, str], tuple[float, float | None]] = {}
        for e in usage:
            for w in e["windows"]:
                latest[(e.get("cli", "?"), w["name"])] = (e["t"], w.get("used"))
        lines.append("\nUsage (latest reading):")
        for (cli, name), (t, used) in sorted(latest.items()):
            pct = "spent" if used is None else f"{100 * used:.0f}%"
            lines.append(f"  {cli} {name} {pct} at {time.strftime('%H:%M:%S', time.gmtime(t))}")
        warnings = [e for e in instants or [] if e.get("span") == "usage-warning"]
        if warnings:
            lines.append(f"  {len(warnings)} low-usage warning(s)")
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
