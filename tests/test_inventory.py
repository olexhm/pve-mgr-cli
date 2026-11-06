from pathlib import Path

import pytest
from typer.testing import CliRunner

from pve_mgr.cli import app
from pve_mgr.client import PveClient
from pve_mgr.errors import ConfigError, InventoryError
from pve_mgr.models import Config
from pve_mgr.services.inventory import HEADER, build_inventory, write_inventory
from tests.fakes import FakeTransport
from tests.test_apply import cluster
from tests.test_plan import device, guest

CONFIG = Config.model_validate(
    {
        "nodes": {"pve1": "root@pve1", "pve2": "root@pve2"},
        "defaults": {"host_node": "pve1"},
    }
)
GATEWAY = device(id=110, name="gw", role="gateway", bridges=["vmbr0", "vmbr10"], ssh_port=2222)
WEB = device(id=111, name="web1", bridges=["vmbr10"])
CACHE = device(
    id=112, name="cache", type="lxc", bridges=["vmbr10"], additional_disk=None,
    ansible_user="root", proxy_jumps=["ansible@192.0.2.10"],
)  # fmt: skip
ARGS = "'-o StrictHostKeyChecking=accept-new -J {}'"


def agent(*addresses: str) -> dict:
    return {"result": [{"ip-addresses": [{"ip-address": address} for address in addresses]}]}


def lab(**replies) -> FakeTransport:
    return cluster(
        guest(110, name="gw", node="pve1"),
        guest(111, name="web1", node="pve2"),
        guest(112, name="cache", node="pve2", type="lxc"),
        **{
            "/nodes/pve1/qemu/110/agent/network-get-interfaces": agent("10.0.10.1", "192.0.2.5"),
            "/nodes/pve1/network/vmbr0": {"cidr": "192.0.2.1/24"},
            "/nodes/pve2/qemu/111/agent/network-get-interfaces": agent("10.0.10.20"),
            "/nodes/pve2/lxc/112/interfaces": [{"name": "eth0", "inet": "10.0.10.30/24"}],
        }
        | replies,
    )


def test_hosts_jump_through_their_node_then_the_gateway_or_explicit_hops():
    inventory = build_inventory(PveClient(lab()), [GATEWAY, WEB, CACHE], CONFIG)

    assert inventory.render() == HEADER + "\n".join(
        [
            "gw ansible_host=192.0.2.5 ansible_user=ansible ansible_port=2222 "
            f"ansible_ssh_common_args={ARGS.format('root@pve1')}",
            "web1 ansible_host=10.0.10.20 ansible_user=ansible ansible_port=22 "
            f"ansible_ssh_common_args={ARGS.format('root@pve2,ansible@192.0.2.5:2222')}",
            "cache ansible_host=10.0.10.30 ansible_user=root ansible_port=22 "
            f"ansible_ssh_common_args={ARGS.format('root@pve2,ansible@192.0.2.10')}",
            "",
        ]
    )
    assert inventory.skipped == ()


def test_dual_homed_guest_without_the_role_is_not_a_gateway():
    plain = device(id=110, name="gw", bridges=["vmbr0", "vmbr10"])
    inventory = build_inventory(PveClient(lab()), [plain, WEB], CONFIG)
    assert inventory.hosts[1].proxy_jumps == ("root@pve2",)


def test_missing_and_unreachable_guests_are_skipped():
    transport = lab(**{"/nodes/pve2/qemu/111/agent/network-get-interfaces": agent()})
    absent = device(id=113, name="absent")

    inventory = build_inventory(PveClient(transport), [GATEWAY, WEB, absent], CONFIG)

    assert [host.device.name for host in inventory.hosts] == ["gw"]
    assert inventory.skipped == (("absent", "does not exist"), ("web1", "no address (running)"))


def test_gateway_without_an_address_is_an_error():
    transport = lab(**{"/nodes/pve1/qemu/110/agent/network-get-interfaces": agent()})
    with pytest.raises(InventoryError, match="Gateway gw has no address yet"):
        build_inventory(PveClient(transport), [GATEWAY, WEB], CONFIG)


def test_node_without_an_ssh_target_is_a_config_error():
    config = Config.model_validate(
        {"transport": "api", "nodes": {"pve1": {"api": "https://pve1:8006"}}}
    )
    with pytest.raises(ConfigError, match=r"nodes\.pve1\.ssh is not set"):
        build_inventory(PveClient(lab()), [GATEWAY], config)


def test_write_replaces_the_file(tmp_path: Path):
    path = tmp_path / "inventory.ini"
    path.write_text("old")
    write_inventory(path, "new\n")
    assert path.read_text() == "new\n"
    assert list(tmp_path.iterdir()) == [path]
    with pytest.raises(InventoryError, match="Cannot write"):
        write_inventory(tmp_path / "missing" / "inventory.ini", "x")


def test_cli_writes_to_the_configured_file_or_prints(config_path, write_devices, monkeypatch):
    transport = cluster(
        guest(110, name="web1", node="pve1"),
        **{"/nodes/pve1/qemu/110/agent/network-get-interfaces": agent("192.0.2.20")},
    )
    monkeypatch.setattr("pve_mgr.cli.build_transport", lambda config: transport)
    args = ["inventory", str(write_devices("d.yml", (110, "web1"), (111, "web2")))]
    args += ["-c", str(config_path)]

    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    written = (config_path.parent / "inventory.ini").read_text()
    assert written.startswith(HEADER + "web1 ansible_host=192.0.2.20 ")

    printed = CliRunner().invoke(app, [*args, "-o", "-"])
    assert written in printed.output
    assert "skipped web2: does not exist" in printed.output
