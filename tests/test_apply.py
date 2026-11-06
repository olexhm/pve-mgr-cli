import pytest

from pve_mgr.client import PveClient
from pve_mgr.errors import ApplyError, NotReadyError, TransportError
from pve_mgr.services.apply import apply
from pve_mgr.services.readiness import Waiter
from pve_mgr.transport import Call, Method
from tests.fakes import FakeTransport
from tests.test_plan import ISO, TEMPLATE, device, guest, iso_device

LXC_TEMPLATE = guest(9201, type="lxc", node="pve1", template=True, status="stopped")


def cluster(*guests, pools=("lab",), **replies) -> FakeTransport:
    resources = [g.model_dump(mode="json") for g in guests]
    return FakeTransport(
        {"/cluster/resources": resources, "/pools": [{"poolid": p} for p in pools]} | replies
    )


def run(
    transport: FakeTransport,
    *devices,
    waiter: Waiter | None = None,
    wait_for_installs: bool = False,
) -> list[str]:
    log: list[str] = []
    apply(
        PveClient(transport),
        devices,
        report=lambda subject, message: log.append(f"{subject}: {message}"),
        waiter=waiter,
        wait_for_installs=wait_for_installs,
    )
    return log


ISO_CONTENT = {"/nodes/pve2/storage/local/content": [{"volid": "local:iso/debian.iso"}]}
AGENT = {
    "/nodes/pve2/qemu/110/agent/network-get-interfaces": {
        "result": [{"ip-addresses": [{"ip-address": "192.0.2.20"}]}]
    },
    "/nodes/pve2/network/vmbr0": {"cidr": "192.0.2.1/24"},
}


def test_qemu_guest_is_cloned_configured_migrated_extended_and_started():
    transport = cluster(TEMPLATE, pools=())

    run(transport, device(migration_storage="local-lvm"))

    assert transport.executed == [
        Call(Method.POST, "/pools", {"poolid": "lab"}),
        Call(
            Method.POST,
            "/nodes/pve1/qemu/9101/clone",
            {"newid": 110, "name": "web1", "pool": "lab", "full": True},
        ),
        Call(
            Method.PUT,
            "/nodes/pve1/qemu/110/config",
            {
                "cores": 2,
                "memory": 2048,
                "net0": "model=virtio,bridge=vmbr0,firewall=0",
                "net1": "model=virtio,bridge=vmbr10,firewall=0",
                "agent": "enabled=1",
                "delete": "tags",
            },
        ),
        Call(
            Method.POST,
            "/nodes/pve1/qemu/110/migrate",
            {"target": "pve2", "targetstorage": "local-lvm", "with-local-disks": True},
        ),
        Call(Method.PUT, "/nodes/pve2/qemu/110/config", {"scsi1": "local-lvm:50"}),
        Call(Method.POST, "/nodes/pve2/qemu/110/status/start", {}),
    ]


def test_lxc_guest_stays_on_the_builder_node():
    container = device(
        type="lxc",
        template_id=9201,
        host_node="pve1",
        bridges=["vmbr10"],
        additional_disk=None,
        migration_storage="local-lvm",
    )
    transport = cluster(LXC_TEMPLATE)

    run(transport, container)

    assert transport.executed == [
        Call(
            Method.POST,
            "/nodes/pve1/lxc/9201/clone",
            {"newid": 110, "hostname": "web1", "pool": "lab", "full": True},
        ),
        Call(
            Method.PUT,
            "/nodes/pve1/lxc/110/config",
            {
                "cores": 2,
                "memory": 2048,
                "net0": "name=eth0,bridge=vmbr10,ip=dhcp,firewall=0",
                "delete": "tags",
            },
        ),
        Call(Method.POST, "/nodes/pve1/lxc/110/status/start", {}),
    ]


def test_declared_tags_replace_the_templates():
    transport = cluster(TEMPLATE)

    run(transport, device(tags=["lab", "web"]))

    configure = next(call for call in transport.executed if call.path.endswith("/config"))
    assert configure.params["tags"] == "lab;web"
    assert "delete" not in configure.params


def test_existing_guests_are_left_alone():
    transport = cluster(TEMPLATE, guest(node="pve1"), **{"/nodes/pve1/qemu/110/config": {}})
    log = run(transport, device())
    assert transport.executed == []
    assert log == ["web1: exists, left as it is"]


def test_each_guest_is_ready_before_the_next_is_created():
    replies = {
        "/nodes/pve2/qemu/110/agent/network-get-interfaces": {
            "result": [{"ip-addresses": [{"ip-address": "192.0.2.20"}]}]
        },
        "/nodes/pve2/qemu/111/agent/network-get-interfaces": {
            "result": [{"ip-addresses": [{"ip-address": "192.0.2.21"}]}]
        },
        "/nodes/pve2/network/vmbr0": {"cidr": "192.0.2.1/24"},
    }
    transport = cluster(TEMPLATE, **replies)

    log = run(transport, device(), device(id=111, name="web2"), waiter=Waiter())

    paths = [call.path for call in transport.calls]
    assert paths.index("/nodes/pve2/qemu/110/agent/network-get-interfaces") < paths.index(
        "/nodes/pve1/qemu/9101/clone", paths.index("/nodes/pve1/qemu/9101/clone") + 1
    )
    assert "web1: ready at 192.0.2.20" in log
    assert "web2: ready at 192.0.2.21" in log


def test_waiting_gives_up_after_the_timeout():
    ticks = iter(range(0, 1000, 10))
    waiter = Waiter(timeout=25, sleep=lambda _: None, clock=lambda: float(next(ticks)))
    with pytest.raises(ApplyError, match="reported no address within 25s") as exc:
        run(cluster(TEMPLATE), device(), waiter=waiter)
    assert isinstance(exc.value.__cause__, NotReadyError)


