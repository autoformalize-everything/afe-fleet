"""End to end: workers and the admin on local repositories, a fake agent and a fake forge."""

from dataclasses import replace

import pytest
from conftest import FakeForge, World, edit_repo, git

from afe_fleet.admin import Admin
from afe_fleet.configrepo import ConfigRepo
from afe_fleet.coord import GitAdmin, GitCoord, ServerAdmin, ServerCoord
from afe_fleet.lockrepo import LockRepo
from afe_fleet.model import Need
from afe_fleet.server import Server
from afe_fleet.worker import Worker


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make_worker(world, user, mode="work", forge=None, **host_kw):
    loaded = world.loaded()
    cfg = ConfigRepo(loaded.config.url, world.root / "stores" / f"cfg-{user}")
    if loaded.fleet.mode == "github":
        coord = GitCoord(LockRepo(str(world.lock_repo), world.root / "stores" / f"lock-{user}", who=user), cfg)
    else:
        coord = ServerCoord(loaded.fleet.url, cfg, tries=2)
    w = Worker(replace(loaded, config=cfg), world.host(user, mode, **host_kw), coord, forge or FakeForge(),
               state_dir=world.root / "state" / user)
    return w


def make_admin(world, forge, clock=None):
    loaded = world.loaded()
    if loaded.fleet.mode == "github":
        coord = GitAdmin(LockRepo(str(world.lock_repo), world.root / "stores" / "lock-admin", who="admin"),
                         loaded.config)
    else:
        coord = ServerAdmin(ServerCoord(loaded.fleet.url, loaded.config, tries=2))
    return Admin(loaded, coord, forge, clock=clock or Clock())


def lock_branches(world):
    out = git("for-each-ref", "--format=%(refname)", "refs/heads/task-lock/", cwd=world.lock_repo)
    return out.splitlines()


def status(world, task):
    return LockRepo(str(world.lock_repo), world.root / "stores" / "reader", who="reader").read(task).status


def test_github_mode_full_lifecycle(world):
    forge = FakeForge()
    alice = make_worker(world, "alice", "work", forge)
    bob = make_worker(world, "bob", "approve", forge)
    admin = make_admin(world, forge)

    assert alice.run_once()  # demo/a (demo/b waits for it)
    st = status(world, "demo/a")
    assert st.core.need is Need.REVIEW and st.core.lease is None
    pr = st.core.pr
    assert pr["branch"] == "task/demo/a/e1" and pr["spec"] and pr["gh_user"] == "alice"
    assert git("show", f"{pr['branch']}:solution.txt", cwd=world.code_repo) == "ok"

    assert not alice.run_once()  # her own PR: no self-review; demo/b blocked by its dependency
    assert bob.run_once()  # reviews demo/a
    st = status(world, "demo/a")
    assert st.core.need is Need.MERGE and st.last["reviewed_sha"] == pr["head_sha"]
    assert forge.reviews[0][2] == "approve"

    assert any("merged and accepted" in a for a in admin.sweep())
    assert status(world, "demo/a").core.need is Need.DONE
    assert any("wrote done/demo/a.json" in a for a in admin.sweep())
    assert any("deleted lock branch" in a for a in admin.sweep())  # grace is 0 in the test fleet
    assert lock_branches(world) == []
    assert git("cat-file", "-e", "main:done/demo/a.json", cwd=world.config_repo) == ""
    assert '"archive": "demo/a"' in git("log", "--format=%B", "archive", cwd=world.lock_repo)
    # the archived history is the whole task: claim, report, claim, review, accept
    tip = git("log", "-1", "--format=%P", "archive", cwd=world.lock_repo).split()[-1]
    assert len(git("rev-list", tip, cwd=world.lock_repo).splitlines()) == 5

    assert alice.run_once()  # now demo/b
    assert status(world, "demo/b").core.need is Need.REVIEW
    assert not alice.run_once()  # demo/a is done for good, even with its branch gone


def test_failure_counts_a_strike_and_cools_down(world):
    w = make_worker(world, "carol", "nothing")
    assert w.run_once()
    st = status(world, "demo/a")
    assert st.core.need is Need.WORK and st.core.strikes == 1 and st.core.pr is None
    assert st.last["outcome"] == "failure" and st.last["debug"]["detail"] == "could not do it"
    assert not w.run_once()  # backoff after a failure


def test_verify_failure_gets_a_repair_turn(world):
    w = make_worker(world, "dan", "fix")
    assert w.run_once()
    st = status(world, "demo/a")
    assert st.core.need is Need.REVIEW, st.last
    assert st.last["agent"]["turns"] == 2


def test_uncommitted_work_is_committed_by_the_worker(world):
    w = make_worker(world, "erin", "uncommitted")
    assert w.run_once()
    pr = status(world, "demo/a").core.pr
    assert git("show", f"{pr['branch']}:solution.txt", cwd=world.code_repo) == "ok"


