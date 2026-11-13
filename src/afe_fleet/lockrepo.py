"""GitHub mode: the lock repo. One branch per task in flight, `task-lock/<task-id>`.

Each commit's tree is exactly `status.json`; its message is JSON naming the event. Every write
is a compare-and-swap: `git push --force-with-lease=<ref>:<expected>`, with an empty expected
value meaning "create only". A missing branch is a task nobody has claimed (`Status.initial`).
Plain fast-forward pushes are never used: once branches are deleted, they would let a zombie
recreate one (docs/DESIGN.md section 4, F2).
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from .gitutil import Git, GitError
from .model import Event, event_from_json, event_to_json, step
from .status import Status, apply

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Snap:
    """What a client read. `rev` is the tip commit, or None when the branch does not exist."""

    task: str
    rev: str | None
    status: Status


@dataclass(frozen=True)
class Upd:
    ref: str
    expect: str | None  # None: the ref must not exist
    new: str | None  # None: delete


class Unreachable(Exception):
    """The remote could not be reached, so whether a write landed is unknown. Not a lost lease:
    retry with the same commit (or read back)."""


class Result:
    WON = "won"
    LOST = "lost"  # git rejected: someone else moved the branch first
    DENIED = "denied"  # the state machine rejected the event; nothing was pushed


@dataclass(frozen=True)
class Submitted:
    result: str
    snap: Snap | None = None  # the new state when won

    @property
    def won(self) -> bool:
        return self.result == Result.WON


class LockRepo:
    def __init__(self, url: str, store: Path, *, prefix: str = "task-lock/", who: str = "afe-fleet"):
        self.url = url
        self.prefix = prefix
        self.who = who
        self.git = Git(store, name=who).init_bare()

    # -- names -------------------------------------------------------------------------------

    def ref(self, task: str) -> str:
        return f"refs/heads/{self.prefix}{task}"

    def _local(self, task: str) -> str:
        return f"refs/lock/{task}"

    # -- reads -------------------------------------------------------------------------------

    def tips(self) -> dict[str, str]:
        """Every lock branch tip, straight from the remote (`git ls-remote`)."""
        out = self.git("ls-remote", self.url, f"refs/heads/{self.prefix}*")
        base = f"refs/heads/{self.prefix}"
        res = {}
        for line in out.splitlines():
            sha, _, ref = line.partition("\t")
            if ref.startswith(base):
                res[ref[len(base):]] = sha
        return res

    def tip(self, task: str) -> str | None:
        out = self.git("ls-remote", self.url, self.ref(task))
        for line in out.splitlines():
            sha, _, ref = line.partition("\t")
            if ref == self.ref(task):
                return sha
        return None

    def status_at(self, sha: str) -> Status:
        return Status.loads(self.git("cat-file", "-p", f"{sha}:status.json"))

    def read(self, task: str) -> Snap:
        """`snapshot()` for one task. No branch: the task is unclaimed."""
        if self.tip(task) is None:
            return Snap(task, None, Status.initial(task))
        p = self.git.run("fetch", "-q", "--no-tags", self.url, f"+{self.ref(task)}:{self._local(task)}")
        if p.returncode != 0:  # deleted between ls-remote and fetch
            return Snap(task, None, Status.initial(task))
        sha = self.git("rev-parse", self._local(task))
        return Snap(task, sha, self.status_at(sha))

    def snapshot(self) -> dict[str, Snap]:
        """Every task with a lock branch, in one fetch. Tasks missing here are unclaimed."""
        self.git("fetch", "-q", "--no-tags", "--prune", self.url,
                 f"+refs/heads/{self.prefix}*:refs/lock/*")
        out = self.git("for-each-ref", "--format=%(refname)\t%(objectname)", "refs/lock/")
        res = {}
        for line in out.splitlines():
            ref, _, sha = line.partition("\t")
            task = ref[len("refs/lock/"):]
            try:
                res[task] = Snap(task, sha, self.status_at(sha))
            except (GitError, ValueError, KeyError) as e:
                log.warning("unreadable status on %s at %s: %s", task, sha[:8], e)
        return res

    # -- writes ------------------------------------------------------------------------------

    def commit(self, status: Status, parent: str | None, message: dict) -> str:
        blob = self.git("hash-object", "-w", "--stdin", input=status.dumps())
        tree = self.git("mktree", input=f"100644 blob {blob}\tstatus.json\n")
        parents = ["-p", parent] if parent else []
        return self.git("commit-tree", tree, *parents, "-m", json.dumps(message, sort_keys=True))

    def push(self, updates: list[Upd]) -> list[bool]:
        """One round trip; per update, whether the remote accepted it."""
        if not updates:
            return []
        leases = [f"--force-with-lease={u.ref}:{u.expect or ''}" for u in updates]
        specs = [f"{u.new or ''}:{u.ref}" for u in updates]
        p = self.git.run("push", "--porcelain", *leases, self.url, *specs)
        flags: dict[str, str] = {}
        for line in p.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and ":" in parts[1]:
                flags[parts[1].rsplit(":", 1)[1]] = parts[0]
        res = []
        for u in updates:
            if u.ref not in flags:
                raise GitError(["push", u.ref], p.returncode, p.stderr + p.stdout)
            res.append(flags[u.ref] != "!")
        return res

    def cas(self, task: str, expect: str | None, new: str | None, tries: int = 3) -> bool:
        """Compare-and-swap with the "unknown outcome → read back" rule.

        True: the ref now holds `new` (possibly from an earlier attempt whose reply was lost).
        False: the ref holds something else: someone moved it first. Definitive.
        Raises Unreachable when neither can be established."""
        err: Exception | None = None
        for attempt in range(tries):
            try:
                if self.push([Upd(self.ref(task), expect, new)])[0]:
                    return True
            except (GitError, OSError, TimeoutError) as e:
                err = e
                log.info("push %s: %s (reading back)", task, e)
            try:
                tip = self.tip(task)
            except (GitError, OSError, TimeoutError) as e:
                err = e
                log.info("ls-remote %s: %s", task, e)
                time.sleep(min(2 ** attempt, 10))
                continue
            if tip == new:
                return True  # it landed; only the reply was lost
            if tip != expect:
                return False  # someone else moved it
            time.sleep(min(2 ** attempt, 10))  # not applied, not moved: transient
        raise Unreachable(f"{task}: could not confirm the write: {err}")

    def message(self, ev: Event | None, **extra) -> dict:
        m = {"by": self.who, "nonce": secrets.token_hex(8), **extra}
        if ev is not None:
            m["event"] = event_to_json(ev)
        return m

    def prepare(self, snap: Snap, ev: Event, *, meta: dict | None = None,
                last: dict | None = None) -> Snap | None:
        """Build (locally) the commit for `ev` on top of `snap`; None if the rules deny it.
        Retrying a write must push this same commit, so a lost reply reads back as success."""
        new = apply(snap.status, ev, meta=meta, last=last)
        if new is None:
            return None
        return Snap(snap.task, self.commit(new, snap.rev, self.message(ev)), new)

    def submit(self, snap: Snap, ev: Event, *, meta: dict | None = None,
               last: dict | None = None) -> Submitted:
        """Raises Unreachable if the outcome cannot be established."""
        new = self.prepare(snap, ev, meta=meta, last=last)
        if new is None:
            return Submitted(Result.DENIED)
        if self.cas(snap.task, snap.rev, new.rev):
            return Submitted(Result.WON, new)
        return Submitted(Result.LOST)

    def delete(self, task: str, expect: str) -> bool:
        """Admin only (bypasses the deletion rule). Fences any holder: its next push expects a
        tip that no longer exists."""
        return self.cas(task, expect, None)

    # -- archive -----------------------------------------------------------------------------

    def archive(self, task: str, tip: str, branch: str = "archive") -> str:
        """Keep a task's history reachable after its lock branch is deleted: a commit on the
        archive branch whose second parent is the lock tip. Idempotent."""
        ref = f"refs/heads/{branch}"
        for _ in range(10):
            cur = None
            out = self.git("ls-remote", self.url, ref)
            if out:
                cur = out.split("\t")[0]
                self.git("fetch", "-q", "--no-tags", self.url, f"+{ref}:refs/archive")
                if self.git.run("merge-base", "--is-ancestor", tip, cur).returncode == 0:
                    return cur
            empty = self.git("mktree", input="")
            parents = (["-p", cur] if cur else []) + ["-p", tip]
            msg = json.dumps({"archive": task, "tip": tip, "by": self.who}, sort_keys=True)
            sha = self.git("commit-tree", empty, *parents, "-m", msg)
            if self.push([Upd(ref, cur, sha)])[0]:
                return sha
        raise GitError(["push", ref], 1, "archive: too much contention")

    # -- audit -------------------------------------------------------------------------------

    def audit(self, tip: str) -> list[str]:
        """Is the branch ending at `tip` a legal run from the initial status? Returns the
        violations (empty = legal). Needs the commits locally (`read` or `snapshot` first)."""
        bad = []
        log_out = self.git("log", "--reverse", "--format=%H%x09%P%x09%B%x00", tip)
        prev = None
        for i, rec in enumerate(r for r in log_out.split("\x00") if r.strip()):
            sha, parents, body = rec.strip("\n").split("\t", 2)
            where = f"#{i} {sha[:8]}"
            if len(parents.split()) > 1:
                bad.append(f"{where}: merge commit")
            if self.git("ls-tree", "--name-only", sha) != "status.json":
                bad.append(f"{where}: tree is not exactly status.json")
            try:
                st = self.status_at(sha)
            except Exception as e:  # noqa: BLE001 - any unreadable status is a violation
                bad.append(f"{where}: unreadable status.json: {e}")
                prev = None
                continue
            if prev is None:
                prev = Status.initial(st.task)
            try:
                ev = event_from_json(json.loads(body)["event"])
            except (ValueError, KeyError, TypeError) as e:
                bad.append(f"{where}: no event in commit message: {e}")
                prev = st
                continue
            if step(prev.core, ev) != st.core:
                bad.append(f"{where}: not a legal step: {event_to_json(ev)}")
            prev = st
        return bad
