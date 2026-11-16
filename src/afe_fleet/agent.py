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

Runtimes:
  * local: the agent CLI runs on this machine, as you, in the worktree.
  * container: the agent CLI runs on this machine (your login, transcripts here), but with the
    container-tools plugin, so its tools act only inside a container made for the attempt from
    the project's image and removed after it. The worktree, its git directory and the task,
    out and home directories are mounted at their own paths, so every path means the same on
    both sides; `mounts` adds caches and the like.
  * docker: the agent CLI itself in a container (kept, unused).

Agent CLIs: claude (above), or codex (`cli = "codex"`): one `codex exec --json` process per turn,
the prompt on stdin, later turns as `codex exec resume <thread> -`; after each completed turn the
same verify/repair loop. In the container runtime Codex keeps its login and sessions on this
machine but executes everything (commands, file reads and patches) through `codex-exec-server`
running inside the attempt's container: a per-attempt CODEX_HOME holds an environments.toml whose
only environment spawns `docker exec -i <container> codex-exec-server --listen stdio`, so there is
no listener and no host fallback. Codex's own sandbox (bubblewrap) cannot run in a container and
is off there (`-s danger-full-access`): the container is the sandbox.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .agentchoice import AgentChoice, cli_args
from .config import AgentConfig

log = logging.getLogger(__name__)

# The container-tools plugin, shipped inside this package.
PLUGIN_DIR = Path(__file__).resolve().parent / "container-tools"

# Where the codex exec-server binary is mounted in the attempt's container.
EXEC_SERVER_IN_CONTAINER = "/opt/afe-fleet/codex-exec-server"

