"""Self-update.

`fleet.toml` pins `[fleet] version` and `source` (e.g. `git+https://github.com/o/afe-fleet`).
When a uv-tool-installed worker sees a different version, it reinstalls from `source` at tag
`v<version>`, rewrites its unit file, and exits so systemd starts the new binary. A worker run
from a checkout never updates itself; it only warns.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__

log = logging.getLogger(__name__)

EXIT_UPDATED = 75  # systemd restarts us (Restart=always)


def install_kind() -> str:
    """"tool" when installed with `uv tool install`, "checkout" otherwise."""
    prefix = Path(sys.prefix).resolve()
    return "tool" if "uv/tools" in prefix.as_posix() else "checkout"


def vtuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split(".") if x.isdigit())


def too_old(min_version: str | None) -> bool:
    return bool(min_version) and vtuple(__version__) < vtuple(min_version)


def maybe_update(version: str | None, source: str | None) -> bool:
    """Returns True if a new version was installed (the caller should exit EXIT_UPDATED)."""
    if not version or version == __version__:
        return False
    if install_kind() != "tool" or not source:
        log.warning("fleet.toml wants afe-fleet %s, this is %s; not self-updating (%s)", version, __version__,
                    "running from a checkout" if install_kind() != "tool" else "no [fleet] source")
        return False
    uv = shutil.which("uv")
    if not uv:
        log.error("cannot self-update: uv not on PATH")
        return False
    spec = f"afe-fleet @ {source}@v{version}"
    log.info("updating afe-fleet %s → %s from %s", __version__, version, source)
    p = subprocess.run([uv, "tool", "install", "--force", spec], capture_output=True, text=True, timeout=900)
    if p.returncode != 0:
        log.error("self-update failed: %s", p.stderr.strip()[-1000:])
        return False
    check = subprocess.run([shutil.which("afe-fleet") or "afe-fleet", "version"], capture_output=True, text=True)
    if check.returncode != 0 or check.stdout.strip() != version:
        log.error("self-update installed something that reports %r; continuing on %s", check.stdout.strip(),
                  __version__)
        return False
    return True
