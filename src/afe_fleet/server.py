"""Server mode: the coordination server. Stdlib HTTP + SQLite, no authentication.

A claim returns a random task token. Updating a task requires its current token, so only the
current holder can heartbeat, report or review; once the lease ends (report, review, expiry,
accept, rework) the token is dead. That is the server-mode fencing token
(`zombie_fenced` in the Lean model). Admin endpoints are open: the server trusts its network.

    POST /worker/task-claim    {worker, gh_user, kinds, filters, allow_self_review, meta}
                               -> 200 {task, epoch, kind, token, status, spec_rev} | 204
    POST /worker/task-update   {task, token, kind: heartbeat|report|review, rid,
                                meta?, outcome?, verdict?, last?}       -> 200 {status} | 409
    GET  /tasks                -> {tasks: [{task, rev, status, present, spec}]}
    GET  /tasks?task=<id>      -> {task, rev, status}
    GET  /admin/events?after=<seq>&wait=<s>   -> {events: [...], last}
    POST /admin/task-accepted  {task, rev?, last?, rid?}
    POST /admin/task-rework    {task, rev?, last?, rid?}
    POST /admin/task-expire    {task, epoch, rev?, last?, rid?}
    POST /admin/task-cancelled {task, rev?, last?, rid?}
    PUT  /admin/tasks          {rev, tasks: [{id, spec, deps, kinds, priority}]}

Leases expire on the server's own clock: no heartbeat for `ttl` seconds. Timers restart when
the server starts, so a server outage does not expire every lease at once.
Every mutating request is idempotent: claims by `meta.nonce`, other writes by `rid`.
"""

from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .configrepo import TaskDef
from .coord import Want, eligible, rank
from .model import (Accept, Cancel, Claim, Expire, Heartbeat, Need, Outcome, Report, Review, Rework, Verdict,
                    event_to_json)
from .status import Status, apply

log = logging.getLogger(__name__)

SCHEMA = """
create table if not exists tasks(
  id text primary key, spec text, deps text, kinds text, priority integer default 0,
  present integer default 1, rev integer default 0, status text, token text);
create table if not exists events(
  seq integer primary key autoincrement, task text, rev integer, event text, status text, at text);
create table if not exists requests(rid text primary key, code integer, response text, at real);
create table if not exists meta(k text primary key, v text);
"""


class Conflict(Exception):
    def __init__(self, msg: str, status: Status | None = None):
        super().__init__(msg)
        self.status = status


