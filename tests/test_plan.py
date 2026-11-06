import pytest

from pve_mgr.models import Device, Guest
from pve_mgr.services.plan import Action, Difference, make_plan

DEVICE = {
    "type": "qemu",
    "id": 110,
    "name": "web1",
    "pool": "lab",
    "template": "qemu-debian13",
    "template_id": 9101,
    "builder_node": "pve1",
    "host_node": "pve2",
    "cores": 2,
    "memory_mb": 2048,
    "bridges": ["vmbr0", "vmbr10"],
    "ansible_user": "ansible",
    "additional_disk": {"size": "50G", "storage": "local-lvm"},
}
CONFIG = {
    "cores": 2,
    "memory": "2048",
    "net0": "virtio=BC:24:11:00:00:01,bridge=vmbr0,firewall=0",
    "net1": "virtio=BC:24:11:00:00:02,bridge=vmbr10",
    "scsi1": "local-lvm:vm-110-disk-1,size=51200M",
}


def device(**override) -> Device:
    return Device.model_validate(DEVICE | override)


def guest(vmid: int = 110, **override) -> Guest:
    fields = {"type": "qemu", "node": "pve2", "status": "running", "name": "web1", "pool": "lab"}
    return Guest.model_validate({"vmid": vmid} | fields | override)


TEMPLATE = guest(9101, node="pve1", name="tpl", pool=None, template=True, status="stopped")


def test_missing_guest_is_created_with_its_pool():
    plan = make_plan([device()], {9101: TEMPLATE}, {}, pools=set())
    assert [entry.action for entry in plan.entries] == [Action.CREATE]
    assert plan.pools_to_create == ("lab",)
    assert plan.problems == ()


def test_matching_guest_is_unchanged_and_needs_no_pool():
    plan = make_plan([device()], {110: guest()}, {110: CONFIG}, pools=set())
    assert [entry.action for entry in plan.entries] == [Action.UNCHANGED]
    assert plan.pools_to_create == ()


def test_drift_lists_every_difference():
    config = CONFIG | {"cores": 4, "net1": "virtio=AA,bridge=vmbr20", "scsi1": "ceph:x,size=20G"}
    plan = make_plan([device()], {110: guest(node="pve1")}, {110: config}, pools={"lab"})

    (entry,) = plan.entries
    assert entry.action is Action.DRIFT
    assert entry.differences == (
        Difference("node", "pve2", "pve1"),
        Difference("cores", "2", "4"),
        Difference("bridges", "vmbr0,vmbr10", "vmbr0,vmbr20"),
        Difference("scsi1", "local-lvm:50G", "ceph:20G"),
    )


def test_tags_are_compared_in_any_order():
    guests, tagged = {110: guest()}, device(tags=["web", "lab"])

    plan = make_plan([tagged], guests, {110: CONFIG | {"tags": "lab;web"}}, pools={"lab"})
    assert plan.entries[0].action is Action.UNCHANGED

    plan = make_plan([tagged], guests, {110: CONFIG}, pools={"lab"})
    assert plan.entries[0].differences == (Difference("tags", "lab;web", "none"),)


def test_tags_left_over_from_the_template_are_drift():
    plan = make_plan([device()], {110: guest()}, {110: CONFIG | {"tags": "tpl"}}, pools={"lab"})
    assert plan.entries[0].differences == (Difference("tags", "none", "tpl"),)


def test_missing_settings_are_reported_as_unset():
    config = {"memory": 2048, "net0": "bridge=vmbr0", "net1": "bridge=vmbr10"}
    plan = make_plan([device()], {110: guest()}, {110: config}, pools={"lab"})
    assert plan.entries[0].differences == (
        Difference("cores", "2", "unset"),
        Difference("scsi1", "local-lvm:50G", "unset"),
    )


@pytest.mark.parametrize(
    ("guests", "problem"),
    [
        ({}, "does not exist"),
        ({9101: guest(9101, node="pve1")}, "is not a template"),
        (
            {9101: guest(9101, node="pve1", type="lxc", template=True)},
            "is lxc, but is used for qemu devices",
        ),
        ({9101: guest(9101, node="pve2", template=True)}, "is on pve2, not on builder node pve1"),
    ],
)
def test_unusable_template_is_a_problem(guests, problem):
    plan = make_plan([device(), device(id=111, name="web2")], guests, {}, pools={"lab"})
    assert len(plan.problems) == 1
    assert problem in plan.problems[0]


