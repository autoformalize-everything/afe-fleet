"""Connectivity failures vs lease failures, and retries of the final update."""

import threading

import pytest

import afe_fleet.lockrepo as lockrepo_mod
from afe_fleet.configrepo import ConfigRepo
from afe_fleet.coord import Fenced, GitAdmin, GitCoord, ServerCoord, Unreachable, Want
from afe_fleet.gitutil import GitError
from afe_fleet.lockrepo import LockRepo
from afe_fleet.model import Cancel, Need, Outcome
from afe_fleet.server import Server


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(lockrepo_mod.time, "sleep", lambda s: None)


def git_coord(world, who):
    lock = LockRepo(str(world.lock_repo), world.root / "stores" / who, who=who)
    return GitCoord(lock, ConfigRepo(str(world.config_repo), world.root / "stores" / f"cfg-{who}"))


def claim(coord, who="w"):
    lease = coord.claim(Want(who, kinds=("work",)), {"nonce": "n"})
    assert lease is not None
    return lease


# -- GitHub mode -----------------------------------------------------------------------------

def test_git_heartbeat_unreachable_is_not_fenced(world):
    c = git_coord(world, "w")
    lease = claim(c)
    good = c.lock.url
    c.lock.url = str(world.root / "nowhere.git")
    with pytest.raises(Unreachable) as e:
        c.heartbeat(lease, {})
    assert not isinstance(e.value, Fenced)
    c.lock.url = good
    c.heartbeat(lease, {})  # the lease was never lost


def test_git_heartbeat_after_cancel_is_fenced_not_unreachable(world):
    c = git_coord(world, "w")
    lease = claim(c)
    admin = GitAdmin(LockRepo(str(world.lock_repo), world.root / "stores" / "admin", who="admin"), c.config)
    assert admin.write(admin.snapshot()[lease.task], Cancel(), {"outcome": "cancelled"})
    with pytest.raises(Fenced):
        c.heartbeat(lease, {})


def test_git_final_report_retries_with_backoff_until_reachable(world):
    c = git_coord(world, "w")
    lease = claim(c)
    good = c.lock.url
    c.lock.url = str(world.root / "nowhere.git")
    waits = []

    def sleep(s):
        waits.append(s)
        if len(waits) == 3:
            c.lock.url = good  # the network comes back

    c.sleep = sleep
    c.report(lease, Outcome("failure"), {"outcome": "failure"})
    assert waits == [5, 10, 20]
    st = c.read(lease.task)
    assert st.core.lease is None and st.core.strikes == 1


def test_git_final_report_gives_up_after_patience(world):
    c = git_coord(world, "w")
    lease = claim(c)
    c.lock.url = str(world.root / "nowhere.git")
    c.sleep = lambda s: None
    c.patience = 60
    with pytest.raises(Unreachable):
        c.report(lease, Outcome("failure"), {"outcome": "failure"})


def test_git_lost_reply_reads_back_as_success(world, monkeypatch):
    """The push lands but its reply is lost: the read-back sees our commit and reports success."""
    c = git_coord(world, "w")
    lease = claim(c)
    real = LockRepo.push
    calls = []

    def flaky(self, updates):
        res = real(self, updates)
        calls.append(res)
        if len(calls) == 1:
            raise GitError(["push"], 128, "connection reset")
        return res

    monkeypatch.setattr(LockRepo, "push", flaky)
    c.report(lease, Outcome("failure"), {"outcome": "failure"})
    assert len(calls) == 1  # no second push needed
    assert c.read(lease.task).core.strikes == 1


# -- server mode -----------------------------------------------------------------------------

@pytest.fixture
def server(tmp_path):
    srv = Server(tmp_path / "s.sqlite", "127.0.0.1", 0, ttl=60, sweep=1).start()
    srv.store.sync_tasks({"tasks": [{"id": "a"}]})
    yield srv
    srv.stop()


def test_server_heartbeat_unreachable_is_not_fenced(server):
    c = ServerCoord(server.url, tries=1)
    lease = c.claim(Want("w"), {"nonce": "n"})
    c.url = "http://127.0.0.1:9"  # nothing listens there
    with pytest.raises(Unreachable) as e:
        c.heartbeat(lease, {})
    assert not isinstance(e.value, Fenced)
    c.url = server.url
    c.heartbeat(lease, {})


def test_server_final_report_retries_same_request(server, monkeypatch):
    c = ServerCoord(server.url, tries=1)
    lease = c.claim(Want("w"), {"nonce": "n"})
    real = ServerCoord.call
    sent = []

    def lossy(self, method, path, body=None):
        out = real(self, method, path, body)
        sent.append(body)
        if len(sent) == 1:  # applied on the server, reply lost
            raise lockrepo_mod.Unreachable("reply lost")
        return out

    monkeypatch.setattr(ServerCoord, "call", lossy)
    c.sleep = lambda s: None
    c.report(lease, Outcome("failure"), {"outcome": "failure"})
    assert len(sent) == 2 and sent[0]["rid"] == sent[1]["rid"]  # retried with the same id
    st = c.read("a")
    assert st.core.strikes == 1 and st.core.need is Need.WORK  # applied once


# -- the worker's heartbeat thread -----------------------------------------------------------

class ScriptedCoord:
    def __init__(self, script):
        self.script = list(script)

    def heartbeat(self, lease, meta):
        what = self.script.pop(0) if self.script else "ok"
        if what == "down":
            raise Unreachable("network down")
        if what == "fenced":
            raise Fenced("cancelled")


def test_worker_keeps_working_while_unreachable_and_stops_when_fenced(world):
    from afe_fleet.coord import Lease
    from afe_fleet.status import Status
    from afe_fleet.worker import Worker
    loaded = world.loaded()
    loaded.fleet.heartbeat = 0.01
    w = Worker(loaded, world.host("u"), ScriptedCoord(["down", "down", "ok", "fenced"]), None,
               state_dir=world.root / "state")
    lease = Lease("a", 1, "u/host", "work", Status.initial("a"))
    cancel = threading.Event()
    t = threading.Thread(target=w._heartbeat, args=(lease, cancel))
    t.start()
    assert cancel.wait(30)
    t.join(5)
    assert lease.extra.get("unreachable_beats") == 2  # two connectivity failures: kept working
    assert lease.extra.get("fenced") is True  # the lease failure stopped it
