from pathlib import Path

import pytest

from pve_mgr.loader import load_config
from pve_mgr.models import Config

CONFIG = """
nodes:
  pve1: root@pve1
  pve2: root@pve2
defaults:
  host_node: pve1
  disk_storage: local-lvm
templates:
  qemu-debian13: 9101
  lxc-debian13: 9201
"""

DEVICE = """
  - type: qemu
    id: {id}
    name: {name}
    pool: lab
    template: qemu-debian13
    cores: 2
    memory_mb: 2048
    bridges: [vmbr0]
    ansible_user: ansible
"""

ISO_DEVICE = """
devices:
  - type: qemu
    id: 120
    name: install1
    pool: lab
    iso:
      image: local:iso/debian.iso
      disk: {size: 20G}
    cores: 2
    memory_mb: 2048
    bridges: [vmbr0]
    ansible_user: ansible
"""


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "pve-mgr.yml"
    path.write_text(CONFIG)
    return path


@pytest.fixture
def config(config_path: Path) -> Config:
    return load_config(config_path)


@pytest.fixture
def write_devices(tmp_path: Path):
    def write(filename: str, *devices: tuple[int, str], extra: str = "") -> Path:
        path = tmp_path / filename
        body = "".join(DEVICE.format(id=id_, name=name) for id_, name in devices)
        path.write_text(f"devices:{body}{extra}")
        return path

    return write


@pytest.fixture
def iso_devices(tmp_path: Path) -> Path:
    path = tmp_path / "iso.yml"
    path.write_text(ISO_DEVICE)
    return path
