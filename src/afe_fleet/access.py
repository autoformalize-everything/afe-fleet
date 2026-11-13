"""Write-access requests (GitHub mode, docs/DESIGN.md section 6, "Access requests").

An open issue in the lock repo with `write-request` in its title gets its creator write access.
The grantee is always the issue's author (authenticated by GitHub); nothing in the title or
body is read. Every decision is derived from GitHub's current state, and comments carry a
marker so none is posted twice; a crash anywhere is repaired by the next sweep.
"""

from __future__ import annotations

from .forge import Forge

MARK = "<!-- afe-fleet access={} -->"
MAX_REINVITES = 3


class AccessRequests:
    def __init__(self, forge: Forge, repo: str, mode: str, deny: tuple[str, ...] = ()):
        self.forge = forge
        self.repo = repo
        self.mode = mode
        self.deny = {d.lower() for d in deny}

    def _say(self, number: int, step: str, text: str, comments: list[str]) -> None:
        mark = MARK.format(step)
        if not any(mark in c for c in comments):
            self.forge.comment_issue(self.repo, number, f"{text}\n\n{mark}")
            comments.append(mark)

    def sweep(self) -> list[str]:
        acts = []
        issues = [i for i in self.forge.open_issues(self.repo)
                  if "write-request" in (i.get("title") or "").lower()
                  and (i.get("user") or {}).get("type") == "User"]
        if not issues:
            return acts
        invites = None
        for issue in issues:
            n, user = issue["number"], issue["user"]["login"]
            comments = self.forge.issue_comments(self.repo, n)
            perm = self.forge.permission(self.repo, user)
            if perm in ("write", "admin"):  # check first: PUT would *change* a maintainer's role
                self._say(n, "granted", f"@{user} has write access to `{self.repo}`.", comments)
                self.forge.close_issue(self.repo, n)
                acts.append(f"access: {user} has write; closed #{n}")
                continue
            if user.lower() in self.deny or self.mode == "off":
                self._say(n, "declined", "Write requests are not accepted for this account.", comments)
                self.forge.close_issue(self.repo, n)
                acts.append(f"access: declined {user} (#{n})")
                continue
            labels = {(lbl.get("name") or "").lower() for lbl in issue.get("labels", [])}
            if "write-request-failed" in labels:
                continue  # left for a human
            if self.mode == "approve" and "write-approved" not in labels:
                continue
            if invites is None:
                invites = self.forge.invitations(self.repo)
            mine = [i for i in invites if (i.get("login") or "").lower() == user.lower()]
            if any(not i["expired"] for i in mine):
                self._say(n, "invited", f"Invitation sent: accept it at "
                          f"https://github.com/{self.repo}/invitations", comments)
                continue
            reinvites = sum(1 for c in comments if MARK.format("reinvited") in c)
            if mine:
                for i in mine:
                    self.forge.delete_invitation(self.repo, i["id"])
                if reinvites >= MAX_REINVITES:
                    self._say(n, "expired", "The invitation expired too many times; closing. "
                              "Open a new issue to try again.", comments)
                    self.forge.close_issue(self.repo, n)
                    acts.append(f"access: {user} never accepted; closed #{n}")
                    continue
                self.forge.comment_issue(self.repo, n, f"The invitation expired; sending a new one.\n\n"
                                         f"{MARK.format('reinvited')}")
            code = self.forge.invite(self.repo, user)
            if code == 201:
                self._say(n, "invited", f"Invitation sent: accept it at "
                          f"https://github.com/{self.repo}/invitations", comments)
                acts.append(f"access: invited {user} (#{n})")
            elif code == 204:
                acts.append(f"access: granted {user} directly (#{n})")
            elif code == 422:
                # over GitHub's 50 invitations per repository per day: a later sweep retries
                acts.append(f"access: invite {user} deferred (HTTP 422, #{n})")
            else:
                self._say(n, "failed", f"Could not grant access (HTTP {code}). A maintainer will look.",
                          comments)
                self.forge.add_label(self.repo, n, "write-request-failed")
                acts.append(f"access: invite {user} failed with {code} (#{n})")
        return acts
