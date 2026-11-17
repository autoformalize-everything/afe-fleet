"""`sudo afe-fleet trace`: every process the agents run, from the kernel, into the event log.

A bpftrace program (after https://gist.github.com/m1el/6f7686c89c263b852b60a817ab0ff5be) reports
process forks, execs and exits on the whole machine, each with its cgroup. This keeps those in
afe-fleet's containers (a cgroup v2 id is the inode of the cgroup's directory; Docker's are
`docker-<id>.scope` or `docker/<id>`), and writes one complete span per program image to
`fleets/<fleet>/events/trace-<date>.jsonl`:

    {"t": 1794873455.1, "ph": "X", "dur": 12.3, "span": "proc", "w": "afe-fleet-afe-alice_box-0",
     "container": "afe-fleet-afe-alice_box-0", "pid": 4242, "ppid": 4200, "cmd": "/usr/bin/lake",
     "args": "lake build", "exit": 0, "cpu": 31.2}

A fork starts a span (`fork of <parent's program>`), an exec ends the current span and starts
the next, and the exit ends the last; `cpu` is the process's user + system time, its exited
threads included, its children not. `afe-fleet timeline` draws the processes of each worker's
containers beside the worker's own row (the worker's `container-start` spans name them).

Needs root, for bpf; the event files it creates are given to the user who ran sudo.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .events import EventLog

log = logging.getLogger(__name__)

PROGRAM = r"""
#define CLONE_THREAD 0x00010000

// sched_process_fork fires for threads too: remember whether the clone asked for a thread
tracepoint:syscalls:sys_enter_clone3 { @thread[tid] = (args->uargs->flags & CLONE_THREAD) != 0; }
tracepoint:syscalls:sys_exit_clone3 { delete(@thread[tid]); }
tracepoint:syscalls:sys_enter_clone { @thread[tid] = (args->clone_flags & CLONE_THREAD) != 0; }
tracepoint:syscalls:sys_exit_clone { delete(@thread[tid]); }

tracepoint:sched:sched_process_fork /!@thread[tid]/
{
    printf("F %llu %llu %d %d\n", nsecs, cgroup, args->child_pid, curtask->group_leader->pid);
}

tracepoint:syscalls:sys_enter_execve, tracepoint:syscalls:sys_enter_execveat
{
    // an exec from a thread other than the main one also reports the main thread's exit
    if (pid != tid) { @exec_tid[pid] = tid; }
    printf("E %llu %llu %d %d %llu %s\n", nsecs, cgroup, pid, curtask->real_parent->tgid,
           curtask->utime + curtask->stime + curtask->signal->utime + curtask->signal->stime,
           str(args->filename));
    // one line per argument, so lines from other CPUs cannot split one; argv ends with NULL,
    // and the environment follows it: never read past that
    $i = 0;
    while ($i < 32) {
        $p = args->argv[$i];
        if ($p == 0) { break; }
        printf("A %d %d %s\n", pid, $i, str($p));
        $i++;
    }
}

tracepoint:sched:sched_process_exit
{
    $tid = @exec_tid[tid];
    delete(@exec_tid[tid]);
    $tid = $tid ? $tid : tid;
    if ((int32)$tid == curtask->group_leader->pid) {
        printf("X %llu %llu %d %d %llu\n", nsecs, cgroup, pid, curtask->exit_code,
               curtask->utime + curtask->stime + curtask->signal->utime + curtask->signal->stime);
    }
}
"""

CGROUP_ROOT = Path("/sys/fs/cgroup")
SCOPE = re.compile(r"^(?:docker-)?([0-9a-f]{64})(?:\.scope)?$")


def docker_name(cid: str) -> str | None:
    try:
        p = subprocess.run(["docker", "inspect", "-f", "{{.Name}}", cid], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout.strip().lstrip("/") or None if p.returncode == 0 else None


class Containers:
    """cgroup id → container name, for the containers whose names start with `prefix`."""

    def __init__(self, prefix: str = "afe-fleet-", root: Path = CGROUP_ROOT, namer=docker_name):
        self.prefix, self.root, self.namer = prefix, root, namer
        self.names: dict[int, str | None] = {}  # None: not one of ours

    def _scan(self) -> dict[int, str]:
        """inode → container id of Docker's container cgroups."""
        found = {}
        for parent in (self.root / "system.slice", self.root / "docker"):
            try:
                entries = list(os.scandir(parent))
            except OSError:
                continue
            for e in entries:
                m = SCOPE.match(e.name)
                if m and e.is_dir(follow_symlinks=False):
                    try:
                        found[e.inode()] = m.group(1)
                    except OSError:
                        pass
        return found

    def name(self, cgroup: int) -> str | None:
        if cgroup not in self.names:
            cid = self._scan().get(cgroup)
            n = self.namer(cid) if cid else None
            self.names[cgroup] = n if n and n.startswith(self.prefix) else None
        return self.names[cgroup]