# The attempt's container only has to stay up: the tools exec into it.
KEEPALIVE = "trap 'exit 0' TERM INT; while :; do sleep 3600 & wait $!; done"

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
    def __init__(self, cfg: AgentConfig, image: str | None, name: str, label: str,
                 identity: tuple[str, str] | None = None):
        self.cfg = cfg
        self.image = image
        self.name = name  # container name; unique per worker slot
        self.label = label
        self.identity = identity  # (name, email) for the agent's commits in a container

    def paths(self, ws: Workspace) -> dict[str, str]:
        """Where the agent sees the task files and writes its result, for the prompt."""
        if self.cfg.runtime in ("local", "container"):
            return {"task_dir": str(ws.task), "out_dir": str(ws.out)}
        return {"task_dir": "/task", "out_dir": "/out"}

    def plugin_dir(self) -> Path:
        return Path(os.path.expanduser(self.cfg.plugin_dir)) if self.cfg.plugin_dir else PLUGIN_DIR

    def agent_argv(self, ws: Workspace, extra: list[str] | None = None) -> tuple[list[str], dict[str, str], Path]:
        """The agent command line; `extra` (model, effort flags) goes after the configured command."""
        extra = extra or []
        if self.cfg.runtime == "local":
            # Runs as you, with your HOME: the agent CLI uses your normal login and settings.
            env = {**os.environ, **self.cfg.env, "AFE_WORK": str(ws.work), "AFE_TASK": str(ws.task),
                   "AFE_OUT": str(ws.out)}
            if self.cfg.isolate_home:
                env["HOME"] = str(ws.home)
            return [os.path.expanduser(a) for a in self.cfg.command] + extra, env, ws.work
        if not self.image:
            raise ValueError(f"project.toml has no image; the {self.cfg.runtime} runtime needs one")
        if self.cfg.runtime == "container":
            # Your login and settings, as for local; the plugin sends the tools to the container
            # start() made (it never creates one) and removes every other tool and MCP server.
            env = {**os.environ, "AFE_WORK": str(ws.work), "AFE_TASK": str(ws.task), "AFE_OUT": str(ws.out),
                   "CONTAINER_TOOLS_CONTAINER": self.name, "CONTAINER_TOOLS_IMAGE": "",
                   "CONTAINER_TOOLS_WORKDIR": str(ws.work), "CONTAINER_TOOLS_PERMISSIONS": "allow"}
            argv = [os.path.expanduser(a) for a in self.cfg.command]
            argv += ["--plugin-dir", str(self.plugin_dir()), "--strict-mcp-config", *extra]
            return argv, env, ws.work
        argv = ["docker", "run", "--rm", "-i", "--name", self.name, "--label", f"afe-fleet={self.label}",
                "--user", f"{os.getuid()}:{os.getgid()}", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", "-w", "/work",
                "-v", f"{ws.work}:/work", "-v", f"{ws.task}:/task:ro", "-v", f"{ws.out}:/out",
                "-v", f"{ws.home}:/home/agent", "-e", "HOME=/home/agent"]
        for m in self.cfg.mounts:
            argv += ["-v", os.path.expanduser(m)]
        for k, v in self.cfg.env.items():
            argv += ["-e", f"{k}={v}"]
        argv += [*self.cfg.docker_args, self.image, *self.cfg.command, *extra]
        return argv, dict(os.environ), ws.root

    def _container_args(self, ws: Workspace, name: str) -> list[str]:
        """`docker run` options for the container runtime: the attempt's directories at their own
        paths (the worktree's git directory too, so commits work), as you, nothing extra allowed."""
        args = ["--name", name, "--label", f"afe-fleet={self.label}", "--init",
                "--user", f"{os.getuid()}:{os.getgid()}", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", "-w", str(ws.work), "-e", f"HOME={ws.home}"]
        if self.cfg.network:
            args += ["--network", self.cfg.network]
        mounts = [f"{ws.work}:{ws.work}", f"{ws.task}:{ws.task}:ro", f"{ws.out}:{ws.out}", f"{ws.home}:{ws.home}"]
        common = _git_common_dir(ws.work)
        if common is not None and not common.is_relative_to(ws.work):
            mounts.append(f"{common}:{common}")
        if self.cfg.cli == "codex":
            mounts.append(f"{self.codex_exec_server()}:{EXEC_SERVER_IN_CONTAINER}:ro")
        mounts += [os.path.expanduser(m) for m in self.cfg.mounts]
        for m in mounts:
            args += ["-v", m]
        env = dict(self.cfg.env)
        if self.identity:
            name_, email = self.identity
            env.update(GIT_AUTHOR_NAME=name_, GIT_AUTHOR_EMAIL=email, GIT_COMMITTER_NAME=name_,
                       GIT_COMMITTER_EMAIL=email)
        for k, v in env.items():
            args += ["-e", f"{k}={v}"]
        return args + list(self.cfg.docker_args)

    # -- codex --------------------------------------------------------------------------------

    def codex_exec_server(self) -> Path:
        """The static codex-exec-server binary to mount: configured, or the one shipped next to codex."""
        if self.cfg.codex_exec_server:
            p = Path(os.path.expanduser(self.cfg.codex_exec_server))
        else:
            exe = shutil.which(os.path.expanduser(self.cfg.command[0]))
            if not exe:
                raise ValueError(f"{self.cfg.command[0]} is not on PATH")
            p = Path(exe).resolve().parent / "codex-exec-server"
        if not p.is_file():
            raise ValueError(f"no codex-exec-server at {p}; set [agent] codex_exec_server")
        return p

    def codex_env(self, ws: Workspace) -> dict[str, str]:
        """The environment for each codex process. In the container runtime this writes the
        attempt's CODEX_HOME: the login shared with yours (symlinks, so refreshes stay serialized
        on the same lock files), a copy of your config, and the container as the only environment.

        A workaround (Codex 0.167): a stdio exec-server can only be named in
        $CODEX_HOME/environments.toml, with no path override or -c key; CODEX_EXEC_SERVER_URL takes
        only ws(s):// and no auth token, so it would need container networking and an open port."""
        env = {**os.environ, "AFE_WORK": str(ws.work), "AFE_TASK": str(ws.task), "AFE_OUT": str(ws.out)}
        if self.cfg.runtime == "local":
            env.update(self.cfg.env)
            if self.cfg.isolate_home:
                env["HOME"] = str(ws.home)
            return env
        if not self.image:
            raise ValueError("project.toml has no image; the container runtime needs one")
        mine = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        home = ws.root / "codex-home"
        home.mkdir(parents=True, exist_ok=True)
        for name in ("auth.json", "auth.lock", "auth.json.lock"):
            if not (home / name).is_symlink():
                (home / name).symlink_to(mine / name)
        for name in ("config.toml", "AGENTS.md"):
            if (mine / name).is_file() and not (home / name).exists():
                shutil.copyfile(mine / name, home / name)
        script = home / "exec-server.sh"
        script.write_text("#!/bin/sh\nexec docker exec -i -w " + shlex.quote(str(ws.work)) + " "
                          + shlex.quote(self.name) + f" {EXEC_SERVER_IN_CONTAINER} --listen stdio\n")
        script.chmod(0o755)
        (home / "environments.toml").write_text(
            'default = "container"\ninclude_local = false\n\n[[environments]]\nid = "container"\n'
            f"program = {json.dumps(str(script))}\n")
        env["CODEX_HOME"] = str(home)
        return env

    def codex_argv(self, extra: list[str], session: str | None) -> list[str]:
        """One turn: a new session reading the prompt from stdin, or `resume <session>`."""
        argv = [os.path.expanduser(a) for a in self.cfg.command]
        if self.cfg.runtime == "container":
            argv += ["-s", "danger-full-access"]  # runs only inside the container, which is the sandbox
        argv += extra
        return argv + (["resume", session, "-"] if session else ["-"])

    def start(self, ws: Workspace) -> None:
        """Container runtime: create and start the attempt's container (pulling the image if
        needed). Raises RuntimeError with docker's message when it cannot."""
        if self.cfg.runtime != "container":
            return
        argv = ["docker", "run", "-d", *self._container_args(ws, self.name), "--entrypoint", "/bin/sh",
                self.image, "-c", KEEPALIVE]
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=900)
        except subprocess.TimeoutExpired:
            raise RuntimeError("docker run timed out") from None
        if p.returncode != 0:
            raise RuntimeError(f"docker run failed: {(p.stderr or p.stdout).strip()[-2000:]}")

    def stop(self) -> None:
        """Container runtime: remove the attempt's container. Never raises."""
        if self.cfg.runtime == "container":
            try:
                subprocess.run(["docker", "rm", "-f", self.name], capture_output=True, timeout=120)
            except (OSError, subprocess.TimeoutExpired) as e:
                log.warning("could not remove container %s: %s", self.name, e)

    def verify_argv(self, ws: Workspace, cmd: list[str]) -> tuple[list[str], Path]:
        if self.cfg.runtime == "local":
            return cmd, ws.work
        if self.cfg.runtime == "container":
            # a fresh container from the same image and mounts: nothing the agent left running
            return ["docker", "run", "--rm", *self._container_args(ws, self.name + "-verify"), self.image,
                    *cmd], ws.work
        return (["docker", "run", "--rm", "--name", self.name + "-verify", "--label", f"afe-fleet={self.label}",
                 "--user", f"{os.getuid()}:{os.getgid()}", "-w", "/work", "-v", f"{ws.work}:/work",
                 "-e", "HOME=/tmp", self.image, *cmd], ws.root)

    def kill(self, verify: bool = False) -> None:
        """Kill the agent's container, or with `verify` the verify container."""
        if self.cfg.runtime in ("docker", "container"):
            subprocess.run(["docker", "kill", self.name + ("-verify" if verify else "")], capture_output=True,
                           timeout=60)

    def remove_strays(self) -> None:
        """Containers are children of the docker daemon, not of us: a crashed worker leaves
        them running. Remove any with this slot's name before starting."""
        if self.cfg.runtime in ("docker", "container"):
            for n in (self.name, self.name + "-verify"):
                subprocess.run(["docker", "rm", "-f", n], capture_output=True, timeout=60)


