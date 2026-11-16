"""The worker: claim a task, run the agent on it, report.

    loop:  recover → maintain → claim → attempt (heartbeat thread + agent) → report

The worker, not the agent, decides the outcome and does every push, PR and review. The agent
gets model credentials and the workspace, nothing else.

A journal (`workers/<worker-id>/slot-<n>/journal.json`) is written before each side effect, so a worker that dies
mid-attempt releases its lease on restart instead of waiting for the TTL.
"""

from __future__ import annotations

import fnmatch
import gzip
import json
import logging
import os
import re
import secrets
import shlex
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import agentchoice
from .agent import Runtime, Verify, Workspace, run_agent, run_verify
from .agentchoice import AgentChoice
from .config import HostConfig, Loaded, fleet_home, id_slug, worker_home
from .configrepo import TaskDef
from .events import EventLog, Events
from .coord import Coord, Fenced, GitCoord, Lease, ServerCoord, Unreachable, Want
from .forge import Forge, ForgeError
from .gitutil import GitError, repo_url
from .model import Outcome, Verdict
from .repocache import SharedRepo
from .status import UnknownVersion, now_iso

log = logging.getLogger(__name__)

WORK_PROMPT = """You are working on task `{task}`.

The task description is in {task_dir}/TASK.md and machine-readable context in
{task_dir}/task.json (previous attempts, review feedback, the PR so far). The repository is
checked out in the current directory, a git worktree on its own branch.

Do the task. Commit your work with clear messages as you go. When you are done, write the file
{out_dir}/result.json (this exact path: it is outside the repository, and no result file
belongs in the repository):

    {{"outcome": "success" | "partial" | "failure",
      "summary": "<what you did>",
      "progress": "<what is left, if anything>",
      "debug": "<why it failed or what blocked you, if anything>"}}

Before you finish, do what the reviewer will do, because everything they find costs another
round:{checks}
- Read `git status` and `git diff --stat origin/{base}...HEAD`: every changed file must be one
  the task allows{scope}, and nothing may be left untracked.
- Re-read each claim you make about existing code against that code, and each reference you
  give (file and line) at that place.
- In the summary, say what you checked and quote the result.

Do not push and do not open pull requests; that is done for you.
{notes}{extra}"""

MERGED_NOTE = """
`origin/{base}` had moved since this pull request was last worked on; it has been merged into
this branch for you (the last commit), without conflicts. Make sure the checks still pass.
"""

CONFLICT_NOTE = """
`origin/{base}` has moved and this branch conflicts with it. A merge of `origin/{base}` is in
progress in the worktree, with conflicts in:

{files}

Resolve each conflict so that both sides' intent survives, `git add` the file, and conclude with
`git commit --no-edit`. For a file this task does not own, take the version from
`origin/{base}` (`git checkout --theirs -- <file>`). Do not redo or restructure the work: if
task.json shows nothing else outstanding, the merge and the checks are the whole job.
"""

APPROVED_NOTE = """
Commit {approved} of this pull request was already reviewed and approved. Review what changed
since then, `git diff {approved} {sha}`. A merge of `origin/{base}` in that range brings in the
base's own changes, which are not under review: check that the merge is right and that the
pull request's content survived it. Do not review the approved part again, unless the new
changes break it.
"""

REVIEW_PROMPT = """You are reviewing the pull request for task `{task}`.

The task description is in {task_dir}/TASK.md; {task_dir}/task.json has the PR and its
history. The PR head ({sha}) is checked out in the current directory; it is based on
`origin/{base}`.

Review whether the change does what the task asks, correctly and completely. Do not modify the
repository. Ask for changes only for what is wrong or missing by the task's own terms, all of it
in this one review, with the fix you expect; preferences that the task and the project's rules
do not require belong in the summary, not in a request for changes.{scoped}
Write {out_dir}/review.json:

    {{"verdict": "approve" | "changes",
      "summary": "<the review, for the author>",
      "comments": [{{"path": "...", "line": 1, "body": "..."}}]}}
{extra}"""


