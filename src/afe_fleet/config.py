"""Configuration.

* `fleet.toml` (shared, in the config repo or a local file): what coordination needs.
* `~/.config/afe-fleet/<fleet>.toml` (per host): worker identity, slots, filters, agent runtime.

A *source* is what the user passes on the command line: `org/repo`, a git URL or path whose
default branch holds `fleet.toml`, or a path to a `fleet.toml` file.
"""

from __future__ import annotations

import fcntl
import getpass
import hashlib
import os
import re
import socket
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import agentchoice
from .configrepo import ConfigRepo
from .gitutil import repo_url


def parse_duration(v: str | int | float) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*", str(v))
    if not m:
        raise ValueError(f"bad duration {v!r}")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "afe-fleet"


def data_home() -> Path:
    """Everything afe-fleet stores on a machine, except the per-host config file:

        ~/.local/share/afe-fleet/
          repos/<repo>-<hash>.git, .lock     shared code clones (hold attempts' unpushed commits)
          mirrors/<hash>.git                 config-repo mirrors (fleet.toml)
          fleets/<fleet>/workers/<worker-id>/          one worker process's own state, held by
            lock                                       an exclusive lock while the process runs
            slot-<n>/{journal.json,lock.git,config.git}   crash journal, lock- and config-repo mirrors
          fleets/<fleet>/admin/{lock,lock.git}         the admin's (one per fleet on a host)
          fleets/<fleet>/attempts/<task>-e<epoch>/{work,task,out}   worktree per attempt
          fleets/<fleet>/server.sqlite       server mode

    Any number of worker processes, with different worker ids, share one home: per-process state
    is under its worker id, the shared clones are file-locked, and attempts are unique per lease.

    `$AFE_FLEET_HOME` overrides the whole directory; otherwise `$XDG_DATA_HOME/afe-fleet`.
    """
    if os.environ.get("AFE_FLEET_HOME"):
        return Path(os.environ["AFE_FLEET_HOME"])
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "afe-fleet"


def fleet_home(fleet: str) -> Path:
    return data_home() / "fleets" / fleet


def id_slug(worker_id: str) -> str:
    """A worker id as a path component and container-name part (`alice/box1` → `alice_box1`)."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", worker_id)


def worker_home(fleet: str, worker_id: str) -> Path:
    """The state one worker process owns; see `data_home`."""
    return fleet_home(fleet) / "workers" / id_slug(worker_id)


class AlreadyRunning(Exception):
    pass


def hold_lock(path: Path, what: str):
    """Takes an exclusive lock on `path` for as long as the returned file stays open (the process
    lifetime, in practice), or raises AlreadyRunning naming the holder's pid."""
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.seek(0)
        holder = f.read().strip() or "unknown"
        f.close()
        raise AlreadyRunning(f"{what} is already running on this host (pid {holder})") from None
    f.seek(0)
    f.truncate()
    f.write(f"{os.getpid()}\n")
    f.flush()
    return f


@dataclass
class FleetConfig:
    name: str
    mode: str  # "github" | "server"
    lock_repo: str | None
    tasks_repo: str | None
    url: str | None
    version: str | None = None
    min_version: str | None = None
    source: str | None = None  # where self-update installs from, e.g. git+https://github.com/o/r
    heartbeat: float = 300
    ttl: float = 1200
    access_mode: str = "off"  # "auto" | "approve" | "off"
    access_deny: tuple[str, ...] = ()
    done_grace: float = 3600
    poll: float = 20
    ci_timeout: float = 3 * 3600
    raw: dict = field(default_factory=dict)

    @staticmethod
    def parse(text: str, *, base: Path | None = None) -> FleetConfig:
        d = tomllib.loads(text)
        f, c, lease = d.get("fleet", {}), d.get("coord", {}), d.get("lease", {})
        access, admin = d.get("access", {}), d.get("admin", {})
        if "name" not in f:
            raise ValueError("fleet.toml: [fleet] name is required")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", f["name"]):
            raise ValueError(f"fleet.toml: bad fleet name {f['name']!r}")
        mode = c.get("mode", "github")
        if mode not in ("github", "server"):
            raise ValueError(f"fleet.toml: [coord] mode must be github or server, not {mode!r}")

        def rel(p: str | None) -> str | None:
            if p and base and p.startswith((".", "/")) and not p.startswith("/"):
                return str((base / p).resolve())
            return p

        cfg = FleetConfig(
            name=f["name"], mode=mode, lock_repo=rel(c.get("lock_repo")), tasks_repo=rel(c.get("tasks_repo")),
            url=c.get("url"), version=f.get("version"), min_version=f.get("min_version"),
            source=f.get("source"),
            heartbeat=parse_duration(lease.get("heartbeat", "5m")), ttl=parse_duration(lease.get("ttl", "20m")),
            access_mode=access.get("write_request", "off"), access_deny=tuple(access.get("deny", ())),
            done_grace=parse_duration(admin.get("done_grace", "1h")), poll=parse_duration(admin.get("poll", "20s")),
            ci_timeout=parse_duration(admin.get("ci_timeout", "3h")), raw=d)
        if mode == "github" and not cfg.lock_repo:
            raise ValueError("fleet.toml: github mode needs [coord] lock_repo")
        if mode == "server" and not cfg.url:
            raise ValueError("fleet.toml: server mode needs [coord] url")
        if cfg.ttl <= cfg.heartbeat:
            raise ValueError("fleet.toml: [lease] ttl must be longer than heartbeat")
        return cfg


