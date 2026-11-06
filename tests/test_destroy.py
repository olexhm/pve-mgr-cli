import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pve_mgr.cli import app
from pve_mgr.client import PveClient
from pve_mgr.errors import DestroyError, PveMgrError, TransportError
from pve_mgr.known_hosts import KnownHosts
from pve_mgr.services.destroy import destroy, find_targets
from pve_mgr.transport import Call, Method
from tests.fakes import FakeTransport
from tests.test_apply import cluster
from tests.test_plan import device, guest

GATEWAY = device(id=110, name="gw")
WEB = device(id=111, name="web1")
AGENT = {"result": [{"ip-addresses": [{"ip-address": "192.0.2.21"}]}]}


def lab(members: list[str], **replies) -> FakeTransport:
    return cluster(
        guest(110, name="gw", status="stopped"),
        guest(111, name="web1"),
        **{
            "/pools/lab": {"members": [{"id": member} for member in members]},
            "/nodes/pve2/qemu/111/agent/network-get-interfaces": AGENT,
            "/nodes/pve2/network/vmbr0": {"cidr": "192.0.2.1/24"},
        }
        | replies,
    )


def run(transport: FakeTransport, *devices) -> tuple[list[str], list[tuple[list[str], int]]]:
    client = PveClient(transport)
    log: list[str] = []
    forgotten: list[tuple[list[str], int]] = []
    destroy(
        client,
        find_targets(client, devices),
        report=lambda subject, message: log.append(f"{subject}: {message}"),
        forget=lambda hosts, port: forgotten.append((list(hosts), port)),
    )
    return log, forgotten


def test_guests_are_destroyed_in_reverse_order_and_the_empty_pool_removed():
    transport = lab(["qemu/110", "qemu/111"])

    log, forgotten = run(transport, GATEWAY, WEB, device(id=112, name="absent"))

    purge = {"purge": True, "destroy-unreferenced-disks": True}
    assert transport.executed == [
        Call(Method.POST, "/nodes/pve2/qemu/111/status/stop", {}),
        Call(Method.DELETE, "/nodes/pve2/qemu/111", purge),
        Call(Method.DELETE, "/nodes/pve2/qemu/110", purge),
        Call(Method.DELETE, "/pools/lab", {}),
    ]
    assert forgotten == [(["web1", "192.0.2.21"], 22), (["gw"], 22)]
    assert log[-1] == "lab: deleting empty pool"


def test_pool_with_other_members_is_kept():
    transport = lab(["qemu/110", "qemu/111", "qemu/500"])
    log, _ = run(transport, GATEWAY, WEB)
    assert Call(Method.DELETE, "/pools/lab", {}) not in transport.executed
    assert log[-1] == "lab: pool kept, it still contains qemu/500"


def test_failure_leaves_the_pool_alone():
    def fail(call: Call):
        raise TransportError("volume is busy")

    transport = lab(["qemu/110", "qemu/111"], **{"DELETE /nodes/pve2/qemu/111": fail})
    with pytest.raises(TransportError):
        run(transport, GATEWAY, WEB)
    assert [call.method for call in transport.executed] == [Method.POST, Method.DELETE]


@pytest.mark.parametrize(
    ("other", "message"),
    [
        ({"name": "someone-else"}, "VMID 111 is qemu 'someone-else', not qemu 'web1'"),
        ({"type": "lxc"}, "VMID 111 is lxc 'web1', not qemu 'web1'"),
        ({"template": True}, "(a template)"),
    ],
)
def test_guests_that_do_not_match_the_file_are_never_destroyed(other, message):
    transport = cluster(guest(110, name="gw"), guest(111, **other))
    with pytest.raises(DestroyError) as exc:
        find_targets(PveClient(transport), [GATEWAY, WEB])
    assert message in str(exc.value)
    assert transport.executed == []


def test_known_hosts_entries_are_removed_with_ssh_keygen(tmp_path: Path):
    path = tmp_path / "known_hosts"
    path.write_text("")
    argvs: list[list[str]] = []

    def runner(argv):
        argvs.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    KnownHosts(path, runner).forget(["web1", "192.0.2.21"], 2222)
    assert argvs == [
        ["ssh-keygen", "-f", str(path), "-R", "[web1]:2222"],
        ["ssh-keygen", "-f", str(path), "-R", "[192.0.2.21]:2222"],
    ]

    KnownHosts(tmp_path / "missing", runner).forget(["web1"])
    assert len(argvs) == 2


def test_known_hosts_failure_is_reported(tmp_path: Path):
    path = tmp_path / "known_hosts"
    path.write_text("")
    failing = KnownHosts(path, lambda argv: subprocess.CompletedProcess(argv, 255, "", "denied"))
    with pytest.raises(PveMgrError, match="Could not remove web1"):
        failing.forget(["web1"])


@pytest.fixture
def cli(config_path, write_devices, monkeypatch):
    transport = cluster(
        guest(110, name="web1", node="pve1", status="stopped"),
        **{"/pools/lab": {"members": [{"id": "qemu/110"}]}},
    )
    monkeypatch.setattr("pve_mgr.cli.build_transport", lambda config: transport)
    monkeypatch.setattr("pve_mgr.cli.KnownHosts.forget", lambda self, hosts, port=22: None)
    args = ["destroy", str(write_devices("d.yml", (110, "web1"))), "-c", str(config_path)]
    return transport, args


def test_cli_destroy_asks_first(cli):
    transport, args = cli
    result = CliRunner().invoke(app, args, input="n\n")
    assert result.exit_code == 1
    assert "To be destroyed" in result.output
    assert transport.executed == []


def test_cli_destroy_with_yes(cli):
    transport, args = cli
    result = CliRunner().invoke(app, [*args, "--yes"])
    assert result.exit_code == 0, result.output
    assert [call.path for call in transport.executed] == ["/nodes/pve1/qemu/110", "/pools/lab"]
