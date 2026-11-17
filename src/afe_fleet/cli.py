"""afe-fleet command line.

    afe-fleet worker run SOURCE [options]      claim tasks and run the agent on them
    afe-fleet admin run SOURCE [--once]        merge, expire, retire, grant access
    afe-fleet server run SOURCE [options]      the coordination server (server mode)
    afe-fleet status SOURCE                    task table
    afe-fleet service install ROLE SOURCE      systemd user service, unique per fleet and role
    afe-fleet service uninstall ROLE SOURCE|FLEET
    afe-fleet service status ROLE SOURCE|FLEET
    afe-fleet service list
    afe-fleet version

SOURCE is `org/repo` (or a git URL/path) whose default branch holds fleet.toml, or a path to a
fleet.toml file.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from dataclasses import replace
from pathlib import Path

from . import __version__, service, update
from .capacity import SpareCapacity, Usage
from .config import AlreadyRunning, HostConfig, Loaded, fleet_home, hold_lock, load, worker_home
from .events import EventLog
from .configrepo import ConfigRepo
from .gitutil import repo_url

log = logging.getLogger("afe_fleet")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s")


def _sigterm_to_interrupt() -> None:
    def handler(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, handler)


def _forge(args):
    from .forge import GhForge
    return GhForge()


def _coord(loaded: Loaded, who: str, slot_dir: Path):
    """A slot's coordination, over its own config- and lock-repo mirrors (`slot_dir`): concurrent
    fetches into one repository collide."""
    from .coord import GitCoord, ServerCoord
    from .lockrepo import LockRepo
    cfg = ConfigRepo(loaded.config.url, slot_dir / "config.git")
    fleet = loaded.fleet
    if fleet.mode == "github":
        lock = LockRepo(repo_url(fleet.lock_repo), slot_dir / "lock.git", who=who)
        return GitCoord(lock, cfg), cfg
    return ServerCoord(fleet.url, cfg), cfg


def _admin_coord(loaded: Loaded, store: str = "lock.git"):
    from .coord import GitAdmin, ServerAdmin, ServerCoord
    from .lockrepo import LockRepo
    fleet = loaded.fleet
    if fleet.mode == "github":
        lock = LockRepo(repo_url(fleet.lock_repo), fleet_home(fleet.name) / "admin" / store, who="admin")
        return GitAdmin(lock, loaded.config)
    return ServerAdmin(ServerCoord(fleet.url, loaded.config))


# -- worker ------------------------------------------------------------------------------------

def cmd_worker(args) -> int:
    from .worker import Worker, run_slots
    loaded = load(args.source)
    forge = _forge(args)
    gh_user = args.gh_user
    def host_config(user):
        return HostConfig.load(loaded.fleet.name, Path(args.host_config) if args.host_config else None,
                               worker_id=args.id, gh_user=user, slots=args.slots,
                               kinds=args.kinds.split(",") if args.kinds else None, filters=args.filter,
                               model=args.model, effort=args.effort, spare_capacity=args.spare_capacity,
                               spare_ease_in=args.spare_ease_in, spare_allowance=args.spare_allowance,
                               max_open_prs=args.max_open_prs, usage_warning=args.usage_warning)

    host = host_config(gh_user)
    if host.gh_user is None:
        login = forge.login()
        host = host_config(login) if args.id is None and login else replace(host, gh_user=login)
    home = worker_home(loaded.fleet.name, host.worker_id)
    try:
        held = hold_lock(home / "lock", f"worker {host.worker_id} of fleet {loaded.fleet.name}")  # noqa: F841
    except AlreadyRunning as e:
        log.error("%s; give each worker process on a host its own --id (or [worker] id)", e)
        return 75
    log.info("afe-fleet %s worker %s for fleet %s (%s mode, %d slot%s)", __version__, host.worker_id,
             loaded.fleet.name, loaded.fleet.mode, host.slots, "" if host.slots == 1 else "s")
    update_lock = threading.Lock()

    def maintain() -> bool:
        with update_lock:
            if loaded.rev is not None:  # fleet.toml lives in a repo: re-read it
                try:
                    fresh = load(args.source)
                    loaded.fleet = fresh.fleet
                except Exception as e:  # noqa: BLE001 - keep the old config
                    log.warning("could not reload fleet.toml: %s", e)
            if update.maybe_update(loaded.fleet.version, loaded.fleet.source):
                unit = service.unit_dir() / service.unit_name("worker", loaded.fleet.name)
                if unit.exists():
                    service.install("worker", loaded.fleet.name, _service_args(args), start=False, linger=False)
                log.info("updated; exiting so systemd starts the new version")
                raise SystemExit(update.EXIT_UPDATED)
            if update.too_old(loaded.fleet.min_version):
                log.error("afe-fleet %s is older than min_version %s; not claiming", __version__,
                          loaded.fleet.min_version)
                return False
            return True

    events = EventLog(fleet_home(loaded.fleet.name) / "events", host.worker_id)
    usage = Usage(host.agent.cli, host.agent.usage_command or None, exe=os.path.expanduser(host.agent.command[0]),
                  events=events.bind(host.worker_id), warn_below=host.usage_warning)
    capacity = None
    if host.spare_capacity is not None:
        capacity = SpareCapacity(host.agent.cli, host.spare_capacity, ease_in=host.spare_ease_in,
                                 allowance=host.spare_allowance, usage=usage)
        log.info("claiming only with spare %s capacity: used < %g × elapsed + %g × (1 − elapsed) + %g in every "
                 "window", host.agent.cli, host.spare_capacity, host.spare_ease_in, host.spare_allowance)

    def make(slot: int) -> Worker:
        coord, cfg = _coord(loaded, host.worker_id, home / f"slot-{slot}")
        w = Worker(replace(loaded, config=cfg), host, coord, forge, slot=slot, slot_dir=home / f"slot-{slot}")
        w.events_log = events
        w.usage = usage
        w.maintain = maintain
        w.capacity = capacity
        if args.once:
            w.idle = 0
        return w

    if args.once:
        w = make(0)
        w.recover()
        return 0 if w.run_once() else 3
    _sigterm_to_interrupt()
    run_slots(make, host.slots)
    return 0


def _service_args(args) -> list[str]:
    return [args.source]


# -- admin -------------------------------------------------------------------------------------

def cmd_admin(args) -> int:
    from .admin import Admin
    loaded = load(args.source)
    if args.action == "cancel":  # a one-off beside a running admin: its own mirror
        adm = Admin(loaded, _admin_coord(loaded, "cancel.git"), _forge(args))
        for a in adm.cancel(args.task, reason=args.reason, remove=args.remove):
            print(a)
        return 0
    try:
        held = hold_lock(fleet_home(loaded.fleet.name) / "admin" / "lock",  # noqa: F841
                         f"the admin of fleet {loaded.fleet.name}")
    except AlreadyRunning as e:
        log.error("%s", e)
        return 75
    adm = Admin(loaded, _admin_coord(loaded), _forge(args))
    adm.ev = EventLog(fleet_home(loaded.fleet.name) / "events", "admin").bind("admin")
    log.info("afe-fleet %s admin for fleet %s (%s mode)", __version__, loaded.fleet.name, loaded.fleet.mode)
    if args.once:
        for a in adm.sweep():
            print(a)
        return 0
    _sigterm_to_interrupt()
    try:
        adm.run_forever()
    except KeyboardInterrupt:
        adm.stop.set()
    return 0


# -- server ------------------------------------------------------------------------------------

def cmd_server(args) -> int:
    from urllib.parse import urlparse

    from .server import Server
    loaded = load(args.source)
    fleet = loaded.fleet
    u = urlparse(fleet.url or "http://127.0.0.1:8737")
    host, port = (args.listen.rsplit(":", 1) if args.listen else (u.hostname or "127.0.0.1", u.port or 8737))
    db = Path(args.db) if args.db else fleet_home(fleet.name) / "server.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    srv = Server(db, host, int(port), ttl=fleet.ttl)
    log.info("afe-fleet %s server for fleet %s on %s (db %s, ttl %.0fs)", __version__, fleet.name, srv.url, db,
             fleet.ttl)
    _sigterm_to_interrupt()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.stop()
    return 0


# -- timeline ----------------------------------------------------------------------------------

def _events_dir(args, *, need_events: bool) -> Path:
    from .config import data_home
    if args.events:
        return Path(args.events)
    fleets = sorted(p.name for p in (data_home() / "fleets").glob("*")
                    if (p / "events").is_dir() or (not need_events and p.is_dir()))
    name = args.fleet or (fleets[0] if len(fleets) == 1 else None)
    if name is None:
        raise ValueError(f"name the fleet with --fleet (fleets on this host: {', '.join(fleets) or 'none'})")
    return fleet_home(name) / "events"


def cmd_trace(args) -> int:
    import pwd

    from . import trace
    owner = None
    if os.geteuid() == 0 and os.environ.get("SUDO_UID"):
        # run by sudo: write to the invoking user's event log, as that user's files
        owner = (int(os.environ["SUDO_UID"]), int(os.environ.get("SUDO_GID", os.environ["SUDO_UID"])))
        if not os.environ.get("AFE_FLEET_HOME"):
            os.environ["HOME"] = pwd.getpwuid(owner[0]).pw_dir
            os.environ.pop("XDG_DATA_HOME", None)
    directory = _events_dir(args, need_events=False)
    return trace.run(directory, bpftrace=args.bpftrace, duration=args.duration, owner=owner, prefix=args.prefix)


def cmd_timeline(args) -> int:
    import json

    from . import timeline
    directory = _events_dir(args, need_events=True)
    events = timeline.read_events(directory, timeline.parse_time(args.since), timeline.parse_time(args.until))
    if not events:
        raise ValueError(f"no events in {directory}")
    spans, instants = timeline.spans(events)
    out = Path(args.output)
    out.write_text(json.dumps(timeline.chrome_trace(spans, instants)))
    print(timeline.summary(spans, instants))
    print(f"\nTrace: {out} ({len(spans)} spans); open it in https://ui.perfetto.dev or chrome://tracing")
    return 0


# -- fuzz --------------------------------------------------------------------------------------

def cmd_fuzz(args) -> int:
    import threading

    from .admin import Admin
    from .fuzz import Runner, load_campaigns
    loaded = load(args.source)
    home = fleet_home(loaded.fleet.name) / "fuzz"
    try:
        held = hold_lock(home / "lock", f"the fuzz runner of fleet {loaded.fleet.name}")  # noqa: F841
    except AlreadyRunning as e:
        log.error("%s", e)
        return 75

    def cancel(task: str, reason: str):
        return Admin(loaded, _admin_coord(loaded, "fuzz-lock.git"), None).cancel(task, reason=reason)

    events = EventLog(fleet_home(loaded.fleet.name) / "events", "fuzz")
    runner = Runner(loaded.config, home, cancel=cancel, events=events.bind("fuzz"))
    campaigns = load_campaigns(loaded.config, loaded.config.fetch())
    wanted = set(args.campaign or [])
    chosen = [c for c in campaigns if not wanted or c.key in wanted or c.name in wanted]
    if not chosen:
        raise ValueError(f"no campaign{' named ' + ', '.join(sorted(wanted)) if wanted else 's'} in the config repo "
                         f"(tasks/<project>/fuzz.toml); there are: {', '.join(c.key for c in campaigns) or 'none'}")
    if args.import_dir:
        for c in chosen:
            print(f"{c.key}: imported {runner.import_corpus(c, Path(args.import_dir))} corpus files")
    if args.once:
        for c in chosen:
            for a in runner.round(c):
                print(f"{c.key}: {a}")
        return 0
    stop = threading.Event()
    log.info("fuzz runner for %s", ", ".join(c.key for c in chosen))
    try:
        runner.run_forever(stop, {c.key for c in chosen})
    except KeyboardInterrupt:
        stop.set()
    return 0


# -- status ------------------------------------------------------------------------------------

def cmd_status(args) -> int:
    from .admin import Admin
    loaded = load(args.source)
    print(Admin(loaded, _admin_coord(loaded), None).status_md())
    return 0


# -- service -----------------------------------------------------------------------------------

def _fleet_name(arg: str) -> str:
    if any(u.endswith(f"-{arg}.service") for u in service.installed()):
        return arg
    return load(arg).fleet.name


def cmd_service(args) -> int:
    if args.action == "list":
        for u in service.installed():
            print(u)
        return 0
    if args.action == "install":
        fleet = load(args.source).fleet.name
        path, changed, notes = service.install(args.role, fleet, [args.source, *args.extra],
                                               start=not args.no_start, linger=not args.no_linger)
        print(f"{'wrote' if changed else 'unchanged'} {path}")
        for n in notes:
            print(n)
        if not args.no_start:
            print(f"started; logs: journalctl --user -fu {path.name}")
        return 0
    fleet = _fleet_name(args.source)
    if args.action == "uninstall":
        removed = service.uninstall(args.role, fleet)
        print(f"removed {service.unit_name(args.role, fleet)}" if removed else "not installed")
        return 0
    print(service.status(args.role, fleet))
    return 0


# -- parser ------------------------------------------------------------------------------------

def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="afe-fleet", description="Cooperatively coordinated agent workers.")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("version", help="print the version")

    w = sub.add_parser("worker", help="run a worker").add_subparsers(dest="action", required=True)
    wr = w.add_parser("run", help="claim tasks and work on them")
    wr.add_argument("source")
    wr.add_argument("--slots", type=int, help="tasks worked on in parallel (default 1)")
    wr.add_argument("--id", help="worker id (default <gh-user>/<hostname>)")
    wr.add_argument("--gh-user", help="GitHub login (default: from gh)")
    wr.add_argument("--kinds", help="comma-separated: work,review")
    wr.add_argument("--filter", action="append", help="glob on task ids; repeatable")
    wr.add_argument("--host-config", help="per-host config (default ~/.config/afe-fleet/<fleet>.toml)")
    wr.add_argument("--model", help="override the model for every task (e.g. fable-5.2, opus-5.5, sonnet-5.5)")
    wr.add_argument("--effort", help="override the effort level: low, medium, high, xhigh, max")
    wr.add_argument("--spare-capacity", type=float, metavar="LIMIT",
                    help="claim only while, in every rate-limit window of the agent's subscription, used < "
                         "LIMIT × elapsed + EASE_IN × (1 − elapsed) + ALLOWANCE (LIMIT 1: no faster than "
                         "the windows refill)")
    wr.add_argument("--spare-ease-in", type=float, metavar="EASE_IN",
                    help="head start right after a window resets, fading to 0 at its end (fraction of the limit)")
    wr.add_argument("--spare-allowance", type=float, metavar="ALLOWANCE",
                    help="constant margin over the pace line (fraction of the limit)")
    wr.add_argument("--max-open-prs", type=int, metavar="N",
                    help="start no new work while N of this worker's own PRs await review or merging "
                         "(reviews and work on an existing PR continue)")
    wr.add_argument("--usage-warning", type=float, metavar="FRACTION",
                    help="warn when a subscription window has less than this fraction of its limit left "
                         "(default 0.1; 0: never)")
    wr.add_argument("--once", action="store_true", help="one claim and attempt, then exit")

    a = sub.add_parser("admin", help="run the admin").add_subparsers(dest="action", required=True)
    ar = a.add_parser("run", help="reconcile continuously")
    ar.add_argument("source")
    ar.add_argument("--once", action="store_true", help="one sweep, print the actions, exit")
    ac = a.add_parser("cancel", help="withdraw a task; its holder's next heartbeat fails")
    ac.add_argument("source")
    ac.add_argument("task")
    ac.add_argument("--remove", action="store_true", help="also delete tasks/<task>/ from the config repo")
    ac.add_argument("--reason", default="cancelled by the admin")

    s = sub.add_parser("server", help="run the coordination server").add_subparsers(dest="action", required=True)
    sr = s.add_parser("run", help="serve")
    sr.add_argument("source")
    sr.add_argument("--db", help="SQLite path (default ~/.local/share/afe-fleet/fleets/<fleet>/server.sqlite)")
    sr.add_argument("--listen", help="host:port (default from [coord] url)")

    st = sub.add_parser("status", help="print the task table")
    st.add_argument("source")

    tl = sub.add_parser("timeline", help="a trace of what workers spent their time on, from the event logs")
    tl.add_argument("--fleet", help="the fleet's name (default: the only fleet with event logs on this host)")
    tl.add_argument("--events", help="an events directory to read instead (e.g. one copied from another host)")
    tl.add_argument("--since", help="from this time (ISO 8601, or seconds since the epoch)")
    tl.add_argument("--until", help="up to this time")
    tl.add_argument("-o", "--output", default="afe-fleet-trace.json", help="where to write the Chrome trace")

    fz = sub.add_parser("fuzz", help="fuzz campaigns without a model, filing findings as tasks (fuzz.py)")
    fz.add_argument("action", choices=["run"])
    fz.add_argument("source")
    fz.add_argument("--campaign", action="append", help="only this campaign (name or project/name); repeatable")
    fz.add_argument("--once", action="store_true", help="one round of each campaign now, then exit")
    fz.add_argument("--import", dest="import_dir", metavar="DIR",
                    help="first add the files under DIR to the campaigns' corpora")

    tr = sub.add_parser("trace", help="record every process run in the fleet's containers into the event log "
                                      "(bpftrace; run with sudo)")
    tr.add_argument("--fleet", help="the fleet's name (default: the only fleet on this host)")
    tr.add_argument("--events", help="an events directory to write to instead")
    tr.add_argument("--duration", type=float, metavar="SECONDS", help="stop after this long (default: ctrl-c)")
    tr.add_argument("--bpftrace", default="bpftrace", help="the bpftrace executable")
    tr.add_argument("--prefix", default="afe-fleet-", help="trace the containers whose names start with this")

    sv = sub.add_parser("service", help="manage systemd user services").add_subparsers(dest="action", required=True)
    si = sv.add_parser("install", help="install, enable and start (idempotent)")
    si.add_argument("role", choices=service.ROLES)
    si.add_argument("source")
    si.add_argument("extra", nargs="*", help="extra arguments for `<role> run` (after --)")
    si.add_argument("--no-start", action="store_true")
    si.add_argument("--no-linger", action="store_true", help="do not enable lingering")
    for name in ("uninstall", "status"):
        x = sv.add_parser(name)
        x.add_argument("role", choices=service.ROLES)
        x.add_argument("source", help="the same SOURCE as at install, or the fleet name")
    sv.add_parser("list", help="list installed afe-fleet units")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.cmd == "version":
        print(__version__)
        return 0
    _setup_logging(args.verbose)
    try:
        return {"worker": cmd_worker, "admin": cmd_admin, "server": cmd_server, "status": cmd_status,
                "timeline": cmd_timeline, "trace": cmd_trace, "fuzz": cmd_fuzz, "service": cmd_service}[args.cmd](args)
    except (ValueError, FileNotFoundError, RuntimeError) as e:
        log.error("%s", e)
        return 2


if __name__ == "__main__":
    sys.exit(main())