def out_of_scope(git, base_ref: str, patterns: list[str]) -> list[str]:
    """The files this branch changes (committed since it left `base_ref`, staged, modified or
    untracked) that match none of the glob `patterns` (`*` crosses directories)."""
    mb = git.run("merge-base", base_ref, "HEAD").stdout.strip() or base_ref
    names = set(git.run("diff", "--name-only", mb, "HEAD").stdout.splitlines())
    for line in git.run("status", "--porcelain", "--untracked-files=all").stdout.splitlines():
        names.add(line[3:].split(" -> ")[-1].strip('"'))
    return sorted(n for n in names if n and not any(fnmatch.fnmatchcase(n, pat) for pat in patterns))


def archive_attempt(ws: Workspace, task_logs: Path, name: str) -> Path | None:
    """Keeps what an attempt leaves for later reading, before its directory goes:
    `<task_logs>/<UTC time>-<name>/` with the agent's transcript (its JSONL output), the task
    files it was given, what it wrote in `out/`, and Codex's own session files. JSON Lines are
    gzipped. Never raises: losing a log must not lose the attempt."""
    dest = task_logs / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{name}"
    try:
        files = [(ws.transcript, "transcript.jsonl")]
        for sub in ("task", "out"):
            files += [(p, f"{sub}/{p.relative_to(ws.root / sub)}") for p in sorted((ws.root / sub).rglob("*"))]
        sessions = ws.root / "codex-home" / "sessions"
        files += [(p, f"codex-sessions/{p.relative_to(sessions)}") for p in sorted(sessions.rglob("*"))]
        files = [(src, rel) for src, rel in files if src.is_file()]
        if not files:
            return None
        for src, rel in files:
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if src.suffix == ".jsonl":
                with src.open("rb") as fin, gzip.open(target.with_name(target.name + ".gz"), "wb") as fout:
                    shutil.copyfileobj(fin, fout)
            else:
                shutil.copyfile(src, target)
        return dest
    except OSError as e:
        log.warning("could not archive the attempt's logs to %s: %s", dest, e)
        return None


def slug(task: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", task)


@dataclass
class Journal:
    path: Path

    def write(self, **data) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, self.path)

    def read(self) -> dict | None:
        try:
            return json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


