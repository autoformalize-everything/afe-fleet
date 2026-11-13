"""The config repo: `fleet.toml`, the task tree, and done records.

    fleet.toml
    tasks/<task-id>/TASK.md          a task is a directory with TASK.md and no task below it
    tasks/<task-id>/task.toml        optional: deps, kinds, tags
    tasks/**/project.toml            nearest one above (or in) a task describes its project
    done/<task-id>.json              written by the admin when a task is accepted
    cancelled/<task-id>.json         written by the admin when a task is cancelled
    STATUS.md                        written by the admin; for humans only

Every read here is a `git fetch` of the branch tip, never a cached HTTP read, because the
worker's post-claim done check relies on it being linearizable (docs/DESIGN.md section 4).
"""

from __future__ import annotations

import hashlib
import json
import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .gitutil import Git, GitError

log = logging.getLogger(__name__)

TASKS_DIR = "tasks"
DONE_DIR = "done"
CANCELLED_DIR = "cancelled"


@dataclass(frozen=True)
class TaskDef:
    id: str
    spec: str  # content hash of the task directory + its project.toml
    project: dict
    meta: dict  # task.toml
    deps: tuple[str, ...] = ()
    kinds: tuple[str, ...] = ("work", "review")

    @property
    def priority(self) -> int:
        return int(self.meta.get("priority", 0))


@dataclass
class Tree:
    """The config repo at one commit."""

    rev: str
    tasks: dict[str, TaskDef] = field(default_factory=dict)
    done: set[str] = field(default_factory=set)  # accepted: satisfies dependencies
    cancelled: set[str] = field(default_factory=set)  # withdrawn: does not

    @property
    def closed(self) -> set[str]:
        """Tasks nobody may claim again."""
        return self.done | self.cancelled


