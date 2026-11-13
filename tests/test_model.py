"""The Python state machine agrees with the Lean model, pair by pair.

tests/fixtures/lean_step.json is generated from docs/design-lean/ExportStep.lean: every state
reachable within 8 events over a small alphabet, with every event `step` accepts there and its
result. Every other event of the alphabet must be denied.
"""

import json
from pathlib import Path

import pytest

from afe_fleet.model import (INIT, Accept, Claim, Core, Expire, Heartbeat, Need, Outcome, Report, Review, Rework,
                             Verdict, event_from_json, event_to_json, step)
from afe_fleet.status import Status, apply

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "lean_step.json").read_text())


def test_fixture_is_substantial():
    assert len(FIXTURE["alphabet"]) == 153
    assert len(FIXTURE["cases"]) > 200


def test_events_round_trip_in_lean_encoding():
    for j in FIXTURE["alphabet"]:
        assert event_to_json(event_from_json(j)) == j


def test_every_pair_matches_lean():
    alphabet = [event_from_json(j) for j in FIXTURE["alphabet"]]
    checked = 0
    for case in FIXTURE["cases"]:
        state = Core.from_json(case["state"])
        accepted = {i: Core.from_json(t) for i, t in case["accepted"]}
        for i, ev in enumerate(alphabet):
            assert step(state, ev) == accepted.get(i), (case["state"], FIXTURE["alphabet"][i])
            checked += 1
    assert checked == len(alphabet) * len(FIXTURE["cases"])


def test_happy_path():
    t = INIT
    for ev in [Claim("a", 0), Heartbeat("a", 1), Report("a", 1, Outcome("success", 7)), Claim("b", 1),
               Review("b", 2, Verdict.APPROVE), Accept()]:
        t = step(t, ev)
        assert t is not None
    assert t.need is Need.DONE


def test_zombie_and_self_review():
    t = step(step(INIT, Claim("a", 0)), Expire(1))
    assert step(t, Report("a", 1, Outcome("success", 7))) is None
    t = step(step(t, Claim("a", 1)), Report("a", 2, Outcome("success", 7)))
    assert t.need is Need.REVIEW and step(t, Claim("a", 2)) is None


def test_status_payload():
    s = Status.initial("x/y")
    s = apply(s, Claim("w", 0), meta={"nonce": "n1", "spec": "s"})
    assert s.lease == {"nonce": "n1", "spec": "s", "worker": "w"} and s.core.epoch == 1
    s2 = apply(s, Heartbeat("w", 1), meta={"heartbeat_at": "t"})
    assert s2.lease["heartbeat_at"] == "t" and s2.core == s.core
    s3 = apply(s2, Report("w", 1, Outcome("failure")), last={"outcome": "failure"})
    assert s3.lease is None and s3.core.strikes == 1 and s3.last["outcome"] == "failure"
    assert Status.loads(s3.dumps()) == s3
    assert apply(s3, Rework()) is None


def test_outcome_validation():
    with pytest.raises(ValueError):
        Outcome("success")
    with pytest.raises(ValueError):
        Outcome("failure", 3)
