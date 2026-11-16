"""Spare capacity: parsing claude-usage / codex-usage output, the max(used / elapsed) < limit rule,
failing closed, caching, and the worker not claiming without spare capacity."""

import math
from datetime import UTC, datetime

import pytest

from afe_fleet.capacity import (SpareCapacity, UsageUnknown, Window, parse_claude, parse_codex, read_claude,
                                read_codex, verdict)
from afe_fleet.worker import Worker

NOW = datetime(2026, 11, 16, 21, 0, tzinfo=UTC).timestamp()
H, D = 3600, 86400


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).isoformat()


def claude_report(session=(13, NOW + 2 * H), weekly=(16, NOW + 1.25 * D), scoped=(5, NOW + 1.25 * D)):
    """Shaped like `claude-usage` output (Claude Code's /usage report)."""
    def lim(kind, group, pct_reset, scope=None):
        pct, reset = pct_reset
        return {"kind": kind, "group": group, "percent": pct, "resets_at": reset and iso(reset), "scope": scope,
                "severity": "normal", "is_active": False}
    return {"session": {"total_cost_usd": 0}, "rate_limits": {"limits": [
        lim("session", "session", session),
        lim("weekly_all", "weekly", weekly),
        lim("weekly_scoped", "weekly", scoped, {"model": {"display_name": "Fable"}, "surface": None}),
    ], "extra_usage": {"is_enabled": False}}}


def codex_report(primary=(2, 300, NOW + 4 * H), secondary=(0, 10080, NOW + 6 * D), allowed=True, reached=None):
    """Shaped like `codex-usage` output (the app-server's account/rateLimits/read)."""
    def win(v):
        return None if v is None else {"usedPercent": v[0], "windowDurationMins": v[1], "resetsAt": v[2]}
    return {"ordinaryUsageAllowed": allowed, "rateLimits": {
        "limitId": "codex", "primary": win(primary), "secondary": win(secondary),
        "planType": "plus", "rateLimitReachedType": reached}}


def test_claude_windows():
    ws = {w.name: w for w in parse_claude(claude_report(), NOW)}
    assert set(ws) == {"session", "weekly_all", "weekly_scoped (Fable)"}
    assert ws["session"].used == pytest.approx(0.13) and ws["session"].elapsed == pytest.approx(0.6)  # 3h of 5h
    assert ws["weekly_all"].elapsed == pytest.approx(1 - 1.25 / 7)


def test_codex_windows():
    ws = {w.name: w for w in parse_codex(codex_report(), NOW)}
    assert set(ws) == {"5h", "7d"}
    assert ws["5h"].used == pytest.approx(0.02) and ws["5h"].elapsed == pytest.approx(0.2)
    assert ws["7d"].ratio == 0  # nothing used
    assert [w.name for w in parse_codex(codex_report(secondary=None), NOW)] == ["5h"]  # a plan with one window


def test_the_rule_is_max_used_over_elapsed_below_the_limit():
    # session 13% at 60% (0.22), weekly 16% at ~82% (0.19), scoped 5% (0.06): spare at 1 and at 0.5
    assert verdict(parse_claude(claude_report(), NOW), 1.0)[0]
    ok, why = verdict(parse_claude(claude_report(), NOW), 0.2)
    assert not ok and why.startswith("session 13% used at 60% of its window: ratio 0.22 ≥ 0.2")
    # spending faster than the clock in any one window blocks
    assert not verdict(parse_claude(claude_report(session=(70, NOW + 2 * H)), NOW), 1.0)[0]
    assert not verdict(parse_codex(codex_report(secondary=(50, 10080, NOW + 6 * D)), NOW), 1.0)[0]


def test_edges():
    assert Window("w", 0.0, 0.0).ratio == 0  # nothing used: never blocks
    assert math.isinf(Window("w", 0.01, 0.0).ratio)  # used in a window that has not started
    # an idle Claude window (no reset clock, 0%) counts as unused
    assert verdict(parse_claude(claude_report(session=(0, None)), NOW), 1.0)[0]
    # Codex says no
    assert not verdict(parse_codex(codex_report(allowed=False), NOW), 10.0)[0]
    assert not verdict(parse_codex(codex_report(reached="primary"), NOW), 10.0)[0]


