"""The coordination interface the worker and admin use, with its two backends.

* `GitCoord`: GitHub mode. Lock branches in the lock repo (`lockrepo.py`), tasks and done
  records in the config repo (`configrepo.py`). Task selection happens on the worker.
* `ServerCoord`: server mode. A client for `server.py`; selection happens on the server, and
  a claim returns a task token that is the only way to update the task.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import random
import secrets
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from .configrepo import ConfigRepo, TaskDef, Tree
from .lockrepo import LockRepo, Result, Snap, Unreachable
from .model import (Accept, Cancel, Claim, Expire, Heartbeat, Need, Outcome, Report, Review, Rework,
                    Verdict, claimable)
from .status import Status

log = logging.getLogger(__name__)


@dataclass
class Lease:
    task: str
    epoch: int
    worker: str
    kind: str  # "work" | "review"
    status: Status
    spec_rev: str | None = None
    handle: Any = None  # GitCoord: the Snap of our latest commit; ServerCoord: the task token
    extra: dict = field(default_factory=dict)
    # The heartbeat thread and the attempt both write: serialize them, or a heartbeat landing
    # between the report's read and its push would make the report look like a lost lease.
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    closed: bool = False  # a final update (report / review) landed; heartbeats become no-ops


@dataclass(frozen=True)
class Want:
    """What a worker is willing to claim."""

    worker: str
    gh_user: str | None = None
    kinds: tuple[str, ...] = ("review", "work")
    filters: tuple[str, ...] = ()  # glob patterns on task ids; empty = everything
    allow_self_review: bool = False


def _cooling(st: Status, now: float) -> bool:
    """Advisory backoff after failures: min(5 min * 2^strikes, 6 h) since the last outcome."""
    if not st.core.strikes or not st.last or "at" not in st.last:
        return False
    try:
        at = datetime.strptime(st.last["at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()
    except ValueError:
        return False
    return now - at < min(300 * 2 ** st.core.strikes, 6 * 3600)


def eligible(td: TaskDef, st: Status, want: Want, done: set[str], now: float | None = None,
             closed: set[str] | None = None) -> str | None:
    """The kind of claim `want` could make on this task, or None. `done` satisfies dependencies;
    `closed` (done or cancelled) can never be claimed."""
    if td.id in (closed if closed is not None else done) or st.core.lease is not None:
        return None
    if want.filters and not any(fnmatch.fnmatchcase(td.id, f) for f in want.filters):
        return None
    kind = {Need.WORK: "work", Need.REVIEW: "review"}.get(st.core.need)
    if kind is None or kind not in want.kinds or kind not in td.kinds:
        return None
    if not claimable(st.core, want.worker):
        return None
    if kind == "review" and not want.allow_self_review and want.gh_user:
        if isinstance(st.core.pr, dict) and st.core.pr.get("gh_user") == want.gh_user:
            return None
    if any(d not in done for d in td.deps):
        return None
    if _cooling(st, now if now is not None else time.time()):
        return None
    return kind


def rank(kind: str, st: Status, td: TaskDef) -> tuple:
    """Lower sorts first: review, then continuing work (has a PR), then new work."""
    order = 0 if kind == "review" else (1 if st.core.pr is not None else 2)
    return (order, -td.priority, random.random())


class Fenced(Exception):
    """Our lease is gone (expired, cancelled, task removed, or someone else's write landed).
    Definitive: stop working on this task."""


class Coord:
    """Worker-side operations. Each raises `Fenced` when the lease is definitively lost and
    `Unreachable` when the backend cannot be reached (outcome unknown). `heartbeat` makes one
    attempt; `report` and `review` are final updates and retry with backoff for up to
    `patience` seconds, reusing the same commit / request id so a retry is idempotent."""

    patience: float = 3600
    sleep = staticmethod(time.sleep)

    def claim(self, want: Want, meta: dict) -> Lease | None: ...
    def heartbeat(self, lease: Lease, meta: dict) -> None: ...
    def report(self, lease: Lease, outcome: Outcome, last: dict) -> None: ...
    def review(self, lease: Lease, verdict: Verdict, last: dict) -> None: ...

    def _with_backoff(self, what: str, attempt, final: bool):
        """Run `attempt()`; on Unreachable, retry with exponential backoff if `final`."""
        delay, waited = 5.0, 0.0
        while True:
            try:
                return attempt()
            except Unreachable as e:
                if not final or waited >= self.patience:
                    raise
                log.warning("%s: coordination unreachable (%s); retrying in %.0fs", what, e, delay)
                self.sleep(delay)
                waited += delay
                delay = min(delay * 2, 300)
    def read(self, task: str) -> Status: ...
    def tasks(self) -> Tree: ...


# --------------------------------------------------------------------------------------------
# GitHub mode
# --------------------------------------------------------------------------------------------


class GitCoord(Coord):
    def __init__(self, lock: LockRepo, config: ConfigRepo):
        self.lock = lock
        self.config = config

    def tasks(self) -> Tree:
        return self.config.load(self.config.fetch())

    def read(self, task: str) -> Status:
        return self.lock.read(task).status

    def claim(self, want: Want, meta: dict) -> Lease | None:
        tree = self.tasks()
        snaps = self.lock.snapshot()
        now = time.time()
        cands = []
        for tid, td in tree.tasks.items():
            snap = snaps.get(tid) or Snap(tid, None, Status.initial(tid))
            kind = eligible(td, snap.status, want, tree.done, now, tree.closed)
            if kind:
                cands.append((rank(kind, snap.status, td), kind, td, snap))
        cands.sort(key=lambda c: c[0])
        for _, kind, td, snap in cands:
            m = {**meta, "spec_rev": tree.rev, "spec": td.spec, "kind": kind}
            res = self.lock.submit(snap, Claim(want.worker, snap.status.core.epoch), meta=m)
            if res.result == Result.LOST:
                continue  # someone moved first; try the next one
            if not res.won:
                continue
            if snap.rev is None:
                # Our claim created the branch. The task may have been accepted or cancelled and
                # its branch deleted after we read the config: re-fetch and look for a record.
                if self.config.has_done(self.config.fetch(), td.id):
                    log.info("claimed %s from a stale config: it is closed; stopping", td.id)
                    return None
            new = res.snap
            return Lease(td.id, new.status.core.epoch, want.worker, kind, new.status, tree.rev, new)
        return None

    def _write(self, lease: Lease, ev, *, meta=None, last=None, final: bool = False) -> None:
        with lease.lock:
            if lease.closed:
                if final:
                    raise Fenced(f"{lease.task}: already reported")
                return
            self._write_locked(lease, ev, meta=meta, last=last, final=final)
            lease.closed = final

    def _write_locked(self, lease: Lease, ev, *, meta=None, last=None, final: bool = False) -> None:
        new = self.lock.prepare(lease.handle, ev, meta=meta, last=last)
        if new is None:
            raise Fenced(f"{lease.task}: the rules deny {type(ev).__name__} from our last known state")
        expect = lease.handle.rev
        if not self._with_backoff(lease.task, lambda: self.lock.cas(lease.task, expect, new.rev), final):
            raise Fenced(f"{lease.task}: the lock branch moved (lease expired, cancelled or taken)")
        lease.handle = new
        lease.status = new.status

    def heartbeat(self, lease: Lease, meta: dict) -> None:
        self._write(lease, Heartbeat(lease.worker, lease.epoch), meta=meta)

    def report(self, lease: Lease, outcome: Outcome, last: dict) -> None:
        self._write(lease, Report(lease.worker, lease.epoch, outcome), last=last, final=True)

    def review(self, lease: Lease, verdict: Verdict, last: dict) -> None:
        self._write(lease, Review(lease.worker, lease.epoch, verdict), last=last, final=True)


# --------------------------------------------------------------------------------------------
# Server mode
# --------------------------------------------------------------------------------------------


class ServerError(Unreachable):
    """The server could not be reached, or failed (5xx), after retries."""


class ServerCoord(Coord):
    def __init__(self, url: str, config: ConfigRepo | None = None, timeout: float = 30, tries: int = 6):
        self.url = url.rstrip("/")
        self.config = config
        self.timeout = timeout
        self.tries = tries

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, Any]:
        """JSON over HTTP. Retries transport errors; every mutating request carries an id that
        makes the retry idempotent on the server."""
        data = json.dumps(body).encode() if body is not None else None
        last_err: Exception | None = None
        for attempt in range(self.tries):
            req = urllib.request.Request(self.url + path, data=data, method=method,
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    raw = r.read()
                    return r.status, (json.loads(raw) if raw else None)
            except urllib.error.HTTPError as e:
                raw = e.read()
                if e.code >= 500:
                    last_err = e
                else:
                    return e.code, (json.loads(raw) if raw else None)
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                last_err = e
            time.sleep(min(2 ** attempt * 0.5, 30))
        raise ServerError(f"{method} {path}: {last_err}")

    def tasks(self) -> Tree:
        if self.config is None:
            raise ServerError("no config repo")
        return self.config.load(self.config.fetch())

    def read(self, task: str) -> Status:
        code, body = self.call("GET", f"/tasks?task={urllib.request.quote(task)}")
        if code != 200:
            raise ServerError(f"read {task}: {code} {body}")
        return Status.from_json(body["status"])

    def claim(self, want: Want, meta: dict) -> Lease | None:
        nonce = meta.get("nonce") or secrets.token_hex(8)
        code, body = self.call("POST", "/worker/task-claim", {
            "worker": want.worker, "gh_user": want.gh_user, "kinds": list(want.kinds),
            "filters": list(want.filters), "allow_self_review": want.allow_self_review,
            "meta": {**meta, "nonce": nonce}})
        if code == 204:
            return None
        if code != 200:
            raise ServerError(f"claim: {code} {body}")
        st = Status.from_json(body["status"])
        return Lease(body["task"], body["epoch"], want.worker, body["kind"], st,
                     body.get("spec_rev"), body["token"])

    def _update(self, lease: Lease, kind: str, final: bool = False, **fields) -> None:
        with lease.lock:
            if lease.closed:
                if final:
                    raise Fenced(f"{lease.task}: already reported")
                return
            self._update_locked(lease, kind, final, **fields)
            lease.closed = final

    def _update_locked(self, lease: Lease, kind: str, final: bool, **fields) -> None:
        # one request id for every retry: the server replays its first answer
        req = {"task": lease.task, "token": lease.handle, "kind": kind, "rid": secrets.token_hex(8), **fields}
        code, body = self._with_backoff(lease.task, lambda: self.call("POST", "/worker/task-update", req), final)
        if code in (404, 409):
            raise Fenced(f"{lease.task}: {(body or {}).get('error')}")
        if code != 200:
            raise ServerError(f"update {lease.task}: {code} {body}")
        lease.status = Status.from_json(body["status"])

    def heartbeat(self, lease: Lease, meta: dict) -> None:
        self._update(lease, "heartbeat", meta=meta)

    def report(self, lease: Lease, outcome: Outcome, last: dict) -> None:
        self._update(lease, "report", final=True, outcome=outcome.to_json(), last=last)

    def review(self, lease: Lease, verdict: Verdict, last: dict) -> None:
        self._update(lease, "review", final=True, verdict=verdict.value, last=last)


# --------------------------------------------------------------------------------------------
# Admin side
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class AdminView:
    """One task as the admin sees it. `rev` is what an admin write must expect."""

    task: str
    rev: Any
    status: Status


class AdminCoord:
    def snapshot(self) -> dict[str, AdminView]: ...
    def write(self, view: AdminView, ev, last: dict) -> bool: ...
    def retire(self, view: AdminView, record: dict, grace_elapsed: bool) -> None: ...


class GitAdmin(AdminCoord):
    def __init__(self, lock: LockRepo, config: ConfigRepo):
        self.lock = lock
        self.config = config

    def snapshot(self) -> dict[str, AdminView]:
        return {t: AdminView(t, s.rev, s.status) for t, s in self.lock.snapshot().items()}

    def write(self, view: AdminView, ev, last: dict) -> bool:
        try:
            return self.lock.submit(Snap(view.task, view.rev, view.status), ev, last=last).won
        except Unreachable as e:
            log.warning("%s: %s", view.task, e)
            return False

    def delete(self, view: AdminView) -> bool:
        return self.lock.delete(view.task, view.rev)


class ServerAdmin(AdminCoord):
    def __init__(self, client: ServerCoord):
        self.client = client

    def snapshot(self) -> dict[str, AdminView]:
        code, body = self.client.call("GET", "/tasks")
        if code != 200:
            raise ServerError(f"tasks: {code} {body}")
        return {t["task"]: AdminView(t["task"], t["rev"], Status.from_json(t["status"]))
                for t in body["tasks"]}

    def write(self, view: AdminView, ev, last: dict) -> bool:
        match ev:
            case Accept():
                path, body = "/admin/task-accepted", {}
            case Rework():
                path, body = "/admin/task-rework", {}
            case Expire(e):
                path, body = "/admin/task-expire", {"epoch": e}
            case Cancel():
                path, body = "/admin/task-cancelled", {}
            case _:
                raise TypeError(ev)
        code, _ = self.client.call("POST", path, {"task": view.task, "rev": view.rev, "last": last,
                                                   "rid": secrets.token_hex(8), **body})
        return code == 200

    def sync_tasks(self, tree: Tree) -> None:
        code, body = self.client.call("PUT", "/admin/tasks", {
            "rev": tree.rev,
            "tasks": [{"id": td.id, "spec": td.spec, "deps": list(td.deps), "kinds": list(td.kinds),
                       "priority": td.priority} for td in tree.tasks.values()],
        })
        if code != 200:
            raise ServerError(f"sync tasks: {code} {body}")


def with_meta(meta: dict, **kw) -> dict:
    return {**meta, **kw}


__all__ = ["AdminCoord", "AdminView", "Coord", "Fenced", "GitAdmin", "GitCoord", "Lease",
           "ServerAdmin", "ServerCoord", "ServerError", "Unreachable", "Want", "eligible", "rank", "replace"]
