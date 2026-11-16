# container-tools

A Claude Code plugin (a function-hooks mod) that makes the agent's tools see only a Docker
container, while `claude` itself, its login and its transcripts stay on the host.

- **Bash, Read, Write, Edit, NotebookEdit** are answered by the plugin from inside the container,
  via the Docker Engine API. Claude Code's own implementations never run.
- **Every other tool is removed** (MCP tools, WebFetch, Monitor, LSP, worktrees, cron, …), for
  the main agent and subagents alike. Kept: Agent, TodoWrite, Task*, AskUserQuestion, plan mode,
  plus anything listed in `extraTools`.
- **The container is brought up on demand.** A stopped container is started, a paused one
  unpaused, and a missing one created from `image` (pulled if absent) when `image` is set. After
  `startAttempts` failed tries (default 3), the tool call fails with the reason.
- The system prompt gains a short section saying where the tools run and what the working
  directory is.

Requires Claude Code with function-hooks support (written against 2.1.325; the API is early
access).

## Run

```sh
CONTAINER_TOOLS_CONTAINER=my-box CONTAINER_TOOLS_IMAGE=ubuntu:latest CONTAINER_TOOLS_WORKDIR=/work \
  claude --plugin-dir /path/to/afe-fleet/container-tools
```

Headless (`-p`) works the same. Add `--allowedTools ...` or `CONTAINER_TOOLS_PERMISSIONS=allow`,
because nobody can answer a permission prompt there.

## Configuration

Each option is a `userConfig` field (`/config`, or `pluginConfigs` in settings). A
`CONTAINER_TOOLS_*` environment variable overrides it.

| Option | Env | Default | |
| --- | --- | --- | --- |
| `container` | `CONTAINER_TOOLS_CONTAINER` | `claude-sandbox` | name or id |
| `image` | `CONTAINER_TOOLS_IMAGE` | (none) | create the container from this when it is missing; empty: never create |
| `workdir` | `CONTAINER_TOOLS_WORKDIR` | the container's | Bash starts here (created if missing) |
| `network` | `CONTAINER_TOOLS_NETWORK` | `none` | `NetworkMode` of a container the plugin creates |
| `mounts` | `CONTAINER_TOOLS_MOUNTS` | (none) | comma-separated `host:ctr[:ro]` binds of a container the plugin creates |
| `user` | `CONTAINER_TOOLS_USER` | the container's | user for creation and for every exec |
| `permissions` | `CONTAINER_TOOLS_PERMISSIONS` | `session` | `session`: follow the session's rules and mode (an ask becomes a Yes/No question; headless, a refusal). `allow`: run whatever the rules do not explicitly deny |
| `extraTools` | `CONTAINER_TOOLS_EXTRA_TOOLS` | (none) | comma-separated extra tool names to keep |
| `tmpdir` | `CONTAINER_TOOLS_TMPDIR` | `/tmp` | where run files and uploads go, under `.container-tools-<uid>/`; must not be a tmpfs mount |
| `startAttempts` | `CONTAINER_TOOLS_START_ATTEMPTS` | `3` | |

A container the plugin creates runs a sleep loop under `docker-init`, and is labelled
`afe-fleet.container-tools=1`. Containers you create yourself just need `/bin/sh` and `base64`.
`bash` and `setsid` are used when present (busybox images work).

The daemon is found the way the docker CLI finds it: `DOCKER_HOST` (`unix://` or plain `tcp://`),
else `DOCKER_CONTEXT` or `currentContext` in `$DOCKER_CONFIG/config.json`, else
`/var/run/docker.sock`. `DOCKER_API_VERSION` is honoured. TLS client certificates and `ssh://`
are not supported.

## How it works

- `$.http.fetch` (with `socketPath` for unix sockets) talks to the Engine API; the docker CLI is
  not used. Response bodies arrive as text, so every exec runs with a tty (one raw stream, no
  multiplexing headers) and the helper scripts print base64 between random markers. File
  contents reach the container as a ustar archive (`PUT /containers/{id}/archive`), which stays
  valid UTF-8 for text. The archive is staged, then copied with `cat >` so the target keeps its
  owner and mode.
- **Bash**: each call runs `. cmdfile` in a fresh `bash` (or `sh`) in its own process group. The
  directory is carried between calls; variables are not. A watchdog in the container enforces
  `timeout`, and an interrupt kills the group. Output is capped at 8 KB head + 3 KB tail per
  stream, with the full output kept in the container (path given). `run_in_background` detaches
  the command and returns where its output and exit status will appear.
- **Read**: text with offset/limit (line numbers added by Claude Code), images (≤ 3.75 MB) and
  PDFs (whole, ≤ 20 MB). Binary files are refused. **Edit/Write/NotebookEdit** follow Claude
  Code's rules (read first, unchanged since read, unique match unless `replace_all`).
- **Failing closed.** `tool.call` refuses any tool outside the set, and each hook has a `.catch`
  that refuses the call, so an error never falls through to Claude Code's host implementation.
  This holds only while the module is loaded: if it fails to load, the session has Claude
  Code's ordinary host tools. Check that the plugin loaded, for example with the `tools` list in
  `-p --output-format stream-json`'s init message.

## Limits

- Permission rules and checks were written for host paths. In `session` mode, a container path
  outside the host's working directory counts as "outside the working directories". Mount the
  project at the same path, or use `permissions=allow` inside a disposable container.
- `!` shell commands typed at the prompt, hooks, status lines and other user-side features still
  run on the host. Only the model's tools are contained.
- Background Bash jobs are not Claude Code tasks: no completion notification, no TaskStop.
- No PDF page ranges, no image resizing, no Read of a huge file without `offset`/`limit`.

## Tests

```sh
claude plugin validate container-tools
claude plugin test container-tools      # pure helpers and the fail-closed paths
```
