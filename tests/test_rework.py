"""Fewer review round trips: a task's write scope, the base merged in before a rework, and a
re-review scoped to what changed since the approval."""

import json

from conftest import FakeForge, edit_repo, git
from test_e2e import make_admin, make_worker, status

from afe_fleet.gitutil import Git
from afe_fleet.model import Need
from afe_fleet.worker import APPROVED_NOTE, REVIEW_PROMPT, WORK_PROMPT, out_of_scope


def test_out_of_scope(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    g = Git(r)
    g("init", "-q", "-b", "main")
    (r / "base.txt").write_text("b")
    g("add", "-A")
    g("commit", "-q", "-m", "base")
    g("checkout", "-q", "-b", "task")
    for name in ("src/a/x.lean", "src/b.lean", "out/result.json"):
        (r / name).parent.mkdir(parents=True, exist_ok=True)
        (r / name).write_text("x")
    g("add", "src")
    g("commit", "-q", "-m", "work")  # out/result.json stays untracked
    assert out_of_scope(g, "main", ["src/*"]) == ["out/result.json"]
    assert out_of_scope(g, "main", ["src/a/*", "out/*"]) == ["src/b.lean"]
    assert out_of_scope(g, "main", ["*"]) == []


def set_project(world, extra: str) -> None:
    edit_repo(world.config_repo, {"tasks/demo/project.toml":
              f'repo = "{world.code_repo}"\nbase = "main"\nverify = ["sh", "-c", "grep -q ok solution.txt"]\n{extra}'})


def test_a_change_outside_the_task_scope_does_not_pass(world):
    set_project(world, 'paths = ["docs/*"]\n')
    alice = make_worker(world, "alice", "work", FakeForge())
    assert alice.run_once()
    st = status(world, "demo/a")
    assert st.core.need is Need.WORK  # verify passed, the scope did not: no review
    assert "may change only: docs/*" in json.dumps(st.last) and "solution.txt" in json.dumps(st.last)


def test_inside_the_scope_passes(world):
    set_project(world, 'paths = ["solution.txt", "attempts.txt"]\n')  # all the fake agent writes
    alice = make_worker(world, "alice", "work", FakeForge())
    assert alice.run_once()
    assert status(world, "demo/a").core.need is Need.REVIEW


def test_rework_after_approval_merges_the_base_and_scopes_the_next_review(world):
    forge = FakeForge()
    alice, bob, admin = make_worker(world, "alice", "work", forge), make_worker(world, "bob", "approve", forge), \
        make_admin(world, forge)
    assert alice.run_once() and bob.run_once()
    approved = status(world, "demo/a").last["reviewed_sha"]

    forge.mergeable = False  # the base moved under the approved PR
    edit_repo(world.code_repo, {"elsewhere.txt": "from main\n"})
    assert any("merge conflict; rework" in a for a in admin.sweep())
    assert status(world, "demo/a").last["approved_sha"] == approved

    forge.mergeable = True
    assert alice.run_once()  # the rework: origin/main is merged in before the agent runs
    st = status(world, "demo/a")
    assert st.core.need is Need.REVIEW
    pr = st.core.pr
    assert pr["approved_sha"] == approved and pr["head_sha"] != approved
    files = git("ls-tree", "--name-only", pr["head_sha"], cwd=world.code_repo).split()
    assert "elsewhere.txt" in files and "solution.txt" in files
    # a merge commit on top of what was approved
    merges = git("rev-list", "--merges", f"{approved}..{pr['head_sha']}", cwd=world.code_repo).split()
    assert len(merges) == 1 and approved in git("log", "-1", "--format=%P", merges[0], cwd=world.code_repo).split()

    assert bob.run_once()  # reviews again, and it merges
    assert any("merged and accepted" in a for a in admin.sweep())


def test_prompts():
    review = REVIEW_PROMPT.format(task="t", sha="HEAD1", base="main", task_dir="/t", out_dir="/o", extra="",
                                  scoped=APPROVED_NOTE.format(approved="OLD1", sha="HEAD1", base="main"))
    assert "git diff OLD1 HEAD1" in review and "all of it\nin this one review" in review
    work = WORK_PROMPT.format(task="t", base="main", task_dir="/t", out_dir="/o", extra="", notes="",
                              checks="\n- Run `sh verify.sh`", scope=" (src/*)")
    assert "/o/result.json (this exact path" in work and "the task allows (src/*)" in work
