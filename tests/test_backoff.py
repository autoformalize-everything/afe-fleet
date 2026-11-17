"""The backoff after failures counts from the last strike, not from the last outcome."""

from dataclasses import replace
from datetime import UTC, datetime

from afe_fleet.coord import _cooling
from afe_fleet.model import Claim, Expire, Outcome, Report, Review, Verdict
from afe_fleet.status import Status, apply

NOW = datetime(2026, 11, 17, 1, 0, 0, tzinfo=UTC).timestamp()


def iso(minutes_ago: float) -> str:
    return datetime.fromtimestamp(NOW - 60 * minutes_ago, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_strikes_record_when_they_happened():
    s = apply(Status.initial("t"), Claim("a", 0))
    assert s.failed_at is None
    s = apply(s, Report("a", 1, Outcome("failure")), last={"outcome": "failure"})
    assert s.core.strikes == 1 and s.failed_at is not None
    first = replace(s, failed_at="2026-01-01T00:00:00Z")
    s = apply(apply(first, Claim("b", 1)), Report("b", 2, Outcome("success", {"n": 1})), last={"outcome": "success"})
    assert s.failed_at == "2026-01-01T00:00:00Z"  # a success keeps it
    s = apply(apply(s, Claim("c", 2)), Expire(3), last={"outcome": "expired"})
    assert s.core.strikes == 2 and s.failed_at != "2026-01-01T00:00:00Z"  # an expired lease is a strike
    assert Status.loads(s.dumps()).failed_at == s.failed_at


def test_a_review_does_not_restart_the_backoff():
    s = apply(apply(Status.initial("t"), Claim("a", 0)), Report("a", 1, Outcome("failure")), last={})
    s = apply(apply(s, Claim("a", 1)), Report("a", 2, Outcome("success", {"n": 1})), last={})
    s = apply(apply(s, Claim("r", 2)), Review("r", 3, Verdict.CHANGES), last={"outcome": "changes"})
    assert s.core.strikes == 1  # one strike: 10 minutes
    changed_now = replace(s, last={"outcome": "changes", "at": iso(1)})
    assert not _cooling(replace(changed_now, failed_at=iso(20)), NOW)  # failed 20 min ago: free
    assert _cooling(replace(changed_now, failed_at=iso(5)), NOW)  # failed 5 min ago: still cooling


def test_statuses_from_before_failed_at():
    s = replace(Status.initial("t"), core=replace(Status.initial("t").core, strikes=1))
    assert _cooling(replace(s, last={"outcome": "failure", "at": iso(5)}), NOW)
    assert not _cooling(replace(s, last={"outcome": "failure", "at": iso(15)}), NOW)
    assert not _cooling(replace(s, last={"outcome": "changes", "at": iso(1)}), NOW)  # not a failure
