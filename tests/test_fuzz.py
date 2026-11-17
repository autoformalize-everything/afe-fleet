"""The fuzz runner: rounds against a stand-in libFuzzer target, findings filed in the config repo."""

import json
import os
import stat
import sys

from conftest import edit_repo, git

from afe_fleet.configrepo import ConfigRepo
from afe_fleet.fuzz import Runner, load_campaigns, signature
from afe_fleet.repocache import SharedRepo

# A libFuzzer stand-in. Fuzzing (-fork=...) writes the inputs listed in $FAKE_FUZZ/crashes as
# artifacts; one input fails unless its first word is listed in $FAKE_FUZZ/fixed; minimizing
# drops trailing "x"s.
FAKE_TARGET = r'''#!PYTHON
import os, sys
ctl = os.environ["FAKE_FUZZ"]
args = sys.argv[1:]
opts = dict(a[1:].split("=", 1) for a in args if a.startswith("-") and "=" in a)
files = [a for a in args if not a.startswith("-")]
if "fork" in opts:
    crashes = open(os.path.join(ctl, "crashes")).read().split() if os.path.exists(os.path.join(ctl, "crashes")) else []
    for i, c in enumerate(crashes):
        open(opts["artifact_prefix"] + f"crash-{i:03d}", "w").write(c)
    open(os.path.join(ctl, "fuzzed"), "a").write(" ".join(args) + "\n")
    sys.exit(0)
data = open(files[0]).read()
if "minimize_crash" in opts:
    open(opts["exact_artifact_path"], "w").write(data.rstrip("x"))
    sys.exit(0)
fixed = open(os.path.join(ctl, "fixed")).read().split() if os.path.exists(os.path.join(ctl, "fixed")) else []
word = data.rstrip("x")
if word.startswith("BUG") and word not in fixed:
    print("==4242==ERROR: AddressSanitizer: deadly signal")
    print(f"AFE-FUZZ: disagree {word}")
    sys.exit(1)
sys.exit(0)
'''


def setup(world, tmp_path, monkeypatch, max_open=1):
    ctl = tmp_path / "ctl"
    ctl.mkdir()
    monkeypatch.setenv("FAKE_FUZZ", str(ctl))
    target = tmp_path / "fake_fuzzer"
    target.write_text(FAKE_TARGET.replace("PYTHON", sys.executable))
    target.chmod(target.stat().st_mode | stat.S_IEXEC)
    edit_repo(world.config_repo, {"tasks/demo/fuzz.toml": f'''
[[campaign]]
name = "dec"
build = ["sh", "-c", "true"]
target = "{target}"
every = "1h"
minutes = 1
jobs = 2
max_open = {max_open}
paths = ["src/*"]
fix = "Fix the decoder."
'''})
    cfg = ConfigRepo(str(world.config_repo), tmp_path / "cfg.git")
    cancelled = []
    runner = Runner(cfg, tmp_path / "fuzzhome", cancel=lambda task, reason: cancelled.append((task, reason)),
                    repo=lambda url: SharedRepo(url, tmp_path / "repos"))
    (campaign,) = load_campaigns(cfg, cfg.fetch())
    return ctl, cfg, runner, campaign, cancelled


def files(world, prefix="tasks/demo/fuzz-findings/dec/"):
    return sorted(git("ls-tree", "-r", "--name-only", "main", prefix, cwd=world.config_repo).split())


def test_signatures():
    assert signature("noise\nAFE-FUZZ: disagree ok/data at 7\n") == "disagree ok/data at 7"
    a = signature("==123==ERROR: AddressSanitizer: heap-buffer-overflow on address 0x6020000000f1\n"
                  "SUMMARY: AddressSanitizer: heap-buffer-overflow /src/x.c:42:7 in decode\n")
    b = signature("==999==ERROR: AddressSanitizer: heap-buffer-overflow on address 0x7fff00000001\n"
                  "SUMMARY: AddressSanitizer: heap-buffer-overflow /src/x.c:42:7 in decode\n")
    assert a == b == "AddressSanitizer: heap-buffer-overflow /src/x.c:N:N in decode"
    c = signature("==7== ERROR: libFuzzer: deadly signal\n    #0 0x55 in __sanitizer_print_stack_trace\n"
                  "    #1 0x56 in fuzzer::PrintStackTrace()\n    #2 0x57 in bz_decode_block /src/d.c:9\n")
    assert c == "libFuzzer: deadly signal in bz_decode_block"


