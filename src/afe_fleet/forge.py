"""Code-hosting operations (pull requests, reviews, merges, collaborators).

`GhForge` drives the `gh` CLI. The worker and admin depend only on the `Forge` interface, so
tests run against `tests/fakes.py` and never touch GitHub. `GhForge` is therefore not exercised
by the test suite; treat it as the part most likely to need fixes on first real use.

A PR record (stored in `status.json` as `pr`) is a dict:
    {"repo": "org/name", "number": 42, "url": "...", "branch": "task/x/e3",
     "fetch": "<git url holding branch>", "head_sha": "...", "draft": bool,
     "spec": "<task spec hash>", "gh_user": "<author login>"}
"""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Any

log = logging.getLogger(__name__)


class ForgeError(Exception):
    pass


class Forge:
    def login(self) -> str | None: ...

    def push_target(self, repo: str, gh_user: str | None, mode: str) -> tuple[str, str]:
        """(git url to push work branches to, PR head owner prefix or "")."""
        ...

    def ensure_pr(self, *, repo: str, branch: str, head_owner: str, base: str, title: str, body: str,
                  draft: bool) -> dict: ...

    def post_review(self, pr: dict, sha: str, verdict: str, body: str) -> None: ...

    def pr_state(self, pr: dict) -> dict:
        """{"state": open|closed|merged, "head_sha", "mergeable": bool|None,
            "checks": success|failure|pending|none, "merge_sha"}"""
        ...

    def merge(self, pr: dict, sha: str) -> bool: ...

    def close(self, pr: dict, comment: str) -> None: ...

    # access requests (lock repo)
    def open_issues(self, repo: str) -> list[dict]: ...
    def issue_comments(self, repo: str, number: int) -> list[str]: ...
    def comment_issue(self, repo: str, number: int, body: str) -> None: ...
    def close_issue(self, repo: str, number: int) -> None: ...
    def add_label(self, repo: str, number: int, label: str) -> None: ...
    def permission(self, repo: str, user: str) -> str: ...
    def invitations(self, repo: str) -> list[dict]: ...
    def delete_invitation(self, repo: str, invitation_id: int) -> None: ...
    def invite(self, repo: str, user: str) -> int: ...


