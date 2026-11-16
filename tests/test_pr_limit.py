"""max_open_prs: no new work while the worker's own PRs pile up; reviews and reworks continue."""

from dataclasses import replace

import pytest

from afe_fleet.config import HostConfig
from afe_fleet.configrepo import TaskDef
from afe_fleet.coord import Want, eligible
from afe_fleet.model import Core, Need
from afe_fleet.status import Status
from afe_fleet.worker import Worker

PR = {"number": 1, "gh_user": "alice"}


def st(task, need=Need.WORK, pr=None, author=None):
    return Status(task, Core(need, 1, None, pr, author))


def td(tid):
    return TaskDef(tid, "spec", {}, {}, (), ("work", "review"))


def test_counting_own_open_prs():
    statuses = [st("a", Need.REVIEW, PR, "alice/box"), st("b", Need.MERGE, PR, "alice/box"),
                st("c", Need.REVIEW, PR, "bob/box"), st("d", Need.WORK, PR, "alice/box"),  # sent back: not waiting
                st("e", Need.DONE, PR, "alice/box")]
    assert not Want("alice/box").at_pr_limit(statuses)  # no limit
    assert Want("alice/box", max_open_prs=2).at_pr_limit(statuses)
    assert not Want("alice/box", max_open_prs=3).at_pr_limit(statuses)
    assert not Want("bob/box", max_open_prs=2).at_pr_limit(statuses)
    # a process with slots counts every slot's PRs; a similarly named worker's are not its own
    slots = Want("alice/box-0", max_open_prs=2, authors=("alice/box-0", "alice/box-1"))
    assert slots.at_pr_limit([st("a", Need.REVIEW, PR, "alice/box-0"), st("b", Need.REVIEW, PR, "alice/box-1")])
    assert not slots.at_pr_limit([st("a", Need.REVIEW, PR, "alice/box-0"), st("b", Need.REVIEW, PR, "alice/box-codex")])


def test_at_the_limit_only_new_work_is_held_back():
    want = Want("alice/box", gh_user="alice", allow_self_review=True, max_open_prs=1)
    fresh, rework, review = st("new"), st("mine", Need.WORK, PR, "alice/box"), st("theirs", Need.REVIEW, PR, "bob/box")
    for new_work, expect in ((True, ["work", "work", "review"]), (False, [None, "work", "review"])):
        got = [eligible(td(s.task), s, want, set(), new_work=new_work) for s in (fresh, rework, review)]
        assert got == expect


def test_worker_passes_its_limit_and_slots(world, tmp_path):
    host = replace(world.host("alice"), worker_id="alice/box", max_open_prs=2, slots=2)
    w = Worker(world.loaded(), host, None, None, slot=1, state_dir=tmp_path / "s")
    want = w.want()
    assert (want.worker, want.max_open_prs, want.authors) == ("alice/box-1", 2, ("alice/box-0", "alice/box-1"))


def test_setting(tmp_path):
    p = tmp_path / "h.toml"
    p.write_text("[worker]\nmax_open_prs = 2\n")
    assert HostConfig.load("f", p).max_open_prs == 2
    assert HostConfig.load("f", p, max_open_prs=5).max_open_prs == 5
    p.write_text("[worker]\nmax_open_prs = 0\n")
    with pytest.raises(ValueError, match="max_open_prs"):
        HostConfig.load("f", p)
