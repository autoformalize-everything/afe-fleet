"""Running git, with a private object store per client."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


class GitError(Exception):
    def __init__(self, args: list[str], code: int, stderr: str):
        super().__init__(f"git {' '.join(args)} exited {code}: {stderr.strip()[-2000:]}")
        self.code = code
        self.stderr = stderr


@dataclass
class Git:
    """git with a fixed working directory (usually a bare repository) and identity."""

    dir: Path
    name: str = "afe-fleet"
    email: str = "afe-fleet@invalid"
    env: dict[str, str] = field(default_factory=dict)
    timeout: float = 300

    def _env(self, extra: dict[str, str] | None) -> dict[str, str]:
        e = dict(os.environ)
        e.update({
            "GIT_AUTHOR_NAME": self.name, "GIT_AUTHOR_EMAIL": self.email,
            "GIT_COMMITTER_NAME": self.name, "GIT_COMMITTER_EMAIL": self.email,
            "GIT_TERMINAL_PROMPT": "0",
            # never let git detach background maintenance into our process tree
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "gc.autoDetach", "GIT_CONFIG_VALUE_0": "false",
            "GIT_CONFIG_KEY_1": "maintenance.autoDetach", "GIT_CONFIG_VALUE_1": "false",
        })
        e.update(self.env)
        if extra:
            e.update(extra)
        return e

    def run(self, *args: str, input: str | bytes | None = None, env: dict[str, str] | None = None,
            check: bool = False, timeout: float | None = None) -> subprocess.CompletedProcess:
        binary = isinstance(input, bytes)
        p = subprocess.run(["git", *args], cwd=self.dir, input=input, capture_output=True,
                           text=not binary, env=self._env(env), timeout=timeout or self.timeout)
        if check and p.returncode != 0:
            err = p.stderr if isinstance(p.stderr, str) else p.stderr.decode(errors="replace")
            raise GitError(list(args), p.returncode, err)
        return p

    def __call__(self, *args: str, input: str | bytes | None = None, env: dict[str, str] | None = None,
                 timeout: float | None = None) -> str:
        p = self.run(*args, input=input, env=env, check=True, timeout=timeout)
        out = p.stdout if isinstance(p.stdout, str) else p.stdout.decode()
        return out.strip()

    def init_bare(self) -> Git:
        self.dir.mkdir(parents=True, exist_ok=True)
        if not (self.dir / "HEAD").exists():
            self("init", "-q", "--bare", ".")
        return self


def repo_url(spec: str, protocol: str = "ssh") -> str:
    """`org/repo` → a GitHub URL; anything that already looks like a URL or path is kept."""
    if "://" in spec or spec.startswith(("git@", "/", ".", "~")) or Path(spec).exists():
        return os.path.expanduser(spec)
    if spec.count("/") == 1:
        if protocol == "https":
            return f"https://github.com/{spec}.git"
        return f"git@github.com:{spec}.git"
    raise ValueError(f"not a repository: {spec!r}")


def github_slug(url_or_spec: str) -> str | None:
    """`org/repo` for a GitHub repository URL or spec; None otherwise."""
    s = url_or_spec
    for prefix in ("git@github.com:", "https://github.com/", "ssh://git@github.com/"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    else:
        if "://" in s or s.startswith(("/", ".", "~")):
            return None
    s = s.removesuffix(".git").strip("/")
    return s if s.count("/") == 1 else None
