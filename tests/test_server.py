"""Server mode: tokens, fencing, idempotency, expiry, task sync, cancel."""

import time

import pytest

from afe_fleet.coord import Fenced, ServerAdmin, ServerCoord, Want
from afe_fleet.model import Cancel, Need, Outcome, Verdict
from afe_fleet.server import Server


@pytest.fixture
def server(tmp_path):
    srv = Server(tmp_path / "s.sqlite", "127.0.0.1", 0, ttl=2, sweep=0.2).start()
    yield srv
    srv.stop()


def sync(client, tasks):
    code, _ = client.call("PUT", "/admin/tasks", {"rev": "r1", "tasks": tasks})
    assert code == 200


def test_claim_returns_token_and_only_holder_can_update(server):
    c = ServerCoord(server.url, tries=2)
    sync(c, [{"id": "a", "spec": "s1"}])
    lease = c.claim(Want("w1"), {"nonce": "n1"})
    assert lease.task == "a" and lease.kind == "work" and lease.handle
    assert c.claim(Want("w2"), {"nonce": "n2"}) is None  # nothing else to claim
    c.heartbeat(lease, {"heartbeat_at": "t"})
    # anyone without the token is refused
    code, body = c.call("POST", "/worker/task-update", {"task": "a", "token": "guess", "kind": "heartbeat"})
    assert code == 409
    c.report(lease, Outcome("success", {"n": 1, "gh_user": "u1"}), {"outcome": "success"})
    # the lease ended, so the token is dead
    code, _ = c.call("POST", "/worker/task-update", {"task": "a", "token": lease.handle, "kind": "heartbeat"})
    assert code == 409
    c.heartbeat(lease, {})  # after the final report, the worker's own heartbeats are no-ops
    with pytest.raises(Fenced):
        c.report(lease, Outcome("failure"), {})  # and a second report is refused


def test_claim_retry_with_same_nonce_returns_same_lease(server):
    c = ServerCoord(server.url, tries=2)
    sync(c, [{"id": "a"}])
    l1 = c.claim(Want("w1"), {"nonce": "same"})
    l2 = c.claim(Want("w1"), {"nonce": "same"})
    assert l1.handle == l2.handle and l1.epoch == l2.epoch


def test_update_retry_is_idempotent(server):
    c = ServerCoord(server.url, tries=2)
    sync(c, [{"id": "a"}])
    lease = c.claim(Want("w1"), {"nonce": "n"})
    body = {"task": "a", "token": lease.handle, "kind": "report", "rid": "r-1", "outcome": "failure",
            "last": {"outcome": "failure"}}
    first = c.call("POST", "/worker/task-update", body)
    again = c.call("POST", "/worker/task-update", body)  # the reply was "lost"; resend
    assert first == again and first[0] == 200


def test_server_expires_silent_leases(server):
    c = ServerCoord(server.url, tries=2)
    sync(c, [{"id": "a"}])
    lease = c.claim(Want("w1"), {"nonce": "n"})
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and c.read("a").core.lease is not None:
        time.sleep(0.2)
    st = c.read("a")
    assert st.core.lease is None and st.core.strikes == 1 and st.last["reason"] == "no heartbeat"
    with pytest.raises(Fenced):
        c.heartbeat(lease, {})


def test_review_selfreview_deps_and_admin(server):
    c = ServerCoord(server.url, tries=2)
    sync(c, [{"id": "a"}, {"id": "b", "deps": ["a"]}])
    w1 = Want("w1", gh_user="alice")
    lease = c.claim(w1, {"nonce": "1"})
    assert lease.task == "a"  # b waits for a
    c.report(lease, Outcome("success", {"n": 1, "gh_user": "alice"}), {"outcome": "success"})
    assert c.claim(Want("w1", gh_user="alice", kinds=("review",)), {"nonce": "2"}) is None
    rv = c.claim(Want("w2", gh_user="bob", kinds=("review",)), {"nonce": "3"})
    c.review(rv, Verdict.APPROVE, {"outcome": "approve", "reviewed_sha": "abc"})
    admin = ServerAdmin(c)
    v = admin.snapshot()["a"]
    assert v.status.core.need is Need.MERGE
    from afe_fleet.model import Accept
    assert admin.write(v, Accept(), {"outcome": "accepted"})
    nxt = c.claim(w1, {"nonce": "4"})
    assert nxt.task == "b"  # dependency satisfied


def test_cancel_fences_holder_and_removal_cancels(server):
    c = ServerCoord(server.url, tries=2)
    sync(c, [{"id": "a"}, {"id": "b"}])
    la = c.claim(Want("w1", filters=("a",)), {"nonce": "1"})
    lb = c.claim(Want("w2", filters=("b",)), {"nonce": "2"})
    admin = ServerAdmin(c)
    assert admin.write(admin.snapshot()["a"], Cancel(), {"outcome": "cancelled"})
    with pytest.raises(Fenced):
        c.heartbeat(la, {})
    sync(c, [{"id": "a"}])  # b removed from the tree → cancelled, holder fenced
    with pytest.raises(Fenced):
        c.heartbeat(lb, {})
    assert c.read("b").core.need is Need.CANCELLED
    assert c.claim(Want("w3"), {"nonce": "3"}) is None  # cancelled tasks are never claimed


def test_spec_change_fences_holder(server):
    c = ServerCoord(server.url, tries=2)
    sync(c, [{"id": "a", "spec": "s1"}])
    lease = c.claim(Want("w1"), {"nonce": "1"})
    sync(c, [{"id": "a", "spec": "s2"}])
    with pytest.raises(Fenced):
        c.heartbeat(lease, {})
    assert c.read("a").last["reason"] == "spec-changed"


def test_events_feed(server):
    c = ServerCoord(server.url, tries=2)
    sync(c, [{"id": "a"}])
    c.claim(Want("w1"), {"nonce": "1"})
    code, body = c.call("GET", "/admin/events?after=0")
    assert code == 200 and body["events"][0]["event"] == {"claim": {"w": "w1", "e": 0}}
    code, body = c.call("GET", f"/admin/events?after={body['last']}&wait=0.3")
    assert body["events"] == []
