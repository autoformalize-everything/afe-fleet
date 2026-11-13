# afe-fleet

Distributed, cooperatively coordinated agents that make progress on a tree of tasks.

Workers claim a task, run an agent in a container, and report a PR. An admin process merges
approved, CI-green PRs and releases stuck leases. Coordination runs over either a GitHub repo
(one branch per task, compare-and-swap by push) or a small HTTP server; one fleet uses exactly
one of the two.

## Status

Design and formal model only. Nothing is implemented yet.

- [`DESIGN.md`](DESIGN.md): the design, the mapping to both backends, the failure matrix and
  the build order.
- [`TaskMachine.lean`](TaskMachine.lean): the task state machine and its proofs.

## Checking the model

```sh
lean TaskMachine.lean
```

Lean 4.35, core only. No output means every theorem checked.