# What `cli = "codex"` changes in the agent defaults (explicit settings still win).
CODEX_DEFAULTS = {"codex": {
    "command": ["codex", "exec", "--json"],
    "model_args": ["-m", "{model}"],
    "effort_args": ["-c", "model_reasoning_effort={effort}"],
}}


@dataclass
class AgentConfig:
    # "local": directly on this machine | "container": claude on this machine, its tools in a
    # per-attempt container (container-tools plugin) | "docker": claude itself in a container (unused)
    runtime: str = "local"
    cli: str = "claude"  # the agent CLI: "claude" (stream-json over stdin) | "codex" (codex exec --json)
    isolate_home: bool = False  # local runtime: give the agent an empty per-attempt HOME
    command: list[str] = field(default_factory=lambda: [
        "claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
        "--dangerously-skip-permissions"])
    mounts: list[str] = field(default_factory=list)  # extra docker -v specs, e.g. caches or creds read-only
    env: dict[str, str] = field(default_factory=dict)
    docker_args: list[str] = field(default_factory=list)
    network: str = ""  # container runtime: --network of the attempt's container ("": docker's default)
    plugin_dir: str = ""  # container runtime: the container-tools plugin (default: the one shipped here)
    codex_exec_server: str = ""  # container runtime, codex: static exec-server binary (default: next to codex)
    usage_command: list[str] = field(default_factory=list)  # prints the CLI's usage JSON (capacity.py)
    max_wall: float = 4 * 3600
    stall: float = 20 * 60
    repairs: int = 2
    # model and effort (see agentchoice.py): the host's [agent] table and CLI overrides
    choice_table: dict = field(default_factory=dict)  # host default, lowest precedence
    choice_cli: dict = field(default_factory=dict)  # --model / --effort, highest precedence
    model_args: list[str] = field(default_factory=lambda: ["--model", "{model}"])
    effort_args: list[str] = field(default_factory=lambda: ["--effort", "{effort}"])


