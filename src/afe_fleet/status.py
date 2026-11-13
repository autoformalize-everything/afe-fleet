"""`status.json`: the model's `Core` plus payload.

Layout (docs/DESIGN.md section 3):

    {"v": 1, "task": "...", "need": ..., "epoch": ..., "lease": {"worker": ..., ...} | null,
     "pr": {...} | null, "author": ..., "strikes": ..., "rounds": ..., "last": {...} | null}

`Core.lease` is `lease.worker`. Lease metadata (heartbeat time, note, spec, nonce) and `last`
are payload: the model ignores them, the audit compares only `Core`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from .model import Core, Event, Heartbeat, Need, Review, Verdict, step

STATUS_VERSION = 1


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Status:
    task: str
    core: Core = field(default_factory=Core)
    lease: dict | None = None  # metadata; lease["worker"] == core.lease
    rounds: int = 0  # review rounds that ended in "changes"
    last: dict | None = None  # last outcome / verdict / expiry with debug info

    def to_json(self) -> dict:
        c = self.core.to_json()
        return {
            "v": STATUS_VERSION,
            "task": self.task,
            "need": c["need"],
            "epoch": c["epoch"],
            "lease": self.lease if self.core.lease is not None else None,
            "pr": c["pr"],
            "author": c["author"],
            "strikes": c["strikes"],
            "rounds": self.rounds,
            "last": self.last,
        }

    def dumps(self) -> str:
        return json.dumps(self.to_json(), indent=2, sort_keys=True) + "\n"

    @staticmethod
    def from_json(j: dict) -> Status:
        v = j.get("v", STATUS_VERSION)
        if v != STATUS_VERSION:
            raise UnknownVersion(v)
        lease = j.get("lease")
        core = Core(Need(j["need"]), int(j["epoch"]), lease["worker"] if lease else None,
                    j.get("pr"), j.get("author"), int(j.get("strikes", 0)))
        return Status(j["task"], core, lease, int(j.get("rounds", 0)), j.get("last"))

    @staticmethod
    def loads(s: str | bytes) -> Status:
        return Status.from_json(json.loads(s))

    @staticmethod
    def initial(task: str) -> Status:
        return Status(task)


class UnknownVersion(Exception):
    """A status written by a newer afe-fleet. Stop claiming and update."""


def apply(s: Status, ev: Event, *, meta: dict | None = None, last: dict | None = None) -> Status | None:
    """Apply `ev` to `s`. `meta` is the lease metadata after the event (claim, heartbeat);
    `last` replaces the last-outcome record (report, review, expire, rework, accept)."""
    core = step(s.core, ev)
    if core is None:
        return None
    lease = None
    if core.lease is not None:
        lease = dict(s.lease or {})
        lease.update(meta or {})
        lease["worker"] = core.lease
    rounds = s.rounds
    if isinstance(ev, Review) and ev.v is Verdict.CHANGES:
        rounds += 1
    new_last = s.last
    if last is not None:
        new_last = {"epoch": s.core.epoch, "at": now_iso(), **last}
    if isinstance(ev, Heartbeat):
        new_last = s.last
    return replace(s, core=core, lease=lease, rounds=rounds, last=new_last)
