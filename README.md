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
fleets/<fleet>/logs/<task>/<time>-e<epoch>-<kind>-<worker>/   each attempt's transcript (gzipped
                                   JSON Lines), task files, result, Codex sessions; kept, never pruned
fleets/<fleet>/workers/<worker-id>/     one worker process's journal and mirrors, per slot
fleets/<fleet>/admin/, server.sqlite
```

Configuration stays in `~/.config/afe-fleet/<fleet>.toml` (per host) and
`~/.config/systemd/user/` (services).

Several worker processes can run on one host in one home, for example one per agent CLI: give
each its own `--id` (and usually its own `--host-config`). Each process keeps its journal,
mirrors and container names under its worker id and holds a lock on it, so a second process
with the same id refuses to start.

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
tasks/<project>/project.toml      repo, base, image, verify, prepare, paths (nearest one above a task applies)
tasks/<task-id>/TASK.md           the task; a task is a directory with TASK.md and none below it
tasks/<task-id>/task.toml         optional: deps = [...], kinds = ["work", "review"], priority,
                                  verify = [...], paths = [...], [agent] model/effort
tasks/<task-id>/done.json         written by the admin when the task is accepted
tasks/<task-id>/cancelled.json    written by the admin when the task is cancelled
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
# max_open_prs = 2          # start no new work while 2 of this worker's PRs await review or merging
# spare_capacity = 1.0     # claim only with spare subscription capacity (below)
# spare_ease_in = 0.1      # ... with a head start after each window resets

[agent]
# Runs directly on this machine, as you, in a git worktree; it uses your normal Claude login.
command = ["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json",
           "--verbose", "--dangerously-skip-permissions"]
max_wall = "4h"
stall = "20m"
repairs = 2
# model = "opus-5.5"       # host default when the task and project say nothing
# effort = "high"
```

### Where the time goes: the event log and timelines

