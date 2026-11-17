"""trace.py: the kernel's process events, as spans of the fleet's containers."""

import json
import os

from afe_fleet import timeline
from afe_fleet.events import EventLog
from afe_fleet.trace import Containers, Tracer

CID = "ab" * 32
OTHER = "cd" * 32


def cgroups(tmp_path):
    (tmp_path / "system.slice" / f"docker-{CID}.scope").mkdir(parents=True)
    (tmp_path / "system.slice" / f"docker-{OTHER}.scope").mkdir()
    (tmp_path / "system.slice" / "ssh.service").mkdir()
    ino = lambda p: (tmp_path / "system.slice" / p).stat().st_ino  # noqa: E731
    names = {CID: "afe-fleet-afe-alice_box-0", OTHER: "someone-elses"}
    asked = []

    def namer(cid):
        asked.append(cid)
        return names.get(cid)
    return Containers(root=tmp_path, namer=namer), ino(f"docker-{CID}.scope"), ino(f"docker-{OTHER}.scope"), \
        ino("ssh.service"), asked


def test_containers_by_cgroup(tmp_path):
    c, ours, theirs, host, asked = cgroups(tmp_path)
    assert c.name(ours) == "afe-fleet-afe-alice_box-0"
    assert c.name(theirs) is None and c.name(host) is None and c.name(12345) is None
    c.name(ours), c.name(host)
    assert asked == [CID, OTHER]  # each container named once; host cgroups never


def test_lines_to_spans(tmp_path):
    c, ours, _, host, _ = cgroups(tmp_path)
    out = tmp_path / "events"
    t = Tracer(EventLog(out, "trace"), c, offset=1000.0)
    S = 10**9
    for line in [
        f"E {1 * S} {ours} 100 1 {5 * S} /bin/sh",  # docker exec's first program
        "A 100 0 sh", "A 100 1 -c", "A 100 2 lake build",
        f"F {2 * S} {ours} 101 100",  # sh forks...
        f"E {2 * S} {host} 999 1 0 /usr/bin/ls",  # (the host: ignored)
        "A 999 0 ls",
        f"E {3 * S} {ours} 101 100 0 /usr/bin/lake",  # ...and the child runs lake
        f"F {4 * S} {ours} 102 101",
        "A 101 0 lake", "A 101 1 build",  # arguments may come after other CPUs' lines
        f"X {5 * S} {ours} 102 9 0",  # killed by SIGKILL
        f"X {13 * S} {ours} 101 256 {7 * S}",  # lake: exit 1 after 7 s of CPU
        "garbage line",
        f"E {14 * S} {ours} 100 1 {6 * S} /bin/true",  # sh execs true: sh's span ends here
    ]:
        t.line(line)
    t.close(1020.0)
    recs = [json.loads(x) for f in out.iterdir() for x in f.read_text().splitlines()]
    by = {(r["pid"], r["cmd"]): r for r in recs}
    assert set(by) == {(101, "fork of /bin/sh"), (101, "/usr/bin/lake"), (102, "fork of /usr/bin/lake"),
                       (100, "/bin/sh"), (100, "/bin/true")}
    lake = by[(101, "/usr/bin/lake")]
    assert lake["args"] == "lake build" and lake["t"] == 1003.0 and lake["dur"] == 10.0
    assert lake["exit"] == 1 and lake["cpu"] == 7.0 and lake["ppid"] == 100
    assert by[(102, "fork of /usr/bin/lake")]["signal"] == 9
    sh = by[(100, "/bin/sh")]
    assert sh["args"] == "sh -c lake build" and sh["replaced"] and sh["dur"] == 13.0 and sh["cpu"] == 1.0
    assert by[(100, "/bin/true")]["open"] and by[(100, "/bin/true")]["dur"] == 6.0
    assert all(r["w"] == "afe-fleet-afe-alice_box-0" for r in recs)


def test_event_files_for_the_sudo_user(tmp_path):
    me = (os.getuid(), os.getgid())
    EventLog(tmp_path / "fleet" / "new", "trace", owner=me).write({"t": 1.0})
    f = next((tmp_path / "fleet" / "new").iterdir())
    assert (f.stat().st_uid, f.stat().st_gid) == me


def test_processes_in_the_timeline():
    events = [
        {"t": 10.0, "w": "alice/box-0", "ph": "B", "span": "attempt", "task": "a", "epoch": 1, "kind": "work"},
        {"t": 11.0, "w": "alice/box-0", "ph": "B", "span": "container-start", "container": "afe-fleet-x-0"},
        {"t": 12.0, "w": "alice/box-0", "ph": "E", "span": "container-start"},
        {"t": 13.0, "w": "afe-fleet-x-0", "ph": "X", "dur": 5.0, "span": "proc", "container": "afe-fleet-x-0",
         "pid": 7, "cmd": "/usr/bin/lake", "args": "lake build", "exit": 0, "cpu": 9.5},
        {"t": 14.0, "w": "afe-fleet-x-0-verify", "ph": "X", "dur": 1.0, "span": "proc",
         "container": "afe-fleet-x-0-verify", "pid": 8, "cmd": "/usr/bin/git", "args": "git diff", "exit": 1},
        {"t": 15.0, "w": "afe-fleet-y-0", "ph": "X", "dur": 1.0, "span": "proc", "container": "afe-fleet-y-0",
         "pid": 9, "cmd": "fork of /bin/sh", "args": "sh"},
        {"t": 30.0, "w": "alice/box-0", "ph": "E", "span": "attempt", "outcome": "success"},
    ]
    spans, instants = timeline.spans(events)
    procs = {s.args["pid"]: s.w for s in spans if s.name == "proc"}
    assert procs == {7: "alice/box-0", 8: "alice/box-0", 9: "container afe-fleet-y-0"}
    trace = timeline.chrome_trace(spans, instants)["traceEvents"]
    groups = {e["args"]["name"] for e in trace if e["name"] == "process_name"}
    assert "alice/box-0: processes" in groups
    rows = {e["args"]["name"] for e in trace if e["name"] == "thread_name"}
    assert "lake [7]" in rows and "alice/box-0" in rows and "afe-fleet-x-0" not in rows
    text = timeline.summary(spans, instants)
    assert "lake" in text and "9.5s" in text and "2 program runs, 1 failed or killed" in text
    workers = text.split("\n\n")[0]
    assert "afe-fleet" not in workers  # containers are not workers
