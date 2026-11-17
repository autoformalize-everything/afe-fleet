"""Polling GitHub cheaply: PR states in one GraphQL request, ETags on REST reads."""

import subprocess

from conftest import FakeForge
from test_e2e import make_admin, make_worker

from afe_fleet.forge import GhForge, checks_state, graphql_pr_state


def test_checks_state():
    assert checks_state([], []) == "none"
    assert checks_state(["success", "skipped"], ["success"]) == "success"
    assert checks_state(["success", None], []) == "pending"
    assert checks_state([], ["expected"]) == "pending"
    assert checks_state(["success", "timed_out", None], []) == "failure"
    assert checks_state([], ["success", "error"]) == "failure"


def test_graphql_answer():
    node = {"state": "OPEN", "mergeable": "CONFLICTING", "headRefOid": "abc", "mergeCommit": None,
            "commits": {"nodes": [{"commit": {"statusCheckRollup": {"contexts": {"nodes": [
                {"__typename": "CheckRun", "conclusion": "SUCCESS"},
                {"__typename": "CheckRun", "conclusion": None},
                {"__typename": "StatusContext", "state": "SUCCESS"}]}}}}]}}
    assert graphql_pr_state(node) == {"state": "open", "head_sha": "abc", "mergeable": False,
                                      "checks": "pending", "merge_sha": None}
    merged = {"state": "MERGED", "mergeable": "UNKNOWN", "headRefOid": "abc", "mergeCommit": {"oid": "m"},
              "commits": {"nodes": [{"commit": {"statusCheckRollup": None}}]}}
    assert graphql_pr_state(merged) == {"state": "merged", "head_sha": "abc", "mergeable": None,
                                        "checks": "none", "merge_sha": "m"}


def test_rest_reads_revalidate_with_etags():
    f = GhForge()
    sent = []
    replies = iter(['HTTP/2.0 200 OK\r\nEtag: W/"v1"\r\n\r\n{"n": 1}',
                    'HTTP/2.0 304 Not Modified\r\nEtag: W/"v1"\r\n\r\n',
                    'HTTP/2.0 200 OK\r\nEtag: W/"v2"\r\n\r\n{"n": 2}'])

    def run(*args, input=None, check=True):
        sent.append(args)
        return subprocess.CompletedProcess(args, 0, next(replies), "")
    f._run = run
    assert f.api("repos/o/r/pulls/1") == (200, {"n": 1})
    assert f.api("repos/o/r/pulls/1") == (200, {"n": 1})  # 304: the cached body, no rate limit spent
    assert ["-H", 'If-None-Match: W/"v1"'] == list(sent[1][-2:])
    assert f.api("repos/o/r/pulls/1") == (200, {"n": 2})


class CountingForge(FakeForge):
    def __init__(self):
        super().__init__()
        self.single = self.batched = 0

    def pr_state(self, pr):
        self.single += 1
        return super().pr_state(pr)

    def pr_states(self, prs):
        self.batched += 1
        return {(p["repo"], int(p["number"])): FakeForge.pr_state(self, p) for p in prs}


def test_the_admin_asks_for_all_prs_at_once(world):
    forge = CountingForge()
    alice = make_worker(world, "alice", "work", forge)
    admin = make_admin(world, forge)
    assert alice.run_once()  # demo/a: a PR in review
    admin.sweep()
    assert (forge.batched, forge.single) == (1, 0)
