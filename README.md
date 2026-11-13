# afe-fleet

Distributed, cooperatively coordinated agents that make progress on a tree of tasks.

Workers claim a task, run an agent in a container, and report a PR. An admin process merges
approved, CI-green PRs and releases stuck leases. Coordination runs over either a GitHub repo
(one `task-lock/<task-id>` branch per task, compare-and-swap by push) or a small HTTP server; one fleet uses exactly
one of the two.

## Status

Design, formal model, and a test that the model runs on plain git operations. The worker,
admin and server are not implemented yet.

- [`docs/DESIGN.md`](docs/DESIGN.md): the design, the mapping to both backends, the failure
  matrix and the build order.
- [`docs/design-lean/`](docs/design-lean/): the formal model and the git test, as one Lake
  project.
  - [`TaskMachine.lean`](docs/design-lean/TaskMachine.lean): the task state machine and its
    proofs.
  - [`GitBackend.lean`](docs/design-lean/GitBackend.lean): the GitHub backend's operations
    (`snapshot`, `submit`, audit) as calls to `git ls-remote`, `git fetch` and `git push`.
  - [`GitBackendTest.lean`](docs/design-lean/GitBackendTest.lean): every state transition and
    every denied update, with real pushes; and the failure modes of deleting lock branches
    after acceptance, each reproduced without its guard and prevented with it.

## Checking

```sh
cd docs/design-lean
lean TaskMachine.lean     # the proofs; no output means every theorem checked
lake test                 # the git test, against a throwaway local bare repository
```

The run against a real remote is optional; without one it is skipped. To include it (it
works under `task-lock-test/<run>/` and deletes its branches afterwards):

```sh
cd docs/design-lean
lake test -- --remote git@github.com:OWNER/REPO.git
```

`AFE_FLEET_TEST_REMOTE=<url>` does the same, and `--remote-only` skips the local run.

Lean 4.35; no dependencies beyond the toolchain and `git`.
