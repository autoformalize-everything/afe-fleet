"""Model and effort selection: aliases, validation, precedence, and the flags the agent gets."""

import json

import pytest
from conftest import FakeForge, edit_repo

from afe_fleet.agent import Runtime, Workspace
from afe_fleet.agentchoice import DEFAULTS, AgentChoice, choose, cli_args, normalize_model
from afe_fleet.config import AgentConfig, HostConfig
from afe_fleet.model import Need


def test_aliases_and_full_ids():
    assert normalize_model("fable-5.2") == "claude-fable-5-2"
    assert normalize_model("opus-5.5") == "claude-opus-5-5"
    assert normalize_model("sonnet-5.5") == "claude-sonnet-5-5"
    assert normalize_model("opus") == "opus"  # the CLI resolves the family to its latest model
    assert normalize_model("claude-sonnet-5-5") == "claude-sonnet-5-5"
    for bad in ("gpt-5", "opus 5.5", "claude_opus"):
        with pytest.raises(ValueError):
            normalize_model(bad)


def test_effort_levels():
    for e in ("low", "medium", "high", "xhigh", "max"):
        assert choose("work", {"effort": e}).effort == e
    with pytest.raises(ValueError):
        choose("work", {"effort": "extreme"})


def test_precedence_field_by_field_and_per_kind():
    cli = {}
    task = {"effort": "xhigh"}
    project = {"model": "fable-5.2", "effort": "high", "review": {"model": "opus-5.5"}}
    host = {"model": "sonnet-5.5", "effort": "low", "review": {"effort": "medium"}}
    assert choose("work", cli, task, project, host) == AgentChoice("claude-fable-5-2", "xhigh")
    # review: the project's [agent.review] model beats its [agent] model; effort still from the task
    assert choose("review", cli, task, project, host) == AgentChoice("claude-opus-5-5", "xhigh")
    # the CLI beats everything
    assert choose("review", {"model": "sonnet-5.5"}, task, project, host).model == "claude-sonnet-5-5"
    # nothing set anywhere: the built-in defaults, Opus for work and Sonnet for review
    assert choose("work", cli, {}, {}, {}, DEFAULTS) == AgentChoice("claude-opus-5-5", None)
    assert choose("review", cli, {}, {}, {}, DEFAULTS) == AgentChoice("claude-sonnet-5-5", None)
    # a host-level [agent] model beats the built-in [agent.review] default
    assert choose("review", {}, {}, {}, {"model": "fable-5.2"}, DEFAULTS).model == "claude-fable-5-2"
    assert choose("work") == AgentChoice(None, None)


def test_flags_appended_to_the_agent_command(tmp_path):
    ws = Workspace(tmp_path).create()
    rt = Runtime(AgentConfig(command=["claude", "-p"]), None, "n", "l")
    extra = cli_args(AgentChoice("claude-opus-5-5", "high"), rt.cfg.model_args, rt.cfg.effort_args)
    argv, _, _ = rt.agent_argv(ws, extra)
    assert argv == ["claude", "-p", "--model", "claude-opus-5-5", "--effort", "high"]
    assert cli_args(AgentChoice(), ["--model", "{model}"], ["--effort", "{effort}"]) == []


def test_host_config_and_cli_overrides(tmp_path):
    p = tmp_path / "h.toml"
    p.write_text('[agent]\nmodel = "sonnet-5.5"\n[agent.review]\neffort = "max"\n')
    h = HostConfig.load("x", p, model="fable-5.2")
    assert h.agent.choice_table == {"model": "sonnet-5.5", "review": {"effort": "max"}}
    assert h.agent.choice_cli == {"model": "fable-5.2"}
    p.write_text('[agent]\nmodel = "gpt-5"\n')
    with pytest.raises(ValueError):
        HostConfig.load("x", p)
    with pytest.raises(ValueError):
        HostConfig.load("x", tmp_path / "missing.toml", effort="ultra")


def test_invalid_task_agent_config_makes_the_task_unclaimable(world):
    edit_repo(world.config_repo, {"tasks/demo/a/task.toml": '[agent]\nmodel = "gpt-5"\n'})
    tree = world.loaded().config
    t = tree.load(tree.fetch())
    assert "demo/a" not in t.tasks and "demo/b" in t.tasks


def test_worker_passes_model_and_effort_and_records_them(world, tmp_path):
    from test_e2e import make_worker, status
    edit_repo(world.config_repo, {"tasks/demo/project.toml": (
        (world.config_repo.with_name("config.git-seed") / "tasks/demo/project.toml").read_text()
        + '\n[agent]\nmodel = "fable-5.2"\neffort = "high"\n\n[agent.review]\nmodel = "opus-5.5"\n')})
    log = tmp_path / "argv.log"
    forge = FakeForge()
    a = make_worker(world, "alice", "work", forge)
    b = make_worker(world, "bob", "approve", forge)
    for w in (a, b):
        w.host.agent.env["FAKE_AGENT_ARGV_LOG"] = str(log)
    assert a.run_once()
    assert status(world, "demo/a").last["agent"]["model"] == "claude-fable-5-2"
    assert b.run_once()
    st = status(world, "demo/a")
    assert st.core.need is Need.MERGE
    assert st.last["agent"] == {"model": "claude-opus-5-5", "effort": "high"}
    work_argv, review_argv = [json.loads(line) for line in log.read_text().splitlines()]
    assert work_argv[-4:] == ["--model", "claude-fable-5-2", "--effort", "high"]
    assert review_argv[-4:] == ["--model", "claude-opus-5-5", "--effort", "high"]
    assert "(claude-opus-5-5, high effort)" in forge.reviews[0][3]


def test_worker_uses_the_built_in_defaults(world, tmp_path):
    from test_e2e import make_worker, status
    log = tmp_path / "argv.log"
    forge = FakeForge()
    a = make_worker(world, "alice", "work", forge)
    b = make_worker(world, "bob", "approve", forge)
    for w in (a, b):
        w.host.agent.env["FAKE_AGENT_ARGV_LOG"] = str(log)
    assert a.run_once() and b.run_once()
    work_argv, review_argv = [json.loads(line) for line in log.read_text().splitlines()]
    assert work_argv[-2:] == ["--model", "claude-opus-5-5"]
    assert review_argv[-2:] == ["--model", "claude-sonnet-5-5"]
    assert status(world, "demo/a").last["agent"] == {"model": "claude-sonnet-5-5", "effort": None}
