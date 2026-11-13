"""Configuration.

* `fleet.toml` (shared, in the config repo or a local file): what coordination needs.
* `~/.config/afe-fleet/<fleet>.toml` (per host): worker identity, slots, filters, agent runtime.

A *source* is what the user passes on the command line: `org/repo`, a git URL or path whose
default branch holds `fleet.toml`, or a path to a `fleet.toml` file.
"""

from __future__ import annotations

import getpass
import hashlib
import os
import re
import socket
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

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
          mirrors/<hash>.git                 config-repo mirrors
          fleets/<fleet>/lock-<slot>.git     lock-repo mirrors
          fleets/<fleet>/journal-<slot>.json
          fleets/<fleet>/attempts/<task>-e<epoch>/{work,task,out}   worktree per attempt
          fleets/<fleet>/server.sqlite       server mode

    `$AFE_FLEET_HOME` overrides the whole directory; otherwise `$XDG_DATA_HOME/afe-fleet`.
    """
    if os.environ.get("AFE_FLEET_HOME"):
        return Path(os.environ["AFE_FLEET_HOME"])
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "afe-fleet"


def fleet_home(fleet: str) -> Path:
    return data_home() / "fleets" / fleet


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


@dataclass
class AgentConfig:
    runtime: str = "local"  # "local": directly on this machine | "docker" (not used for now)
    isolate_home: bool = False  # local runtime: give the agent an empty per-attempt HOME
    command: list[str] = field(default_factory=lambda: [
        "claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
        "--dangerously-skip-permissions"])
    mounts: list[str] = field(default_factory=list)  # extra docker -v specs, e.g. creds read-only
    env: dict[str, str] = field(default_factory=dict)
    docker_args: list[str] = field(default_factory=list)
    max_wall: float = 4 * 3600
    stall: float = 20 * 60
    repairs: int = 2


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
    agent: AgentConfig = field(default_factory=AgentConfig)

    @staticmethod
    def load(fleet: str, path: Path | None = None, **overrides) -> HostConfig:
        path = path or config_home() / f"{fleet}.toml"
        d = tomllib.loads(path.read_text()) if path.exists() else {}
        w, a = d.get("worker", {}), d.get("agent", {})
        gh_user = overrides.get("gh_user") or w.get("gh_user")
        default_id = f"{gh_user or getpass.getuser()}/{socket.gethostname()}"
        agent = AgentConfig()
        for k in ("runtime", "command", "mounts", "env", "docker_args", "repairs", "isolate_home"):
            if k in a:
                setattr(agent, k, a[k])
        for k in ("max_wall", "stall"):
            if k in a:
                setattr(agent, k, parse_duration(a[k]))
        host = HostConfig(
            worker_id=overrides.get("worker_id") or w.get("id") or default_id, gh_user=gh_user,
            slots=int(overrides.get("slots") or w.get("slots", 1)),
            kinds=tuple(overrides.get("kinds") or w.get("kinds", ("review", "work"))),
            filters=tuple(overrides.get("filters") or w.get("filters", ())),
            allow_self_review=bool(w.get("allow_self_review", False)), push=w.get("push", "origin"),
            git_protocol=w.get("git_protocol", "ssh"), agent=agent)
        if agent.runtime not in ("docker", "local"):
            raise ValueError(f"{path}: [agent] runtime must be docker or local")
        return host


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