def test_template_is_not_checked_for_existing_guests():
    plan = make_plan([device()], {110: guest()}, {110: CONFIG}, pools={"lab"})
    assert plan.problems == ()


ISO = {"image": "local:iso/debian.iso", "disk": {"size": "20G", "storage": "local-lvm"}}


def iso_device(**override) -> Device:
    fields = {"template": None, "template_id": None, "builder_node": "pve2", "iso": ISO}
    return device(**fields | override)


def test_iso_device_needs_its_image_on_the_host_node():
    isos = {("pve1", "local:iso/debian.iso"), ("pve2", "local:iso/other.iso")}
    plan = make_plan([iso_device()], {}, {}, pools={"lab"}, isos=isos)
    assert plan.problems == ("ISO 'local:iso/debian.iso' not found on pve2",)

    plan = make_plan([iso_device(host_node="pve1")], {}, {}, pools={"lab"}, isos=isos)
    assert plan.problems == ()


def test_installed_iso_guest_is_not_compared_with_its_image():
    plan = make_plan([iso_device()], {110: guest()}, {110: CONFIG}, pools={"lab"})
    assert plan.entries[0].action is Action.UNCHANGED
    assert plan.problems == ()


HARDWARE = {
    "cpu": "host",
    "numa": False,
    "cpu_limit": 2,
    "onboot": True,
    "description": "web server",
    "nic": {"model": "virtio", "firewall": False},
    "options": {"hotplug": "disk,network", "serial0": "socket"},
}
HARDWARE_CONFIG = CONFIG | {
    "cpu": "host",
    "numa": 0,
    "cpulimit": "2",
    "onboot": 1,
    "description": "web server\n",
    "hotplug": "network,disk,usb",
    "serial0": "socket",
    "vga": "qxl",
    "sockets": 4,
}


def hardware_drift(hardware: dict, config: dict, **override) -> tuple[Difference, ...]:
    devices = [device(hardware=hardware, **override)]
    return make_plan(devices, {110: guest()}, {110: config}, pools={"lab"}).entries[0].differences


def test_written_hardware_matches_however_proxmox_stores_it():
    assert hardware_drift(HARDWARE, HARDWARE_CONFIG) == ()


def test_hardware_drift_is_reported_under_the_proxmox_name():
    config = HARDWARE_CONFIG | {"cpu": "kvm64", "onboot": 0, "hotplug": "disk", "cpulimit": 1.5}
    del config["numa"]
    assert hardware_drift(HARDWARE, config) == (
        Difference("cpu", "host", "kvm64"),
        Difference("numa", "0", "unset"),
        Difference("cpulimit", "2", "1.5"),
        Difference("onboot", "1", "0"),
        Difference("hotplug", "disk,network", "disk"),
    )


def test_nic_settings_are_compared_per_nic():
    nic = {"nic": {"model": "virtio", "firewall": True, "mtu": 1400}}
    config = CONFIG | {
        "net0": "virtio=BC:24:11:00:00:01,bridge=vmbr0,firewall=1,mtu=1400",
        "net1": "e1000=BC:24:11:00:00:02,bridge=vmbr10",
    }
    assert hardware_drift(nic, config) == (
        Difference(
            "net1", "model=virtio,firewall=1,mtu=1400", "e1000=BC:24:11:00:00:02,bridge=vmbr10"
        ),
    )


def test_disk_settings_are_compared_when_written():
    disk = {"size": "50G", "storage": "local-lvm", "ssd": True, "discard": True}
    stored = "local-lvm:vm-110-disk-1,discard=on,iothread=1,size=50G"

    plan = make_plan(
        [device(additional_disk=disk)], {110: guest()}, {110: CONFIG | {"scsi1": stored}}, {"lab"}
    )
    assert plan.entries[0].differences == (
        Difference("scsi1", "local-lvm:50G,ssd=1,discard=on", "local-lvm:50G,discard=on"),
    )

    plan = make_plan(
        [device(additional_disk=disk)],
        {110: guest()},
        {110: CONFIG | {"scsi1": stored + ",ssd=1"}},
        {"lab"},
    )
    assert plan.entries[0].action is Action.UNCHANGED