class ConfigRepo:
    def __init__(self, url: str, store: Path, *, branch: str | None = None, who: str = "afe-fleet"):
        self.url = url
        self.git = Git(store, name=who).init_bare()
        self._branch = branch

    @property
    def branch(self) -> str:
        if self._branch is None:
            out = self.git("ls-remote", "--symref", self.url, "HEAD")
            for line in out.splitlines():
                if line.startswith("ref: "):
                    self._branch = line.split()[1].removeprefix("refs/heads/")
                    break
            else:
                self._branch = "main"
        return self._branch

    def fetch(self) -> str | None:
        """Fetch the branch tip; returns its commit, or None if the branch does not exist."""
        p = self.git.run("fetch", "-q", "--no-tags", self.url, f"+refs/heads/{self.branch}:refs/cfg/head")
        if p.returncode != 0:
            if "couldn't find remote ref" in p.stderr:
                return None
            raise GitError(["fetch", self.url], p.returncode, p.stderr)
        return self.git("rev-parse", "refs/cfg/head")

    def read_file(self, rev: str, path: str) -> bytes | None:
        p = self.git.run("cat-file", "blob", f"{rev}:{path}", input=b"")
        return p.stdout if p.returncode == 0 else None

    def has_done(self, rev: str | None, task: str) -> bool:
        """Is there a done or cancelled record for `task` at `rev`?"""
        if rev is None:
            return False
        return any(self.git.run("cat-file", "-e", f"{rev}:{d}/{task}.json").returncode == 0
                   for d in (DONE_DIR, CANCELLED_DIR))

    def load(self, rev: str | None) -> Tree:
        if rev is None:
            return Tree("")
        files = self.git("ls-tree", "-r", "--full-tree", "--format=%(objectname)\t%(path)", rev).splitlines()
        blobs = dict(reversed(line.split("\t", 1)) for line in files)
        def records(d: str) -> set[str]:
            return {p[len(d) + 1:-len(".json")] for p in blobs if p.startswith(d + "/") and p.endswith(".json")}
        done, cancelled = records(DONE_DIR), records(CANCELLED_DIR)
        task_dirs = {p.rsplit("/", 1)[0] for p in blobs
                     if p.startswith(TASKS_DIR + "/") and p.endswith("/TASK.md")}
        leaves = {d for d in task_dirs if not any(o != d and o.startswith(d + "/") for o in task_dirs)}
        tasks = {}
        for d in sorted(leaves):
            tid = d[len(TASKS_DIR) + 1:]
            proj_path = self._nearest(blobs, d, "project.toml")
            if proj_path is None:
                log.debug("task %s has no project.toml above it; skipped", tid)
                continue
            try:
                project = tomllib.loads(self.read_file(rev, proj_path).decode())
                meta_raw = self.read_file(rev, f"{d}/task.toml")
                meta = tomllib.loads(meta_raw.decode()) if meta_raw else {}
            except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
                log.warning("task %s: bad toml: %s", tid, e)
                continue
            dir_hash = self.git("rev-parse", f"{rev}:{d}")
            spec = hashlib.sha256(f"{dir_hash}\n{blobs[proj_path]}\n".encode()).hexdigest()[:16]
            tasks[tid] = TaskDef(tid, spec, project, meta, tuple(meta.get("deps", ())),
                                 tuple(meta.get("kinds", ("work", "review"))))
        return Tree(rev, tasks, done, cancelled)

    @staticmethod
    def _nearest(blobs: dict[str, str], d: str, name: str) -> str | None:
        parts = d.split("/")
        for i in range(len(parts), 0, -1):
            p = "/".join(parts[:i] + [name])
            if p in blobs:
                return p
        return None

    def task_text(self, rev: str, task: str) -> str:
        raw = self.read_file(rev, f"{TASKS_DIR}/{task}/TASK.md")
        return raw.decode(errors="replace") if raw else ""

    # -- admin writes ------------------------------------------------------------------------

    def write(self, changes: dict[str, bytes | None], message: str, tries: int = 10) -> str:
        """Commit `changes` (path → content, None = delete) on the branch, compare-and-swap on
        its tip, retrying on contention. Idempotent: no commit if nothing changes."""
        index = self.git.dir / "afe-index"
        env = {"GIT_INDEX_FILE": str(index)}
        for _ in range(tries):
            parent = self.fetch()
            index.unlink(missing_ok=True)
            if parent:
                self.git("read-tree", parent, env=env)
            for path, content in changes.items():
                if content is None:
                    self.git("rm", "--cached", "-q", "--ignore-unmatch", path, env=env)
                else:
                    blob = self.git("hash-object", "-w", "--stdin", input=content)
                    self.git("update-index", "--add", "--cacheinfo", f"100644,{blob},{path}", env=env)
            tree = self.git("write-tree", env=env)
            if parent and tree == self.git("rev-parse", f"{parent}^{{tree}}"):
                return parent
            parents = ["-p", parent] if parent else []
            sha = self.git("commit-tree", tree, *parents, "-m", message)
            ref = f"refs/heads/{self.branch}"
            p = self.git.run("push", "--porcelain", f"--force-with-lease={ref}:{parent or ''}",
                             self.url, f"{sha}:{ref}")
            if p.returncode == 0:
                return sha
            log.info("config write lost a race (%s); retrying", p.stderr.strip()[-200:])
        raise GitError(["push", self.url], 1, "config write: too much contention")

    def write_done(self, task: str, record: dict, *, cancelled: bool = False, remove_task: bool = False) -> str:
        """Record a closed task. With `remove_task`, also delete its directory from the tree."""
        body = json.dumps(record, indent=2, sort_keys=True).encode() + b"\n"
        d = CANCELLED_DIR if cancelled else DONE_DIR
        changes: dict[str, bytes | None] = {f"{d}/{task}.json": body}
        if remove_task:
            rev = self.fetch()
            if rev:
                out = self.git("ls-tree", "-r", "--name-only", rev, f"{TASKS_DIR}/{task}/")
                changes.update({p: None for p in out.splitlines()})
        return self.write(changes, f"{'cancelled' if cancelled else 'done'}: {task}")
