from pathlib import Path

import pytest

from pve_mgr.errors import ConfigError
from pve_mgr.loader import find_config, load_config, load_devices

EXAMPLES = Path(__file__).parent.parent / "examples"


def test_examples_are_valid():
    config = load_config(EXAMPLES / "pve-mgr.yml")
    devices = load_devices([EXAMPLES / "devices.yml"], config)
    assert [d.name for d in devices] == ["lab-gateway", "lab-web1", "lab-cache"]


@pytest.mark.parametrize(
    ("config_file", "devices_files"),
    [
        ("pve-mgr.yml", ["swarm.yml"]),
        ("pve-mgr.yml", ["containers.yml"]),
        ("pve-mgr.yml", ["iso.yml"]),
        ("pve-mgr.yml", ["network/gateway.yml", "network/backend.yml"]),
        ("minimal/pve-mgr.yml", ["minimal/devices.yml"]),
        ("api/pve-mgr.yml", ["api/devices.yml"]),
    ],
)
def test_every_example_is_valid(config_file, devices_files):
    config = load_config(EXAMPLES / config_file)
    assert load_devices([EXAMPLES / name for name in devices_files], config)


def test_config_paths_are_relative_to_config_file(config, config_path):
    assert config.inventory == config_path.parent / "inventory.ini"
    assert config.known_hosts == Path.home() / ".ssh" / "known_hosts"


def test_defaults_are_resolved(config, write_devices):
    extra = "    additional_disk: {size: 50}\n"
    (device,) = load_devices([write_devices("d.yml", (110, "web1"), extra=extra)], config)
    assert device.template_id == 9101
    assert device.host_node == device.builder_node == "pve1"
    assert device.migration_storage == "local-lvm"
    assert device.additional_disk is not None
    assert device.additional_disk.storage == "local-lvm"
    assert device.additional_disk.size == "50G"


def test_iso_device_is_built_on_its_host_node(config, iso_devices):
    (device,) = load_devices([iso_devices], config)
    assert device.template_id is None
    assert device.builder_node == device.host_node == "pve1"
    assert device.iso is not None
    assert device.iso.disk.storage == "local-lvm"


def test_disk_and_hardware_settings_survive_resolution(config, tmp_path, iso_devices):
    text = iso_devices.read_text().replace(
        "disk: {size: 20G}", "disk: {size: 20G, bus: virtio, discard: true}"
    )
    extra = "    hardware: {cpu: host, options: {hotplug: disk}}\n"
    extra += "    additional_disk: {size: 50, ssd: true}\n"
    path = tmp_path / "hw.yml"
    path.write_text(text + extra)

    (device,) = load_devices([path], config)

    assert device.iso is not None
    assert (device.iso.disk.slot, device.iso.disk.options()) == ("virtio0", ("discard=on",))
    assert device.additional_disk is not None
    assert device.additional_disk.options() == ("ssd=1",)
    assert device.hardware is not None
    assert device.hardware.params() == {"cpu": "host", "hotplug": "disk"}


def test_iso_disk_needs_a_storage(tmp_path, iso_devices):
    path = tmp_path / "bare.yml"
    path.write_text("nodes:\n  pve1: root@pve1\n")
    with pytest.raises(ConfigError, match=r"iso\.disk\.storage: not set"):
        load_devices([iso_devices], load_config(path))


def test_files_are_loaded_in_order(config, write_devices):
    first = write_devices("a.yml", (110, "gw"))
    second = write_devices("b.yml", (111, "web1"), (112, "web2"))
    assert [d.name for d in load_devices([second, first], config)] == ["web1", "web2", "gw"]


def test_duplicates_across_files_are_rejected(config, write_devices):
    first = write_devices("a.yml", (110, "web1"))
    second = write_devices("b.yml", (110, "other"), (111, "web1"))
    with pytest.raises(ConfigError) as exc:
        load_devices([first, second], config)
    assert "id 110 already used" in str(exc.value)
    assert "name already used" in str(exc.value)


def test_one_gateway_per_bridge(config, write_devices):
    extra = "    bridges: [vmbr0, vmbr10]\n    role: gateway\n"
    first = write_devices("a.yml", (110, "gw1"), extra=extra)
    second = write_devices("b.yml", (111, "gw2"), extra=extra)
    with pytest.raises(ConfigError, match="vmbr10 already has gateway gw1"):
        load_devices([first, second], config)


def test_all_resolution_errors_are_reported_together(config, write_devices):
    extra = "    template: nope\n    host_node: pve9\n"
    with pytest.raises(ConfigError) as exc:
        load_devices([write_devices("d.yml", (110, "web1"), extra=extra)], config)
    assert "unknown template 'nope'" in str(exc.value)
    assert "unknown node 'pve9'" in str(exc.value)


def test_validation_errors_name_the_file_and_field(config, tmp_path):
    path = tmp_path / "d.yml"
    path.write_text("devices:\n  - type: qemu\n")
    with pytest.raises(ConfigError) as exc:
        load_devices([path], config)
    assert str(path) in str(exc.value)
    assert "devices.0.id" in str(exc.value)


@pytest.mark.parametrize("content", ["", "- a\n- b\n", "nodes: [unclosed"])
def test_malformed_config_is_a_config_error(tmp_path, content):
    path = tmp_path / "pve-mgr.yml"
    path.write_text(content)
    with pytest.raises(ConfigError):
        load_config(path)


def test_find_config(tmp_path, monkeypatch, config_path):
    monkeypatch.chdir(tmp_path)
    assert find_config().name == "pve-mgr.yml"
    assert find_config(config_path) == config_path
    with pytest.raises(ConfigError):
        find_config(tmp_path / "missing.yml")


def test_find_config_reports_searched_paths(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("pve_mgr.loader.USER_CONFIG_DIR", tmp_path / "nowhere")
    with pytest.raises(ConfigError, match="No config file found"):
        find_config()
