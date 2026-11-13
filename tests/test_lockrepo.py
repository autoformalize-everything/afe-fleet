"""GitHub mode backend against local bare repositories: CAS, races, fencing, audit, retiring."""

import json
import threading

from conftest import git

from afe_fleet.coord import Fenced, GitAdmin, GitCoord, Want
from afe_fleet.lockrepo import LockRepo, Result, Snap
from afe_fleet.model import Cancel, Claim, Expire, Heartbeat, Need, Outcome, Report, Review, Verdict
from afe_fleet.status import Status


def lock(world, who, name=None):
    return LockRepo(str(world.lock_repo), world.root / "stores" / (name or who), who=who)


def test_missing_branch_is_unclaimed_and_first_claim_creates_it(world):
    a = lock(world, "a")
    s = a.read("x")
    assert s.rev is None and s.status == Status.initial("x")
    r = a.submit(s, Claim("a", 0), meta={"nonce": "n"})
    assert r.won and a.tip("x") == r.snap.rev
    assert a.read("x").status.core.lease == "a"
    assert a.audit(r.snap.rev) == []


def test_stale_write_rejected_and_denied_event_not_pushed(world):
    a, b = lock(world, "a"), lock(world, "b")
    s = a.read("x")
    assert a.submit(s, Claim("a", 0)).won
    assert b.submit(s, Claim("b", 0)).result == Result.LOST  # same snapshot, second writer
    fresh = b.read("x")
    before = b.tip("x")
    assert b.submit(fresh, Claim("b", 1)).result == Result.DENIED  # leased: the rules say no
    assert b.tip("x") == before


def test_concurrent_claims_have_one_winner(world):
    clients = [lock(world, f"c{i}") for i in range(8)]
    snaps = [c.read("race") for c in clients]
    results = [None] * 8
    go = threading.Barrier(8)

    def run(i):
        go.wait()
        results[i] = clients[i].submit(snaps[i], Claim(f"c{i}", 0)).result

    ts = [threading.Thread(target=run, args=(i,)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert results.count(Result.WON) == 1 and results.count(Result.LOST) == 7


def test_zombie_fenced_after_expire(world):
    w, admin = lock(world, "w"), lock(world, "admin")
    w.submit(w.read("x"), Claim("w", 0))
    held = w.read("x")
    assert admin.submit(admin.read("x"), Expire(1)).won
    assert w.submit(held, Report("w", 1, Outcome("success", {"n": 1}))).result == Result.LOST
    assert w.submit(w.read("x"), Report("w", 1, Outcome("success", {"n": 1}))).result == Result.DENIED


def test_audit_flags_forged_commit(world):
    a = lock(world, "a")
    r = a.submit(a.read("x"), Claim("a", 0))
    forged = a.commit(Status("x", r.snap.status.core.__class__(Need.MERGE, 1, None, {"n": 1}, "a", 0)),
                      r.snap.rev, a.message(Heartbeat("a", 1)))
    assert a.cas("x", r.snap.rev, forged)
    bad = a.audit(forged)
    assert len(bad) == 1 and "not a legal step" in bad[0]


def test_cancel_while_leased_fails_the_heartbeat_before_and_after_deletion(world):
    """The admin cancels, records and deletes; the holder's heartbeat fails every time."""
    from afe_fleet.configrepo import ConfigRepo
    wl = lock(world, "w")
    cfg = ConfigRepo(str(world.config_repo), world.root / "stores" / "cfg-w")
    coord = GitCoord(wl, cfg)
    lease = coord.claim(Want("w", kinds=("work",)), {"nonce": "n1"})
    assert lease is not None and lease.task == "demo/a"
    coord.heartbeat(lease, {"heartbeat_at": "t"})

    admin = GitAdmin(lock(world, "admin"), ConfigRepo(str(world.config_repo), world.root / "stores" / "cfg-a"))
    v = admin.snapshot()["demo/a"]
    assert admin.write(v, Cancel(), {"outcome": "cancelled"})
    try:
        coord.heartbeat(lease, {"heartbeat_at": "t2"})
        raise AssertionError("heartbeat after cancel should fail")
    except Fenced:
        pass
    v = admin.snapshot()["demo/a"]
    assert v.status.core.need is Need.CANCELLED
    assert admin.delete(v)
    try:
        coord.heartbeat(lease, {"heartbeat_at": "t3"})
        raise AssertionError("heartbeat after deletion should fail")
    except Fenced:
        pass
    assert wl.tip("demo/a") is None  # and the branch was not recreated


def test_plain_push_would_recreate_deleted_branch(world):
    """Why every write uses --force-with-lease (DESIGN.md section 4, F2)."""
    w, admin = lock(world, "w"), lock(world, "admin")
    w.submit(w.read("x"), Claim("w", 0))
    held = w.read("x")
    tip = admin.read("x").rev
    assert admin.delete("x", tip)
    zombie = w.commit(held.status, held.rev, w.message(Heartbeat("w", 1)))
    assert not w.cas("x", held.rev, zombie)  # lease push: rejected
    git("push", "-q", str(world.lock_repo), f"{zombie}:refs/heads/task-lock/x", cwd=w.git.dir)
    assert w.tip("x") == zombie  # a plain push recreates it


def test_archive_keeps_history_reachable(world):
    a = lock(world, "a")
    r = a.submit(a.read("x"), Claim("a", 0))
    arch = a.archive("x", r.snap.rev)
    assert a.archive("x", r.snap.rev) == arch  # idempotent
    assert a.delete("x", r.snap.rev)
    log = git("log", "--format=%H", "refs/heads/archive", cwd=world.lock_repo).splitlines()
    assert r.snap.rev in log
    msg = json.loads(git("log", "-1", "--format=%B", "refs/heads/archive", cwd=world.lock_repo))
    assert msg["archive"] == "x"


def test_review_flow_and_self_review(world):
    a, b = lock(world, "a"), lock(world, "b")
    r = a.submit(a.read("x"), Claim("a", 0))
    r = a.submit(r.snap, Report("a", 1, Outcome("success", {"n": 1})))
    assert r.won and r.snap.status.core.need is Need.REVIEW
    assert a.submit(r.snap, Claim("a", 1)).result == Result.DENIED
    r2 = b.submit(b.read("x"), Claim("b", 1))
    r2 = b.submit(r2.snap, Review("b", 2, Verdict.APPROVE), last={"reviewed_sha": "abc"})
    assert r2.snap.status.core.need is Need.MERGE and r2.snap.status.last["reviewed_sha"] == "abc"
    assert b.audit(r2.snap.rev) == []
    assert isinstance(Snap("x", None, Status.initial("x")), Snap)
