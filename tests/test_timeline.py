"""The event log and the timeline built from it."""

import json

from conftest import FakeForge
from test_e2e import make_admin, make_worker

from afe_fleet.events import EventLog
from afe_fleet.timeline import chrome_trace, read_events, spans, summary


def names(ss, w=None):
    return [s.name for s in ss if w is None or s.w == w]


def test_spans_pair_and_nest(tmp_path):
    log = EventLog(tmp_path, "alice/box")
    ev = log.bind("alice/box-0")
    with ev.span("attempt", task="a", epoch=1, kind="work") as end:
        ev.begin("tool", id="t1", name="Bash", detail="ls")
        ev.begin("tool", id="t2", name="Read", detail="x")  # overlapping calls pair by id
        ev.end("tool", id="t1")
        ev.end("tool", id="t2", error=True)
        end["outcome"] = "success"
    ev.begin("idle", reason="no-task")  # still open: drawn to the last event, marked open
    (f,) = tmp_path.glob("*.jsonl")
    assert f.name.startswith("alice_box-")
    ss, _ = spans(read_events(tmp_path))
    by = {(s.name, s.args.get("id")): s for s in ss}
    assert by[("attempt", None)].args == {"task": "a", "epoch": 1, "kind": "work", "outcome": "success"}
    assert by[("tool", "t2")].args["error"] is True and by[("tool", "t1")].end <= by[("tool", "t2")].end
    assert by[("idle", None)].open
    trace = chrome_trace(ss, [])
    assert {e["ph"] for e in trace["traceEvents"]} == {"M", "X"}
    assert any(e["name"] == "Bash: ls" for e in trace["traceEvents"])


def test_a_worker_and_admin_round_leave_a_timeline(world, tmp_path):
    forge = FakeForge()
    log = EventLog(tmp_path / "events", "alice/host")
    alice = make_worker(world, "alice", "work", forge)
    alice.events_log = log
    assert alice.run_once()
    assert not alice.run_once()  # nothing left that alice can take: no-task
    bob = make_worker(world, "bob", "approve", forge)
    bob.events_log = EventLog(tmp_path / "events", "bob/host")
    assert bob.run_once()
    ss, _ = spans(read_events(tmp_path / "events"))
    alice_spans = names(ss, "alice/host")
    for name in ("claim", "attempt", "fetch", "worktree", "agent", "turn", "verify", "push", "pr", "report", "cleanup"):
        assert name in alice_spans, name
    att = next(s for s in ss if s.name == "attempt" and s.w == "alice/host")
    assert att.args["outcome"] == "success" and att.args["task"] == "demo/a"
    assert next(s for s in ss if s.name == "verify" and s.w == "alice/host").args["ok"] is True
    assert {"review-post", "report"} <= set(names(ss, "bob/host"))
    text = summary(ss)
    assert "alice/host" in text and "Phases" in text and "work demo/a e1" in text

    admin = make_admin(world, forge)
    admin.ev = EventLog(tmp_path / "events", "admin").bind("admin")
    admin.stop.wait = lambda _t: admin.stop.set()  # one sweep, then stop
    admin.run_forever()
    ss, instants = spans(read_events(tmp_path / "events"))
    sweep = next(s for s in ss if s.name == "sweep")
    assert sweep.w == "admin" and sweep.args["actions"] >= 1
    assert any("merged and accepted" in e["text"] for e in instants)


def test_codex_tool_calls_become_spans(tmp_path, monkeypatch):
    from afe_fleet import agent
    seen = []
    real = agent.Events.emit

    def spy(self, ph, span, **fields):
        seen.append((ph, span, fields.get("name")))
        return real(self, ph, span, **fields)

    monkeypatch.setattr(agent.Events, "emit", spy)
    msgs = [{"type": "item.started", "item": {"id": "i1", "type": "command_execution", "command": "lake build"}},
            {"type": "item.completed", "item": {"id": "i1", "type": "command_execution", "status": "completed",
                                                "exit_code": 0}},
            {"type": "item.completed", "item": {"id": "i2", "type": "agent_message", "text": "done"}}]
    ev = EventLog(tmp_path, "w").bind("w")
    for m in msgs:
        agent.codex_tool_events(ev, m)
    assert seen == [("B", "tool", "command_execution"), ("E", "tool", None)]
    agent.claude_tool_events(ev, {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "u1", "name": "Bash", "input": {"command": "ls -la"}}]}})
    agent.claude_tool_events(ev, {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "u1", "is_error": False}]}})
    ss, _ = spans(read_events(tmp_path))
    assert sorted(s.args.get("name") for s in ss) == ["Bash", "command_execution"]
    assert json.loads(next(tmp_path.glob("*.jsonl")).read_text().splitlines()[0])["span"] == "tool"
