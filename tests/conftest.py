"""Shared fixtures: local bare repositories standing in for GitHub, a fake forge, a fake agent.

Nothing here touches the network or GitHub.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from afe_fleet.config import AgentConfig, HostConfig, load
from afe_fleet.forge import Forge

FAKE_AGENT = Path(__file__).parent / "fake_agent.py"

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@invalid", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@invalid"}


def git(*args: str, cwd: Path | str) -> str:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env={**os.environ, **GIT_ENV})
    assert p.returncode == 0, f"git {args}: {p.stderr}"
    return p.stdout.strip()


def seed_repo(bare: Path, files: dict[str, str], branch: str = "main") -> None:
    """Create a bare repo whose `branch` holds `files`."""
    bare.mkdir(parents=True)
    git("init", "-q", "--bare", "-b", branch, ".", cwd=bare)
    work = bare.with_name(bare.name + "-seed")
    git("clone", "-q", str(bare), str(work), cwd=bare.parent)
    git("checkout", "-q", "-b", branch, cwd=work)
    for path, text in files.items():
        p = work / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    git("add", "-A", cwd=work)
    git("commit", "-q", "-m", "seed", cwd=work)
    git("push", "-q", "origin", f"HEAD:{branch}", cwd=work)


def edit_repo(bare: Path, files: dict[str, str | None], branch: str = "main") -> None:
    work = bare.with_name(bare.name + "-seed")
    git("pull", "-q", "--rebase", "origin", branch, cwd=work)
    for path, text in files.items():
        p = work / path
        if text is None:
            p.unlink()
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
    git("add", "-A", cwd=work)
    git("commit", "-q", "-m", "edit", cwd=work)
    git("push", "-q", "origin", f"HEAD:{branch}", cwd=work)


@dataclass
class FakeForge(Forge):
    """PRs in memory; branches live in the local code repo."""

    prs: dict[int, dict] = field(default_factory=dict)
    reviews: list[tuple] = field(default_factory=list)
    checks: str = "success"
    mergeable: bool | None = True

    def login(self):
        return None

    def push_target(self, repo, gh_user, mode):
        return repo, ""

    def ensure_pr(self, *, repo, branch, head_owner, base, title, body, draft):
        for n, pr in self.prs.items():
            if pr["branch"] == branch and pr["state"] == "open":
                pr["draft"] = draft
                return {"repo": repo, "number": n, "url": f"pr/{n}", "branch": branch, "fetch": None}
        n = len(self.prs) + 1
        self.prs[n] = {"repo": repo, "branch": branch, "base": base, "title": title, "draft": draft,
                       "state": "open"}
        return {"repo": repo, "number": n, "url": f"pr/{n}", "branch": branch, "fetch": None}

    def post_review(self, pr, sha, verdict, body):
        self.reviews.append((pr["number"], sha, verdict, body))

    def _head(self, pr):
        out = subprocess.run(["git", "ls-remote", pr["fetch"], f"refs/heads/{pr['branch']}"],
                             capture_output=True, text=True).stdout
        return out.split("\t")[0] if out else None

    def pr_state(self, pr):
        p = self.prs[pr["number"]]
        return {"state": p["state"], "head_sha": self._head(pr), "mergeable": self.mergeable,
                "checks": self.checks, "merge_sha": p.get("merge_sha")}

    def merge(self, pr, sha):
        p = self.prs[pr["number"]]
        if self._head(pr) != sha:
            return False
        p["state"], p["merge_sha"] = "merged", sha
        return True

    def close(self, pr, comment):
        self.prs[pr["number"]]["state"] = "closed"
        self.prs[pr["number"]].setdefault("comments", []).append(comment)

    def comment(self, pr, body):
        self.prs[pr["number"]].setdefault("comments", []).append(body)

    def open_prs(self, repo, branch_prefix):
        return [{"repo": repo, "number": n, "branch": p["branch"], "url": f"pr/{n}"}
                for n, p in self.prs.items() if p["state"] == "open" and p["branch"].startswith(branch_prefix)]


FLEET_TOML = """
[fleet]
name = "{name}"

[coord]
mode = "{mode}"
lock_repo = "{lock}"
url = "{url}"

[lease]
heartbeat = "{heartbeat}"
ttl = "{ttl}"

[admin]
done_grace = "0s"
poll = "1s"
"""


@dataclass
class World:
    root: Path
    mode: str = "github"
    url: str = "http://127.0.0.1:1"

    @property
    def config_repo(self) -> Path:
        return self.root / "config.git"

    @property
    def lock_repo(self) -> Path:
        return self.root / "lock.git"

    @property
    def code_repo(self) -> Path:
        return self.root / "code.git"

    def build(self, *, heartbeat: str = "1s", ttl: str = "3s", tasks: dict[str, str] | None = None) -> World:
        seed_repo(self.code_repo, {"README.md": "code\n"})
        self.lock_repo.mkdir()
        git("init", "-q", "--bare", ".", cwd=self.lock_repo)
        tasks = tasks if tasks is not None else {
            "demo/a": "Write solution.txt containing ok.\n",
            "demo/b": "Depends on a.\n",
        }
        files = {
            "fleet.toml": FLEET_TOML.format(name="t", mode=self.mode, lock=self.lock_repo, url=self.url,
                                            heartbeat=heartbeat, ttl=ttl),
            "tasks/demo/project.toml": f'repo = "{self.code_repo}"\nbase = "main"\n'
                                       'verify = ["sh", "-c", "grep -q ok solution.txt"]\n',
        }
        for tid, text in tasks.items():
            files[f"tasks/{tid}/TASK.md"] = text
        if "demo/b" in tasks:
            # a task-level verify overrides the project's
            files["tasks/demo/b/task.toml"] = 'deps = ["demo/a"]\nverify = ["sh", "-c", "test -f solution.txt"]\n'
        seed_repo(self.config_repo, files)
        return self

    def loaded(self):
        return load(str(self.config_repo))

    def host(self, user: str, mode: str = "work", **kw) -> HostConfig:
        agent = AgentConfig(runtime="local", command=[sys.executable, str(FAKE_AGENT)], env={"FAKE_AGENT": mode},
                            max_wall=60, stall=20, repairs=1)
        return HostConfig(worker_id=f"{user}/host", gh_user=user, agent=agent, **kw)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.delenv("AFE_FLEET_HOME", raising=False)
    for var, sub in (("XDG_CACHE_HOME", "cache"), ("XDG_STATE_HOME", "state"), ("XDG_CONFIG_HOME", "config"),
                     ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(var, str(tmp_path / "home" / sub))
    for k, v in GIT_ENV.items():
        monkeypatch.setenv(k, v)


@pytest.fixture
def world(tmp_path) -> World:
    return World(tmp_path / "w").build()