def test_changes_requested_then_fixed_continuing_from_the_pr(world):
    forge = FakeForge()
    a = make_worker(world, "alice", "work", forge)
    b = make_worker(world, "bob", "changes", forge)
    assert a.run_once()
    assert b.run_once()
    st = status(world, "demo/a")
    assert st.core.need is Need.WORK and st.rounds == 1 and st.last["outcome"] == "changes"
    first_pr = st.core.pr
    assert a.run_once()  # continues from the previous PR branch
    st = status(world, "demo/a")
    assert st.core.need is Need.REVIEW and st.core.pr["branch"] == f"task/demo/a/e{st.core.epoch}"
    log = git("log", "--format=%H", st.core.pr["branch"], cwd=world.code_repo)
    assert first_pr["head_sha"] in log


def test_admin_expires_a_silent_lease_on_its_own_clock(world):
    loaded = world.loaded()
    coord = GitCoord(LockRepo(str(world.lock_repo), world.root / "stores" / "z", who="z"),
                     ConfigRepo(loaded.config.url, world.root / "stores" / "cfg-z"))
    from afe_fleet.coord import Want
    lease = coord.claim(Want("z", kinds=("work",)), {"nonce": "n"})
    clock = Clock()
    admin = make_admin(world, FakeForge(), clock)
    admin.fleet.heartbeat = 1e9  # no polling-gap resets in this test
    assert admin.sweep() == []  # first sighting starts the timer
    clock.t += admin.fleet.ttl - 1
    assert admin.sweep() == []
    clock.t += 2
    assert any("expired z" in a for a in admin.sweep())
    st = status(world, lease.task)
    assert st.core.lease is None and st.core.strikes == 1


def test_admin_reworks_on_red_ci_and_closed_pr(world):
    forge = FakeForge()
    assert make_worker(world, "alice", "work", forge).run_once()
    assert make_worker(world, "bob", "approve", forge).run_once()
    forge.checks = "failure"
    acts = make_admin(world, forge).sweep()
    assert any("CI failed; rework" in a for a in acts)
    assert status(world, "demo/a").core.need is Need.WORK


def test_admin_cancel_and_task_removal(world):
    forge = FakeForge()
    w = make_worker(world, "alice", "work", forge)
    admin = make_admin(world, forge)
    acts = admin.cancel("demo/b", reason="not needed")
    assert acts[0] == "demo/b: cancelled"
    assert status(world, "demo/b").core.need is Need.CANCELLED
    assert git("cat-file", "-e", "main:cancelled/demo/b.json", cwd=world.config_repo) == ""
    assert any("deleted lock branch" in a for a in admin.sweep())
    assert w.run_once() and status(world, "demo/a").core.need is Need.REVIEW
    assert not w.run_once()  # demo/b is cancelled

    edit_repo(world.config_repo, {"tasks/demo/a/TASK.md": None})  # a human removes demo/a
    acts = admin.sweep()
    assert any("removed from the tree; cancelled" in a for a in acts)
    assert lock_branches(world) == []


def test_worker_restart_releases_its_lease(world):
    w = make_worker(world, "fran", "work")
    from afe_fleet.coord import Want
    lease = w.coord.claim(Want(w.worker_id, kinds=("work",)), {"nonce": "nn", "worker": w.worker_id})
    w.journal.write(step="prepared", task=lease.task, epoch=lease.epoch, kind="work", nonce="nn", token=None)
    w2 = make_worker(world, "fran", "work")  # a fresh process, same worker id
    w2.recover()
    st = status(world, lease.task)
    assert st.core.lease is None and st.last["debug"]["kind"] == "infra"
    assert w2.journal.read() is None


def test_server_mode_full_lifecycle(tmp_path):
    srv = Server(tmp_path / "s.sqlite", "127.0.0.1", 0, ttl=30, sweep=1).start()
    try:
        world = World(tmp_path / "w", mode="server", url=srv.url).build()
        forge = FakeForge()
        admin = make_admin(world, forge)
        admin.sweep()  # publishes the task tree to the server
        alice = make_worker(world, "alice", "work", forge)
        bob = make_worker(world, "bob", "approve", forge)
        assert alice.run_once()
        assert not alice.run_once()
        assert bob.run_once()
        assert any("merged and accepted" in a for a in admin.sweep())
        c = ServerCoord(srv.url)
        assert c.read("demo/a").core.need is Need.DONE
        assert alice.run_once()  # dependency satisfied
        assert c.read("demo/b").core.need is Need.REVIEW
        edit_repo(world.config_repo, {"tasks/demo/b/TASK.md": None})
        admin.sweep()
        assert c.read("demo/b").core.need is Need.CANCELLED
    finally:
        srv.stop()


@pytest.mark.parametrize("mode", ["hang"])
def test_stalled_agent_is_killed_and_reported(world, mode):
    w = make_worker(world, "gus", mode)
    w.host.agent.stall = 1.5
    assert w.run_once()
    st = status(world, "demo/a")
    assert st.core.lease is None and st.core.strikes == 1
    assert st.last["agent"]["reason"] == "stall"
