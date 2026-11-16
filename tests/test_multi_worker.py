"""Several worker processes in one AFE_FLEET_HOME: per-worker state, one process per worker id."""

import os
import subprocess
import sys

import pytest

from afe_fleet.config import AlreadyRunning, hold_lock, worker_home
from afe_fleet.worker import Worker


class NoCoord:
    def claim(self, *a):
        return None


def test_two_workers_in_one_home_keep_their_state_apart(world):
    loaded = world.loaded()
    from dataclasses import replace
    a = Worker(loaded, replace(world.host("alice"), worker_id="alice/box"), NoCoord(), None)
    b = Worker(loaded, replace(world.host("alice"), worker_id="alice/box-codex"), NoCoord(), None)
    assert a.state == b.state  # attempts live side by side, unique per lease
    assert a.journal.path != b.journal.path
    assert a.journal.path == worker_home(loaded.fleet.name, "alice/box") / "slot-0" / "journal.json"
    td = type("TD", (), {"project": {"image": "img"}})()
    wt = type("WT", (), {"git": type("G", (), {"name": "n", "email": "e"})()})()
    names = {w._runtime(td, wt).name for w in (a, b)}
    assert names == {"afe-fleet-t-alice_box-0", "afe-fleet-t-alice_box-codex-0"}


def test_one_process_per_worker_id(tmp_path):
    lock = tmp_path / "workers" / "alice_box" / "lock"
    held = hold_lock(lock, "worker alice/box")
    with pytest.raises(AlreadyRunning, match=rf"worker alice/box is already running on this host \(pid {os.getpid()}\)"):
        hold_lock(lock, "worker alice/box")
    # across processes too, and the lock goes with its holder
    probe = [sys.executable, "-c", "import sys; from afe_fleet.config import hold_lock; hold_lock(__import__('pathlib').Path(sys.argv[1]), 'w')",
             str(lock)]
    assert subprocess.run(probe, capture_output=True).returncode != 0
    held.close()
    assert subprocess.run(probe, capture_output=True).returncode == 0
    hold_lock(lock, "worker alice/box").close()


def test_a_legacy_journal_is_adopted(world, tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "journal-0.json").write_text('{"step": "claimed", "task": "demo/a", "epoch": 1}')
    w = Worker(world.loaded(), world.host("alice"), NoCoord(), None, state_dir=state)
    assert w.journal.read() == {"step": "claimed", "task": "demo/a", "epoch": 1}
    assert not (state / "journal-0.json").exists()


def test_concurrent_fetches_into_one_mirror(world, tmp_path):
    """Several processes load the same fleet.toml mirror at once (they all start together)."""
    from concurrent.futures import ThreadPoolExecutor

    from afe_fleet.configrepo import ConfigRepo
    from conftest import edit_repo
    url = str(world.config_repo)
    store = tmp_path / "mirror.git"
    edit_repo(world.config_repo, {"tasks/demo/NOTE.md": "moved\n"})  # a new tip: every fetch updates the ref
    with ThreadPoolExecutor(8) as pool:
        revs = list(pool.map(lambda _: ConfigRepo(url, store).fetch(), range(16)))
    assert len(set(revs)) == 1 and revs[0]
