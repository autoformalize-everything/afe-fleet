"""Usage as the agents report it, recorded and warned about."""

import json
import logging
import math

import pytest
from test_capacity import NOW, claude_report

from afe_fleet.capacity import (CodexSessionTail, SpareCapacity, Usage, UsageUnknown, Window, parse_claude_stream,
                                parse_codex_session)
from afe_fleet.events import EventLog
from afe_fleet.timeline import read_events

H, D = 3600, 86400

CLAUDE_EVENT = {"status": "allowed", "unifiedWindows": {
    "five_hour": {"utilization": 0.61, "resetsAt": NOW + 2 * H, "status": "allowed"},
    "seven_day": {"utilization": 0.22, "resetsAt": NOW + 3 * D, "status": "allowed"}}}

CODEX_RATE_LIMITS = {"limit_id": "codex", "primary": {"used_percent": 59.0, "window_minutes": 300,
                                                      "resets_at": NOW + 4 * H},
                     "secondary": {"used_percent": 9.0, "window_minutes": 10080, "resets_at": NOW + 6 * D},
                     "rate_limit_reached_type": None}


def test_stream_formats():
    ws = {w.name: w for w in parse_claude_stream(CLAUDE_EVENT, NOW)}  # named as /usage names them
    assert ws["session"].used == 0.61 and ws["session"].elapsed == pytest.approx(0.6)
    assert ws["weekly_all"].resets == NOW + 3 * D
    assert [w.name for w in parse_claude_stream({"unifiedWindows": {"seven_day_opus": {
        "utilization": 0.1, "resetsAt": NOW + D, "status": "allowed"}}}, NOW)] == ["weekly_scoped (opus)"]
    spent = parse_claude_stream({"unifiedWindows": {"five_hour": {"utilization": 1.0, "resetsAt": NOW + H,
                                                                  "status": "rejected"}}}, NOW)
    assert math.isinf(spent[0].used)
    ws = {w.name: w for w in parse_codex_session(CODEX_RATE_LIMITS, NOW)}
    assert ws["5h"].used == 0.59 and ws["7d"].used == 0.09 and ws["5h"].elapsed == pytest.approx(0.2)


def test_following_a_codex_session_file(tmp_path):
    day = tmp_path / "sessions" / "2026" / "11" / "17"
    day.mkdir(parents=True)
    f = day / "rollout.jsonl"
    tail = CodexSessionTail(str(tmp_path))
    assert tail.poll() is None
    with f.open("w") as fh:
        fh.write(json.dumps({"payload": {"type": "token_count", "rate_limits": {"primary": {"used_percent": 1}}}}) + "\n")
        fh.write(json.dumps({"payload": {"type": "token_count", "rate_limits": CODEX_RATE_LIMITS}}) + "\n")
        fh.write('{"payload": {"type": "token_co')  # a line still being written
    assert tail.poll() == CODEX_RATE_LIMITS
    assert tail.poll() is None  # nothing new
    with f.open("a") as fh:
        fh.write('unt", "rate_limits": {"primary": {"used_percent": 70}}}}\n')
    assert tail.poll() == {"primary": {"used_percent": 70}}
    assert CodexSessionTail(None).poll() is None


def test_observations_queries_and_warnings(tmp_path, caplog):
    clock = [NOW]
    queries = []

    def reader():
        queries.append(clock[0])
        return claude_report(session=(95, NOW + 2 * H))

    ev = EventLog(tmp_path, "w").bind("w")
    usage = Usage("claude", reader=reader, clock=lambda: clock[0], events=ev, warn_below=0.1)
    usage.observe(parse_claude_stream(CLAUDE_EVENT, NOW), "stream")
    assert usage.reading(300)[0][0].used == 0.61 and queries == []  # fresh: no query
    clock[0] += 300
    with caplog.at_level(logging.WARNING):
        windows, err = usage.reading(300)  # stale: queried, and the session is at 95%
        usage.reading(0)  # the same window again: one warning only
    assert len(queries) == 2 and err is None
    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == ["claude usage: the session window has 5% left (95% used), resets in 1h55m"]
    events = read_events(tmp_path)
    sources = [e["source"] for e in events if e["span"] == "usage"]
    assert sources == ["stream", "query", "query"]
    assert [e for e in events if e["span"] == "usage-warning"][0]["window"] == "session"

    # a spare-capacity check shares the reading
    sc = SpareCapacity("claude", 1.0, usage=usage)
    assert sc.check()[0] is False  # 95% used at 60% of the window

    def failing():
        raise UsageUnknown("not logged in")
    broken = Usage("codex", reader=failing, events=ev)
    assert broken.reading(0) == (None, "not logged in")
    assert read_events(tmp_path)[-1]["error"] == "not logged in"


def test_a_spent_window_warns(caplog):
    usage = Usage("codex", reader=dict, warn_below=0.1)
    with caplog.at_level(logging.WARNING):
        usage.observe([Window("5h", math.inf, 0.5, NOW + H)], "stream")
    assert "the 5h window is spent" in caplog.text


def test_usage_counters_in_the_trace(tmp_path):
    from afe_fleet.timeline import chrome_trace, spans, summary
    ev = EventLog(tmp_path, "w").bind("w")
    usage = Usage("claude", reader=dict, events=ev)
    usage.observe(parse_claude_stream(CLAUDE_EVENT, NOW), "stream")
    with ev.span("turn") as end:
        end.update(cost=0.25, tokens={"input": 10, "output": 200, "cache_read": 1000})
    ss, instants = spans(read_events(tmp_path))
    counters = [e for e in chrome_trace(ss, instants)["traceEvents"] if e["ph"] == "C"]
    assert counters and counters[0]["name"] == "claude usage %" and counters[0]["args"] == {"session": 61.0,
                                                                                             "weekly_all": 22.0}
    text = summary(ss, instants)
    assert "$0.25" in text and "claude session 61%" in text
