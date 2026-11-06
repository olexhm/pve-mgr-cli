import json

import pytest
from typer.testing import CliRunner

from pve_mgr.cli import app, main
from pve_mgr.transport import DryRunTransport
from tests.fakes import FakeTransport

runner = CliRunner()


def test_validate_lists_devices(config_path, write_devices):
    devices = write_devices("d.yml", (110, "web1"))
    result = runner.invoke(app, ["validate", str(devices), "--config", str(config_path)])
    assert result.exit_code == 0, result.output
    assert "web1" in result.output


def test_config_can_come_from_environment(config_path, write_devices, monkeypatch):
    monkeypatch.setenv("PVE_MGR_CONFIG", str(config_path))
    result = runner.invoke(app, ["validate", str(write_devices("d.yml", (110, "web1")))])
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize("kind", ["config", "devices"])
def test_schema_is_valid_json(kind):
    result = runner.invoke(app, ["schema", kind])
    assert result.exit_code == 0
    assert json.loads(result.output)["type"] == "object"


def test_errors_exit_without_traceback(config_path, tmp_path, monkeypatch, capsys):
    bad = tmp_path / "bad.yml"
    bad.write_text("devices: []\n")
    monkeypatch.setattr("sys.argv", ["pve-mgr", "validate", str(bad), "-c", str(config_path)])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "error:" in err
    assert "Traceback" not in err


def test_status_shows_state_node_and_address(config_path, write_devices, monkeypatch):
    transport = FakeTransport(
        {
            "/cluster/resources": [
                {"vmid": 110, "type": "qemu", "node": "pve2", "status": "running", "name": "web1"}
            ],
            "/nodes/pve2/qemu/110/agent/network-get-interfaces": {
                "result": [{"ip-addresses": [{"ip-address": "192.0.2.20"}]}]
            },
            "/nodes/pve2/network/vmbr0": {"cidr": "192.0.2.1/24"},
        }
    )
    monkeypatch.setattr("pve_mgr.cli.build_transport", lambda config: transport)
    devices = write_devices("d.yml", (110, "web1"), (111, "web2"))

    result = runner.invoke(app, ["status", str(devices), "-c", str(config_path)])

    assert result.exit_code == 0, result.output
    web1, web2 = (line for line in result.output.splitlines() if "web" in line)
    assert "pve2" in web1
    assert "running" in web1
    assert "192.0.2.20" in web1
    assert "absent" in web2
    assert transport.executed == []


def test_plan_lists_creations_drift_and_pools(config_path, write_devices, monkeypatch):
    transport = FakeTransport(
        {
            "/cluster/resources": [
                {"vmid": 110, "type": "qemu", "node": "pve1", "status": "running", "name": "web1"},
                {"vmid": 9101, "type": "qemu", "node": "pve1", "status": "stopped", "template": 1},
            ],
            "/nodes/pve1/qemu/110/config": {"cores": 4, "memory": "2048", "net0": "bridge=vmbr0"},
            "/pools": [],
        }
    )
    monkeypatch.setattr("pve_mgr.cli.build_transport", lambda config: transport)
    devices = write_devices("d.yml", (110, "web1"), (111, "web2"))

    result = runner.invoke(app, ["plan", str(devices), "-c", str(config_path)])

    assert result.exit_code == 0, result.output
    assert "cores: wanted 2, is 4" in result.output
    assert "pool: wanted lab, is unset" in result.output
    assert "pool to create: lab" in result.output
    assert "1 create, 0 unchanged, 1 drift" in result.output
    assert transport.executed == []


def test_apply_dry_run_prints_the_calls_without_making_them(
    config_path, write_devices, monkeypatch
):
    inner = FakeTransport(
        {
            "/cluster/resources": [
                {"vmid": 9101, "type": "qemu", "node": "pve1", "status": "stopped", "template": 1}
            ],
            "/pools": [],
        }
    )
    monkeypatch.setattr("pve_mgr.cli.build_dry_run", lambda config: DryRunTransport(inner))
    devices = write_devices("d.yml", (110, "web1"))

    result = runner.invoke(app, ["apply", str(devices), "-c", str(config_path), "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "POST   /pools poolid=lab" in result.output
    assert "POST   /nodes/pve1/qemu/9101/clone newid=110 name=web1 pool=lab full=True" in (
        result.output
    )
    assert "POST   /nodes/pve1/qemu/110/status/start" in result.output
    assert inner.executed == []


def test_status_json(config_path, write_devices, monkeypatch):
    transport = FakeTransport({"/cluster/resources": []})
    monkeypatch.setattr("pve_mgr.cli.build_transport", lambda config: transport)
    devices = write_devices("d.yml", (110, "web1"))

    result = runner.invoke(app, ["status", str(devices), "-c", str(config_path), "--json"])

    assert json.loads(result.output) == [
        {
            "name": "web1",
            "id": 110,
            "type": "qemu",
            "node": None,
            "state": "absent",
            "address": None,
        }
    ]


def test_root_options_and_environment_override_the_config(config_path, write_devices, monkeypatch):
    seen = []

    def build(config):
        seen.append((config.transport.value, config.entry))
        return FakeTransport({"/cluster/resources": []})

    monkeypatch.setattr("pve_mgr.cli.build_transport", build)
    args = ["status", str(write_devices("d.yml", (110, "web1"))), "-c", str(config_path)]

    assert runner.invoke(app, ["--entry-node", "pve2", *args]).exit_code == 0
    monkeypatch.setenv("PVE_MGR_ENTRY_NODE", "pve1")
    assert runner.invoke(app, args).exit_code == 0
    monkeypatch.delenv("PVE_MGR_ENTRY_NODE")
    assert runner.invoke(app, args).exit_code == 0
    assert seen == [("ssh", "pve2"), ("ssh", "pve1"), ("ssh", "pve1")]

    result = runner.invoke(app, ["--transport", "api", *args])
    assert result.exit_code != 0
    assert seen[3:] == []


def iso_cluster(monkeypatch) -> FakeTransport:
    transport = FakeTransport(
        {
            "/cluster/resources": [],
            "/pools": [{"poolid": "lab"}],
            "/nodes/pve1/storage/local/content": [{"volid": "local:iso/debian.iso"}],
            "/nodes/pve1/qemu/120/agent/network-get-interfaces": {
                "result": [{"ip-addresses": [{"ip-address": "192.0.2.40"}]}]
            },
            "/nodes/pve1/network/vmbr0": {"cidr": "192.0.2.1/24"},
        }
    )
    monkeypatch.setattr("pve_mgr.cli.build_transport", lambda config: transport)
    return transport


def test_plan_shows_the_iso_a_guest_is_installed_from(config_path, iso_devices, monkeypatch):
    iso_cluster(monkeypatch)
    result = runner.invoke(app, ["plan", str(iso_devices), "-c", str(config_path)])
    assert result.exit_code == 0, result.output
    assert "problem" not in result.output
    assert "1 create" in result.output


@pytest.mark.parametrize(("flags", "waited"), [([], False), (["-w"], True), (["--no-wait"], False)])
def test_apply_waits_for_iso_guests_only_on_request(
    config_path, iso_devices, monkeypatch, flags, waited
):
    transport = iso_cluster(monkeypatch)

    result = runner.invoke(app, ["apply", str(iso_devices), "-c", str(config_path), *flags])

    assert result.exit_code == 0, result.output
    assert ("ready at 192.0.2.40" in result.output) is waited
    assert any(call.path == "/nodes/pve1/qemu" for call in transport.executed)
