import pytest
from pydantic import ValidationError

from pve_mgr.models import Config, DeviceSpec, DiskSpec, Hardware

DEVICE = {
    "type": "qemu",
    "id": 110,
    "name": "web1",
    "pool": "lab",
    "template": "qemu-debian13",
    "cores": 2,
    "memory_mb": 2048,
    "bridges": ["vmbr0"],
    "ansible_user": "ansible",
}
ISO = {"image": "local:iso/debian.iso", "disk": {"size": "20G"}}
ISO_DEVICE = DEVICE | {"template": None, "iso": ISO}


@pytest.mark.parametrize(
    ("raw", "size", "gib"),
    [
        (20, "20G", 20),
        ("20", "20G", 20),
        ("20g", "20G", 20),
        ("20GiB", "20G", 20),
        ("2T", "2T", 2048),
    ],
)
def test_disk_size_is_normalized(raw, size, gib):
    disk = DiskSpec(size=raw)
    assert (disk.size, disk.size_gib) == (size, gib)


@pytest.mark.parametrize("raw", ["0G", "1.5G", "20M", "big"])
def test_disk_size_rejects_invalid(raw):
    with pytest.raises(ValidationError):
        DiskSpec(size=raw)


@pytest.mark.parametrize(
    "override",
    [
        {"type": "kvm"},
        {"id": 99},
        {"name": "bad_name"},
        {"cores": 0},
        {"ssh_port": 70000},
        {"bridges": []},
        {"bridges": ["vmbr0", "vmbr0"]},
        {"bridges": ["vmbr0", "vmbr1", "vmbr2"]},
        {"role": "gateway"},
        {"role": "router", "bridges": ["vmbr0", "vmbr1"]},
        {"proxy_jumps": ["a@b,c@d"]},
        {"tags": ["Web"]},
        {"tags": ["a;b"]},
        {"tags": ["web", "web"]},
        {"additional_disk": {"size": "10G", "slot": "scsi0"}},
        {"type": "lxc", "additional_disk": {"size": "10G"}},
        {"bridge": "vmbr0"},
        {"template": None},
        {"iso": ISO},
        {"additional_disk": {"size": "10G", "slot": "virtio1", "ssd": True}},
        {"additional_disk": {"size": "10G", "slot": "sata1", "iothread": True}},
        {"additional_disk": {"size": "10G", "cache": "fast"}},
        {"type": "lxc", "hardware": {"cpu": "host"}},
        {"hardware": {"cpu": "host,flags=+aes"}},
        {"hardware": {"sockets": 0}},
        {"hardware": {"startup": "first"}},
        {"hardware": {"vga": "hd"}},
        {"hardware": {"nic": {"model": "e1000", "mtu": 1400}}},
        {"hardware": {"cpus": 2}},
        {"hardware": {"options": {"cores": 4}}},
        {"hardware": {"options": {"cpulimit": 2}}},
        {"hardware": {"options": {"net0": "virtio,bridge=vmbr9"}}},
        {"hardware": {"options": {"scsi1": "local-lvm:10"}, "additional_disk": {"size": "10G"}}},
        {"hardware": {"options": {"Hotplug": "disk"}}},
        {"hardware": {"options": {"hotplug": ["disk"]}}},
    ],
)
def test_device_rejects_invalid(override):
    with pytest.raises(ValidationError):
        DeviceSpec.model_validate(DEVICE | override)


@pytest.mark.parametrize(
    "override",
    [
        {"type": "lxc"},
        {"builder_node": "pve1"},
        {"additional_disk": {"size": "10G", "slot": "ide2"}},
        {"iso": ISO | {"image": "debian.iso"}},
        {"iso": ISO | {"image": "local:vztmpl/debian.tar.zst"}},
        {"iso": ISO | {"disk": {"size": "20G", "slot": "scsi1"}}},
        {"iso": ISO | {"bios": "ovmf"}},
        {"iso": ISO | {"tpm": "v3"}},
        {"iso": ISO | {"disk": {"size": "20G", "bus": "nvme"}}},
        {"iso": ISO | {"disk": {"size": "20G", "bus": "virtio", "ssd": True}}},
        {"iso": {"image": "local:iso/debian.iso"}},
        {"hardware": {"options": {"ide2": "none"}}},
        {"hardware": {"options": {"scsi0": "local-lvm:10"}}},
        {"hardware": {"options": {"efidisk0": "local-lvm:1"}}},
    ],
)
def test_iso_device_rejects_invalid(override):
    with pytest.raises(ValidationError):
        DeviceSpec.model_validate(ISO_DEVICE | override)