def test_a_round_files_findings_and_paces_tasks(world, tmp_path, monkeypatch):
    ctl, cfg, runner, c, cancelled = setup(world, tmp_path, monkeypatch, max_open=1)
    (ctl / "crashes").write_text("BUG1xxx BUG1x BUG2xx")
    acts = runner.round(c)
    assert sum("new finding" in a for a in acts) == 2  # two signatures from three crashes
    assert sum("filed as task" in a for a in acts) == 1  # max_open = 1
    names = files(world)
    dirs = sorted({n.rsplit("/", 1)[0] for n in names})
    assert len(dirs) == 2
    recs = {d: json.loads(git("show", f"main:{d}/finding.json", cwd=world.config_repo)) for d in dirs}
    filed = [d for d in dirs if f"{d}/TASK.md" in names]
    assert len(filed) == 1 and recs[filed[0]]["state"] == "filed"
    (queued,) = [d for d in dirs if d not in filed]
    assert recs[queued]["state"] == "queued"
    # minimized, and the input kept in the repo
    inputs = {recs[d]["signature"]: git("show", f"main:{d}/input.bin", cwd=world.config_repo) for d in dirs}
    assert inputs == {"disagree BUG1": "BUG1", "disagree BUG2": "BUG2"}
    # the filed one is a task the fleet sees; the queued one is only a record
    tree = cfg.load(cfg.fetch())
    task_ids = {t for t in tree.tasks if "fuzz-findings" in t}
    assert task_ids == {filed[0].removeprefix("tasks/")}
    td = tree.tasks[filed[0].removeprefix("tasks/")]
    assert td.meta["paths"] == ["src/*"] and td.meta["priority"] == 70
    md = git("show", f"main:{filed[0]}/TASK.md", cwd=world.config_repo)
    assert "AFE-FUZZ: disagree" in md and "Fix the decoder." in md
    fuzzed = (ctl / "fuzzed").read_text()
    assert "-fork=2" in fuzzed and "-max_total_time=60" in fuzzed

    # the same bugs again: nothing new, nothing filed (one task is still open)
    assert not [a for a in runner.round(c) if "new finding" in a or "filed" in a]

    # the filed bug gets fixed on the branch: its finding is gone, its task cancelled, and the
    # queued one takes its place
    filed_sig = recs[filed[0]]["signature"].split()[-1]
    (ctl / "fixed").write_text(filed_sig)
    (ctl / "crashes").write_text("")
    acts = runner.round(c)
    assert any("no longer fails" in a for a in acts)
    assert cancelled and cancelled[0][0] == filed[0].removeprefix("tasks/")
    assert any(f"{queued.rsplit('/', 1)[1]}: filed as task" in a for a in acts)
    assert json.loads(git("show", f"main:{filed[0]}/finding.json", cwd=world.config_repo))["state"] == "gone"

    # it comes back: a regression, filed anew under a new id
    (ctl / "fixed").write_text("")
    (ctl / "crashes").write_text(filed_sig)
    acts = runner.round(c)
    assert any("-r1: new finding" in a for a in acts)


def test_nothing_is_lost_across_runners(world, tmp_path, monkeypatch):
    """A second runner (another host, or after a restart with an empty disk) knows every finding."""
    ctl, cfg, runner, c, _ = setup(world, tmp_path, monkeypatch, max_open=1)
    (ctl / "crashes").write_text("BUG1 BUG2 BUG3")
    runner.round(c)
    fresh = Runner(ConfigRepo(str(world.config_repo), tmp_path / "other-cfg.git"), tmp_path / "other-home",
                   cancel=lambda *a: None, repo=lambda url: SharedRepo(url, tmp_path / "other-repos"))
    acts = fresh.round(c)
    assert not [a for a in acts if "new finding" in a]
    assert len({n.rsplit("/", 1)[0] for n in files(world)}) == 3


def test_due_and_corpus_import(world, tmp_path, monkeypatch):
    ctl, cfg, runner, c, _ = setup(world, tmp_path, monkeypatch)
    assert runner.due(c)
    runner.round(c)
    assert not runner.due(c)  # every = 1h
    src = tmp_path / "old-corpus"
    (src / "a").mkdir(parents=True)
    (src / "a" / "x").write_bytes(b"one")
    (src / "y").write_bytes(b"two")
    (src / "z").write_bytes(b"one")  # same content as a/x
    assert runner.import_corpus(c, src) == 2
    assert runner.import_corpus(c, src) == 0
    assert len(os.listdir(runner.state_dir(c) / "corpus")) == 2


def test_campaigns_do_not_change_task_specs(world, tmp_path, monkeypatch):
    cfg = ConfigRepo(str(world.config_repo), tmp_path / "c0.git")
    before = {k: v.spec for k, v in cfg.load(cfg.fetch()).tasks.items()}
    setup(world, tmp_path, monkeypatch)
    after = {k: v.spec for k, v in cfg.load(cfg.fetch()).tasks.items()}
    assert before == after
