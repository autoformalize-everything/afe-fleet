"""One shared clone per code repository per host, and a git worktree per attempt.

    ~/.local/share/afe-fleet/repos/<name>-<hash>.git     bare repository, remote `origin`
    ~/.local/share/afe-fleet/repos/<name>-<hash>.lock    flock(2) lock file

A worktree's commits live in this repository's object store, so it must not be treated as a
disposable cache. Worktrees themselves are per attempt, under
~/.local/share/afe-fleet/fleets/<fleet>/attempts/ (see worker.py and config.data_home).

Every slot of every afe-fleet process on the machine, in any fleet, uses the same clone for the
same URL. Operations that change shared state (fetch, worktree add/remove, ref and branch
deletion, prune) hold an exclusive file lock, so concurrent workers cannot collide on git's own
lock files. Work inside a worktree (the agent committing, the verify command) needs no lock:
each worktree has its own index, HEAD and branch.

Branches and temporary refs created here are namespaced per attempt and removed with the
worktree. Automatic gc is off: `git gc` would repack refs and objects under the feet of agents
committing in other worktrees.
"""

from __future__ import annotations

import fcntl
import hashlib
import logging
import os
import re
import shutil
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from .config import data_home
from .gitutil import Git, GitError

log = logging.getLogger(__name__)


def _store_name(url: str) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", url.rstrip("/").removesuffix(".git").rsplit("/", 1)[-1])[:40]
    return f"{base}-{hashlib.sha1(url.encode()).hexdigest()[:12]}"


class LockTimeout(Exception):
    pass


@dataclass
class Worktree:
    """A checkout for one attempt. `branch` and `refs` are removed with it."""

    path: Path
    git: Git
    branch: str | None = None
    refs: list[str] = field(default_factory=list)


class SharedRepo:
    def __init__(self, url: str, root: Path | None = None, *, lock_timeout: float = 900):
        self.url = url
        root = root or data_home() / "repos"
        root.mkdir(parents=True, exist_ok=True)
        name = _store_name(url)
        self.path = root / f"{name}.git"
        self.lockfile = root / f"{name}.lock"
        self.lock_timeout = lock_timeout
        self.git = Git(self.path)

    # -- locking -------------------------------------------------------------------------------

    @contextmanager
    def locked(self):
        """Exclusive across threads and processes. Polls so a stuck holder surfaces as a timeout."""
        fd = os.open(self.lockfile, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            deadline = time.monotonic() + self.lock_timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        raise LockTimeout(f"{self.lockfile}: held for over {self.lock_timeout:.0f}s") from None
                    time.sleep(0.05)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    # -- setup and fetch -----------------------------------------------------------------------

    def _ensure(self) -> None:
        """Create or repair the shared clone. Call with the lock held."""
        if not (self.path / "HEAD").exists():
            self.path.mkdir(parents=True, exist_ok=True)
            self.git("init", "-q", "--bare", ".")
        remotes = self.git("remote").split()
        if "origin" not in remotes:
            self.git("remote", "add", "origin", self.url)
        elif self.git("remote", "get-url", "origin") != self.url:
            self.git("remote", "set-url", "origin", self.url)
        for k, v in (("gc.auto", "0"), ("maintenance.auto", "false"), ("fetch.writeCommitGraph", "false"),
                     ("remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")):
            self.git("config", k, v)

    def fetch(self) -> None:
        """Bring `origin/*` up to date. Worktrees refer to `origin/<base>`."""
        with self.locked():
            self._ensure()
            self.git("fetch", "-q", "--prune", "origin")

    def fetch_ref(self, url: str, branch: str, ref: str) -> str | None:
        """Fetch `branch` from `url` (e.g. a fork) into `ref`; returns its commit, or None."""
        with self.locked():
            self._ensure()
            p = self.git.run("fetch", "-q", "--no-tags", url, f"+refs/heads/{branch}:{ref}")
            if p.returncode != 0:
                log.info("fetch %s %s: %s", url, branch, p.stderr.strip()[-300:])
                return None
            return self.git("rev-parse", ref)

    # -- worktrees -----------------------------------------------------------------------------

    def add(self, path: Path, start: str, *, branch: str | None = None, refs: list[str] | None = None,
            name: str | None = None, email: str | None = None) -> Worktree:
        """A new worktree at `path` on `start`: on a new local `branch`, or detached."""
        with self.locked():
            self._ensure()
            self.git("worktree", "prune")
            if path.exists():
                shutil.rmtree(path)
            args = ["worktree", "add", "-q", "-f"]
            args += ["-B", branch] if branch else ["--detach"]
            self.git(*args, str(path), start)
        wt_git = Git(path, name=name or "afe-fleet", email=email or "afe-fleet@invalid")
        return Worktree(path, wt_git, branch, list(refs or []))

    def remove(self, wt: Worktree) -> None:
        """Remove the worktree, its local branch and temporary refs. Never raises."""
        try:
            with self.locked():
                self.git.run("worktree", "remove", "--force", str(wt.path))
                shutil.rmtree(wt.path, ignore_errors=True)
                self.git.run("worktree", "prune")
                if wt.branch:
                    self.git.run("branch", "-D", wt.branch)
                for ref in wt.refs:
                    self.git.run("update-ref", "-d", ref)
        except (LockTimeout, GitError, OSError) as e:
            log.warning("could not clean up worktree %s: %s", wt.path, e)

    def prune(self) -> None:
        """Forget worktrees whose directories are gone (a worker crashed or was killed)."""
        if (self.path / "HEAD").exists():
            with self.locked():
                self.git.run("worktree", "prune")

    def worktrees(self) -> list[str]:
        out = self.git("worktree", "list", "--porcelain") if (self.path / "HEAD").exists() else ""
        return [line.split(" ", 1)[1] for line in out.splitlines() if line.startswith("worktree ")][1:]