def test_failure_stops_and_names_what_was_created():
    def fail_second(call: Call):
        if call.params["newid"] == 111:
            raise TransportError("storage is full")

    transport = cluster(TEMPLATE, **{"POST /nodes/pve1/qemu/9101/clone": fail_second})
    devices = [device(), device(id=111, name="web2"), device(id=112, name="web3")]

    with pytest.raises(ApplyError) as exc:
        run(transport, *devices)

    assert "web2: storage is full" in str(exc.value)
    assert "Created before the failure: web1." in str(exc.value)
    assert not any(call.params.get("newid") == 112 for call in transport.calls)


def test_template_problems_block_apply_before_any_change():
    transport = cluster(pools=())
    with pytest.raises(ApplyError, match="does not exist"):
        run(transport, device())
    assert transport.executed == []


def test_iso_guest_is_created_on_its_host_node_and_boots_the_installer():
    transport = cluster(**ISO_CONTENT)

    log = run(transport, iso_device(tags=["lab"]), waiter=Waiter())

    assert transport.executed == [
        Call(
            Method.POST,
            "/nodes/pve2/qemu",
            {
                "vmid": 110,
                "name": "web1",
                "pool": "lab",
                "ostype": "l26",
                "bios": "seabios",
                "scsihw": "virtio-scsi-single",
                "scsi0": "local-lvm:20",
                "ide2": "local:iso/debian.iso,media=cdrom",
                "boot": "order=scsi0;ide2",
            },
        ),
        Call(
            Method.PUT,
            "/nodes/pve2/qemu/110/config",
            {
                "cores": 2,
                "memory": 2048,
                "net0": "model=virtio,bridge=vmbr0,firewall=0",
                "net1": "model=virtio,bridge=vmbr10,firewall=0",
                "agent": "enabled=1",
                "tags": "lab",
            },
        ),
        Call(Method.PUT, "/nodes/pve2/qemu/110/config", {"scsi1": "local-lvm:50"}),
        Call(Method.POST, "/nodes/pve2/qemu/110/status/start", {}),
    ]
    assert not any("agent" in call.path for call in transport.calls)
    assert log[-1] == "web1: boots the installer; finish the installation from its console"


def test_uefi_iso_guest_gets_an_efi_disk():
    transport = cluster(**ISO_CONTENT)

    run(transport, iso_device(hardware={"bios": "ovmf", "machine": "q35"}))

    created = transport.executed[0].params
    assert (created["bios"], created["machine"]) == ("ovmf", "q35")
    assert created["efidisk0"] == "local-lvm:1,efitype=4m"


def test_iso_guest_takes_its_disk_bus_disk_settings_and_tpm():
    transport = cluster(**ISO_CONTENT)
    disk = {"size": "20G", "storage": "local-lvm", "bus": "virtio", "discard": True}
    disk["iothread"] = True

    run(
        transport,
        iso_device(
            iso=ISO | {"disk": disk, "tpm": "v2.0"},
            hardware={"ostype": "win11", "scsihw": "virtio-scsi-pci"},
        ),
    )

    created = transport.executed[0].params
    assert "scsi0" not in created
    assert created["virtio0"] == "local-lvm:20,discard=on,iothread=1"
    assert created["boot"] == "order=virtio0;ide2"
    assert created["tpmstate0"] == "local-lvm:1,version=v2.0"
    assert (created["ostype"], created["scsihw"]) == ("win11", "virtio-scsi-pci")


def test_written_hardware_is_set_on_a_clone_and_nothing_else():
    transport = cluster(TEMPLATE)
    hardware = {
        "cpu": "host",
        "sockets": 2,
        "balloon_mb": 1024,
        "onboot": True,
        "nic": {"model": "e1000", "firewall": True},
        "options": {"hotplug": "disk,network"},
    }

    run(
        transport,
        device(
            hardware=hardware,
            additional_disk={"size": "50G", "storage": "local-lvm", "ssd": True, "cache": "none"},
        ),
    )

    configure, _, attach, _ = transport.executed[1:]
    assert configure.params == {
        "cores": 2,
        "memory": 2048,
        "net0": "model=e1000,bridge=vmbr0,firewall=1",
        "net1": "model=e1000,bridge=vmbr10,firewall=1",
        "agent": "enabled=1",
        "cpu": "host",
        "sockets": 2,
        "balloon": 1024,
        "onboot": True,
        "hotplug": "disk,network",
        "delete": "tags",
    }
    assert attach.params == {"scsi1": "local-lvm:50,ssd=1,cache=none"}


def test_nic_mtu_is_set_when_written():
    transport = cluster(TEMPLATE)
    run(transport, device(hardware={"nic": {"mtu": 1400}}))
    assert transport.executed[1].params["net0"] == "model=virtio,bridge=vmbr0,firewall=0,mtu=1400"


def test_iso_guest_is_waited_for_when_asked():
    transport = cluster(**ISO_CONTENT, **AGENT)
    log = run(transport, iso_device(), waiter=Waiter(), wait_for_installs=True)
    assert log[-1] == "web1: ready at 192.0.2.20"


@pytest.mark.parametrize("content", [{}, {"/nodes/pve2/storage/local/content": []}])
def test_missing_iso_blocks_apply_before_any_change(content):
    transport = cluster(pools=(), **content)
    with pytest.raises(ApplyError, match=r"ISO 'local:iso/debian.iso' not found on pve2"):
        run(transport, iso_device())
    assert transport.executed == []