@dataclass
class Image:
    start: float  # seconds since the epoch
    container: str
    pid: int
    ppid: int
    cmd: str
    args: str = ""
    forked: bool = False  # started by a fork, before any exec
    cpu0: int = 0  # the process's CPU time (ns) when this image started: CPU time survives exec


class Tracer:
    """Turns the program's lines into `proc` spans."""

    def __init__(self, out: EventLog, containers: Containers, offset: float):
        self.out, self.containers = out, containers
        self.offset = offset  # wall time = monotonic (bpftrace's nsecs) + offset
        self.images: dict[int, Image] = {}

    def wall(self, ns: str) -> float:
        return int(ns) / 1e9 + self.offset

    def emit(self, img: Image, end: float, **fields) -> None:
        self.out.write({"t": round(img.start, 6), "ph": "X", "dur": round(max(0.0, end - img.start), 6),
                        "span": "proc", "w": img.container, "container": img.container, "pid": img.pid,
                        "ppid": img.ppid, "cmd": f"fork of {img.cmd}" if img.forked else img.cmd,
                        "args": img.args[:500], **fields})

    def line(self, line: str) -> None:
        kind, _, rest = line.partition(" ")
        try:
            if kind == "F":  # F nsecs cgroup child parent
                ns, cg, child, parent = rest.split()
                name = self.containers.name(int(cg))
                if name is not None:
                    p = self.images.get(int(parent))
                    self.images[int(child)] = Image(self.wall(ns), name, int(child), int(parent),
                                                    p.cmd if p else "?", p.args if p else "", forked=True)
            elif kind == "E":  # E nsecs cgroup pid ppid cpu_ns filename
                ns, cg, pid, ppid, cpu, filename = rest.split(" ", 5)
                name = self.containers.name(int(cg))
                if name is None:
                    return
                t, pid_ = self.wall(ns), int(pid)
                prev = self.images.get(pid_)
                if prev is not None:
                    self.emit(prev, t, replaced=True, cpu=round((int(cpu) - prev.cpu0) / 1e9, 3))
                self.images[pid_] = Image(t, name, pid_, int(ppid), filename, cpu0=int(cpu))
            elif kind == "A":  # A pid index arg
                pid, i, arg = (rest.split(" ", 2) + [""])[:3]
                img = self.images.get(int(pid))
                if img is not None and not img.forked and len(img.args) < 500:
                    img.args = f"{img.args} {arg}" if int(i) else arg
            elif kind == "X":  # X nsecs cgroup pid exit_code cpu_ns
                ns, _cg, pid, code, cpu = rest.split()
                img = self.images.pop(int(pid), None)
                if img is not None:
                    status, sig = int(code) >> 8 & 0xFF, int(code) & 0x7F
                    self.emit(img, self.wall(ns), **({"signal": sig} if sig else {"exit": status}),
                              cpu=round((int(cpu) - img.cpu0) / 1e9, 3))
        except ValueError:
            log.debug("unparsed: %r", line)

    def close(self, at: float) -> None:
        """Spans for the processes still running when tracing stops."""
        for img in self.images.values():
            self.emit(img, at, open=True)
        self.images.clear()


def run(events_dir: Path, *, bpftrace: str = "bpftrace", duration: float | None = None,
        owner: tuple[int, int] | None = None, prefix: str = "afe-fleet-") -> int:
    if os.geteuid() != 0:
        raise RuntimeError("tracing needs root: sudo $(command -v afe-fleet) trace")
    exe = shutil.which(bpftrace)
    if exe is None:
        raise RuntimeError(f"{bpftrace} is not installed")
    out = EventLog(events_dir, "trace", owner=owner)
    tracer = Tracer(out, Containers(prefix), time.time() - time.monotonic())
    proc = subprocess.Popen([exe, "-q", "-B", "line", "-e", PROGRAM], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, errors="replace", bufsize=1)
    log.info("tracing the processes of %s* containers into %s; ctrl-c stops", prefix, events_dir)
    timer = threading.Timer(duration, proc.terminate) if duration else None
    if timer:
        timer.start()
    try:
        for raw in proc.stdout:
            tracer.line(raw.rstrip("\n"))
    except KeyboardInterrupt:
        pass
    finally:
        if timer:
            timer.cancel()
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        tracer.close(time.time())
    err = proc.stderr.read().strip() if proc.stderr else ""
    if proc.returncode not in (0, -15, 143):
        log.error("bpftrace exited %s: %s", proc.returncode, err[-3000:])
        return 1
    return 0
