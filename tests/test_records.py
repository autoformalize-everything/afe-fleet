"""Done and cancelled records live beside the task, and do not change its spec."""

import json

from conftest import edit_repo, git

from afe_fleet.configrepo import ConfigRepo


def repo(world, tmp_path):
    return ConfigRepo(str(world.config_repo), tmp_path / "cfg.git")


def test_a_record_beside_the_task_closes_it_without_changing_its_spec(world, tmp_path):
    cfg = repo(world, tmp_path)
    before = cfg.load(cfg.fetch())
    cfg.write_done("demo/a", {"task": "demo/a", "at": 1.0})
    after = cfg.load(cfg.fetch())
    assert git("cat-file", "-e", "main:tasks/demo/a/done.json", cwd=world.config_repo) == ""
    assert after.done == {"demo/a"} and "demo/a" in after.closed
    assert after.tasks["demo/a"].spec == before.tasks["demo/a"].spec  # the record is not part of the task
    assert cfg.has_done(after.rev, "demo/a") and not cfg.has_done(after.rev, "demo/b")
    assert json.loads(cfg.read_record(after.rev, "demo/a", "done"))["at"] == 1.0
    # a real change to the task still changes the spec
    edit_repo(world.config_repo, {"tasks/demo/a/TASK.md": "changed\n"})
    assert cfg.load(cfg.fetch()).tasks["demo/a"].spec != before.tasks["demo/a"].spec


def test_records_in_the_old_place_still_count(world, tmp_path):
    edit_repo(world.config_repo, {"done/demo/a.json": '{"at": 2.0}\n', "cancelled/demo/b.json": '{"at": 3.0}\n'})
    cfg = repo(world, tmp_path)
    tree = cfg.load(cfg.fetch())
    assert tree.done == {"demo/a"} and tree.cancelled == {"demo/b"}
    assert cfg.has_done(tree.rev, "demo/b")
    assert json.loads(cfg.read_record(tree.rev, "demo/b", "cancelled"))["at"] == 3.0


def test_a_removed_task_keeps_only_its_record(world, tmp_path):
    cfg = repo(world, tmp_path)
    cfg.write_done("demo/b", {"task": "demo/b"}, cancelled=True, remove_task=True)
    files = git("ls-tree", "-r", "--name-only", "main", "tasks/demo/b/", cwd=world.config_repo).split()
    assert files == ["tasks/demo/b/cancelled.json"]
    tree = cfg.load(cfg.fetch())
    assert "demo/b" not in tree.tasks and tree.cancelled == {"demo/b"}