def test_iso_disk_is_slot_zero_of_its_bus():
    iso = DeviceSpec.model_validate(ISO_DEVICE).iso
    assert iso is not None
    assert (iso.disk.slot, iso.disk.options(), iso.tpm) == ("scsi0", (), None)

    disk = {"size": "20G", "bus": "virtio", "discard": True, "iothread": False, "cache": "none"}
    iso = DeviceSpec.model_validate(ISO_DEVICE | {"iso": ISO | {"disk": disk}}).iso
    assert iso is not None
    assert iso.disk.slot == "virtio0"
    assert iso.disk.options() == ("discard=on", "iothread=0", "cache=none")


def test_hardware_params_use_proxmox_names_and_skip_what_is_not_written():
    assert Hardware().params() == {}
    hardware = Hardware.model_validate(
        {
            "bios": "ovmf",
            "cpu": "host",
            "numa": False,
            "cpu_limit": 1.5,
            "cpu_units": 200,
            "balloon_mb": 0,
            "onboot": True,
            "nic": {"model": "e1000"},
            "options": {"hotplug": "disk,network", "vcpus": 1, "acpi": True},
        }
    )
    assert hardware.params() == {
        "bios": "ovmf",
        "cpu": "host",
        "numa": False,
        "cpulimit": "1.5",
        "cpuunits": 200,
        "balloon": 0,
        "onboot": True,
        "hotplug": "disk,network",
        "vcpus": 1,
        "acpi": True,
    }


def test_options_may_add_nics_beyond_the_bridges():
    extra = {"hardware": {"options": {"net1": "virtio,bridge=vmbr9,tag=20"}}}
    assert DeviceSpec.model_validate(DEVICE | extra).hardware is not None


def test_device_defaults():
    device = DeviceSpec.model_validate(DEVICE)
    assert device.ssh_port == 22
    assert device.bridge == "vmbr0"
    assert device.proxy_jumps == ()
    assert device.tags == ()
    assert device.served_bridge is None


def test_only_a_declared_gateway_serves_a_bridge():
    bridges = {"bridges": ["vmbr0", "vmbr10"]}
    assert DeviceSpec.model_validate(DEVICE | bridges).served_bridge is None
    gateway = DeviceSpec.model_validate(DEVICE | bridges | {"role": "gateway"})
    assert gateway.served_bridge == "vmbr10"


def test_node_ssh_shorthand():
    config = Config.model_validate({"nodes": {"pve1": "root@pve1"}})
    assert config.nodes["pve1"].ssh == "root@pve1"
    assert config.default_host_node == config.default_builder_node == "pve1"


def test_only_the_entry_node_needs_an_endpoint():
    nodes = {"pve1": {"api": "https://pve1:8006"}, "pve2": "root@pve2"}
    config = Config.model_validate(
        {"transport": "api", "nodes": nodes, "defaults": {"host_node": "pve1"}}
    )
    assert config.entry == "pve1"
    with pytest.raises(ValidationError):
        Config.model_validate(
            {"transport": "api", "nodes": nodes, "defaults": {"host_node": "pve2"}}
        )


@pytest.mark.parametrize(
    ("target", "sudo", "expected"),
    [("root@pve1", None, False), ("admin@pve1", None, True), ("root@pve1", True, True)],
)
def test_sudo_defaults_to_non_root_targets(target, sudo, expected):
    config = Config.model_validate({"nodes": {"pve1": target}, "sudo": sudo})
    assert config.use_sudo is expected


@pytest.mark.parametrize(
    "raw",
    [
        {"nodes": {}},
        {"nodes": {"pve1": "root@pve1", "pve2": "root@pve2"}},
        {"nodes": {"pve1": "root@pve1"}, "defaults": {"host_node": "nope"}},
        {"nodes": {"pve1": "root@pve1"}, "transport": "api"},
        {"nodes": {"pve1": "root@pve1"}, "templates": {"t": 0}},
        {"nodes": {"pve1": "root@pve1"}, "entry_node": "pve9"},
    ],
    ids=[
        "no-nodes",
        "ambiguous-host",
        "unknown-default",
        "no-api-endpoint",
        "bad-template-id",
        "unknown-entry",
    ],
)
def test_config_rejects_invalid(raw):
    with pytest.raises(ValidationError):
        Config.model_validate(raw)


def test_migration_storage_falls_back_to_disk_storage():
    config = Config.model_validate(
        {"nodes": {"pve1": "root@pve1"}, "defaults": {"disk_storage": "local-lvm"}}
    )
    assert config.default_migration_storage == "local-lvm"
