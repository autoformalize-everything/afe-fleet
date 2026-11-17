"""The container runtime: claude on the host with the container-tools plugin, tools in a
per-attempt container. The docker tests need a reachable daemon and alpine:latest."""

import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from afe_fleet.agent import PLUGIN_DIR, Runtime, Verify, Workspace, run_agent, run_verify
from afe_fleet.config import AgentConfig


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t"})


def worktree_workspace(tmp_path: Path) -> tuple[Workspace, Path]:
    """A workspace whose work directory is a worktree of a separate repository, as the worker makes."""
    repo = tmp_path / "repo.git"
    seed = tmp_path / "seed"
    seed.mkdir()
    git("init", "-q", "-b", "main", cwd=seed)
    (seed / "README").write_text("hi\n")
    git("add", "README", cwd=seed)
    git("commit", "-q", "-m", "init", cwd=seed)
    git("clone", "-q", "--bare", str(seed), str(repo), cwd=tmp_path)
    ws = Workspace(tmp_path / "attempt").create()
    git("worktree", "add", "-q", "--detach", str(ws.work), "main", cwd=repo)
    return ws, repo


def test_plugin_ships_inside_the_package():
    assert (PLUGIN_DIR / ".claude-plugin" / "plugin.json").is_file()
    assert (PLUGIN_DIR / "hooks" / "register.ts").is_file()


def test_container_agent_runs_on_the_host_with_the_plugin(tmp_path):
    ws, repo = worktree_workspace(tmp_path)
    cfg = AgentConfig(runtime="container", command=["claude", "-p"], mounts=["/cache:/cache"], env={"X": "1"})
    rt = Runtime(cfg, "img:1", "afe-fleet-f-0", "f", identity=("bot", "bot@example.com"))
    argv, env, cwd = rt.agent_argv(ws, ["--model", "m"])
    assert argv == ["claude", "-p", "--plugin-dir", str(PLUGIN_DIR), "--strict-mcp-config", "--model", "m"]
    assert cwd == ws.work
    assert env["CONTAINER_TOOLS_CONTAINER"] == "afe-fleet-f-0"
    assert env["CONTAINER_TOOLS_WORKDIR"] == str(ws.work)
    assert env["CONTAINER_TOOLS_IMAGE"] == ""  # the plugin must never create the container itself
    assert env["HOME"] == os.environ["HOME"]  # the host login
    assert rt.paths(ws) == {"task_dir": str(ws.task), "out_dir": str(ws.out)}

    args = rt._container_args(ws, rt.name)
    vols = [args[i + 1] for i, a in enumerate(args) if a == "-v"]
    assert f"{ws.work}:{ws.work}" in vols
    assert f"{repo.resolve()}:{repo.resolve()}" in vols  # the worktree's git directory, for commits
    assert f"{ws.task}:{ws.task}:ro" in vols and f"{ws.out}:{ws.out}" in vols and "/cache:/cache" in vols
    envs = [args[i + 1] for i, a in enumerate(args) if a == "-e"]
    assert "X=1" in envs and "GIT_AUTHOR_NAME=bot" in envs and "GIT_COMMITTER_EMAIL=bot@example.com" in envs
    assert "--network" not in args  # docker's default network unless configured
    with_net = Runtime(AgentConfig(runtime="container", network="none"), "img:1", "n", "l")._container_args(ws, "n")
    assert with_net[with_net.index("--network") + 1] == "none"
    assert "--cpus" not in args  # no CPU cap unless configured
    capped = Runtime(AgentConfig(runtime="container", cpus=8), "img:1", "n", "l")._container_args(ws, "n")
    assert capped[capped.index("--cpus") + 1] == "8"

    vargv, vcwd = rt.verify_argv(ws, ["make", "check"])
    assert vargv[:3] == ["docker", "run", "--rm"] and vargv[-3:] == ["img:1", "make", "check"]
    assert "afe-fleet-f-0-verify" in vargv


def test_container_runtime_needs_an_image(tmp_path):
    ws = Workspace(tmp_path).create()
    with pytest.raises(ValueError, match="image"):
        Runtime(AgentConfig(runtime="container"), None, "n", "l").agent_argv(ws)


def test_prepare_runs_in_the_worktree_before_the_agent(tmp_path):
    ws, _ = worktree_workspace(tmp_path)
    agent = "import pathlib, sys; sys.exit(0 if pathlib.Path('prepared').exists() else 7)"
    cfg = AgentConfig(command=[sys.executable, "-c", agent])
    rt = Runtime(cfg, None, "n", "l", prepare=["sh", "-c", "touch prepared"])
    r = run_agent(rt, ws, "go", verify=lambda: Verify(True, ""), cancel=threading.Event(), repairs=0)
    assert (ws.work / "prepared").exists() and r.reason == "exit" and r.exit_code == 0


def test_failing_prepare_ends_the_attempt(tmp_path):
    ws, _ = worktree_workspace(tmp_path)
    cfg = AgentConfig(command=[sys.executable, "-c", "raise SystemExit('the agent must not start')"])
    rt = Runtime(cfg, None, "n", "l", prepare=["sh", "-c", "echo no cache >&2; exit 3"])
    r = run_agent(rt, ws, "go", verify=lambda: Verify(True, ""), cancel=threading.Event())
    assert r.reason == "error" and "prepare failed (exit 3): no cache" in r.errors[0]


def _docker_ok() -> bool:
    if not shutil.which("docker"):
        return False
    p = subprocess.run(["docker", "image", "inspect", "alpine:latest"], capture_output=True)
    return p.returncode == 0


@pytest.mark.skipif(not _docker_ok(), reason="needs docker and alpine:latest")
def test_container_lifecycle(tmp_path):
    ws, _ = worktree_workspace(tmp_path)
    cfg = AgentConfig(runtime="container")
    rt = Runtime(cfg, "alpine:latest", f"afe-fleet-test-{os.getpid()}", "afe-fleet-test",
                 prepare=["sh", "-c", "test -f /etc/alpine-release && touch prepared"])
    rt.remove_strays()
    try:
        rt.start(ws)
        state = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", rt.name],
                               capture_output=True, text=True, check=True).stdout.split()
        assert state == ["true"]
        # prepare ran inside the container, in the worktree, as us
        assert (ws.work / "prepared").stat().st_uid == os.getuid()
        # the tools' view: the worktree at its own path, writable as us
        subprocess.run(["docker", "exec", rt.name, "sh", "-c", "touch made-inside && cat README"],
                       check=True, capture_output=True)
        made = ws.work / "made-inside"
        assert made.exists() and made.stat().st_uid == os.getuid()
        assert run_verify(rt, ws, ["test", "-f", "made-inside"], 120).ok
        assert not run_verify(rt, ws, ["test", "-f", "missing"], 120).ok
    finally:
        rt.stop()
    gone = subprocess.run(["docker", "inspect", rt.name], capture_output=True)
    assert gone.returncode != 0