Every worker and admin process appends what it does to
`~/.local/share/afe-fleet/fleets/<fleet>/events/<worker-id>-<date>.jsonl`: spans with a begin and
an end (`idle` with its reason, `claim`, `attempt` with `fetch`, `worktree`, `merge-base`,
`container-start`, `prepare`, `agent` › `turn` › `tool`, `verify`, `push`, `pr`, `report`,
`review-post`, `cleanup`; the admin's `sweep` and its actions). Tool calls are timed as the
agent's output stream reports them, the same way for Claude and Codex.

```sh
afe-fleet timeline [--fleet afe] [--since 2026-11-17T09:00Z] [-o trace.json]
```

prints where the time went (each worker's busy and idle time by reason, time per phase, the
slowest tools, every attempt) and writes a Chrome trace: open it in https://ui.perfetto.dev or
`chrome://tracing` for one row per worker slot. `--events DIR` reads logs copied from other
hosts. Each attempt's transcripts are kept beside it, in `fleets/<fleet>/logs/`.

### Fewer review rounds

Each "changes requested" costs an attempt and another review, so the worker removes the
avoidable causes:

- **Scope.** `paths = ["src/part/*", "tests/part.txt"]` in `task.toml` (or `project.toml`) lists
  what a task may change, as globs on repository paths (`*` crosses directories). After the
  verify command the worker checks every file the branch changed, committed or not, against it;
  anything else fails the attempt's verify with the list, like a failed test, so the agent gets
  to undo it before a reviewer has to ask.
- **Self-review.** The work prompt names the verify command and has the agent do the reviewer's
  checks itself before finishing: run verify, read its own diff against the scope, re-read what
  it cites. The result file's path is given as an absolute path outside the repository.
- **The base, merged first.** When work resumes on an existing pull request whose base has moved,
  the worker merges `origin/<base>` before the agent starts. A clean merge costs the agent
  nothing; a conflict is left in progress, with a prompt that asks for a minimal resolution and
  nothing else.
- **Reviews of what changed.** When a pull request is sent back after an approval (merge conflict,
  CI), the approved commit travels with it, and the next reviewer is told to review only
  `git diff <approved> <head>`. Reviewers are also told to ask for everything in one review and
  only for what the task and the project's rules require.

### Reviews before new work

A claim always prefers a review, then work on an existing PR, then new work. But a worker cannot
review its own PRs, so with nothing else to review it would keep opening new ones. `max_open_prs
= N` in `[worker]` (or `worker run --max-open-prs N`) stops that: while N of the worker's own PRs
(across its slots) await review or merging, it starts no new work. It still reviews and still
continues tasks that already have a PR (changes requested, merge conflicts); otherwise it idles
until its PRs move.

### Spare capacity

`spare_capacity = L` in `[worker]` (or `worker run --spare-capacity L`) makes the worker claim
only while the agent's subscription is used no faster than its rate-limit windows refill. In
every window the subscription reports (Claude: the 5-hour session, the week, and per-model weeks;
Codex: its 5-hour and weekly windows):

    used < L × elapsed + ease_in × (1 − elapsed) + allowance

`used` is the fraction of the window's limit spent and `elapsed` the fraction of the window
passed. The pace line `L × elapsed` alone is `used / elapsed < L`: at `L = 1` the worker never
spends faster than the clock, and below 1 it leaves room for whoever else uses the account. Right
after a reset it allows almost nothing, so `spare_ease_in` (`--spare-ease-in`) grants a head
start that fades linearly to zero by the window's end, and `spare_allowance`
(`--spare-allowance`) a constant margin; both are fractions of the limit and default to 0. For
example `L = 1, ease_in = 0.1` allows 10% at a reset, 55% halfway, 100% at the end.

A window with nothing used never blocks, and a hard limit the provider reports always does.
Usage that cannot be read counts as no spare capacity. Running attempts are not interrupted;
the check gates claims only, and readings are cached for five minutes across slots.

Usage is read through the agent CLI, without a model request: a `/usage` message to `claude` in
stream-json mode, or `account/rateLimits/read` to `codex app-server`. `[agent] usage_command`
replaces that with a command printing the same JSON.

### Running the agent's tools in a container

With `runtime = "container"`, `claude` still runs on the host with your login, but its tools
(Bash, Read, Write, Edit, NotebookEdit) act only inside a container made for each attempt from
the project's `image`. It loads the [`container-tools`](src/afe_fleet/container-tools/) plugin
shipped in the package, and every other host tool and all MCP servers are removed. The worktree
(with its git directory) and the attempt's task, out and home directories are mounted at their
own paths. The container is removed after the attempt; verify runs in a fresh one.

```toml
[agent]
runtime = "container"
# network = "none"                        # docker's default network unless set
mounts = ["~/.cache/afe-fleet/container:/cache"]   # caches, shared between attempts
env = { XDG_CACHE_HOME = "/cache" }      # set in the container
# docker_args = ["--memory", "16g", "--cpus", "4"]
```

```toml
# tasks/<project>/project.toml
image = "afe-c-agent:pilot"               # needs /bin/sh, base64 and git; bash if tasks use it
```

The image is pulled if missing. The container runs as your uid, so files in the worktree stay
yours; `HOME` inside it is the attempt's empty `home/`.

`project.toml` may name a `prepare` command (`prepare = ["sh", "scripts/setup.sh"]`): it runs
once per attempt before the agent starts, in the worktree, where the tools run (inside the
attempt's container in this mode, on the host otherwise). If it fails, the attempt ends as an
error, like a container that cannot start. One use: point a project's package directory at a
prebuilt, read-only copy baked into the image instead of downloading it per attempt. `--dangerously-skip-permissions` is not
needed in this mode: the plugin allows the contained tools itself.

### Codex as the agent

```toml
[agent]
cli = "codex"              # default command: ["codex", "exec", "--json"]
runtime = "container"      # or "local"
```

Each turn is one `codex exec --json` process (the prompt on stdin); repairs and the wrap-up are
`codex exec resume <thread>` turns of the same session. Codex keeps its own configured model
(`model` settings in the task tree name Claude models; put `-m <model>` in `command` to pick
one); `effort` is passed as `-c model_reasoning_effort=…`, `max` as `xhigh`.

In the container runtime Codex runs on the host with your login, but every command, file read
and patch goes through `codex-exec-server` inside the attempt's container: the static binary
next to `codex` is mounted read-only (`codex_exec_server` to use another), and a per-attempt
`CODEX_HOME` (your `auth.json` symlinked, `config.toml` copied) has an `environments.toml` whose
only environment spawns `docker exec -i <container> codex-exec-server --listen stdio`. No port is
opened, and there is no fallback to the host. Codex's own sandbox (bubblewrap) cannot run in a
container, so it is off there (`-s danger-full-access`); the container is the sandbox. Codex's
session files live in that per-attempt home and are removed with the attempt.

Why a per-attempt `CODEX_HOME` (a workaround, as of Codex 0.167): a stdio exec-server
(`program`/`args`) can only be named in `$CODEX_HOME/environments.toml`, which has no path
override and no `-c` equivalent (`codex-rs/exec-server/src/environment_toml.rs`). The settings
outside it take only a WebSocket URL: `CODEX_EXEC_SERVER_URL` with no auth at all (a token in the
URL is ignored), and the app-server's `environment/add` with a bearer token sent only over
`wss://` or loopback. Either would need container networking and an open port. If Codex gains a
CLI flag for a stdio executor, `Runtime.codex_env` is the one place to change.

### Models and effort

`model` and `effort` can be set in `task.toml`, `project.toml` and the host config, each in an
`[agent]` table with optional `[agent.work]` / `[agent.review]` sub-tables, for example:

```toml
# tasks/bzip2/project.toml
[agent]
effort = "high"

[agent.work]
model = "fable-5.2"        # this project's work needs the strongest model
```

**Defaults** when nothing sets a model: Opus 5.5 (`claude-opus-5-5`) for work, Sonnet 5.5
(`claude-sonnet-5-5`) for review. Effort defaults to the agent CLI's own.

- **Models:** `fable-5.2`, `fable-5.1`, `opus-5.5`, `opus-5`, `sonnet-5.5`, `sonnet-5`, `haiku-5.5`;
  the family names `fable`, `opus`,
  `sonnet`, `haiku` (the agent CLI picks the latest); or a full `claude-…` model ID.
- **Effort:** `low`, `medium`, `high`, `xhigh`, `max`.
- **Precedence**, field by field: `worker run --model/--effort`, then `task.toml`, then
  `project.toml`, then the host config, then the defaults above; within each,
  `[agent.<kind>]` before `[agent]`.
- A task whose `task.toml` or `project.toml` names an unknown model or effort is not claimable;
  the worker logs why.
- The choice is passed as `--model` / `--effort` (host config `model_args` / `effort_args` change
  that for other agent CLIs) and recorded in the task's status, the PR body and the review.

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
