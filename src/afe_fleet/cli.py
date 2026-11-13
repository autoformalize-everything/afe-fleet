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
import signal
import sys
import threading
from dataclasses import replace
from pathlib import Path

from . import __version__, service, update
from .config import HostConfig, Loaded, fleet_home, load, source_store
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


def _slot_config(loaded: Loaded, slot: int) -> ConfigRepo:
    """Each slot gets its own object store: concurrent fetches into one repo collide."""
    url = loaded.config.url
    return ConfigRepo(url, source_store(url).with_name(source_store(url).stem + f"-{slot}.git"))


def _coord(loaded: Loaded, who: str, slot: int):
    from .coord import GitCoord, ServerCoord
    from .lockrepo import LockRepo
    cfg = _slot_config(loaded, slot)
    fleet = loaded.fleet
    if fleet.mode == "github":
        lock = LockRepo(repo_url(fleet.lock_repo), fleet_home(fleet.name) / f"lock-{slot}.git", who=who)
        return GitCoord(lock, cfg), cfg
    return ServerCoord(fleet.url, cfg), cfg


def _admin_coord(loaded: Loaded):
    from .coord import GitAdmin, ServerAdmin, ServerCoord
    from .lockrepo import LockRepo
    fleet = loaded.fleet
    if fleet.mode == "github":
        lock = LockRepo(repo_url(fleet.lock_repo), fleet_home(fleet.name) / "lock-admin.git", who="admin")
        return GitAdmin(lock, loaded.config)
    return ServerAdmin(ServerCoord(fleet.url, loaded.config))


# -- worker ------------------------------------------------------------------------------------

def cmd_worker(args) -> int:
    from .worker import Worker, run_slots
    loaded = load(args.source)
    forge = _forge(args)
    gh_user = args.gh_user
    host = HostConfig.load(loaded.fleet.name, Path(args.host_config) if args.host_config else None,
                           worker_id=args.id, gh_user=gh_user, slots=args.slots,
                           kinds=args.kinds.split(",") if args.kinds else None, filters=args.filter)
    if host.gh_user is None:
        host = replace(host, gh_user=forge.login())
        if args.id is None and host.gh_user:
            host = HostConfig.load(loaded.fleet.name, Path(args.host_config) if args.host_config else None,
                                   worker_id=None, gh_user=host.gh_user, slots=args.slots,
                                   kinds=args.kinds.split(",") if args.kinds else None, filters=args.filter)
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

    def make(slot: int) -> Worker:
        coord, cfg = _coord(loaded, host.worker_id, slot)
        w = Worker(replace(loaded, config=cfg), host, coord, forge, slot=slot)
        w.maintain = maintain
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
    adm = Admin(loaded, _admin_coord(loaded), _forge(args))
    log.info("afe-fleet %s admin for fleet %s (%s mode)", __version__, loaded.fleet.name, loaded.fleet.mode)
    if args.action == "cancel":
        for a in adm.cancel(args.task, reason=args.reason, remove=args.remove):
            print(a)
        return 0
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
                "service": cmd_service}[args.cmd](args)
    except (ValueError, FileNotFoundError, RuntimeError) as e:
        log.error("%s", e)
        return 2


if __name__ == "__main__":
    sys.exit(main())