class Worker:
    def __init__(self, loaded: Loaded, host: HostConfig, coord: Coord, forge: Forge, *, slot: int = 0,
                 state_dir: Path | None = None, slot_dir: Path | None = None):
        self.loaded = loaded
        self.fleet = loaded.fleet
        self.host = host
        self.coord = coord
        self.forge = forge
        self.slot = slot
        self.worker_id = host.worker_id if host.slots == 1 and slot == 0 else f"{host.worker_id}-{slot}"
        self.state = (state_dir or fleet_home(self.fleet.name))  # the fleet's: attempts are unique per lease
        self.state.mkdir(parents=True, exist_ok=True)
        # this worker's own (the CLI holds its lock): crash journal, mirrors
        self.slot_dir = slot_dir or (worker_home(self.fleet.name, host.worker_id) if state_dir is None
                                     else self.state / "workers" / id_slug(host.worker_id)) / f"slot-{slot}"
        self.slot_dir.mkdir(parents=True, exist_ok=True)
        self.journal = Journal(self.slot_dir / "journal.json")
        legacy = self.state / f"journal-{slot}.json"  # before per-worker state: one worker per host
        if legacy.exists() and not self.journal.path.exists():
            os.replace(legacy, self.journal.path)
            log.info("adopted the journal of an older version of this worker: %s", legacy)
        self.stop = threading.Event()
        self.idle = 30.0
        self.infra_failures = 0
        self.maintain = None  # called before every claim: config reload, self-update, ...
        self.capacity = None  # capacity.SpareCapacity: claim only with spare subscription capacity
        self.usage = None  # capacity.Usage: the agent CLI's subscription usage, shared by the slots
        self._spare: bool | None = None
        self.events_log: EventLog | None = None  # the process's event log (events.py); None: none
        self.idle_reason = "no-task"
        self._ev_attempt: Events | None = None

    @property
    def ev(self) -> Events:
        return Events(self.events_log, self.worker_id)

    @property
    def ev_attempt(self) -> Events:
        return self._ev_attempt or self.ev

    # -- main loop -----------------------------------------------------------------------------

    def run_forever(self) -> None:
        self.recover()
        while not self.stop.is_set():
            try:
                did = self.run_once()
            except UnknownVersion as e:
                log.error("a status was written by a newer afe-fleet (v=%s); stopping", e)
                raise SystemExit(75) from e
            except Exception:  # noqa: BLE001 - the loop must survive anything
                log.exception("round failed")
                self.idle_reason = "error"
                did = False
            if did:
                self.idle = 30.0
            else:
                if self.usage is not None:  # keep the usage record going while idle (a query at most every 10 min)
                    try:
                        self.usage.reading(600)
                    except Exception:  # noqa: BLE001 - never let recording stop the loop
                        log.exception("reading usage")
                with self.ev.span("idle", reason=self.idle_reason, planned=self.idle):
                    self.stop.wait(self.idle)
                self.idle = min(self.idle * 2, 900)

    def want(self) -> Want:
        base = self.host.worker_id  # the limit is the process's, across its slots
        authors = (base,) if self.host.slots == 1 else tuple(f"{base}-{i}" for i in range(self.host.slots))
        return Want(self.worker_id, self.host.gh_user, self.host.kinds, self.host.filters,
                    self.host.allow_self_review, self.host.max_open_prs, authors)

    def run_once(self) -> bool:
        if self.maintain is not None and self.maintain() is False:
            self.idle_reason = "version"
            return False  # e.g. this version is too old to claim
        if self.infra_failures >= 2:
            log.warning("two infrastructure failures in a row; backing off before claiming again")
            self.infra_failures = 0
            with self.ev.span("idle", reason="backoff", planned=600):
                self.stop.wait(600)
        if self.capacity is not None:
            spare, why = self.capacity.check()
            if spare != self._spare:
                log.info("%s: %s", "spare capacity, claiming" if spare else "no spare capacity, not claiming", why)
                self._spare = spare
            if not spare:
                self.idle_reason = "capacity"
                return False
        nonce = secrets.token_hex(8)
        meta = {"worker": self.worker_id, "gh_user": self.host.gh_user, "nonce": nonce,
                "claimed_at": now_iso(), "heartbeat_at": now_iso()}
        with self.ev.span("claim") as end:
            lease = self.coord.claim(self.want(), meta)
            if lease is not None:
                end.update(task=lease.task, kind=lease.kind, epoch=lease.epoch)
        if lease is None:
            self.idle_reason = "no-task"
            return False
        log.info("claimed %s (%s, epoch %d)", lease.task, lease.kind, lease.epoch)
        self.journal.write(step="claimed", task=lease.task, epoch=lease.epoch, kind=lease.kind, nonce=nonce,
                           token=lease.handle if isinstance(self.coord, ServerCoord) else None)
        try:
            self.attempt(lease)
        except Fenced as e:
            log.warning("lost the lease on %s: %s", lease.task, e)
        finally:
            self.journal.clear()
        return True

    # -- recovery ------------------------------------------------------------------------------

    def recover(self) -> None:
        """A previous process died mid-attempt. If it still holds the lease, release it now
        (a partial report when a PR exists, else a failure) instead of waiting for the TTL."""
        j = self.journal.read()
        if not j:
            return
        try:
            st = self.coord.read(j["task"])
        except Exception as e:  # noqa: BLE001
            log.warning("recover: cannot read %s: %s", j.get("task"), e)
            return
        lease = st.lease or {}
        if st.core.lease == self.worker_id and st.core.epoch == j["epoch"] and lease.get("nonce") == j["nonce"]:
            handle = j.get("token")
            if isinstance(self.coord, GitCoord):
                handle = self.coord.lock.read(j["task"])
            ls = Lease(j["task"], j["epoch"], self.worker_id, j["kind"], st, handle=handle)
            try:
                last = {"outcome": "failure", "summary": "worker restarted mid-attempt",
                        "debug": {"kind": "infra", "detail": f"journal step {j.get('step')}"}}
                if j["kind"] == "work":
                    if j.get("pr"):
                        self.coord.report(ls, Outcome("part", j["pr"]), {**last, "outcome": "partial"})
                    else:
                        self.coord.report(ls, Outcome("failure"), last)
                else:
                    self.coord.review(ls, Verdict.ABSTAIN, {**last, "outcome": "abstain"})
                log.info("recover: released %s (epoch %d)", j["task"], j["epoch"])
            except Fenced:
                pass
        self.journal.clear()

    # -- one attempt ---------------------------------------------------------------------------

    def attempt(self, lease: Lease) -> None:
        tree = self.coord.tasks()
        td = tree.tasks.get(lease.task)
        if td is None:
            raise Fenced(f"{lease.task} is not in the task tree any more")
        ws = Workspace(self.state / "attempts" / f"{slug(lease.task)}-e{lease.epoch}").create()
        cancel = threading.Event()
        hb = threading.Thread(target=self._heartbeat, args=(lease, cancel), daemon=True)
        hb.start()
        repo = SharedRepo(repo_url(td.project["repo"], self.host.git_protocol))
        lease.extra["worktree"] = None
        ev = self._ev_attempt = self.ev.bind(task=lease.task, epoch=lease.epoch, kind=lease.kind)
        ev.begin("attempt")
        try:
            if lease.kind == "work":
                self._work(lease, td, tree.rev, ws, cancel, repo)
            else:
                self._review(lease, td, tree.rev, ws, cancel, repo)
        finally:
            cancel.set()
            hb.join(5)
            with ev.span("cleanup"):
                if lease.extra.get("worktree") is not None:
                    repo.remove(lease.extra["worktree"])
                archive_attempt(ws, self.state / "logs" / slug(lease.task),
                                f"e{lease.epoch}-{lease.kind}-{id_slug(self.worker_id)}")
                shutil.rmtree(ws.root, ignore_errors=True)
            ev.end("attempt", outcome=lease.extra.get("outcome") or ("fenced" if lease.extra.get("fenced") else "error"))
            self._ev_attempt = None

    def _local_name(self, lease: Lease) -> str:
        """Namespace for this attempt's branch and refs in the shared clone (shared by every
        afe-fleet process on the host, in any fleet)."""
        return f"afe/{self.fleet.name}/{slug(lease.task)}/e{lease.epoch}"

    def _heartbeat(self, lease: Lease, cancel: threading.Event) -> None:
        """Heartbeat every `heartbeat` seconds; also turns a worker shutdown into a cancel."""
        next_beat = time.monotonic() + self.fleet.heartbeat
        while not cancel.wait(1.0):
            if self.stop.is_set():
                lease.extra["shutdown"] = True
                cancel.set()
                return
            if time.monotonic() < next_beat:
                continue
            next_beat = time.monotonic() + self.fleet.heartbeat
            try:
                self.coord.heartbeat(lease, {"heartbeat_at": now_iso()})
            except Fenced as e:
                # lease failure: definitive. Stop the agent now.
                log.warning("heartbeat on %s rejected, lease lost (%s); stopping the agent", lease.task, e)
                lease.extra["fenced"] = True
                cancel.set()
                return
            except Unreachable as e:
                # connectivity failure: the lease may well still be ours. Keep working; the final
                # report retries with backoff and finds out.
                since = lease.extra.setdefault("unreachable_since", time.monotonic())
                lease.extra["unreachable_beats"] = lease.extra.get("unreachable_beats", 0) + 1
                log.warning("heartbeat on %s: coordination unreachable for %.0fs (%s); still working",
                            lease.task, time.monotonic() - since, e)
                next_beat = time.monotonic() + min(60.0, self.fleet.heartbeat)  # try again sooner
            except Exception:  # noqa: BLE001 - a bug, not a verdict on the lease
                log.exception("heartbeat on %s failed unexpectedly; still working", lease.task)
            else:
                if "unreachable_since" in lease.extra:
                    log.info("heartbeat on %s: coordination reachable again", lease.task)
                    lease.extra.pop("unreachable_since", None)

    def _choice(self, td: TaskDef, kind: str) -> AgentChoice:
        a = self.host.agent
        return agentchoice.choose(kind, a.choice_cli, td.meta.get("agent"), td.project.get("agent"), a.choice_table,
                                  agentchoice.DEFAULTS).for_cli(a.cli)

    def _runtime(self, td: TaskDef, wt) -> Runtime:
        # one container name per worker and slot: another worker's stray cleanup must not hit ours
        name = f"afe-fleet-{self.fleet.name}-{id_slug(self.host.worker_id)}-{self.slot}"
        return Runtime(self.host.agent, td.project.get("image"), name,
                       self.fleet.name, identity=(wt.git.name, wt.git.email), prepare=td.project.get("prepare"))

    def _write_task_files(self, ws: Workspace, lease: Lease, td: TaskDef, rev: str, kind: str) -> None:
        text = self.loaded.config.task_text(rev, td.id) if rev else ""  # per-slot ConfigRepo
        (ws.task / "TASK.md").write_text(text)
        (ws.task / "task.json").write_text(json.dumps({
            "task": td.id, "kind": kind, "epoch": lease.epoch, "project": td.project, "meta": td.meta,
            "status": lease.status.to_json()}, indent=2))

    # -- work ----------------------------------------------------------------------------------

    def _work(self, lease: Lease, td: TaskDef, rev: str, ws: Workspace, cancel: threading.Event,
              repo: SharedRepo) -> None:
        proj = td.project
        code_url = repo.url
        base = proj.get("base", "main")
        branch = f"task/{lease.task}/e{lease.epoch}"  # the name pushed for the PR
        local = self._local_name(lease)
        ev = self.ev_attempt
        ev.begin("fetch")
        repo.fetch()
        start = f"refs/remotes/origin/{base}"
        refs = []
        prev = lease.status.core.pr
        if isinstance(prev, dict) and prev.get("fetch") and prev.get("branch"):
            # continue from the previous attempt's PR (partial, or changes requested)
            prev_ref = f"refs/{local}/prev"
            if repo.fetch_ref(prev["fetch"], prev["branch"], prev_ref):
                start = prev_ref
                refs.append(prev_ref)
        ev.end("fetch")
        with ev.span("worktree"):
            wt = repo.add(ws.work, start, branch=local, refs=refs, name=self.host.gh_user or self.worker_id,
                          email=f"{self.host.gh_user or 'afe-fleet'}@users.noreply.github.com")
        lease.extra["worktree"] = wt
        git = wt.git
        start_sha = git("rev-parse", "HEAD")
        base_ref = f"refs/remotes/origin/{base}"
        notes = ""
        if start != base_ref and git.run("merge-base", "--is-ancestor", base_ref, "HEAD").returncode != 0:
            # The base moved under an existing PR: merge it before the agent starts, so only a
            # real conflict costs the agent anything.
            with ev.span("merge-base") as end:
                merged = git.run("merge", "--no-edit", base_ref).returncode == 0
                end["conflict"] = not merged
            if merged:
                notes = MERGED_NOTE.format(base=base)
            else:
                files = git.run("diff", "--name-only", "--diff-filter=U").stdout.split()
                notes = CONFLICT_NOTE.format(base=base, files="\n".join(f"    {f}" for f in files))
                log.info("%s: origin/%s conflicts in %s", lease.task, base, ", ".join(files))
        self._write_task_files(ws, lease, td, rev, "work")
        self.journal.write(step="prepared", task=lease.task, epoch=lease.epoch, kind="work",
                           nonce=lease.status.lease.get("nonce"), token=self._token(lease))

        rt = self._runtime(td, wt)
        rt.events, rt.usage = ev, self.usage
        verify_cmd = td.meta.get("verify", proj.get("verify"))  # task.toml overrides project.toml
        paths = td.meta.get("paths", proj.get("paths"))  # what the task may change; None: anything

        def check() -> Verify:
            """The verify command, then the task's scope."""
            v = run_verify(rt, ws, verify_cmd, self.host.agent.max_wall)
            bad = out_of_scope(git, base_ref, paths) if paths else []
            if not bad:
                return v
            return Verify(False, (f"{v.log}\n\n" if not v.ok else "")
                          + f"This task may change only: {', '.join(paths)}. These files are outside that; undo "
                          f"the changes to them (`git checkout origin/{base} -- FILE`, or `git rm` a file the "
                          "branch added), and keep result files out of the repository:\n" + "\n".join(bad))

        prompt = WORK_PROMPT.format(
            task=lease.task, extra=proj.get("prompt", ""), base=base, notes=notes,
            checks=f"\n- Run `{shlex.join(verify_cmd)}` and fix what it reports: it decides whether the attempt counts."
            if verify_cmd else "",
            scope=f" ({', '.join(paths)})" if paths else "", **rt.paths(ws))
        choice = self._choice(td, "work")
        log.info("%s: running the agent (%s)", lease.task, choice.describe())
        run = run_agent(rt, ws, prompt, cancel=cancel, choice=choice, verify=check)
        if lease.extra.get("fenced"):
            raise Fenced(f"{lease.task}: lease lost during the attempt")
        shutdown = bool(lease.extra.get("shutdown"))

        # commit anything the agent left uncommitted
        git("add", "-A")
        if git.run("diff", "--cached", "--quiet").returncode != 0:
            git("commit", "-q", "-m", "afe-fleet: uncommitted changes at end of attempt")
        head = git("rev-parse", "HEAD")
        changed = head != start_sha
        result = self._read_json(ws.out / "result.json")
        # the in-loop verify ran on this tree already, unless the agent never finished a turn
        verify = run.verify if run.verify is not None else check()
        if verify.ok and paths and (bad := out_of_scope(git, base_ref, paths)):  # the sweep above added files
            verify = Verify(False, "changed outside the task's scope: " + ", ".join(bad))
        if shutdown:
            log.info("%s: worker shutting down; reporting what exists", lease.task)
        infra = run.reason == "error" or shutdown or (run.turns == 0 and not changed)
        claimed = (result or {}).get("outcome", "success")
        summary = (result or {}).get("summary", "")
        last = {"worker": self.worker_id, "summary": summary, "progress": (result or {}).get("progress"),
                "agent": {**choice.to_json(), "reason": run.reason, "exit": run.exit_code, "turns": run.turns,
                          "session": run.session_id}}
        if not changed:
            self.infra_failures = self.infra_failures + 1 if infra else 0
            last.update(outcome="failure", debug={
                "kind": "infra" if infra else "task",
                "detail": (result or {}).get("debug") or "; ".join(run.errors) or f"agent ended: {run.reason}",
                "transcript_tail": self._tail(ws.transcript)})
            with ev.span("report", outcome="failure"):
                self.coord.report(lease, Outcome("failure"), last)
            lease.extra["outcome"] = "failure"
            log.info("%s: failure (%s)", lease.task, run.reason)
            return
        self.infra_failures = 0
        success = (verify.ok and claimed not in ("failure", "partial") and run.reason == "exit"
                   and not shutdown)
        push_url, owner = self.forge.push_target(proj["repo"], self.host.gh_user, self.host.push) \
            if "/" in proj["repo"] and not proj["repo"].startswith(("/", ".")) else (code_url, "")
        # A fix after review (or a continuation after a partial) updates the existing PR: push our
        # commits onto the PR's own branch, wherever it lives. The lease is the head we started
        # from, so a concurrent push there makes this fail rather than overwrite it. Only when
        # the PR is closed or its branch cannot be pushed to do we open a new PR.
        reuse = (start != f"refs/remotes/origin/{base}" and isinstance(prev, dict)
                 and prev.get("repo") == proj["repo"] and prev.get("fetch") and prev.get("branch")
                 and self._pr_open(prev))
        ev.begin("push")
        if reuse:
            p = git.run("push", "-q", f"--force-with-lease=refs/heads/{prev['branch']}:{start_sha}", prev["fetch"],
                        f"HEAD:refs/heads/{prev['branch']}")
            if p.returncode == 0:
                branch, push_url = prev["branch"], prev["fetch"]
            else:
                log.warning("%s: cannot push to %s of PR #%s (%s); opening a new PR", lease.task, prev["branch"],
                            prev.get("number"), p.stderr.strip()[-200:])
                reuse = False
        if not reuse:
            p = git.run("push", "-q", f"--force-with-lease=refs/heads/{branch}:", push_url, f"HEAD:refs/heads/{branch}")
            if p.returncode != 0 and git.run("ls-remote", "--exit-code", push_url,
                                             f"refs/heads/{branch}").returncode != 0:
                raise GitError(["push", branch], p.returncode, p.stderr)
        ev.end("push")
        self.journal.write(step="pushed", task=lease.task, epoch=lease.epoch, kind="work",
                           nonce=lease.status.lease.get("nonce"), token=self._token(lease), sha=head)
        title = f"{lease.task}: {summary.splitlines()[0][:60]}" if summary else f"afe-fleet: {lease.task}"
        body = (f"Task `{lease.task}`, attempt {lease.epoch} by `{self.worker_id}` ({choice.describe()}).\n\n{summary}\n\n"
                f"Verify: {'passed' if verify.ok else 'failed'}\n\n<!-- afe-fleet task={lease.task} epoch={lease.epoch} -->")
        ev.begin("pr")
        try:
            pr = self.forge.ensure_pr(repo=proj["repo"], branch=branch, head_owner=owner, base=base,
                                      title=title, body=body, draft=not success)
        except ForgeError as e:
            raise GitError(["pr", branch], 1, str(e)) from e
        pr = {**pr, "head_sha": head, "spec": td.spec, "gh_user": self.host.gh_user, "branch": branch,
              "fetch": pr.get("fetch") or push_url}
        # What a reviewer approved before this rework, so that the next review covers only what
        # changed since (admin: `approved_sha` on a rework after approval; carried while it holds).
        approved = (lease.status.last or {}).get("approved_sha") or (prev or {}).get("approved_sha") \
            if isinstance(prev, dict) else None
        if approved and git.run("merge-base", "--is-ancestor", approved, head).returncode == 0:
            pr["approved_sha"] = approved
        try:
            if reuse:
                self.forge.comment(pr, f"Attempt {lease.epoch} by `{self.worker_id}` ({choice.describe()}) pushed "
                                       f"`{head[:12]}`.\n\n{summary}\n\nVerify: {'passed' if verify.ok else 'failed'}")
            elif isinstance(prev, dict) and prev.get("number") and prev.get("repo") == proj["repo"] \
                    and prev.get("number") != pr.get("number") and self._pr_open(prev):
                self.forge.close(prev, f"Superseded by {pr.get('url') or '#' + str(pr.get('number'))} "
                                       f"(attempt {lease.epoch} of `{lease.task}`).")
        except ForgeError as e:  # cosmetic: the admin also closes superseded PRs
            log.warning("%s: %s", lease.task, e)
        ev.end("pr", number=pr.get("number"))
        self.journal.write(step="pr", task=lease.task, epoch=lease.epoch, kind="work",
                           nonce=lease.status.lease.get("nonce"), token=self._token(lease), pr=pr)
        ev.begin("report")
        if success:
            last["outcome"] = "success"
            self.coord.report(lease, Outcome("success", pr), last)
        else:
            last.update(outcome="partial", debug={"kind": "task", "verify_ok": verify.ok,
                                                  "detail": (result or {}).get("debug"),
                                                  "verify_log": verify.log[-4000:]})
            self.coord.report(lease, Outcome("part", pr), last)
        ev.end("report", outcome=last["outcome"])
        lease.extra["outcome"] = last["outcome"]
        log.info("%s: %s, PR %s", lease.task, last["outcome"], pr.get("url") or pr.get("number"))

    # -- review --------------------------------------------------------------------------------

    def _review(self, lease: Lease, td: TaskDef, rev: str, ws: Workspace, cancel: threading.Event,
                repo: SharedRepo) -> None:
        pr = lease.status.core.pr
        if not isinstance(pr, dict):
            raise Fenced(f"{lease.task}: review without a PR")
        proj = td.project
        ev = self.ev_attempt
        with ev.span("fetch"):
            repo.fetch()  # origin/<base>, for diffs against the base
            pr_ref = f"refs/{self._local_name(lease)}/pr"
            sha = repo.fetch_ref(pr["fetch"], pr["branch"], pr_ref)
        if sha is None:
            raise GitError(["fetch", pr["branch"]], 1, f"cannot fetch the PR branch from {pr['fetch']}")
        with ev.span("worktree"):
            wt = repo.add(ws.work, sha, refs=[pr_ref])
        lease.extra["worktree"] = wt
        self._write_task_files(ws, lease, td, rev, "review")
        rt = self._runtime(td, wt)
        rt.events, rt.usage = ev, self.usage
        base, approved = proj.get("base", "main"), pr.get("approved_sha")
        scoped = ""
        if approved and approved != sha and wt.git.run("merge-base", "--is-ancestor", approved, sha).returncode == 0:
            scoped = APPROVED_NOTE.format(approved=approved, sha=sha, base=base)
        prompt = REVIEW_PROMPT.format(task=lease.task, sha=sha, base=base, scoped=scoped,
                                      extra=proj.get("review_prompt", ""), **rt.paths(ws))
        choice = self._choice(td, "review")
        log.info("%s: reviewing (%s)", lease.task, choice.describe())
        run = run_agent(rt, ws, prompt, cancel=cancel, repairs=0, verify=lambda: Verify(True, ""), choice=choice)
        if lease.extra.get("fenced"):
            raise Fenced(f"{lease.task}: lease lost during review")
        review = {} if lease.extra.get("shutdown") else (self._read_json(ws.out / "review.json") or {})
        verdict = {"approve": Verdict.APPROVE, "changes": Verdict.CHANGES}.get(review.get("verdict"),
                                                                              Verdict.ABSTAIN)
        summary = review.get("summary") or f"review ended without a verdict ({run.reason})"
        if verdict is not Verdict.ABSTAIN:
            body = summary + "".join(f"\n\n`{c.get('path')}:{c.get('line')}`: {c.get('body')}"
                                     for c in review.get("comments", []) if isinstance(c, dict))
            with ev.span("review-post"):
                self.forge.post_review(pr, sha, verdict.value,
                                       f"{body}\n\n_Reviewed by `{self.worker_id}` ({choice.describe()})._")
        with ev.span("report", outcome=verdict.value):
            self.coord.review(lease, verdict, {"outcome": verdict.value, "worker": self.worker_id,
                                               "summary": summary, "reviewed_sha": sha, "agent": choice.to_json(),
                                               "comments": review.get("comments", [])})
        lease.extra["outcome"] = verdict.value
        log.info("%s: review %s at %s", lease.task, verdict.value, sha[:8])

    # -- helpers -------------------------------------------------------------------------------

    def _pr_open(self, pr: dict) -> bool:
        try:
            return self.forge.pr_state(pr)["state"] == "open"
        except (ForgeError, KeyError) as e:
            log.info("cannot read PR state (%s); treating it as closed", e)
            return False

    def _token(self, lease: Lease):
        return lease.handle if isinstance(self.coord, ServerCoord) else None

    @staticmethod
    def _read_json(p: Path) -> dict | None:
        try:
            d = json.loads(p.read_text())
            return d if isinstance(d, dict) else None
        except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
            return None

    @staticmethod
    def _tail(p: Path, n: int = 4000) -> str:
        try:
            return p.read_text(errors="replace")[-n:]
        except FileNotFoundError:
            return ""


def run_slots(make_worker, slots: int) -> None:
    """Run `slots` workers in threads until one of them asks the process to exit."""
    workers = [make_worker(i) for i in range(slots)]
    threads = [threading.Thread(target=w.run_forever, name=f"slot-{i}", daemon=True)
               for i, w in enumerate(workers)]
    for t in threads:
        t.start()
    try:
        while any(t.is_alive() for t in threads):
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("stopping: finishing in-flight reports")
        for w in workers:
            w.stop.set()
        deadline = time.monotonic() + 150
        for t in threads:
            t.join(max(0.0, deadline - time.monotonic()))