class Store:
    def __init__(self, path: str | Path, ttl: float = 1200):
        self.ttl = ttl
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.execute("pragma journal_mode=wal")
        self.db.execute("pragma synchronous=normal")
        self.db.executescript(SCHEMA)
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.beats: dict[str, float] = {}  # task -> monotonic time of the last sign of life
        now = time.monotonic()
        for (tid,) in self.db.execute("select id from tasks where token is not null"):
            self.beats[tid] = now  # timers restart with the server

    # -- helpers -------------------------------------------------------------------------------

    def _row(self, tid: str) -> sqlite3.Row | None:
        cur = self.db.execute("select id, spec, deps, kinds, priority, present, rev, status, token "
                              "from tasks where id = ?", (tid,))
        return cur.fetchone()

    @staticmethod
    def _status(row) -> Status:
        return Status.loads(row[7]) if row and row[7] else Status.initial(row[0])

    def _taskdef(self, row) -> TaskDef:
        return TaskDef(row[0], row[1] or "", {}, {"priority": row[4]}, tuple(json.loads(row[2] or "[]")),
                       tuple(json.loads(row[3] or '["work","review"]')))

    def _closed(self) -> tuple[set[str], set[str]]:
        """(done, done or cancelled)."""
        needs = {tid: json.loads(st)["need"] for tid, st in
                 self.db.execute("select id, status from tasks where status is not null")}
        done = {t for t, n in needs.items() if n == "done"}
        return done, done | {t for t, n in needs.items() if n == "cancelled"}

    def _save(self, tid: str, rev: int, new: Status, ev, token: str | None) -> int:
        self.db.execute("update tasks set rev = ?, status = ?, token = ? where id = ?",
                        (rev + 1, new.dumps(), token, tid))
        self.db.execute("insert into events(task, rev, event, status, at) values (?,?,?,?,?)",
                        (tid, rev + 1, json.dumps(event_to_json(ev)), new.dumps(),
                         time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
        if token is None:
            self.beats.pop(tid, None)
        self.changed.notify_all()
        return rev + 1

    def _replay(self, rid: str | None) -> tuple[int, Any] | None:
        if not rid:
            return None
        r = self.db.execute("select code, response from requests where rid = ?", (rid,)).fetchone()
        return (r[0], json.loads(r[1])) if r else None

    def _remember(self, rid: str | None, code: int, resp: Any) -> None:
        if rid:
            self.db.execute("insert or replace into requests values (?,?,?,?)",
                            (rid, code, json.dumps(resp), time.time()))

    # -- worker --------------------------------------------------------------------------------

    def claim(self, body: dict) -> tuple[int, Any]:
        want = Want(body["worker"], body.get("gh_user"), tuple(body.get("kinds") or ("review", "work")),
                    tuple(body.get("filters") or ()), bool(body.get("allow_self_review")))
        meta = dict(body.get("meta") or {})
        nonce = meta.get("nonce")
        with self.lock:
            self.db.execute("begin immediate")
            try:
                if nonce:  # a retry of a claim that already succeeded
                    for row in self.db.execute("select id, spec, deps, kinds, priority, present, rev, status, "
                                               "token from tasks where token is not null").fetchall():
                        st = self._status(row)
                        if st.lease and st.lease.get("nonce") == nonce and st.core.lease == want.worker:
                            self.db.execute("commit")
                            return 200, self._lease_response(row, st)
                done, closed = self._closed()
                cands = []
                for row in self.db.execute("select id, spec, deps, kinds, priority, present, rev, status, "
                                           "token from tasks where present = 1").fetchall():
                    td, st = self._taskdef(row), self._status(row)
                    kind = eligible(td, st, want, done, closed=closed)
                    if kind:
                        cands.append((rank(kind, st, td), kind, row, st))
                if not cands:
                    self.db.execute("commit")
                    return 204, None
                cands.sort(key=lambda c: c[0])
                _, kind, row, st = cands[0]
                token = secrets.token_urlsafe(24)
                m = {**meta, "spec": row[1], "kind": kind, "gh_user": want.gh_user}
                ev = Claim(want.worker, st.core.epoch)
                new = apply(st, ev, meta=m)
                assert new is not None
                self._save(row[0], row[6], new, ev, token)
                self.beats[row[0]] = time.monotonic()
                self.db.execute("commit")
                return 200, self._lease_response(self._row(row[0]), new)
            except BaseException:
                self.db.execute("rollback")
                raise

    def _lease_response(self, row, st: Status) -> dict:
        spec_rev = self.db.execute("select v from meta where k = 'spec_rev'").fetchone()
        return {"task": row[0], "epoch": st.core.epoch, "kind": (st.lease or {}).get("kind", "work"),
                "token": row[8], "status": st.to_json(), "spec_rev": spec_rev[0] if spec_rev else None}

    def update(self, body: dict) -> tuple[int, Any]:
        tid, token, kind = body.get("task"), body.get("token"), body.get("kind")
        with self.lock:
            if (r := self._replay(body.get("rid"))) is not None:
                return r
            row = self._row(tid)
            if row is None:
                return 404, {"error": f"no task {tid}"}
            st = self._status(row)
            if not token or row[8] != token or st.core.lease is None:
                return 409, {"error": "not the lease holder", "status": st.to_json()}
            w, e = st.core.lease, st.core.epoch
            meta = body.get("meta")
            last = body.get("last")
            match kind:
                case "heartbeat":
                    ev = Heartbeat(w, e)
                case "report":
                    ev = Report(w, e, Outcome.from_json(body["outcome"]))
                case "review":
                    ev = Review(w, e, Verdict(body["verdict"]))
                case _:
                    return 400, {"error": f"bad kind {kind!r}"}
            new = apply(st, ev, meta=meta, last=last)
            if new is None:
                return 409, {"error": "rejected by the state machine", "status": st.to_json()}
            self.db.execute("begin immediate")
            self._save(tid, row[6], new, ev, token if new.core.lease is not None else None)
            if kind == "heartbeat":
                self.beats[tid] = time.monotonic()
            resp = {"status": new.to_json()}
            self._remember(body.get("rid"), 200, resp)
            self.db.execute("commit")
            return 200, resp

    # -- admin ---------------------------------------------------------------------------------

    def admin(self, which: str, body: dict) -> tuple[int, Any]:
        with self.lock:
            if (r := self._replay(body.get("rid"))) is not None:
                return r
            row = self._row(body.get("task", ""))
            if row is None:
                return 404, {"error": "no such task"}
            if body.get("rev") is not None and int(body["rev"]) != row[6]:
                return 409, {"error": "stale rev", "status": self._status(row).to_json(), "rev": row[6]}
            st = self._status(row)
            ev = {"accept": Accept(), "rework": Rework(), "cancel": Cancel(),
                  "expire": Expire(int(body.get("epoch", -1)))}[which]
            new = apply(st, ev, last=body.get("last") or {"outcome": which})
            if new is None:
                return 409, {"error": "rejected by the state machine", "status": st.to_json()}
            self.db.execute("begin immediate")
            rev = self._save(row[0], row[6], new, ev, row[8] if new.core.lease is not None else None)
            resp = {"status": new.to_json(), "rev": rev}
            self._remember(body.get("rid"), 200, resp)
            self.db.execute("commit")
            return 200, resp

    def sync_tasks(self, body: dict) -> tuple[int, Any]:
        """Publish the task tree. Tasks removed from it are cancelled (which fences a holder); a
        changed task definition fences its holder and sends a PR written against the old one back."""
        tasks = {t["id"]: t for t in body.get("tasks", [])}
        with self.lock:
            self.db.execute("begin immediate")
            existing = {r[0]: r for r in self.db.execute(
                "select id, spec, deps, kinds, priority, present, rev, status, token from tasks").fetchall()}
            for tid, t in tasks.items():
                vals = (t.get("spec", ""), json.dumps(t.get("deps", [])),
                        json.dumps(t.get("kinds", ["work", "review"])), int(t.get("priority", 0)))
                if tid not in existing:
                    self.db.execute("insert into tasks(id, spec, deps, kinds, priority, present, rev, status, token) "
                                    "values (?,?,?,?,?,1,0,null,null)", (tid, *vals))
                    continue
                row = existing[tid]
                self.db.execute("update tasks set spec=?, deps=?, kinds=?, priority=?, present=1 where id=?",
                                (*vals, tid))
                if row[1] != vals[0]:
                    self._respec(self._row(tid), vals[0])
            for tid, row in existing.items():
                if tid not in tasks and row[5]:
                    self.db.execute("update tasks set present = 0 where id = ?", (tid,))
                    st = self._status(row)
                    new = apply(st, Cancel(), last={"outcome": "cancelled", "reason": "task removed from the tree"})
                    if new is not None:
                        self._save(tid, row[6], new, Cancel(), None)
            if body.get("rev"):
                self.db.execute("insert or replace into meta values ('spec_rev', ?)", (body["rev"],))
            self.db.execute("commit")
        return 200, {"tasks": len(tasks)}

    def _respec(self, row, spec: str) -> None:
        st = self._status(row)
        if st.core.lease is not None and (st.lease or {}).get("spec") != spec:
            ev = Expire(st.core.epoch)
            self._save(row[0], row[6], apply(st, ev, last={"outcome": "expired", "reason": "spec-changed"}), ev, None)
        elif st.core.need in (Need.REVIEW, Need.MERGE) and isinstance(st.core.pr, dict) \
                and st.core.pr.get("spec") not in (None, spec):
            ev = Rework()
            self._save(row[0], row[6], apply(st, ev, last={"outcome": "rework", "reason": "spec-changed"}), ev, None)

    def expire_stale(self) -> list[str]:
        """Expire leases with no sign of life for `ttl` seconds (server clock)."""
        now = time.monotonic()
        out = []
        with self.lock:
            for tid, beat in list(self.beats.items()):
                if now - beat < self.ttl:
                    continue
                row = self._row(tid)
                st = self._status(row)
                if st.core.lease is None:
                    self.beats.pop(tid, None)
                    continue
                ev = Expire(st.core.epoch)
                new = apply(st, ev, last={"outcome": "expired", "reason": "no heartbeat",
                                          "worker": st.core.lease})
                self.db.execute("begin immediate")
                self._save(tid, row[6], new, ev, None)
                self.db.execute("commit")
                out.append(tid)
        return out

    # -- reads ---------------------------------------------------------------------------------

    def tasks(self, task: str | None) -> tuple[int, Any]:
        with self.lock:
            if task:
                row = self._row(task)
                if row is None:
                    return 404, {"error": "no such task"}
                return 200, {"task": task, "rev": row[6], "status": self._status(row).to_json()}
            rows = self.db.execute(
                "select id, spec, deps, kinds, priority, present, rev, status, token from tasks order by id").fetchall()
            return 200, {"tasks": [{"task": r[0], "rev": r[6], "present": bool(r[5]), "spec": r[1],
                                    "status": self._status(r).to_json()} for r in rows]}

    def events(self, after: int, wait: float) -> tuple[int, Any]:
        deadline = time.monotonic() + max(0.0, min(wait, 60))
        with self.changed:
            while True:
                rows = self.db.execute("select seq, task, rev, event, status, at from events where seq > ? "
                                       "order by seq limit 1000", (after,)).fetchall()
                remaining = deadline - time.monotonic()
                if rows or remaining <= 0:
                    break
                self.changed.wait(remaining)
        evs = [{"seq": r[0], "task": r[1], "rev": r[2], "event": json.loads(r[3]),
                "status": json.loads(r[4]), "at": r[5]} for r in rows]
        return 200, {"events": evs, "last": evs[-1]["seq"] if evs else after}


def make_handler(store: Store):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "afe-fleet"

        def log_message(self, fmt: str, *args) -> None:  # route through logging
            log.debug("%s %s", self.address_string(), fmt % args)

        def _send(self, code: int, body: Any) -> None:
            raw = b"" if body is None else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            if raw:
                self.wfile.write(raw)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n)) if n else {}

        def _dispatch(self, method: str) -> None:
            u = urlparse(self.path)
            q = parse_qs(u.query)
            try:
                body = self._body() if method in ("POST", "PUT") else {}
                match method, u.path:
                    case "GET", "/tasks":
                        code, resp = store.tasks(q.get("task", [None])[0])
                    case "GET", "/admin/events":
                        code, resp = store.events(int(q.get("after", ["0"])[0]), float(q.get("wait", ["0"])[0]))
                    case "GET", "/healthz":
                        code, resp = 200, {"ok": True}
                    case "POST", "/worker/task-claim":
                        code, resp = store.claim(body)
                    case "POST", "/worker/task-update":
                        code, resp = store.update(body)
                    case "POST", "/admin/task-accepted":
                        code, resp = store.admin("accept", body)
                    case "POST", "/admin/task-rework":
                        code, resp = store.admin("rework", body)
                    case "POST", "/admin/task-expire":
                        code, resp = store.admin("expire", body)
                    case "POST", "/admin/task-cancelled":
                        code, resp = store.admin("cancel", body)
                    case "PUT", "/admin/tasks":
                        code, resp = store.sync_tasks(body)
                    case _:
                        code, resp = 404, {"error": f"no route {method} {u.path}"}
            except (KeyError, ValueError, TypeError, json.JSONDecodeError) as e:
                code, resp = 400, {"error": f"bad request: {e}"}
            except Exception as e:  # noqa: BLE001 - never kill the server thread
                log.exception("internal error")
                code, resp = 500, {"error": str(e)}
            self._send(code, resp)

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def do_PUT(self) -> None:
            self._dispatch("PUT")

    return Handler


class Server:
    def __init__(self, db: str | Path, host: str = "127.0.0.1", port: int = 8737, ttl: float = 1200,
                 sweep: float = 5):
        self.store = Store(db, ttl)
        self.httpd = ThreadingHTTPServer((host, port), make_handler(self.store))
        self.httpd.daemon_threads = True
        self.sweep = sweep
        self._stop = threading.Event()

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def _expirer(self) -> None:
        while not self._stop.wait(self.sweep):
            try:
                for tid in self.store.expire_stale():
                    log.info("expired %s (no heartbeat)", tid)
            except Exception:  # noqa: BLE001
                log.exception("expiry sweep failed")

    def start(self) -> Server:
        threading.Thread(target=self._expirer, daemon=True, name="expirer").start()
        threading.Thread(target=self.httpd.serve_forever, daemon=True, name="http").start()
        return self

    def serve_forever(self) -> None:
        threading.Thread(target=self._expirer, daemon=True, name="expirer").start()
        log.info("listening on %s", self.url)
        self.httpd.serve_forever()

    def stop(self) -> None:
        self._stop.set()
        self.httpd.shutdown()
        self.httpd.server_close()