@pytest.mark.parametrize("report", [
    {},
    {"rate_limits": {"limits": []}},
    claude_report(session=(13, None)),  # used, but no reset clock
    {"rate_limits": {"limits": [{"group": "monthly", "percent": 1, "resets_at": iso(NOW + H)}]}},
    {"rate_limits": {"limits": [{"group": "session", "percent": "13", "resets_at": iso(NOW + H)}]}},
    {"rate_limits": {"limits": [{"group": "session", "percent": 13, "resets_at": "soon"}]}},
])
def test_unreadable_claude_usage_is_unknown(report):
    with pytest.raises(UsageUnknown):
        parse_claude(report, NOW)


@pytest.mark.parametrize("report", [{}, codex_report(primary=None, secondary=None),
                                    codex_report(primary=(-1, 300, NOW + H)), codex_report(primary=(5, 300, None))])
def test_unreadable_codex_usage_is_unknown(report):
    with pytest.raises(UsageUnknown):
        parse_codex(report, NOW)


def test_failing_reader_means_no_spare_capacity_and_readings_are_cached():
    clock = [NOW]
    calls = []

    def reader():
        calls.append(clock[0])
        if len(calls) == 1:
            raise UsageUnknown("claude /usage: not logged in")
        return claude_report()

    sc = SpareCapacity("claude", 1.0, clock=lambda: clock[0], reader=reader, ttl=300)
    assert sc.check() == (False, "usage unknown (claude /usage: not logged in)")
    clock[0] += 299
    assert sc.check()[0] is False and len(calls) == 1  # cached, failures included
    clock[0] += 1
    assert sc.check()[0] is True and len(calls) == 2


FAKE_CLAUDE = """#!/usr/bin/env python3
import json, sys
msg = json.loads(sys.stdin.readline())
assert msg["message"]["content"] == "/usage" and "--input-format" in sys.argv, sys.argv
print(json.dumps({"type": "system", "subtype": "init"}))
print(json.dumps({"usage_report": REPORT}))
"""

FAKE_CODEX = """#!/usr/bin/env python3
import json, sys
assert sys.argv[1:] == ["app-server"], sys.argv
for line in sys.stdin:
    req = json.loads(line)
    if req.get("id") == 1:
        print(json.dumps({"id": 1, "result": {}}), flush=True)
    elif req.get("method") == "account/rateLimits/read":
        print(json.dumps({"method": "noise", "params": {}}), flush=True)
        print(json.dumps({"id": req["id"], "result": REPORT}), flush=True)
"""


def fake(tmp_path, name, body, report):
    exe = tmp_path / name
    exe.write_text(body.replace("REPORT", repr(report).replace("None", "None")))
    exe.chmod(0o755)
    return str(exe)


def test_reads_usage_through_the_cli_itself(tmp_path):
    report = claude_report()
    claude = fake(tmp_path, "claude", FAKE_CLAUDE, report)
    assert read_claude(claude) == report
    codex = fake(tmp_path, "codex", FAKE_CODEX, codex_report())
    assert read_codex(codex) == codex_report()
    assert SpareCapacity("codex", 1.0, exe=codex).check()[0] is True
    # a command that prints the JSON replaces the built-in reader
    cmd = fake(tmp_path, "usage", "#!/bin/sh\necho '{\"rateLimits\": {}}'\n", {})
    assert SpareCapacity("codex", 1.0, [cmd]).check() == (False, "usage unknown (the Codex usage report has no windows)")


def test_cli_failures_are_unknown(tmp_path):
    silent = tmp_path / "silent"
    silent.write_text("#!/bin/sh\ncat >/dev/null\n")
    dead = tmp_path / "dead"
    dead.write_text("#!/bin/sh\nexit 0\n")
    for exe in (silent, dead):
        exe.chmod(0o755)
    with pytest.raises(UsageUnknown, match="no usage report"):
        read_claude(str(silent), timeout=5)
    with pytest.raises(UsageUnknown, match="exited before answering|Broken pipe"):
        read_codex(str(dead), timeout=5)
    with pytest.raises(UsageUnknown, match="no rate limits within 1s"):
        read_codex(str(silent), timeout=1)
    with pytest.raises(UsageUnknown):
        read_codex(str(tmp_path / "missing"))


def test_worker_does_not_claim_without_spare_capacity(world, tmp_path):
    class NoClaims:
        def claim(self, *a):
            raise AssertionError("claimed without spare capacity")

    class Blocked:
        def check(self):
            return False, "weekly_all 90% used at 50% of its window: ratio 1.80 ≥ 1"

    w = Worker(world.loaded(), world.host("alice"), NoClaims(), None, state_dir=tmp_path / "state")
    w.capacity = Blocked()
    assert w.run_once() is False