@dataclass
class HostConfig:
    worker_id: str
    gh_user: str | None = None
    slots: int = 1
    kinds: tuple[str, ...] = ("review", "work")
    filters: tuple[str, ...] = ()
    allow_self_review: bool = False
    push: str = "origin"  # "origin" | "fork": where work branches are pushed
    git_protocol: str = "ssh"
    # claim only while, in every subscription window,
    # used < spare_capacity * elapsed + spare_ease_in * (1 - elapsed) + spare_allowance (capacity.py)
    spare_capacity: float | None = None
    spare_ease_in: float = 0.0
    spare_allowance: float = 0.0
    agent: AgentConfig = field(default_factory=AgentConfig)

    @staticmethod
    def load(fleet: str, path: Path | None = None, **overrides) -> HostConfig:
        path = path or config_home() / f"{fleet}.toml"
        d = tomllib.loads(path.read_text()) if path.exists() else {}
        w, a = d.get("worker", {}), d.get("agent", {})
        gh_user = overrides.get("gh_user") or w.get("gh_user")
        default_id = f"{gh_user or getpass.getuser()}/{socket.gethostname()}"
        agent = AgentConfig()
        cli_name = a.get("cli", "claude")
        if cli_name not in ("claude", "codex"):
            raise ValueError(f"{path}: [agent] cli must be claude or codex")
        for k, v in CODEX_DEFAULTS.get(cli_name, {}).items():
            setattr(agent, k, list(v))
        for k in ("runtime", "cli", "command", "mounts", "env", "docker_args", "repairs", "isolate_home",
                  "model_args", "effort_args", "network", "plugin_dir", "codex_exec_server", "usage_command"):
            if k in a:
                setattr(agent, k, a[k])
        agentchoice.validate(a, str(path))
        agent.choice_table = {k: a[k] for k in ("model", "effort", *agentchoice.KINDS) if k in a}
        cli = {k: overrides[k] for k in ("model", "effort") if overrides.get(k)}
        agentchoice.validate(cli, "command line")
        agent.choice_cli = cli
        for k in ("max_wall", "stall"):
            if k in a:
                setattr(agent, k, parse_duration(a[k]))
        host = HostConfig(
            worker_id=overrides.get("worker_id") or w.get("id") or default_id, gh_user=gh_user,
            slots=int(overrides.get("slots") or w.get("slots", 1)),
            kinds=tuple(overrides.get("kinds") or w.get("kinds", ("review", "work"))),
            filters=tuple(overrides.get("filters") or w.get("filters", ())),
            allow_self_review=bool(w.get("allow_self_review", False)), push=w.get("push", "origin"),
            git_protocol=w.get("git_protocol", "ssh"), agent=agent,
            spare_capacity=_positive(overrides.get("spare_capacity") or w.get("spare_capacity"),
                                     f"{path}: [worker] spare_capacity"),
            spare_ease_in=_fraction(_first(overrides.get("spare_ease_in"), w.get("spare_ease_in")),
                                    f"{path}: [worker] spare_ease_in"),
            spare_allowance=_fraction(_first(overrides.get("spare_allowance"), w.get("spare_allowance")),
                                      f"{path}: [worker] spare_allowance"))
        if agent.runtime not in ("container", "docker", "local"):
            raise ValueError(f"{path}: [agent] runtime must be local, container or docker")
        if agent.cli == "codex" and agent.runtime == "docker":
            raise ValueError(f"{path}: [agent] cli = codex needs runtime local or container")
        return host


def _positive(v: object, what: str) -> float | None:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not v > 0:
        raise ValueError(f"{what} must be a positive number, not {v!r}")
    return float(v)


def _first(*vs: object) -> object:
    return next((v for v in vs if v is not None), None)


def _fraction(v: object, what: str) -> float:
    """A non-negative fraction of a limit; absent is 0."""
    if v is None:
        return 0.0
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not v >= 0:
        raise ValueError(f"{what} must be a number ≥ 0, not {v!r}")
    return float(v)


@dataclass
class Loaded:
    source: str
    fleet: FleetConfig
    config: ConfigRepo  # the repo holding the task tree
    rev: str | None  # config repo commit fleet.toml was read from (None for a local file)


def source_store(url: str) -> Path:
    return data_home() / "mirrors" / (hashlib.sha1(url.encode()).hexdigest()[:16] + ".git")


def load(source: str, protocol: str = "ssh") -> Loaded:
    p = Path(source).expanduser()
    if p.is_file():
        fleet = FleetConfig.parse(p.read_text(), base=p.parent)
        if not fleet.tasks_repo:
            raise ValueError(f"{p}: [coord] tasks_repo is required when fleet.toml is a local file")
        url = repo_url(fleet.tasks_repo, protocol)
        return Loaded(source, fleet, ConfigRepo(url, source_store(url)), None)
    url = repo_url(source, protocol)
    repo = ConfigRepo(url, source_store(url))
    rev = repo.fetch()
    if rev is None:
        raise ValueError(f"{source}: the default branch does not exist")
    raw = repo.read_file(rev, "fleet.toml")
    if raw is None:
        raise ValueError(f"{source}: no fleet.toml on the default branch")
    fleet = FleetConfig.parse(raw.decode())
    if fleet.tasks_repo:
        turl = repo_url(fleet.tasks_repo, protocol)
        if turl != url:
            repo = ConfigRepo(turl, source_store(turl))
    return Loaded(source, fleet, repo, rev)
