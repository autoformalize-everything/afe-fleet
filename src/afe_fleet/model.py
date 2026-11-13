"""The task state machine: a direct port of `step` in docs/design-lean/TaskMachine.lean.

Only the fields the model reasons about live here (`Core`). Everything else a status carries
(lease metadata, last outcome, debug info) is payload; see `status.py`.

JSON encodings match Lean's derived `ToJson`, so traces exported from the Lean model can be
checked against this module directly (tests/test_model.py).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Any


class Need(str, Enum):
    WORK = "work"
    REVIEW = "review"
    MERGE = "merge"
    DONE = "done"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Core:
    """`Coord.Task` in the Lean model."""

    need: Need = Need.WORK
    epoch: int = 0
    lease: Any = None  # worker id of the holder
    pr: Any = None  # PR record (JSON value); opaque to the model
    author: Any = None  # worker id that produced the PR under review
    strikes: int = 0

    def to_json(self) -> dict:
        return {
            "need": self.need.value,
            "epoch": self.epoch,
            "lease": self.lease,
            "pr": self.pr,
            "author": self.author,
            "strikes": self.strikes,
        }

    @staticmethod
    def from_json(j: dict) -> Core:
        return Core(Need(j["need"]), int(j["epoch"]), j.get("lease"), j.get("pr"), j.get("author"),
                    int(j.get("strikes", 0)))


INIT = Core()


class Verdict(str, Enum):
    APPROVE = "approve"
    CHANGES = "changes"
    ABSTAIN = "abstain"


@dataclass(frozen=True)
class Outcome:
    kind: str  # "success" | "part" | "failure"
    pr: Any = None

    def __post_init__(self) -> None:
        if self.kind not in ("success", "part", "failure"):
            raise ValueError(f"bad outcome kind {self.kind!r}")
        if (self.kind == "failure") != (self.pr is None):
            raise ValueError("success/part carry a PR, failure does not")

    def to_json(self) -> Any:
        return "failure" if self.kind == "failure" else {self.kind: {"pr": self.pr}}

    @staticmethod
    def from_json(j: Any) -> Outcome:
        if j == "failure":
            return Outcome("failure")
        (kind, body), = j.items()
        return Outcome(kind, body["pr"])


SUCCESS = "success"
PARTIAL = "part"
FAILURE = "failure"


@dataclass(frozen=True)
class Claim:
    w: Any
    e: int


@dataclass(frozen=True)
class Heartbeat:
    w: Any
    e: int


@dataclass(frozen=True)
class Report:
    w: Any
    e: int
    o: Outcome


@dataclass(frozen=True)
class Review:
    w: Any
    e: int
    v: Verdict


@dataclass(frozen=True)
class Expire:
    e: int


@dataclass(frozen=True)
class Accept:
    pass


@dataclass(frozen=True)
class Rework:
    pass


@dataclass(frozen=True)
class Cancel:
    """Admin: withdraw the task (from work or review). Ends any lease."""


Event = Claim | Heartbeat | Report | Review | Expire | Accept | Rework | Cancel

TERMINAL = (Need.DONE, Need.CANCELLED)


def claimable(t: Core, w: Any) -> bool:
    if t.need is Need.WORK:
        return True
    if t.need is Need.REVIEW:
        return t.author != w
    return False


def step(t: Core, ev: Event) -> Core | None:
    """The state machine. `None` means the event is rejected and nothing changes."""
    match ev:
        case Claim(w, e):
            if t.lease is None and t.epoch == e and claimable(t, w):
                return replace(t, lease=w, epoch=e + 1)
        case Heartbeat(w, e):
            if t.lease == w and t.epoch == e:
                return t
        case Report(w, e, o):
            if t.lease == w and t.epoch == e and t.need is Need.WORK:
                if o.kind == SUCCESS:
                    return replace(t, lease=None, need=Need.REVIEW, pr=o.pr, author=w)
                if o.kind == PARTIAL:
                    return replace(t, lease=None, pr=o.pr)
                return replace(t, lease=None, strikes=t.strikes + 1)
        case Review(w, e, v):
            if t.lease == w and t.epoch == e and t.need is Need.REVIEW:
                if v is Verdict.APPROVE:
                    return replace(t, lease=None, need=Need.MERGE)
                if v is Verdict.CHANGES:
                    return replace(t, lease=None, need=Need.WORK)
                return replace(t, lease=None)
        case Expire(e):
            if t.lease is not None and t.epoch == e:
                return replace(t, lease=None, strikes=t.strikes + 1)
        case Accept():
            if t.need in (Need.REVIEW, Need.MERGE):
                return replace(t, need=Need.DONE, lease=None)
        case Rework():
            if t.need in (Need.REVIEW, Need.MERGE):
                return replace(t, need=Need.WORK, lease=None)
        case Cancel():
            if t.need in (Need.WORK, Need.REVIEW):
                return replace(t, need=Need.CANCELLED, lease=None)
        case _:
            raise TypeError(f"not an event: {ev!r}")
    return None


# --- JSON, in Lean's encoding -------------------------------------------------------------

def event_to_json(ev: Event) -> Any:
    match ev:
        case Claim(w, e):
            return {"claim": {"w": w, "e": e}}
        case Heartbeat(w, e):
            return {"heartbeat": {"w": w, "e": e}}
        case Report(w, e, o):
            return {"report": {"w": w, "e": e, "o": o.to_json()}}
        case Review(w, e, v):
            return {"review": {"w": w, "e": e, "v": v.value}}
        case Expire(e):
            return {"expire": {"e": e}}
        case Accept():
            return "accept"
        case Rework():
            return "rework"
        case Cancel():
            return "cancel"
    raise TypeError(f"not an event: {ev!r}")


def event_from_json(j: Any) -> Event:
    if j == "accept":
        return Accept()
    if j == "rework":
        return Rework()
    if j == "cancel":
        return Cancel()
    if not isinstance(j, dict) or len(j) != 1:
        raise ValueError(f"bad event: {j!r}")
    (kind, b), = j.items()
    match kind:
        case "claim":
            return Claim(b["w"], int(b["e"]))
        case "heartbeat":
            return Heartbeat(b["w"], int(b["e"]))
        case "report":
            return Report(b["w"], int(b["e"]), Outcome.from_json(b["o"]))
        case "review":
            return Review(b["w"], int(b["e"]), Verdict(b["v"]))
        case "expire":
            return Expire(int(b["e"]))
    raise ValueError(f"bad event: {j!r}")


def event_name(ev: Event) -> str:
    j = event_to_json(ev)
    return j if isinstance(j, str) else next(iter(j))
