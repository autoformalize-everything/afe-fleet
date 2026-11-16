"""The event log: what each worker and admin process spends its time on, for timelines.

Every process appends to its own files, `fleets/<fleet>/events/<who>-<UTC date>.jsonl` (`who`
is the worker id, or `admin`), one JSON object per line:

    {"t": 1794873455.123, "w": "alice/box-0", "ph": "B", "span": "verify", "task": "a/b", "epoch": 3}
    {"t": 1794873461.875, "w": "alice/box-0", "ph": "E", "span": "verify", "ok": true}

`ph` is `B`/`E` (a span begins or ends: the end of the latest open span of that name and
`id`, for that `w`) or `i` (an instant). `t` is seconds since the epoch; `w` is the worker
slot the event belongs to. Spans nest by time, per `w`:

    idle(reason)  claim  attempt(task, epoch, kind)
                           fetch  worktree  merge-base  container-start  prepare
                           agent  turn(n)  tool(id, name, detail)  verify(ok)
                           push  pr  report  review-post  cleanup
    admin: sweep(actions)  action(text)

Writing never raises: a full disk loses events, not attempts. `timeline.py` turns the files into
a Chrome trace (Perfetto, chrome://tracing, speedscope) and a summary of where the time went.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger(__name__)


class EventLog:
    """One process's event files; safe to share between its threads."""

    def __init__(self, directory: Path | None, who: str):
        self.directory = directory
        self.who = who
        self._lock = threading.Lock()
        self._warned = False

    def write(self, rec: dict) -> None:
        if self.directory is None:
            return
        line = json.dumps(rec, default=str, separators=(",", ":")) + "\n"
        day = time.strftime("%Y-%m-%d", time.gmtime(rec["t"]))
        name = "".join(c if c.isalnum() or c in "._-" else "_" for c in self.who)
        try:
            with self._lock:
                self.directory.mkdir(parents=True, exist_ok=True)
                with open(self.directory / f"{name}-{day}.jsonl", "a") as f:
                    f.write(line)
        except OSError as e:
            if not self._warned:
                self._warned = True
                log.warning("cannot write the event log in %s: %s", self.directory, e)

    def bind(self, w: str, **fields) -> Events:
        return Events(self, w, fields)


class Events:
    """Events for one worker slot (`w`), with fields added to every event (such as the task)."""

    def __init__(self, log_: EventLog | None, w: str, fields: dict | None = None):
        self.log = log_
        self.w = w
        self.fields = fields or {}

    def bind(self, **fields) -> Events:
        return Events(self.log, self.w, {**self.fields, **fields})

    def emit(self, ph: str, span: str, **fields) -> None:
        if self.log is not None:
            self.log.write({"t": time.time(), "w": self.w, "ph": ph, "span": span, **self.fields, **fields})

    def begin(self, span: str, **fields) -> None:
        self.emit("B", span, **fields)

    def end(self, span: str, **fields) -> None:
        self.emit("E", span, **fields)

    def instant(self, span: str, **fields) -> None:
        self.emit("i", span, **fields)

    @contextmanager
    def span(self, name: str, **fields) -> Iterator[dict]:
        """`with ev.span("verify") as end: ...; end["ok"] = v.ok`: what the block puts in the
        yielded dict goes on the end event; an exception ends the span with `error`."""
        end: dict = {}
        self.begin(name, **fields)
        try:
            yield end
        except BaseException as e:
            end.setdefault("error", type(e).__name__)
            raise
        finally:
            self.end(name, **({"id": fields["id"]} if "id" in fields else {}), **end)


NOWHERE = Events(None, "")  # discards everything: for callers without a log
