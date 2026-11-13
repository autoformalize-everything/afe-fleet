"""systemd user services: `afe-fleet-<role>-<fleet>.service`, unique per fleet and role.

The unit runs whatever `afe-fleet` this command was started as: the `uv tool install` entry
point, or the checkout's `.venv/bin/afe-fleet` under `uv run`. PATH is captured at install
time because user services start with a minimal one and the worker needs git, gh and docker.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROLES = ("worker", "admin", "server")


def unit_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user"


def unit_name(role: str, fleet: str) -> str:
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    return f"afe-fleet-{role}-{fleet}.service"


def self_command() -> list[str]:
    """How to run this same afe-fleet from a unit file."""
    exe = Path(sys.argv[0])
    if exe.name == "afe-fleet" and exe.exists():
        return [str(exe.absolute())]
    found = shutil.which("afe-fleet")
    if found and Path(found).resolve().parent == Path(sys.executable).resolve().parent:
        return [found]
    return [sys.executable, "-m", "afe_fleet"]


def render(role: str, fleet: str, args: list[str], *, command: list[str] | None = None,
           path_env: str | None = None) -> str:
    cmd = command or self_command()
    exec_start = " ".join(shlex.quote(a) for a in [*cmd, role, "run", *args])
    path_env = path_env if path_env is not None else os.environ.get("PATH", "/usr/bin:/bin")
    return f"""[Unit]
Description=afe-fleet {role} for {fleet}
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
ExecStart={exec_start}
Restart=always
RestartSec=10
TimeoutStopSec=180
KillMode=mixed
Environment=PYTHONUNBUFFERED=1
Environment={shlex.quote("PATH=" + path_env)}

[Install]
WantedBy=default.target
"""


@dataclass
class Systemctl:
    binary: str = os.environ.get("AFE_FLEET_SYSTEMCTL", "systemctl")
    loginctl: str = os.environ.get("AFE_FLEET_LOGINCTL", "loginctl")

    def __call__(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        p = subprocess.run([self.binary, "--user", *args], capture_output=True, text=True)
        if check and p.returncode != 0:
            raise RuntimeError(f"systemctl --user {' '.join(args)}: {p.stderr.strip()}")
        return p

    def linger(self) -> str:
        user = os.environ.get("USER") or ""
        p = subprocess.run([self.loginctl, "show-user", user, "-p", "Linger", "--value"],
                           capture_output=True, text=True)
        if p.stdout.strip() == "yes":
            return "already enabled"
        p = subprocess.run([self.loginctl, "enable-linger", user], capture_output=True, text=True)
        return "enabled" if p.returncode == 0 else f"could not enable ({p.stderr.strip()})"


def install(role: str, fleet: str, args: list[str], *, start: bool = True, linger: bool = True,
            sc: Systemctl | None = None, command: list[str] | None = None) -> tuple[Path, bool, list[str]]:
    """Write (or rewrite) the unit. Returns (path, changed, notes). Idempotent."""
    sc = sc or Systemctl()
    path = unit_dir() / unit_name(role, fleet)
    text = render(role, fleet, args, command=command)
    changed = not path.exists() or path.read_text() != text
    notes = []
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(text)
        os.replace(tmp, path)
        sc("daemon-reload")
    if start:
        sc("enable", path.name)
        sc("restart" if changed else "start", path.name)
    if linger:
        notes.append(f"lingering {sc.linger()} (the service keeps running after you log out)")
    return path, changed, notes


def uninstall(role: str, fleet: str, *, sc: Systemctl | None = None) -> bool:
    sc = sc or Systemctl()
    path = unit_dir() / unit_name(role, fleet)
    if not path.exists():
        return False
    sc("disable", "--now", path.name, check=False)
    path.unlink()
    sc("daemon-reload")
    sc("reset-failed", path.name, check=False)
    return True


def installed() -> list[str]:
    d = unit_dir()
    return sorted(p.name for p in d.glob("afe-fleet-*.service")) if d.exists() else []


def status(role: str, fleet: str, sc: Systemctl | None = None) -> str:
    sc = sc or Systemctl()
    p = sc("status", "--no-pager", unit_name(role, fleet), check=False)
    return p.stdout or p.stderr