def _git_common_dir(work: Path) -> Path | None:
    """The repository directory a worktree's `.git` file points into, or None."""
    try:
        p = subprocess.run(["git", "-C", str(work), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return Path(p.stdout.strip()) if p.returncode == 0 and p.stdout.strip() else None


def run_verify(rt: Runtime, ws: Workspace, cmd: list[str] | None, timeout: float) -> Verify:
    if not cmd:
        return Verify(True, "no verify command")
    argv, cwd = rt.verify_argv(ws, cmd)
    try:
        p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                           start_new_session=True)
    except subprocess.TimeoutExpired as e:
        rt.kill(verify=True)
        return Verify(False, f"verify timed out after {timeout:.0f}s\n{(e.stdout or '')[-4000:]}")
    out = (p.stdout + p.stderr)[-8000:]
    return Verify(p.returncode == 0, out)


def _kill_tree(proc: subprocess.Popen, rt: Runtime, grace: float = 10) -> None:
    rt.kill()
    _kill_group(proc, grace)


def _kill_group(proc: subprocess.Popen, grace: float = 10) -> None:
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
              cancel: threading.Event, repairs: int | None = None,
              choice: AgentChoice | None = None) -> AgentRun:
    if rt.cfg.cli == "codex":
        return run_codex(rt, ws, prompt, verify=verify, cancel=cancel, repairs=repairs, choice=choice)
    cfg = rt.cfg
    repairs = cfg.repairs if repairs is None else repairs
    argv, env, cwd = rt.agent_argv(ws, cli_args(choice or AgentChoice(), cfg.model_args, cfg.effort_args))
    rt.remove_strays()
    try:
        rt.start(ws)
    except RuntimeError as e:
        rt.stop()
        return AgentRun("error", None, errors=[f"cannot start the agent's container: {e}"])
    try:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, start_new_session=True, text=True, bufsize=1)
    except OSError as e:
        rt.stop()
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
        rt.stop()
    run.exit_code = proc.returncode
    return run


