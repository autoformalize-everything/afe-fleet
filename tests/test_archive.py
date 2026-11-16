"""Attempt logs outlive the attempt."""

import gzip
import json

from conftest import FakeForge
from test_e2e import make_worker

from afe_fleet.agent import Workspace
from afe_fleet.worker import archive_attempt


def test_archive_attempt(tmp_path):
    ws = Workspace(tmp_path / "a").create()
    ws.transcript.write_text('{"type": "result"}\n')
    (ws.task / "TASK.md").write_text("do it")
    (ws.out / "result.json").write_text('{"outcome": "success"}')
    sess = ws.root / "codex-home" / "sessions" / "2026" / "11" / "17"
    sess.mkdir(parents=True)
    (sess / "rollout-1.jsonl").write_text('{"x": 1}\n')
    dest = archive_attempt(ws, tmp_path / "logs" / "demo_a", "e1-work-alice_host")
    assert dest.name.endswith("-e1-work-alice_host")
    assert gzip.open(dest / "transcript.jsonl.gz").read() == b'{"type": "result"}\n'
    assert (dest / "task/TASK.md").read_text() == "do it"
    assert json.loads((dest / "out/result.json").read_text()) == {"outcome": "success"}
    assert gzip.open(dest / "codex-sessions/2026/11/17/rollout-1.jsonl.gz").read() == b'{"x": 1}\n'
    assert archive_attempt(Workspace(tmp_path / "empty"), tmp_path / "logs" / "x", "e1") is None


def test_a_worker_keeps_each_attempts_logs(world):
    w = make_worker(world, "alice", "work", FakeForge())
    assert w.run_once()
    (attempt,) = (w.state / "logs" / "demo_a").iterdir()
    assert attempt.name.endswith("-e1-work-alice_host")
    lines = gzip.open(attempt / "transcript.jsonl.gz").read().decode().splitlines()
    assert any(json.loads(line).get("type") == "result" for line in lines if line.startswith("{"))
    assert (attempt / "task/TASK.md").exists() and (attempt / "out/result.json").exists()
    assert not list((w.state / "attempts").iterdir())  # the attempt directory itself is gone
