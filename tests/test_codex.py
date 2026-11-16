"""Codex as the agent CLI: the per-turn supervisor (with a fake codex), the container wiring,
and the config defaults."""

import json
import sys
import tomllib
import threading
from pathlib import Path

import pytest
from test_container_runtime import worktree_workspace

from afe_fleet.agent import EXEC_SERVER_IN_CONTAINER, Runtime, Verify, run_agent
from afe_fleet.agentchoice import AgentChoice
from afe_fleet.config import AgentConfig, HostConfig

FAKE = Path(__file__).parent / "fake_codex.py"


def codex_cfg(**kw) -> AgentConfig:
    return AgentConfig(cli="codex", command=[sys.executable, str(FAKE)], model_args=["-m", "{model}"],
                       effort_args=["-c", "model_reasoning_effort={effort}"], **kw)


def run(tmp_path, mode, monkeypatch, repairs=2, **kw):
    ws, _ = worktree_workspace(tmp_path)
    log = tmp_path / "argv.log"
    monkeypatch.setenv("FAKE_CODEX", mode)
    monkeypatch.setenv("FAKE_AGENT_ARGV_LOG", str(log))
    for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                 "GIT_COMMITTER_EMAIL": "t@t"}.items():
        monkeypatch.setenv(k, v)
    rt = Runtime(codex_cfg(**kw), None, "n", "l")
    r = run_agent(rt, ws, "do the task", cancel=threading.Event(), repairs=repairs,
                  choice=AgentChoice(None, "high", "codex"),
                  verify=lambda: Verify((ws.work / "done.txt").exists(), "done.txt is missing"))
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return r, ws, calls


def test_one_turn(tmp_path, monkeypatch):
    r, ws, calls = run(tmp_path, "ok", monkeypatch)
    assert (r.reason, r.exit_code, r.turns, r.verify.ok) == ("exit", 0, 1, True)
    assert r.session_id and len(calls) == 1
    assert calls[0] == ["-c", "model_reasoning_effort=high", "-"]  # prompt on stdin, no sandbox flag locally
    assert json.loads((ws.out / "result.json").read_text())["outcome"] == "success"
    assert "turn.completed" in ws.transcript.read_text()


def test_repair_resumes_the_session(tmp_path, monkeypatch):
    r, ws, calls = run(tmp_path, "repair", monkeypatch)
    assert (r.reason, r.turns, r.verify.ok) == ("exit", 2, True)
    assert calls[1][-3:] == ["resume", r.session_id, "-"]


def test_no_repairs_left(tmp_path, monkeypatch):
    r, _, calls = run(tmp_path, "repair", monkeypatch, repairs=0)
    assert (r.turns, r.verify.ok, len(calls)) == (1, False, 1)


def test_failed_turn(tmp_path, monkeypatch):
    r, _, _ = run(tmp_path, "fail", monkeypatch)
    assert (r.reason, r.exit_code, r.turns, r.verify) == ("exit", 1, 0, None)
    assert r.errors == ["model unavailable"]


def test_stall_kills_the_turn(tmp_path, monkeypatch):
    r, _, _ = run(tmp_path, "stall", monkeypatch, stall=2)
    assert r.reason == "stall" and r.turns == 0


def test_container_wiring(tmp_path):
    ws, _ = worktree_workspace(tmp_path)
    server = tmp_path / "codex-exec-server"
    server.write_text("")
    rt = Runtime(codex_cfg(runtime="container", codex_exec_server=str(server)), "img:1", "afe-fleet-f-0", "f")
    assert rt.codex_argv(["-m", "x"], None)[-5:] == ["-s", "danger-full-access", "-m", "x", "-"]
    assert rt.codex_argv([], "T")[-3:] == ["resume", "T", "-"]
    env = rt.codex_env(ws)
    home = Path(env["CODEX_HOME"])
    assert home.parent == ws.root and (home / "auth.json").is_symlink()
    toml = tomllib.loads((home / "environments.toml").read_text())
    assert toml["include_local"] is False and toml["default"] == "container"
    (box,) = toml["environments"]
    assert box["id"] == "container" and box["program"].endswith("docker")
    assert box["args"] == ["exec", "-i", "-w", str(ws.work), "afe-fleet-f-0", EXEC_SERVER_IN_CONTAINER,
                           "--listen", "stdio"]
    args = rt._container_args(ws, rt.name)
    assert f"{server}:{EXEC_SERVER_IN_CONTAINER}:ro" in args


def test_codex_defaults_and_choice(tmp_path):
    p = tmp_path / "h.toml"
    p.write_text('[agent]\ncli = "codex"\nruntime = "container"\n')
    a = HostConfig.load("f", p).agent
    assert a.command == ["codex", "exec", "--json"] and a.effort_args[0] == "-c"
    # Claude models do not carry over; effort does, max as xhigh
    c = AgentChoice("claude-opus-5-5", "max").for_cli("codex")
    assert (c.model, c.effort, c.cli) == (None, "xhigh", "codex")
    assert c.describe() == "codex: default model, xhigh effort"
    p.write_text('[agent]\ncli = "gemini"\n')
    with pytest.raises(ValueError, match="cli"):
        HostConfig.load("f", p)
