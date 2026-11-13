"""Running the agent: one container (or local process) per attempt, driven over JSONL.

Protocol (a subset of Claude Code's `--input-format/--output-format stream-json`):
  * we write one JSON line per user message to stdin:
        {"type": "user", "message": {"role": "user", "content": "<text>"}}
  * the agent writes JSON lines to stdout; `{"type": "system", "subtype": "init", "session_id": ...}`
    gives the session id, and `{"type": "result", ...}` ends a turn.
After each `result` the supervisor runs the project's verify command. If it fails and repairs
remain, the failure log goes back as the next user message; otherwise stdin is closed and the
agent exits. The agent's own claim of success is never trusted.

Liveness: any stdout line counts. No output for `stall` seconds, or the wall-clock limit, or a
cancel (lease lost, shutdown) kills the whole process tree (and the container).
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .config import AgentConfig

log = logging.getLogger(__name__)

WRAP_UP = ("Time is almost up. Commit what you have now, write /out/result.json (or "
           "/out/review.json) describing what is done and what is not, and stop.")


@dataclass
class Workspace:
    root: Path

    @property
    def work(self) -> Path:
        return self.root / "work"

    @property
    def task(self) -> Path:
        return self.root / "task"

    @property
    def out(self) -> Path:
        return self.root / "out"

    @property
    def home(self) -> Path:
        return self.root / "home"

    @property
    def transcript(self) -> Path:
        return self.root / "transcript.jsonl"

    def create(self) -> Workspace:
        for d in (self.task, self.out, self.home):
            d.mkdir(parents=True, exist_ok=True)
        return self


@dataclass
class Verify:
    ok: bool
    log: str


@dataclass
class AgentRun:
    reason: str  # "exit" | "stall" | "timeout" | "cancelled" | "error"
    exit_code: int | None
    session_id: str | None = None
    turns: int = 0
    verify: Verify | None = None
    errors: list[str] = field(default_factory=list)


class Runtime:
    def __init__(self, cfg: AgentConfig, image: str | None, name: str, label: str):
        self.cfg = cfg
        self.image = image
        self.name = name  # container name; unique per worker slot
        self.label = label

    def paths(self, ws: Workspace) -> dict[str, str]:
        """Where the agent sees the task files and writes its result, for the prompt."""
        if self.cfg.runtime == "local":
            return {"task_dir": str(ws.task), "out_dir": str(ws.out)}
        return {"task_dir": "/task", "out_dir": "/out"}

    def agent_argv(self, ws: Workspace) -> tuple[list[str], dict[str, str], Path]:
        if self.cfg.runtime == "local":
            # Runs as you, with your HOME: the agent CLI uses your normal login and settings.
            env = {**os.environ, **self.cfg.env, "AFE_WORK": str(ws.work), "AFE_TASK": str(ws.task),
                   "AFE_OUT": str(ws.out)}
            if self.cfg.isolate_home:
                env["HOME"] = str(ws.home)
            return [os.path.expanduser(a) for a in self.cfg.command], env, ws.work
        if not self.image:
            raise ValueError("project.toml has no image; docker runtime needs one")
        argv = ["docker", "run", "--rm", "-i", "--name", self.name, "--label", f"afe-fleet={self.label}",
                "--user", f"{os.getuid()}:{os.getgid()}", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", "-w", "/work",
                "-v", f"{ws.work}:/work", "-v", f"{ws.task}:/task:ro", "-v", f"{ws.out}:/out",
                "-v", f"{ws.home}:/home/agent", "-e", "HOME=/home/agent"]
        for m in self.cfg.mounts:
            argv += ["-v", os.path.expanduser(m)]
        for k, v in self.cfg.env.items():
            argv += ["-e", f"{k}={v}"]
        argv += [*self.cfg.docker_args, self.image, *self.cfg.command]
        return argv, dict(os.environ), ws.root

    def verify_argv(self, ws: Workspace, cmd: list[str]) -> tuple[list[str], Path]:
        if self.cfg.runtime == "local":
            return cmd, ws.work
        return (["docker", "run", "--rm", "--name", self.name + "-verify", "--label", f"afe-fleet={self.label}",
                 "--user", f"{os.getuid()}:{os.getgid()}", "-w", "/work", "-v", f"{ws.work}:/work",
                 "-e", "HOME=/tmp", self.image, *cmd], ws.root)

    def kill(self) -> None:
        if self.cfg.runtime == "docker":
            subprocess.run(["docker", "kill", self.name], capture_output=True, timeout=60)

    def remove_strays(self) -> None:
        """Containers are children of the docker daemon, not of us: a crashed worker leaves
        them running. Remove any with this slot's name before starting."""
        if self.cfg.runtime == "docker":
            for n in (self.name, self.name + "-verify"):
                subprocess.run(["docker", "rm", "-f", n], capture_output=True, timeout=60)


