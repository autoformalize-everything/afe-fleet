"""Service install/uninstall and the CLI, with a fake systemctl (nothing is really installed)."""

import os
import stat
import subprocess
import sys

from afe_fleet import service
from afe_fleet.cli import main


def fake_bin(tmp_path, name):
    log = tmp_path / f"{name}.log"
    p = tmp_path / name
    p.write_text(f"#!/bin/sh\necho \"$@\" >> {log}\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p), log


def test_render_runs_this_afe_fleet_with_path(tmp_path):
    text = service.render("worker", "fl", ["org/repo"], command=["/opt/afe-fleet"], path_env="/a:/b")
    assert "ExecStart=/opt/afe-fleet worker run org/repo" in text
    assert "Environment=PATH=/a:/b" in text and "Restart=always" in text and "KillMode=mixed" in text


def test_install_is_idempotent_and_uninstall_removes(tmp_path):
    sc_bin, sc_log = fake_bin(tmp_path, "systemctl")
    lg_bin, lg_log = fake_bin(tmp_path, "loginctl")
    sc = service.Systemctl(sc_bin, lg_bin)
    path, changed, notes = service.install("worker", "fl", ["org/repo"], sc=sc, command=["/x/afe-fleet"])
    assert changed and path.name == "afe-fleet-worker-fl.service" and path.exists()
    calls = sc_log.read_text().splitlines()
    assert calls == ["--user daemon-reload", "--user enable afe-fleet-worker-fl.service",
                     "--user restart afe-fleet-worker-fl.service"]
    assert "enable-linger" in lg_log.read_text()
    _, changed, _ = service.install("worker", "fl", ["org/repo"], sc=sc, command=["/x/afe-fleet"], linger=False)
    assert not changed
    assert sc_log.read_text().splitlines()[-1] == "--user start afe-fleet-worker-fl.service"
    assert service.installed() == ["afe-fleet-worker-fl.service"]
    assert service.uninstall("worker", "fl", sc=sc)
    assert not path.exists() and service.installed() == []
    assert "--user disable --now afe-fleet-worker-fl.service" in sc_log.read_text()
    assert not service.uninstall("worker", "fl", sc=sc)


def test_unit_names_are_unique_per_fleet_and_role():
    assert service.unit_name("admin", "a") != service.unit_name("worker", "a") != service.unit_name("worker", "b")


def test_cli_service_install_from_a_fleet_source(world, tmp_path, monkeypatch, capsys):
    sc_bin, sc_log = fake_bin(tmp_path, "systemctl")
    lg_bin, _ = fake_bin(tmp_path, "loginctl")
    monkeypatch.setattr(service.Systemctl, "__init__", lambda self, binary=sc_bin, loginctl=lg_bin: (
        setattr(self, "binary", binary), setattr(self, "loginctl", loginctl), None)[-1])
    assert main(["service", "install", "worker", str(world.config_repo)]) == 0
    unit = service.unit_dir() / "afe-fleet-worker-t.service"
    assert unit.exists() and f"worker run {world.config_repo}" in unit.read_text()
    assert main(["service", "uninstall", "worker", "t"]) == 0  # by fleet name
    assert not unit.exists()


def test_cli_version_and_status(world, capsys):
    assert main(["version"]) == 0
    assert capsys.readouterr().out.strip()
    assert main(["status", str(world.config_repo)]) == 0
    out = capsys.readouterr().out
    assert "| `demo/a` | unclaimed |" in out and "| `demo/b` | unclaimed |" in out


def test_runs_as_a_module():
    p = subprocess.run([sys.executable, "-m", "afe_fleet", "version"], capture_output=True, text=True,
                       env={**os.environ})
    assert p.returncode == 0 and p.stdout.strip()
