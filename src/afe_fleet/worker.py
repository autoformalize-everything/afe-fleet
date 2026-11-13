"""The worker: claim a task, run the agent on it, report.

    loop:  recover → maintain → claim → attempt (heartbeat thread + agent) → report

The worker, not the agent, decides the outcome and does every push, PR and review. The agent
gets model credentials and the workspace, nothing else.

A journal (`journal-<slot>.json`) is written before each side effect, so a worker that dies
mid-attempt releases its lease on restart instead of waiting for the TTL.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .agent import Runtime, Verify, Workspace, run_agent, run_verify
from .config import HostConfig, Loaded, fleet_home
from .configrepo import TaskDef
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

Do the task. Commit your work with clear messages as you go. When you are done, write
{out_dir}/result.json:

    {{"outcome": "success" | "partial" | "failure",
      "summary": "<what you did>",
      "progress": "<what is left, if anything>",
      "debug": "<why it failed or what blocked you, if anything>"}}

Do not push and do not open pull requests; that is done for you.
{extra}"""

REVIEW_PROMPT = """You are reviewing the pull request for task `{task}`.

The task description is in {task_dir}/TASK.md; {task_dir}/task.json has the PR and its
history. The PR head ({sha}) is checked out in the current directory; it is based on
`origin/{base}`.

Review whether the change does what the task asks, correctly and completely. Do not modify the
repository. Write {out_dir}/review.json:

    {{"verdict": "approve" | "changes",
      "summary": "<the review, for the author>",
      "comments": [{{"path": "...", "line": 1, "body": "..."}}]}}
{extra}"""


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
                 state_dir: Path | None = None):
        self.loaded = loaded
        self.fleet = loaded.fleet
        self.host = host
        self.coord = coord
        self.forge = forge
        self.slot = slot
        self.worker_id = host.worker_id if host.slots == 1 and slot == 0 else f"{host.worker_id}-{slot}"
        self.state = (state_dir or fleet_home(self.fleet.name))
        self.state.mkdir(parents=True, exist_ok=True)
        self.journal = Journal(self.state / f"journal-{slot}.json")
        self.stop = threading.Event()
        self.idle = 30.0
        self.infra_failures = 0
        self.maintain = None  # called before every claim: config reload, self-update, ...

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
                did = False
            if did:
                self.idle = 30.0
            else:
                self.stop.wait(self.idle)
                self.idle = min(self.idle * 2, 900)

    def want(self) -> Want:
        return Want(self.worker_id, self.host.gh_user, self.host.kinds, self.host.filters,
                    self.host.allow_self_review)

    def run_once(self) -> bool:
        if self.maintain is not None and self.maintain() is False:
            return False  # e.g. this version is too old to claim
        if self.infra_failures >= 2:
            log.warning("two infrastructure failures in a row; backing off before claiming again")
            self.infra_failures = 0
            self.stop.wait(600)
        nonce = secrets.token_hex(8)
        meta = {"worker": self.worker_id, "gh_user": self.host.gh_user, "nonce": nonce,
                "claimed_at": now_iso(), "heartbeat_at": now_iso()}
        lease = self.coord.claim(self.want(), meta)
        if lease is None:
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
        try:
            if lease.kind == "work":
                self._work(lease, td, tree.rev, ws, cancel, repo)
            else:
                self._review(lease, td, tree.rev, ws, cancel, repo)
        finally:
            cancel.set()
            hb.join(5)
            if lease.extra.get("worktree") is not None:
                repo.remove(lease.extra["worktree"])
            shutil.rmtree(ws.root, ignore_errors=True)

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

    def _runtime(self, td: TaskDef) -> Runtime:
        return Runtime(self.host.agent, td.project.get("image"), f"afe-fleet-{self.fleet.name}-{self.slot}",
                       self.fleet.name)

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
        wt = repo.add(ws.work, start, branch=local, refs=refs, name=self.host.gh_user or self.worker_id,
                      email=f"{self.host.gh_user or 'afe-fleet'}@users.noreply.github.com")
        lease.extra["worktree"] = wt
        git = wt.git
        start_sha = git("rev-parse", "HEAD")
        self._write_task_files(ws, lease, td, rev, "work")
        self.journal.write(step="prepared", task=lease.task, epoch=lease.epoch, kind="work",
                           nonce=lease.status.lease.get("nonce"), token=self._token(lease))

        rt = self._runtime(td)
        verify_cmd = td.meta.get("verify", proj.get("verify"))  # task.toml overrides project.toml
        prompt = WORK_PROMPT.format(task=lease.task, extra=proj.get("prompt", ""), **rt.paths(ws))
        run = run_agent(rt, ws, prompt, cancel=cancel,
                        verify=lambda: run_verify(rt, ws, verify_cmd, self.host.agent.max_wall))
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
        verify = run.verify if run.verify is not None else run_verify(rt, ws, verify_cmd, self.host.agent.max_wall)
        if shutdown:
            log.info("%s: worker shutting down; reporting what exists", lease.task)
        infra = run.reason == "error" or shutdown or (run.turns == 0 and not changed)
        claimed = (result or {}).get("outcome", "success")
        summary = (result or {}).get("summary", "")
        last = {"worker": self.worker_id, "summary": summary, "progress": (result or {}).get("progress"),
                "agent": {"reason": run.reason, "exit": run.exit_code, "turns": run.turns,
                          "session": run.session_id}}
        if not changed:
            self.infra_failures = self.infra_failures + 1 if infra else 0
            last.update(outcome="failure", debug={
                "kind": "infra" if infra else "task",
                "detail": (result or {}).get("debug") or "; ".join(run.errors) or f"agent ended: {run.reason}",
                "transcript_tail": self._tail(ws.transcript)})
            self.coord.report(lease, Outcome("failure"), last)
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
        self.journal.write(step="pushed", task=lease.task, epoch=lease.epoch, kind="work",
                           nonce=lease.status.lease.get("nonce"), token=self._token(lease), sha=head)
        title = f"{lease.task}: {summary.splitlines()[0][:60]}" if summary else f"afe-fleet: {lease.task}"
        body = (f"Task `{lease.task}`, attempt {lease.epoch} by `{self.worker_id}`.\n\n{summary}\n\n"
                f"Verify: {'passed' if verify.ok else 'failed'}\n\n<!-- afe-fleet task={lease.task} epoch={lease.epoch} -->")
        try:
            pr = self.forge.ensure_pr(repo=proj["repo"], branch=branch, head_owner=owner, base=base,
                                      title=title, body=body, draft=not success)
        except ForgeError as e:
            raise GitError(["pr", branch], 1, str(e)) from e
        pr = {**pr, "head_sha": head, "spec": td.spec, "gh_user": self.host.gh_user, "branch": branch,
              "fetch": pr.get("fetch") or push_url}
        try:
            if reuse:
                self.forge.comment(pr, f"Attempt {lease.epoch} by `{self.worker_id}` pushed "
                                       f"`{head[:12]}`.\n\n{summary}\n\nVerify: {'passed' if verify.ok else 'failed'}")
            elif isinstance(prev, dict) and prev.get("number") and prev.get("repo") == proj["repo"] \
                    and prev.get("number") != pr.get("number") and self._pr_open(prev):
                self.forge.close(prev, f"Superseded by {pr.get('url') or '#' + str(pr.get('number'))} "
                                       f"(attempt {lease.epoch} of `{lease.task}`).")
        except ForgeError as e:  # cosmetic: the admin also closes superseded PRs
            log.warning("%s: %s", lease.task, e)
        self.journal.write(step="pr", task=lease.task, epoch=lease.epoch, kind="work",
                           nonce=lease.status.lease.get("nonce"), token=self._token(lease), pr=pr)
        if success:
            last["outcome"] = "success"
            self.coord.report(lease, Outcome("success", pr), last)
        else:
            last.update(outcome="partial", debug={"kind": "task", "verify_ok": verify.ok,
                                                  "detail": (result or {}).get("debug"),
                                                  "verify_log": verify.log[-4000:]})
            self.coord.report(lease, Outcome("part", pr), last)
        log.info("%s: %s, PR %s", lease.task, last["outcome"], pr.get("url") or pr.get("number"))

    # -- review --------------------------------------------------------------------------------

    def _review(self, lease: Lease, td: TaskDef, rev: str, ws: Workspace, cancel: threading.Event,
                repo: SharedRepo) -> None:
        pr = lease.status.core.pr
        if not isinstance(pr, dict):
            raise Fenced(f"{lease.task}: review without a PR")
        proj = td.project
        repo.fetch()  # origin/<base>, for diffs against the base
        pr_ref = f"refs/{self._local_name(lease)}/pr"
        sha = repo.fetch_ref(pr["fetch"], pr["branch"], pr_ref)
        if sha is None:
            raise GitError(["fetch", pr["branch"]], 1, f"cannot fetch the PR branch from {pr['fetch']}")
        wt = repo.add(ws.work, sha, refs=[pr_ref])
        lease.extra["worktree"] = wt
        self._write_task_files(ws, lease, td, rev, "review")
        rt = self._runtime(td)
        prompt = REVIEW_PROMPT.format(task=lease.task, sha=sha, base=proj.get("base", "main"),
                                      extra=proj.get("review_prompt", ""), **rt.paths(ws))
        run = run_agent(rt, ws, prompt, cancel=cancel, repairs=0, verify=lambda: Verify(True, ""))
        if lease.extra.get("fenced"):
            raise Fenced(f"{lease.task}: lease lost during review")
        review = {} if lease.extra.get("shutdown") else (self._read_json(ws.out / "review.json") or {})
        verdict = {"approve": Verdict.APPROVE, "changes": Verdict.CHANGES}.get(review.get("verdict"),
                                                                              Verdict.ABSTAIN)
        summary = review.get("summary") or f"review ended without a verdict ({run.reason})"
        if verdict is not Verdict.ABSTAIN:
            body = summary + "".join(f"\n\n`{c.get('path')}:{c.get('line')}`: {c.get('body')}"
                                     for c in review.get("comments", []) if isinstance(c, dict))
            self.forge.post_review(pr, sha, verdict.value, body)
        self.coord.review(lease, verdict, {"outcome": verdict.value, "worker": self.worker_id,
                                           "summary": summary, "reviewed_sha": sha,
                                           "comments": review.get("comments", [])})
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