class GhForge(Forge):
    def __init__(self, gh: str = "gh"):
        self.gh = gh

    def _run(self, *args: str, input: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
        p = subprocess.run([self.gh, *args], input=input, capture_output=True, text=True, timeout=120)
        if check and p.returncode != 0:
            raise ForgeError(f"gh {' '.join(args[:3])}: {p.stderr.strip()[-500:]}")
        return p

    def api(self, path: str, *, method: str = "GET", body: dict | None = None,
            check: bool = True) -> tuple[int, Any]:
        args = ["api", "-i", "-X", method, path]
        if body is not None:
            args += ["--input", "-"]
        p = self._run(*args, input=json.dumps(body) if body is not None else None, check=False)
        head, _, rest = p.stdout.partition("\r\n\r\n")
        if not rest:
            head, _, rest = p.stdout.partition("\n\n")
        try:
            code = int(head.split()[1])
        except (IndexError, ValueError):
            raise ForgeError(f"gh api {path}: {p.stderr.strip()[-300:]}") from None
        data = json.loads(rest) if rest.strip() else None
        if check and code >= 400:
            raise ForgeError(f"gh api {method} {path}: {code} {data}")
        return code, data

    def login(self) -> str | None:
        p = self._run("api", "user", "--jq", ".login", check=False)
        return p.stdout.strip() or None

    def push_target(self, repo: str, gh_user: str | None, mode: str) -> tuple[str, str]:
        if mode == "fork" and gh_user:
            name = repo.split("/")[1]
            self._run("repo", "fork", repo, "--clone=false", check=False)
            return f"git@github.com:{gh_user}/{name}.git", f"{gh_user}:"
        return f"git@github.com:{repo}.git", ""

    def ensure_pr(self, *, repo, branch, head_owner, base, title, body, draft) -> dict:
        head = f"{head_owner}{branch}"
        _, prs = self.api(f"repos/{repo}/pulls?state=open&head={head.replace(':', '%3A') if ':' in head else repo.split('/')[0] + '%3A' + branch}")
        if prs:
            pr = prs[0]
        else:
            _, pr = self.api(f"repos/{repo}/pulls", method="POST", body={
                "title": title, "body": body, "head": head, "base": base, "draft": draft})
        if pr.get("draft") and not draft:
            self._run("pr", "ready", str(pr["number"]), "-R", repo, check=False)
        return {"repo": repo, "number": pr["number"], "url": pr["html_url"], "branch": branch,
                "fetch": pr["head"]["repo"]["ssh_url"] if pr.get("head", {}).get("repo") else None,
                "head_sha": pr["head"]["sha"], "draft": draft}

    def post_review(self, pr: dict, sha: str, verdict: str, body: str) -> None:
        marker = f"<!-- afe-fleet review sha={sha} -->"
        event = {"approve": "APPROVE", "changes": "REQUEST_CHANGES"}.get(verdict, "COMMENT")
        code, _ = self.api(f"repos/{pr['repo']}/pulls/{pr['number']}/reviews", method="POST", check=False,
                           body={"commit_id": sha, "event": event, "body": f"{body}\n\n{marker}"})
        if code >= 400:  # e.g. GitHub refuses approving your own PR: fall back to a comment
            self.api(f"repos/{pr['repo']}/pulls/{pr['number']}/reviews", method="POST",
                     body={"commit_id": sha, "event": "COMMENT", "body": f"**{verdict}**\n\n{body}\n\n{marker}"})

    def pr_state(self, pr: dict) -> dict:
        _, p = self.api(f"repos/{pr['repo']}/pulls/{pr['number']}")
        state = "merged" if p.get("merged") else p["state"]
        sha = p["head"]["sha"]
        _, runs = self.api(f"repos/{pr['repo']}/commits/{sha}/check-runs?per_page=100")
        conclusions = [r.get("conclusion") for r in runs.get("check_runs", [])]
        _, combined = self.api(f"repos/{pr['repo']}/commits/{sha}/status")
        legacy = combined.get("state") if combined.get("total_count") else None
        if any(c in ("failure", "cancelled", "timed_out", "action_required") for c in conclusions) \
                or legacy in ("failure", "error"):
            checks = "failure"
        elif any(c is None for c in conclusions) or legacy == "pending":
            checks = "pending"
        elif conclusions or legacy == "success":
            checks = "success"
        else:
            checks = "none"
        return {"state": state, "head_sha": sha, "mergeable": p.get("mergeable"), "checks": checks,
                "merge_sha": p.get("merge_commit_sha") if p.get("merged") else None}

    def merge(self, pr: dict, sha: str) -> bool:
        code, _ = self.api(f"repos/{pr['repo']}/pulls/{pr['number']}/merge", method="PUT", check=False,
                           body={"sha": sha, "merge_method": "squash"})
        return code == 200

    def close(self, pr: dict, comment: str) -> None:
        self.api(f"repos/{pr['repo']}/issues/{pr['number']}/comments", method="POST", body={"body": comment})
        self.api(f"repos/{pr['repo']}/pulls/{pr['number']}", method="PATCH", body={"state": "closed"})

    def open_issues(self, repo: str) -> list[dict]:
        _, items = self.api(f"repos/{repo}/issues?state=open&per_page=100&sort=created&direction=asc")
        return [i for i in items if "pull_request" not in i]

    def issue_comments(self, repo: str, number: int) -> list[str]:
        _, items = self.api(f"repos/{repo}/issues/{number}/comments?per_page=100")
        return [c.get("body") or "" for c in items]

    def comment_issue(self, repo: str, number: int, body: str) -> None:
        self.api(f"repos/{repo}/issues/{number}/comments", method="POST", body={"body": body})

    def close_issue(self, repo: str, number: int) -> None:
        self.api(f"repos/{repo}/issues/{number}", method="PATCH", body={"state": "closed"})

    def add_label(self, repo: str, number: int, label: str) -> None:
        self.api(f"repos/{repo}/issues/{number}/labels", method="POST", body={"labels": [label]})

    def permission(self, repo: str, user: str) -> str:
        code, d = self.api(f"repos/{repo}/collaborators/{user}/permission", check=False)
        return d.get("permission", "none") if code == 200 and d else "none"

    def invitations(self, repo: str) -> list[dict]:
        _, items = self.api(f"repos/{repo}/invitations?per_page=100")
        return [{"id": i["id"], "login": (i.get("invitee") or {}).get("login"), "expired": bool(i.get("expired"))}
                for i in items]

    def delete_invitation(self, repo: str, invitation_id: int) -> None:
        self.api(f"repos/{repo}/invitations/{invitation_id}", method="DELETE")

    def invite(self, repo: str, user: str) -> int:
        code, _ = self.api(f"repos/{repo}/collaborators/{user}", method="PUT", body={"permission": "push"},
                           check=False)
        return code
