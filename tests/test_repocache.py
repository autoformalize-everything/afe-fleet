"""The shared clone: worktrees per attempt, locking across threads and processes, cleanup."""

import subprocess
import sys
import threading
import time

import pytest
from conftest import git

from afe_fleet.repocache import LockTimeout, SharedRepo


def test_worktree_on_origin_base_and_cleanup(world):
    repo = SharedRepo(str(world.code_repo))
    repo.fetch()
    wt = repo.add(world.root / "wt1", "refs/remotes/origin/main", branch="afe/t/x/e1", refs=[])
    assert (wt.path / "README.md").read_text() == "code\n"
    # what a verify script does in a worktree: diff against origin/<base>
    assert wt.git("merge-base", "HEAD", "origin/main") == git("rev-parse", "main", cwd=world.code_repo)
    (wt.path / "f.txt").write_text("x\n")
    wt.git("add", "-A")
    wt.git("commit", "-q", "-m", "x")
    assert repo.worktrees() == [str(wt.path)]
    repo.remove(wt)
    assert repo.worktrees() == [] and not wt.path.exists()
    assert repo.git.run("rev-parse", "--verify", "afe/t/x/e1").returncode != 0  # branch gone


def test_same_url_means_same_clone():
    a, b = SharedRepo("git@github.com:o/r.git"), SharedRepo("git@github.com:o/r.git")
    assert a.path == b.path and SharedRepo("git@github.com:o/other.git").path != a.path


def test_concurrent_attempts_share_one_clone(world):
    errors, done = [], []

    def attempt(i):
        try:
            repo = SharedRepo(str(world.code_repo))  # each slot builds its own handle
            repo.fetch()
            ref = f"refs/afe/t/x{i}/prev"
            assert repo.fetch_ref(str(world.code_repo), "main", ref)
            wt = repo.add(world.root / f"wt{i}", ref, branch=f"afe/t/x{i}/e1", refs=[ref])
            (wt.path / f"f{i}.txt").write_text(f"{i}\n")
            wt.git("add", "-A")
            wt.git("commit", "-q", "-m", f"attempt {i}")
            done.append(wt.git("rev-parse", "HEAD"))
            repo.remove(wt)
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and len(set(done)) == 8
    repo = SharedRepo(str(world.code_repo))
    assert repo.worktrees() == []
    assert repo.git("for-each-ref", "--format=%(refname)", "refs/afe/", "refs/heads/") == ""


def test_lock_is_held_across_processes(world, tmp_path):
    repo = SharedRepo(str(world.code_repo), lock_timeout=0.5)
    holder = subprocess.Popen([sys.executable, "-c", (
        "import fcntl, os, sys, time\n"
        f"fd = os.open({str(repo.lockfile)!r}, os.O_RDWR | os.O_CREAT)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX)\n"
        "print('held', flush=True)\n"
        "time.sleep(5)\n")], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        t0 = time.monotonic()
        with pytest.raises(LockTimeout):
            repo.fetch()
        assert time.monotonic() - t0 >= 0.5
    finally:
        holder.kill()
        holder.wait()
    repo.fetch()  # free again


def test_crashed_attempt_is_pruned(world):
    repo = SharedRepo(str(world.code_repo))
    repo.fetch()
    wt = repo.add(world.root / "wt-crash", "refs/remotes/origin/main", branch="afe/t/c/e1")
    import shutil
    shutil.rmtree(wt.path)  # the worker died; nobody called remove()
    repo.prune()
    assert repo.worktrees() == []
    # the same attempt can be set up again, reusing the branch name
    wt2 = repo.add(world.root / "wt-crash", "refs/remotes/origin/main", branch="afe/t/c/e1")
    assert (wt2.path / "README.md").exists()
    repo.remove(wt2)


def test_workers_use_worktrees_and_leave_nothing_behind(world):
    from test_e2e import make_worker
    from conftest import FakeForge
    forge = FakeForge()
    assert make_worker(world, "alice", "work", forge).run_once()
    assert make_worker(world, "bob", "approve", forge).run_once()
    repos = list((world.root.parent / "home" / "data" / "afe-fleet" / "repos").glob("*.git"))
    assert len(repos) == 1  # one shared clone for both workers
    repo = SharedRepo(str(world.code_repo))
    assert repo.path == repos[0]
    assert repo.worktrees() == []
    assert repo.git("for-each-ref", "--format=%(refname)", "refs/afe/", "refs/heads/") == ""


def test_everything_lives_under_the_data_home(world, tmp_path):
    """A full run writes nothing under ~/.cache or ~/.local/state."""
    from test_e2e import make_admin, make_worker
    from conftest import FakeForge
    from afe_fleet.config import data_home, load, source_store
    forge = FakeForge()
    assert make_worker(world, "alice", "work", forge).run_once()
    make_admin(world, forge).sweep()
    load(str(world.config_repo))  # the config-repo mirror
    home = tmp_path / "home"
    assert not (home / "cache").exists() and not (home / "state").exists()
    assert data_home() == home / "data" / "afe-fleet"
    assert source_store(str(world.config_repo)).is_relative_to(data_home() / "mirrors")
    assert (data_home() / "repos").is_dir()


def test_afe_fleet_home_overrides(monkeypatch, tmp_path):
    from afe_fleet.config import data_home, fleet_home
    monkeypatch.setenv("AFE_FLEET_HOME", str(tmp_path / "x"))
    assert data_home() == tmp_path / "x" and fleet_home("f") == tmp_path / "x" / "fleets" / "f"