# -- codex ----------------------------------------------------------------------------------------


@dataclass
class _Turn:
    exit_code: int | None = None
    session_id: str | None = None
    completed: bool = False  # a turn.completed event arrived
    stop: str | None = None  # "cancelled" | "stall" | "timeout" | "error": end the run
    wrap: bool = False  # the wall-clock limit passed mid-turn; the turn was interrupted
    error: str | None = None


def _codex_turn(rt: Runtime, ws: Workspace, argv: list[str], env: dict[str, str], message: str,
                cancel: threading.Event, start: float, wrapped: bool) -> _Turn:
    """Run one `codex exec --json` turn: the message on stdin, events to the transcript."""
    cfg = rt.cfg
    turn = _Turn()
    try:
        proc = subprocess.Popen(argv, cwd=ws.work, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, start_new_session=True, text=True, bufsize=1)
    except OSError as e:
        turn.stop, turn.error = "error", f"cannot start agent: {e}"
        return turn
    last_output = [time.monotonic()]

    def reader() -> None:
        with ws.transcript.open("a") as tr:
            for line in proc.stdout:
                last_output[0] = time.monotonic()
                tr.write(line if line.endswith("\n") else line + "\n")
                tr.flush()
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue  # codex's own notices on stderr
                if not isinstance(msg, dict):
                    continue
                kind = msg.get("type")
                if kind == "thread.started":
                    turn.session_id = msg.get("thread_id") or turn.session_id
                elif kind == "turn.completed":
                    turn.completed = True
                elif kind == "turn.failed":
                    turn.error = json.dumps(msg.get("error"))[:2000]
                elif kind == "error":
                    turn.error = str(msg.get("message"))[:2000]

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    try:
        proc.stdin.write(message)
        proc.stdin.close()
    except (BrokenPipeError, OSError, ValueError):
        pass
    while proc.poll() is None or t.is_alive():
        t.join(1.0)
        now = time.monotonic()
        if cancel.is_set():
            turn.stop = "cancelled"
        elif now - last_output[0] > cfg.stall:
            turn.stop = "stall"
        elif now - start > cfg.max_wall + 300:
            turn.stop = "timeout"
        elif not wrapped and now - start > cfg.max_wall:
            turn.wrap = True
        else:
            continue
        break
    if proc.poll() is None:
        _kill_group(proc)
    t.join(10)
    turn.exit_code = proc.returncode
    return turn


def run_codex(rt: Runtime, ws: Workspace, prompt: str, *, verify: Callable[[], Verify],
              cancel: threading.Event, repairs: int | None = None,
              choice: AgentChoice | None = None) -> AgentRun:
    """The supervisor for Codex: one process per turn, verify after each completed turn, repairs
    and the wrap-up as resumed turns of the same session."""
    cfg = rt.cfg
    repairs = cfg.repairs if repairs is None else repairs
    extra = cli_args(choice or AgentChoice(cli="codex"), cfg.model_args, cfg.effort_args)
    env = rt.codex_env(ws)
    rt.remove_strays()
    try:
        rt.start(ws)
    except RuntimeError as e:
        rt.stop()
        return AgentRun("error", None, errors=[f"cannot start the agent's container: {e}"])
    run = AgentRun("exit", None)
    start = time.monotonic()
    message, wrapped = prompt, False
    try:
        while True:
            turn = _codex_turn(rt, ws, rt.codex_argv(extra, run.session_id), env, message, cancel, start, wrapped)
            run.exit_code = turn.exit_code
            run.session_id = turn.session_id or run.session_id
            if turn.error:
                run.errors.append(turn.error)
            if turn.stop:
                run.reason = turn.stop
                break
            if turn.wrap:
                wrapped = True
                if run.session_id is None:
                    run.reason = "timeout"
                    break
                message = WRAP_UP
                continue
            if not turn.completed:  # exited without finishing the turn
                break
            run.turns += 1
            run.verify = verify()
            if run.verify.ok or repairs <= 0 or wrapped or run.session_id is None:
                break
            if time.monotonic() - start > cfg.max_wall:
                break
            repairs -= 1
            message = ("The verify command failed. Fix it, commit, and finish again.\n\n"
                       f"```\n{run.verify.log[-6000:]}\n```")
    finally:
        rt.stop()
    return run