def run_verify(rt: Runtime, ws: Workspace, cmd: list[str] | None, timeout: float) -> Verify:
    if not cmd:
        return Verify(True, "no verify command")
    argv, cwd = rt.verify_argv(ws, cmd)
    try:
        p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                           start_new_session=True)
    except subprocess.TimeoutExpired as e:
        rt.kill()
        return Verify(False, f"verify timed out after {timeout:.0f}s\n{(e.stdout or '')[-4000:]}")
    out = (p.stdout + p.stderr)[-8000:]
    return Verify(p.returncode == 0, out)


def _kill_tree(proc: subprocess.Popen, rt: Runtime, grace: float = 10) -> None:
    rt.kill()
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(grace)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(30)


def user_message(text: str) -> str:
    return json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n"


def run_agent(rt: Runtime, ws: Workspace, prompt: str, *, verify: Callable[[], Verify],
              cancel: threading.Event, repairs: int | None = None) -> AgentRun:
    cfg = rt.cfg
    repairs = cfg.repairs if repairs is None else repairs
    argv, env, cwd = rt.agent_argv(ws)
    rt.remove_strays()
    try:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, start_new_session=True, text=True, bufsize=1)
    except OSError as e:
        return AgentRun("error", None, errors=[f"cannot start agent: {e}"])

    run = AgentRun("exit", None)
    last_output = [time.monotonic()]
    results: list[dict] = []
    turn_done = threading.Condition()

    def reader() -> None:
        with ws.transcript.open("a") as tr:
            for line in proc.stdout:
                last_output[0] = time.monotonic()
                tr.write(line if line.endswith("\n") else line + "\n")
                tr.flush()
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(msg, dict):
                    continue
                if msg.get("type") == "system" and msg.get("subtype") == "init":
                    run.session_id = msg.get("session_id") or run.session_id
                if msg.get("type") == "result":
                    with turn_done:
                        results.append(msg)
                        turn_done.notify_all()
        with turn_done:
            turn_done.notify_all()

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    def send(text: str) -> bool:
        try:
            proc.stdin.write(user_message(text))
            proc.stdin.flush()
            return True
        except (BrokenPipeError, OSError, ValueError):
            return False

    send(prompt)
    start = time.monotonic()
    wrapped = False
    seen = 0
    try:
        while True:
            with turn_done:
                turn_done.wait(1.0)
                new_results = len(results) > seen
            now = time.monotonic()
            if new_results:
                seen = len(results)
                run.turns = seen
                run.verify = verify()
                if run.verify.ok or repairs <= 0 or wrapped:
                    proc.stdin.close()
                else:
                    repairs -= 1
                    if not send("The verify command failed. Fix it, commit, and finish again.\n\n"
                                f"```\n{run.verify.log[-6000:]}\n```"):
                        proc.stdin.close()
                last_output[0] = time.monotonic()  # verify time is not agent silence
            if proc.poll() is not None and not t.is_alive():
                break
            if cancel.is_set():
                run.reason = "cancelled"
                break
            if now - last_output[0] > cfg.stall:
                run.reason = "stall"
                break
            if now - start > cfg.max_wall + 300:
                run.reason = "timeout"
                break
            if not wrapped and now - start > cfg.max_wall:
                wrapped = True
                if not proc.stdin.closed:
                    send(WRAP_UP)
    finally:
        if proc.poll() is None:
            if run.reason == "exit":  # stdin closed; give it a moment to exit by itself
                try:
                    proc.wait(60)
                except subprocess.TimeoutExpired:
                    pass
            _kill_tree(proc, rt)
        else:
            # always sweep the group: tools leave pollers and daemons behind
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        t.join(10)
    run.exit_code = proc.returncode
    return run
