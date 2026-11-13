"""The admin: a stateless, level-triggered reconciler.

Each sweep derives what to do from what it observes, so a crash anywhere is repaired by the
next sweep, and every write is a compare-and-swap. The only in-memory state is observation
timers (lease TTL, CI timeout); losing them on restart only delays an action.

GitHub mode (docs/DESIGN.md section 6):
  * lease unchanged for TTL across our own polls → expire
  * lease or PR written against an older task definition → expire / rework
  * need = merge → merge when the PR head is the reviewed SHA and CI is green, then accept;
    red CI, conflict, moved head or closed PR → rework
  * need = done / cancelled → write done/ or cancelled/<task>.json, archive, delete the lock
    branch after the grace period
  * lock branch for a task removed from the tree → cancel (fences the holder), archive, delete
  * lock branch for a task with a done/cancelled record but an open status → delete (a claim
    from a stale config)
  * `cancel` (command) → cancel, record, optionally remove the task from the tree
  * write-request issues in the lock repo → grant write access (access.py)
Server mode: the server owns TTL and task-definition changes; the admin syncs the task tree,
merges, and accepts / reworks through the admin endpoints.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field

from .access import AccessRequests
from .config import Loaded
from .configrepo import ConfigRepo, Tree
from .coord import AdminCoord, AdminView, GitAdmin, ServerAdmin
from .forge import Forge, ForgeError
from .gitutil import github_slug
from .model import TERMINAL, Accept, Cancel, Expire, Need, Rework
from .status import now_iso

log = logging.getLogger(__name__)


@dataclass
class Timers:
    lease: dict[str, tuple[object, float]] = field(default_factory=dict)  # task -> (rev, first seen)
    ci: dict[str, tuple[str, float]] = field(default_factory=dict)  # task -> (head sha, first pending)
    last_sweep: float | None = None


class Admin:
    def __init__(self, loaded: Loaded, coord: AdminCoord, forge: Forge, *, clock=time.monotonic,
                 wall=time.time):
        self.loaded = loaded
        self.fleet = loaded.fleet
        self.config: ConfigRepo = loaded.config
        self.coord = coord
        self.forge = forge
        self.clock = clock
        self.wall = wall
        self.t = Timers()
        self.stop = threading.Event()
        self.access = None
        slug = github_slug(self.fleet.lock_repo or "")
        if isinstance(coord, GitAdmin) and slug and self.fleet.access_mode != "off":
            self.access = AccessRequests(forge, slug, self.fleet.access_mode, self.fleet.access_deny)

    def run_forever(self) -> None:
        while not self.stop.is_set():
            try:
                self.sweep()
            except Exception:  # noqa: BLE001 - the loop must survive anything
                log.exception("sweep failed")
            self.stop.wait(self.fleet.poll)

    # -- the sweep -----------------------------------------------------------------------------

    def sweep(self) -> list[str]:
        """One reconciliation pass. Returns a log of the actions taken."""
        acts: list[str] = []
        rev = self.config.fetch()
        tree = self.config.load(rev)
        now = self.clock()
        if self.t.last_sweep is not None and now - self.t.last_sweep > self.fleet.heartbeat:
            log.info("polling gap of %.0fs: restarting lease timers", now - self.t.last_sweep)
            self.t.lease.clear()
        if isinstance(self.coord, ServerAdmin):
            self.coord.sync_tasks(tree)
        views = self.coord.snapshot()
        self.t.last_sweep = self.clock()
        if isinstance(self.coord, GitAdmin) and views and not tree.tasks:
            log.error("the task tree is empty but %d lock branches exist; refusing to act", len(views))
            return acts
        for task, v in sorted(views.items()):
            try:
                acts += self._task(tree, v)
            except ForgeError as e:
                log.warning("%s: forge error: %s", task, e)
        for task in list(self.t.lease):
            if task not in views:
                self.t.lease.pop(task, None)
        if self.access:
            try:
                acts += self.access.sweep()
            except ForgeError as e:
                log.warning("access requests: %s", e)
        for a in acts:
            log.info("%s", a)
        return acts

    def _write(self, v: AdminView, ev, last: dict) -> bool:
        ok = self.coord.write(v, ev, {**last, "by": "admin"})
        if not ok:
            log.info("%s: %s lost a race; next sweep re-reads", v.task, type(ev).__name__)
        return ok

    def _task(self, tree: Tree, v: AdminView) -> list[str]:
        st, task, core = v.status, v.task, v.status.core
        git = isinstance(self.coord, GitAdmin)
        td = tree.tasks.get(task)

        if git:
            if td is None:
                return self._removed(v)
            if task in tree.closed and core.need not in TERMINAL:
                # a claim from a stale config recreated the branch of a closed task
                return [f"{task}: closed task re-claimed from a stale config; deleted"] if self.coord.delete(v) else []

        # -- leases ----------------------------------------------------------------------------
        if core.lease is not None:
            if git and td is not None and (st.lease or {}).get("spec") not in (None, td.spec):
                if self._write(v, Expire(core.epoch), {"outcome": "expired", "reason": "spec-changed"}):
                    return [f"{task}: task definition changed; expired lease of {core.lease}"]
                return []
            if git:
                seen = self.t.lease.get(task)
                if seen is None or seen[0] != v.rev:
                    self.t.lease[task] = (v.rev, self.clock())
                elif self.clock() - seen[1] >= self.fleet.ttl:
                    if self._write(v, Expire(core.epoch), {"outcome": "expired", "reason": "no heartbeat",
                                                           "worker": core.lease}):
                        self.t.lease.pop(task, None)
                        return [f"{task}: no heartbeat for {self.fleet.ttl:.0f}s; expired {core.lease}"]
            return []
        self.t.lease.pop(task, None)

        # -- PRs -------------------------------------------------------------------------------
        pr = core.pr if isinstance(core.pr, dict) else None
        if core.need in (Need.REVIEW, Need.MERGE) and pr:
            if git and td is not None and pr.get("spec") not in (None, td.spec):
                if self._write(v, Rework(), {"outcome": "rework", "reason": "spec-changed"}):
                    return [f"{task}: task definition changed after the PR; rework"]
                return []
            return self._pr(v, pr)

        # -- done / cancelled --------------------------------------------------------------------
        if core.need in TERMINAL and git:
            return self._retire(tree, v)
        return []

    def _removed(self, v: AdminView) -> list[str]:
        """The task is gone from the tree: cancel it (fencing any holder), archive, delete."""
        acts = []
        if v.status.core.need not in TERMINAL:
            if not self._write(v, Cancel(), {"outcome": "cancelled", "reason": "task removed from the tree"}):
                return []
            acts.append(f"{v.task}: removed from the tree; cancelled")
            v = self.coord.snapshot().get(v.task) or v
        self.coord.lock.archive(v.task, v.rev)
        if self.coord.delete(v):
            acts.append(f"{v.task}: archived and deleted lock branch")
        return acts

    def cancel(self, task: str, *, reason: str = "cancelled by the admin", remove: bool = False) -> list[str]:
        """Withdraw a task: cancel it (the holder's next heartbeat fails), record it as cancelled,
        and optionally remove it from the tree. Idempotent."""
        from .status import Status
        acts = []
        views = self.coord.snapshot()
        v = views.get(task)
        if v is None and isinstance(self.coord, GitAdmin):
            v = AdminView(task, None, Status.initial(task))
        if v is None:
            raise ValueError(f"no such task: {task}")
        if v.status.core.need is Need.MERGE:
            raise ValueError(f"{task} is approved and waiting to merge; close its PR (rework) first")
        if v.status.core.need not in TERMINAL:
            if not self._write(v, Cancel(), {"outcome": "cancelled", "reason": reason}):
                raise ValueError(f"{task}: lost a race; try again")
            acts.append(f"{task}: cancelled")
            v = self.coord.snapshot()[task]
        if isinstance(self.coord, GitAdmin):
            self.config.write_done(task, {"task": task, "status": v.status.to_json(), "lock_tip": v.rev,
                                          "at": self.wall(), "reason": reason},
                                   cancelled=v.status.core.need is Need.CANCELLED, remove_task=remove)
            acts.append(f"{task}: recorded{' and removed from the tree' if remove else ''}")
        elif remove:
            acts.append(f"{task}: remove tasks/{task}/ from the config repo yourself (server mode)")
        return acts

    def _pr(self, v: AdminView, pr: dict) -> list[str]:
        task, core = v.task, v.status.core
        info = self.forge.pr_state(pr)
        if info["state"] == "merged":
            if self._write(v, Accept(), {"outcome": "accepted", "merged_sha": info.get("merge_sha")}):
                return [f"{task}: PR merged; accepted"]
            return []
        if info["state"] == "closed":
            if self._write(v, Rework(), {"outcome": "rework", "reason": "PR closed"}):
                return [f"{task}: PR closed without merging; rework"]
            return []
        if core.need is not Need.MERGE:
            return []
        reviewed = (v.status.last or {}).get("reviewed_sha")
        if reviewed and info["head_sha"] != reviewed:
            if self._write(v, Rework(), {"outcome": "rework", "reason": "PR head moved after approval"}):
                return [f"{task}: head moved after approval; rework"]
            return []
        if info["checks"] == "failure" or info.get("mergeable") is False:
            reason = "CI failed" if info["checks"] == "failure" else "merge conflict"
            if self._write(v, Rework(), {"outcome": "rework", "reason": reason}):
                return [f"{task}: {reason}; rework"]
            return []
        if info["checks"] in ("pending", "none") or info.get("mergeable") is None:
            first = self.t.ci.get(task)
            if first is None or first[0] != info["head_sha"]:
                self.t.ci[task] = (info["head_sha"], self.clock())
            elif self.clock() - first[1] > self.fleet.ci_timeout:
                if self._write(v, Rework(), {"outcome": "rework", "reason": "CI timed out"}):
                    self.t.ci.pop(task, None)
                    return [f"{task}: CI timed out; rework"]
            if info["checks"] == "pending" or info.get("mergeable") is None:
                return []
        self.t.ci.pop(task, None)
        if self.forge.merge(pr, info["head_sha"]):
            if self._write(v, Accept(), {"outcome": "accepted", "merged_sha": info["head_sha"]}):
                return [f"{task}: merged and accepted"]
            return [f"{task}: merged; accept will be retried"]
        return []

    def _retire(self, tree: Tree, v: AdminView) -> list[str]:
        task = v.task
        lock = self.coord.lock
        cancelled = v.status.core.need is Need.CANCELLED
        d = "cancelled" if cancelled else "done"
        if task not in (tree.cancelled if cancelled else tree.done):
            self.config.write_done(task, {"task": task, "status": v.status.to_json(), "lock_tip": v.rev,
                                          "at": self.wall()}, cancelled=cancelled)
            lock.archive(task, v.rev)
            return [f"{task}: {d}; wrote {d}/{task}.json and archived"]
        raw = self.config.read_file(tree.rev, f"{d}/{task}.json")
        try:
            at = float(json.loads(raw)["at"])
        except (TypeError, ValueError, KeyError, json.JSONDecodeError):
            at = 0.0
        if self.wall() - at >= self.fleet.done_grace:
            lock.archive(task, v.rev)
            if self.coord.delete(v):
                return [f"{task}: grace period over; deleted lock branch"]
        return []

    # -- STATUS.md -----------------------------------------------------------------------------

    def status_md(self) -> str:
        tree = self.config.load(self.config.fetch())
        views = self.coord.snapshot()
        rows = ["| task | need | lease | PR | strikes |", "|---|---|---|---|---|"]
        for tid in sorted(tree.tasks):
            if tid in tree.closed:
                need, lease, pr, strikes = "done" if tid in tree.done else "cancelled", "", "", ""
            elif tid in views:
                st = views[tid].status
                need, lease = st.core.need.value, st.core.lease or ""
                pr = (st.core.pr or {}).get("url", "") if isinstance(st.core.pr, dict) else ""
                strikes = str(st.core.strikes)
            else:
                need, lease, pr, strikes = "unclaimed", "", "", "0"
            rows.append(f"| `{tid}` | {need} | {lease} | {pr} | {strikes} |")
        return f"# Task status\n\nGenerated by afe-fleet admin at {now_iso()}.\n\n" + "\n".join(rows) + "\n"
