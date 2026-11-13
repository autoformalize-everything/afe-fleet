# afe-fleet

Distributed, cooperatively coordinated agents that make progress on a tree of tasks.

Workers claim a task, run an agent in a container, and report a PR. Other workers review it.
An admin process merges approved, CI-green PRs, releases stuck leases and retires finished
tasks. Coordination runs over either a GitHub repo (one `task-lock/<task-id>` branch per task in
flight, compare-and-swap by push) or a small HTTP server; one fleet uses exactly one of the two.

- [`docs/DESIGN.md`](docs/DESIGN.md): the design, both backends, the failure matrix, and what
  is implemented.
- [`docs/design-lean/`](docs/design-lean/): the formal model (`TaskMachine.lean`), the
  GitHub backend as plain git operations with a test of every transition and denied update
  (`GitBackendTest.lean`), and the exporter that checks the Python state machine against the
  model (`ExportStep.lean`).
- [`src/afe_fleet/`](src/afe_fleet/): the implementation (Python, standard library only).

## Install

Requires Python 3.12+, `git`, and for real work `gh` and the agent CLI (`claude`), both logged
in. Agents run directly on the machine as your user, with full permissions: use a dedicated
machine or account.

Everything afe-fleet stores lives in `~/.local/share/afe-fleet/` (override with `AFE_FLEET_HOME`):

```
repos/<repo>-<hash>.git            code repositories, cloned once per machine and shared by every
                                   worker slot and process
mirrors/                           config-repo mirrors
fleets/<fleet>/attempts/<task>-e<epoch>/work   a git worktree per attempt
fleets/<fleet>/journal-<slot>.json, lock-<slot>.git, server.sqlite
```

Configuration stays in `~/.config/afe-fleet/<fleet>.toml` (per host) and
`~/.config/systemd/user/` (services).

```sh
uv tool install git+https://github.com/autoformalize-everything/afe-fleet   # as a tool
afe-fleet version

# or from a checkout
git clone git@github.com:autoformalize-everything/afe-fleet.git && cd afe-fleet
uv run afe-fleet version
```

## Run

`SOURCE` is `org/repo` (or a git URL or path) whose default branch holds `fleet.toml`, or a path
to a `fleet.toml` file.

```sh
afe-fleet worker run SOURCE [--slots N] [--kinds work,review] [--filter 'zlib/*'] [--once]
afe-fleet admin run SOURCE [--once]
afe-fleet admin cancel SOURCE TASK [--remove]     # the holder's next heartbeat fails
afe-fleet server run SOURCE [--db PATH] [--listen HOST:PORT]   # server mode only
afe-fleet status SOURCE
```

As systemd user services (one per fleet and role; installing again updates the unit):

```sh
afe-fleet service install worker SOURCE     # writes ~/.config/systemd/user/afe-fleet-worker-<fleet>.service,
                                            # enables, starts, and enables lingering
afe-fleet service status worker <fleet>
afe-fleet service uninstall worker <fleet>
afe-fleet service list
journalctl --user -fu afe-fleet-worker-<fleet>
```

The unit runs the same `afe-fleet` you installed it with: the `uv tool` entry point, or the
checkout's `.venv/bin/afe-fleet`. A tool-installed worker updates itself when `fleet.toml`
pins a different `version` (from `source`, tag `v<version>`), then exits for systemd to restart
it; a checkout only warns.

## Configure

The config repo:

```
fleet.toml
tasks/<project>/project.toml      repo, base, image, verify (nearest one above a task applies)
tasks/<task-id>/TASK.md           the task; a task is a directory with TASK.md and none below it
tasks/<task-id>/task.toml         optional: deps = [...], kinds = ["work", "review"], priority
done/<task-id>.json               written by the admin
cancelled/<task-id>.json          written by the admin
```

`fleet.toml`:

```toml
[fleet]
name = "afe"                                  # service names, state and cache directories
version = "0.1.0"                             # optional: workers converge on this
source = "git+https://github.com/autoformalize-everything/afe-fleet"

[coord]
mode = "github"                               # or "server"
lock_repo = "autoformalize-everything/fleet-coord"
# url = "http://coord.example:8737"           # server mode

[lease]
heartbeat = "5m"
ttl = "20m"

[access]
write_request = "off"                         # "auto" | "approve" | "off"

[admin]
done_grace = "1h"
poll = "20s"
```

Per host, `~/.config/afe-fleet/<fleet>.toml`:

```toml
[worker]
id = "alice/box1"          # default <gh-user>/<hostname>
slots = 2
kinds = ["review", "work"]
filters = ["zlib/*"]
push = "fork"              # push work branches to your fork ("origin" if you can push upstream)

[agent]
# Runs directly on this machine, as you, in a git worktree; it uses your normal Claude login.
command = ["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json",
           "--verbose", "--dangerously-skip-permissions"]
max_wall = "4h"
stall = "20m"
repairs = 2
```

## Test

```sh
uv run pytest                       # Python: model vs Lean traces, both backends, worker, admin, services
cd docs/design-lean
lean TaskMachine.lean               # the proofs
lake test                           # every transition and denied update over real git pushes
lake test -- --remote URL           # the same against a real remote (optional)
lake exe export_step > ../../tests/fixtures/lean_step.json   # regenerate the Python fixture
```

The Python tests use local bare repositories, a fake agent (`tests/fake_agent.py`) and a fake
forge; they never touch GitHub. `forge.py` (the `gh` calls) and a real agent CLI are not exercised by any
test yet.
