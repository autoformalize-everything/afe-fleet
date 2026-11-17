"""`afe-fleet fuzz run`: fuzz campaigns without a model, filing what they find as tasks.

Fuzzing is mechanical: build the target, run it for a while, shrink what crashed. Agents are for
what comes after, one finding at a time. A campaign is declared beside a project's project.toml,
in `tasks/<project>/fuzz.toml` (outside every task's directory, so it changes no task's spec):

    [[campaign]]
    name = "decompress"
    build = ["sh", "projects/bzip2/fuzz/build.sh"]          # in the code repo, in the project's image
    target = "projects/bzip2/fuzz/build/decompress_diff"     # a libFuzzer binary
    seeds = ["projects/bzip2/fuzz/corpus/seed"]               # read-only corpus dirs in the repo
    every = "30m"       # a round at most this often
    minutes = 10        # fuzzing time per round
    jobs = 8            # -fork, and the container's --cpus
    max_open = 3        # findings filed as tasks and not yet closed, at most
    priority = 70       # of the tasks it files (higher is claimed first)
    paths = ["projects/bzip2/oracle/Bzip2/Oracle/*", "projects/bzip2/tests/vectors/*"]
    fix = "Fix the oracle. Add input.bin to tests/vectors with libbz2's verdict."

A round, on a fresh worktree of the project's base branch in the project's image:

  1. Build the target (the project's `prepare` first).
  2. Recheck every open finding: run the target on its input. One that no longer fails is marked
     gone, and its task, if it has one, is cancelled ("fixed elsewhere").
  3. Fuzz for `minutes` with `jobs` processes on the campaign's corpus.
  4. Reproduce each crash, read its signature (below), and keep one input per new signature,
     minimized by libFuzzer.
  5. File: every new finding is committed to the config repo at once, as
     `tasks/<project>/fuzz-findings/<campaign>/<hash>/` with `input.bin` and `finding.json`. With
     no TASK.md it is a record, not a task. While fewer than `max_open` findings are open tasks,
     the oldest queued one gets a TASK.md and a task.toml and becomes one.

What lasts: findings are in the config repo the moment they are found, so a restart, another
host or a full disk loses none; `max_open` only paces when they become tasks. The corpus lives
under `fleets/<fleet>/fuzz/<project>/<campaign>/corpus/` and grows across rounds and restarts;
`--import DIR` adds an existing corpus to it.

The signature of a failing run is, in order of preference: the harness's own line
`AFE-FUZZ: <text>` (a harness should print one before it aborts, naming the disagreement in
stable words), a sanitizer's `SUMMARY:` line, libFuzzer's error kind with the first stack frame
outside libFuzzer, or the last line of output; addresses and numbers are blanked so that one bug
has one signature.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .config import parse_duration
from .configrepo import TASKS_DIR, ConfigRepo, Tree
from .events import NOWHERE, Events
from .gitutil import repo_url
from .repocache import SharedRepo

log = logging.getLogger(__name__)

FINDINGS = "fuzz-findings"
ARTIFACTS = ("crash-", "leak-", "timeout-")

DEFAULT_FIX = ("Find the cause and fix it, in the files this task allows. Add the input as a regression "
               "test where the project keeps them.")


@dataclass(frozen=True)
class Campaign:
    project: str  # the project's directory under tasks/, e.g. "bzip2"
    name: str
    target: str
    build: list[str]
    repo: str
    base: str = "master"
    image: str | None = None
    prepare: list[str] | None = None
    seeds: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    every: float = 1800
    minutes: float = 10
    jobs: int = 8
    max_open: int = 3
    paths: list[str] = field(default_factory=list)
    fix: str = DEFAULT_FIX
    priority: int = 70  # of the tasks it files: bugs before new work

    @property
    def key(self) -> str:
        return f"{self.project}/{self.name}"

    @property
    def findings_dir(self) -> str:
        return f"{TASKS_DIR}/{self.project}/{FINDINGS}/{self.name}"


def load_campaigns(config: ConfigRepo, rev: str) -> list[Campaign]:
    """Every `[[campaign]]` of every `tasks/<project>/fuzz.toml` at `rev`."""
    out = []
    names = config.git("ls-tree", "-r", "--name-only", rev, f"{TASKS_DIR}/").splitlines()
    for path in names:
        if not path.endswith("/fuzz.toml"):
            continue
        project_dir = path[: -len("/fuzz.toml")]
        proj_raw = config.read_file(rev, f"{project_dir}/project.toml")
        if proj_raw is None:
            log.warning("%s: no project.toml beside it; skipped", path)
            continue
        proj = tomllib.loads(proj_raw.decode())
        for c in tomllib.loads(config.read_file(rev, path).decode()).get("campaign", []):
            try:
                out.append(Campaign(
                    project=project_dir[len(TASKS_DIR) + 1:], name=str(c["name"]), target=str(c["target"]),
                    build=list(c["build"]), repo=proj["repo"], base=proj.get("base", "master"),
                    image=proj.get("image"), prepare=proj.get("prepare"), seeds=list(c.get("seeds", [])),
                    flags=list(c.get("flags", [])), every=parse_duration(c.get("every", "30m")),
                    minutes=parse_duration(c.get("minutes", 10)) if isinstance(c.get("minutes"), str)
                    else float(c.get("minutes", 10)),
                    jobs=int(c.get("jobs", 8)), max_open=int(c.get("max_open", 3)),
                    paths=list(c.get("paths", [])), fix=str(c.get("fix", DEFAULT_FIX)),
                    priority=int(c.get("priority", 70))))
            except (KeyError, TypeError, ValueError) as e:
                log.warning("%s: bad campaign %s: %s", path, c.get("name", "?"), e)
    return out


# -- signatures ---------------------------------------------------------------------------------

_NOISE = [(re.compile(r"0x[0-9a-fA-F]+"), "0x?"), (re.compile(r"==\d+=="), "==?=="),
          (re.compile(r"(?<![A-Za-z_])\d+(?![A-Za-z_])"), "N")]


def _clean(s: str) -> str:
    for rx, rep in _NOISE:
        s = rx.sub(rep, s)
    return " ".join(s.split())[:300]


def signature(output: str) -> str:
    """One stable line naming what went wrong (see the module's doc)."""
    lines = [ln.rstrip() for ln in output.splitlines()]
    for ln in lines:
        m = re.match(r"^\s*AFE-FUZZ:\s*(.+)$", ln)
        if m:
            return " ".join(m.group(1).split())[:300]  # the harness's own words, kept as written
    for ln in lines:
        if ln.startswith("SUMMARY: ") and "Sanitizer" in ln:
            return _clean(ln[len("SUMMARY: "):])
    for i, ln in enumerate(lines):
        m = re.search(r"ERROR: libFuzzer: ([\w -]+)", ln)
        if m:
            frame = next((f for f in lines[i + 1:] if re.match(r"\s*#\d+ ", f) and "fuzzer::" not in f
                          and "libFuzzer" not in f and "__sanitizer" not in f), "")
            where = re.search(r" in (\S+)", frame)
            return _clean(f"libFuzzer: {m.group(1).strip()}" + (f" in {where.group(1)}" if where else ""))
    rest = [ln for ln in lines if ln.strip()]
    return _clean(rest[-1]) if rest else "no output"


def finding_hash(campaign: Campaign, sig: str) -> str:
    return hashlib.sha256(f"{campaign.key}\n{sig}".encode()).hexdigest()[:12]


# -- where commands run -------------------------------------------------------------------------

class Sandbox:
    """Runs commands in the campaign's image (docker, no network, `cpus` CPUs, as you), with
    the given directories mounted at their own paths; or here, without an image."""

    def __init__(self, image: str | None, workdir: Path, mounts: list[Path], cpus: int, home: Path):
        self.image, self.workdir, self.mounts, self.cpus, self.home = image, workdir, mounts, cpus, home

    def argv(self, cmd: list[str]) -> list[str]:
        if not self.image:
            return cmd
        args = ["docker", "run", "--rm", "--init", "--network", "none", "--user", f"{os.getuid()}:{os.getgid()}",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--cpus", str(self.cpus),
                "-w", str(self.workdir), "-e", f"HOME={self.home}"]
        for m in self.mounts:
            args += ["-v", f"{m}:{m}"]
        return args + [self.image, *cmd]

    def run(self, cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
        env = None if self.image else {**os.environ, "HOME": str(self.home)}
        try:
            return subprocess.run(self.argv(cmd), cwd=self.workdir, capture_output=True, text=True,
                                  errors="replace", timeout=timeout, env=env)
        except subprocess.TimeoutExpired as e:
            out = (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            return subprocess.CompletedProcess(cmd, 124, out, f"timed out after {timeout:.0f}s")


# -- the runner ---------------------------------------------------------------------------------

@dataclass
class Finding:
    hash: str
    path: str  # its directory in the config repo
    record: dict
    is_task: bool
    closed: bool


class Runner:
    """Rounds of every campaign in the config repo, each when it is due."""

    def __init__(self, config: ConfigRepo, home: Path, *, cancel: Callable[[str, str], object],
                 git_protocol: str = "ssh", clock: Callable[[], float] = time.time, events: Events = NOWHERE,
                 sandbox: Callable[..., Sandbox] = Sandbox, repo: Callable[[str], SharedRepo] | None = None):
        self.config, self.home, self.cancel = config, home, cancel
        self.git_protocol, self.clock, self.ev, self.make_sandbox = git_protocol, clock, events, sandbox
        self.repo = repo or (lambda url: SharedRepo(repo_url(url, git_protocol)))

    def state_dir(self, c: Campaign) -> Path:
        return self.home / c.project / c.name

    def _state(self, c: Campaign) -> dict:
        try:
            return json.loads((self.state_dir(c) / "state.json").read_text())
        except (OSError, ValueError):
            return {}

    def due(self, c: Campaign) -> bool:
        return self.clock() - float(self._state(c).get("last_round", 0)) >= c.every

    def run_forever(self, stop, only: set[str] | None = None) -> None:
        while not stop.is_set():
            try:
                tree_rev = self.config.fetch()
                campaigns = load_campaigns(self.config, tree_rev) if tree_rev else []
                for c in campaigns:
                    if (only is None or c.key in only or c.name in only) and self.due(c) and not stop.is_set():
                        self.round(c)
            except Exception:  # noqa: BLE001 - the loop must survive anything
                log.exception("fuzz round failed")
            stop.wait(60)

    def import_corpus(self, c: Campaign, src: Path) -> int:
        dst = self.state_dir(c) / "corpus"
        dst.mkdir(parents=True, exist_ok=True)
        n = 0
        for f in src.rglob("*"):
            if f.is_file():
                name = hashlib.sha1(f.read_bytes()).hexdigest()  # libFuzzer's own naming
                if not (dst / name).exists():
                    shutil.copyfile(f, dst / name)
                    n += 1
        return n

    # -- one round --------------------------------------------------------------------------

    def findings(self, tree: Tree, c: Campaign) -> list[Finding]:
        out = []
        prefix = c.findings_dir + "/"
        names = self.config.git("ls-tree", "-r", "--name-only", tree.rev, prefix).splitlines()
        for path in sorted({p[: p.rindex("/")] for p in names if p.endswith("/finding.json")}):
            raw = self.config.read_file(tree.rev, f"{path}/finding.json")
            try:
                record = json.loads(raw) if raw else {}
            except ValueError:
                record = {}
            task = path[len(TASKS_DIR) + 1:]
            out.append(Finding(path.rsplit("/", 1)[1], path, record, f"{path}/TASK.md" in names,
                               task in tree.closed))
        return out

    def round(self, c: Campaign) -> list[str]:
        acts: list[str] = []
        sd = self.state_dir(c)
        for d in ("corpus", "art", "inputs", "home"):
            (sd / d).mkdir(parents=True, exist_ok=True)
        with self.ev.span("fuzz-round", campaign=c.key) as end:
            repo = self.repo(c.repo)
            repo.fetch()
            wt = repo.add(sd / "work", f"origin/{c.base}")
            try:
                acts = self._round(c, sd, wt.path, wt.git("rev-parse", "HEAD"), repo.path)
            finally:
                repo.remove(wt)
                state = {**self._state(c), "last_round": self.clock()}
                (sd / "state.json").write_text(json.dumps(state, indent=2) + "\n")
            end["actions"] = len(acts)
        for a in acts:
            log.info("%s: %s", c.key, a)
        return acts

    def _round(self, c: Campaign, sd: Path, work: Path, sha: str, git_dir: Path) -> list[str]:
        acts: list[str] = []
        box = self.make_sandbox(c.image, work, [work, git_dir, sd], c.jobs, sd / "home")
        target = str(work / c.target)
        with self.ev.span("fuzz-build") as end:
            for cmd in ([c.prepare] if c.prepare else []) + [c.build]:
                p = box.run(cmd, 3600)
                if p.returncode != 0:
                    end["error"] = "build failed"
                    log.error("%s: %s failed (%s):\n%s", c.key, cmd, p.returncode, (p.stdout + p.stderr)[-3000:])
                    return [f"build failed at {sha[:10]}; no round"]
        tree = self.config.load(self.config.fetch())
        known = self.findings(tree, c)
        changes: dict[str, bytes | None] = {}

        # recheck what is open
        with self.ev.span("fuzz-recheck", open=sum(1 for f in known if not f.closed)):
            for f in known:
                if f.closed or f.record.get("state") == "gone":
                    continue
                raw = self.config.read_file(tree.rev, f"{f.path}/input.bin")
                if raw is None:
                    continue
                inp = sd / "inputs" / f"{f.hash}.bin"
                inp.write_bytes(raw)
                p = box.run([target, str(inp)], 300)
                if p.returncode != 0:
                    continue
                f.record.update(state="gone", gone_at=_iso(self.clock()), gone_on=sha)
                changes[f"{f.path}/finding.json"] = _json(f.record)
                acts.append(f"{f.hash}: no longer fails on {c.base} {sha[:10]}")
                if f.is_task:
                    try:
                        self.cancel(f.path[len(TASKS_DIR) + 1:], f"fuzz: no longer fails on {c.base} {sha[:10]}")
                        acts.append(f"{f.hash}: task cancelled")
                    except ValueError as e:  # e.g. approved and merging: leave it be
                        acts.append(f"{f.hash}: task left open ({e})")

        # fuzz
        art = sd / "art"
        shutil.rmtree(art, ignore_errors=True)
        art.mkdir()
        seeds = [str(work / s) for s in c.seeds if (work / s).is_dir()]
        cmd = [target, f"-fork={c.jobs}", f"-max_total_time={int(c.minutes * 60)}", "-ignore_crashes=1",
               "-ignore_timeouts=1", "-ignore_ooms=1", f"-artifact_prefix={art}/", *c.flags,
               str(sd / "corpus"), *seeds]
        with self.ev.span("fuzz-campaign", jobs=c.jobs, minutes=c.minutes) as end:
            p = box.run(cmd, c.minutes * 60 + 600)
            end["exit"] = p.returncode
        crashes = sorted(a for a in art.iterdir() if a.name.startswith(ARTIFACTS))

        # triage
        seen = {f.hash: f for f in known}
        new: dict[str, dict] = {}
        with self.ev.span("fuzz-triage", artifacts=len(crashes)):
            for a in crashes:
                rep = box.run([target, str(a)], 300)
                if rep.returncode == 0:
                    continue  # does not reproduce on its own (flaky, or needed state)
                sig = signature(rep.stdout + "\n" + rep.stderr)
                h = finding_hash(c, sig)
                if h in new:
                    new[h]["count"] += 1
                    continue
                if h in seen:
                    f = seen[h]
                    if f.record.get("state") == "gone" or f.closed:
                        # it was fixed, and fails again: a regression, filed anew
                        h = f"{h}-r{sum(1 for k in seen if k.startswith(h))}"
                        if h in seen:
                            continue
                    else:
                        continue
                inp, out = a.read_bytes(), rep.stdout + rep.stderr
                small = sd / "art" / f"min-{h}"
                m = box.run([target, "-minimize_crash=1", "-max_total_time=60", f"-exact_artifact_path={small}",
                             str(a)], 600)
                if m.returncode == 0 and small.exists() and len(small.read_bytes()) < len(inp):
                    again = box.run([target, str(small)], 300)
                    if again.returncode != 0 and signature(again.stdout + "\n" + again.stderr) == sig:
                        inp, out = small.read_bytes(), again.stdout + again.stderr
                new[h] = {"campaign": c.key, "signature": sig, "size": len(inp), "raw_size": a.stat().st_size,
                          "kind": a.name.split("-", 1)[0], "first_seen": _iso(self.clock()), "found_on": sha,
                          "count": 1, "output": _tail(out, 80), "state": "queued", "_input": inp}
        for h, rec in new.items():
            path = f"{c.findings_dir}/{h}"
            changes[f"{path}/input.bin"] = rec.pop("_input")
            changes[f"{path}/finding.json"] = _json(rec)
            known.append(Finding(h, path, rec, False, False))
            acts.append(f"{h}: new finding: {rec['signature']}")

        # promote queued findings to tasks while there is room
        open_tasks = sum(1 for f in known if f.is_task and not f.closed and f.record.get("state") != "gone")
        queued = sorted((f for f in known if not f.is_task and not f.closed and f.record.get("state") == "queued"),
                        key=lambda f: f.record.get("first_seen", ""))
        for f in queued[: max(0, c.max_open - open_tasks)]:
            f.record["state"] = "filed"
            f.record["filed_at"] = _iso(self.clock())
            changes[f"{f.path}/finding.json"] = _json(f.record)
            changes[f"{f.path}/TASK.md"] = task_md(c, f.hash, f.record).encode()
            changes[f"{f.path}/task.toml"] = task_toml(c).encode()
            acts.append(f"{f.hash}: filed as task {f.path[len(TASKS_DIR) + 1:]}")
        if changes:
            msg = f"fuzz {c.key}: {len(new)} new finding(s), " \
                  f"{sum(1 for a in acts if 'filed as task' in a)} filed, {sum(1 for a in acts if 'no longer' in a)} gone"
            with self.ev.span("fuzz-file"):
                self.config.write(changes, msg)
        state = self._state(c)
        state.update(last_sha=sha, last_artifacts=len(crashes), corpus=sum(1 for _ in (sd / "corpus").iterdir()))
        (sd / "state.json").write_text(json.dumps(state, indent=2) + "\n")
        return acts


def task_md(c: Campaign, h: str, rec: dict) -> str:
    return f"""# Fuzz finding `{h}`: {rec['signature']}

The `{c.name}` fuzz campaign found an input that makes `{c.target}` fail: `input.bin` in this
task's directory ({rec['size']} bytes, minimized), first on `{c.base}` at {rec['found_on'][:10]}.

Reproduce: build with `{' '.join(c.build)}`, then run `{c.target} <this task's directory>/input.bin`.
It printed:

```text
{rec['output']}
```

{c.fix}

## Done when

- `{c.target} input.bin` exits 0 on your branch, and the project's checks pass.

The fuzz runner reruns this input on `{c.base}` after every round and cancels this task if the
input stops failing there (fixed by another change).
"""


def task_toml(c: Campaign) -> str:
    paths = ", ".join(json.dumps(p) for p in c.paths)
    return f"# Filed by the fuzz runner (campaign {c.key}).\npriority = {c.priority}\npaths = [{paths}]\n"


def _json(d: dict) -> bytes:
    return json.dumps(d, indent=2, sort_keys=True).encode() + b"\n"


def _tail(text: str, n: int) -> str:
    lines = text.rstrip().splitlines()
    return "\n".join(lines[-n:])


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
