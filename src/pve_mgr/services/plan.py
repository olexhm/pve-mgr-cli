"""Compare devices files with the cluster. Reads only."""

import re
from collections.abc import Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from enum import StrEnum

from pve_mgr.client import PveClient
from pve_mgr.errors import TransportError
from pve_mgr.models import Device, Guest, Hardware, NicSpec
from pve_mgr.transport import Json

_BRIDGE = re.compile(r"(?:^|,)bridge=([^,]+)")
_SIZE = re.compile(r"(?:^|,)size=(\d+)([KMGT]?)")
_STORAGE = re.compile(r"^([^:,]+):")
_TAG_SEPARATOR = re.compile(r"[;,\s]+")
_GIB_PER_UNIT = {"": 1 / 1024**3, "K": 1 / 1024**2, "M": 1 / 1024, "G": 1, "T": 1024}


class Action(StrEnum):
    CREATE = "create"
    UNCHANGED = "unchanged"
    DRIFT = "drift"


@dataclass(frozen=True)
class Difference:
    field: str
    wanted: str
    actual: str


@dataclass(frozen=True)
class PlanEntry:
    device: Device
    action: Action
    guest: Guest | None = None
    differences: tuple[Difference, ...] = ()


@dataclass(frozen=True)
class Plan:
    entries: tuple[PlanEntry, ...]
    pools_to_create: tuple[str, ...]
    problems: tuple[str, ...]

    def with_action(self, action: Action) -> tuple[PlanEntry, ...]:
        return tuple(entry for entry in self.entries if entry.action is action)


def build_plan(client: PveClient, devices: Sequence[Device]) -> Plan:
    guests = client.guests()
    configs = {
        device.id: client.guest_config(guests[device.id])
        for device in devices
        if device.id in guests
    }
    return make_plan(devices, guests, configs, client.pools(), _isos(client, devices, guests))


def _isos(
    client: PveClient, devices: Sequence[Device], guests: Mapping[int, Guest]
) -> set[tuple[str, str]]:
    """(node, image) for every image the guests still to be created could boot."""
    wanted = dict.fromkeys(
        (device.host_node, device.iso.image.split(":")[0])
        for device in devices
        if device.iso is not None and device.id not in guests
    )
    found: set[tuple[str, str]] = set()
    for node, storage in wanted:
        try:
            found |= {(node, image) for image in client.iso_images(node, storage)}
        except TransportError:
            continue
    return found


def make_plan(
    devices: Sequence[Device],
    guests: Mapping[int, Guest],
    configs: Mapping[int, Mapping[str, Json]],
    pools: set[str],
    isos: AbstractSet[tuple[str, str]] = frozenset(),
) -> Plan:
    entries: list[PlanEntry] = []
    for device in devices:
        guest = guests.get(device.id)
        if guest is None:
            entries.append(PlanEntry(device, Action.CREATE))
            continue
        differences = _differences(device, guest, configs.get(device.id, {}))
        action = Action.DRIFT if differences else Action.UNCHANGED
        entries.append(PlanEntry(device, action, guest, differences))

    to_create = [entry.device for entry in entries if entry.action is Action.CREATE]
    missing_pools = dict.fromkeys(d.pool for d in to_create if d.pool not in pools)
    problems = dict.fromkeys(
        problem for device in to_create if (problem := _source_problem(device, guests, isos))
    )
    return Plan(tuple(entries), tuple(missing_pools), tuple(problems))


def _source_problem(
    device: Device, guests: Mapping[int, Guest], isos: AbstractSet[tuple[str, str]]
) -> str | None:
    """Why the template or image a new guest is made from cannot be used, if so."""
    if device.iso is not None:
        if (device.host_node, device.iso.image) in isos:
            return None
        return f"ISO '{device.iso.image}' not found on {device.host_node}"
    if device.template_id is None:
        return None
    template = guests.get(device.template_id)
    what = f"template '{device.template}' ({device.template_id})"
    if template is None:
        return f"{what} does not exist"
    if not template.template:
        return f"{what} is not a template"
    if template.type is not device.type:
        return f"{what} is {template.type}, but is used for {device.type} devices"
    if template.node != device.builder_node:
        return f"{what} is on {template.node}, not on builder node {device.builder_node}"
    return None


def _differences(
    device: Device, guest: Guest, config: Mapping[str, Json]
) -> tuple[Difference, ...]:
    wanted_disk = device.additional_disk
    pairs: list[tuple[str, object, object]] = [
        ("type", device.type.value, guest.type.value),
        ("name", device.name, guest.name),
        ("node", device.host_node, guest.node),
        ("pool", device.pool, guest.pool),
        ("cores", device.cores, _int(config.get("cores"))),
        ("memory_mb", device.memory_mb, _int(config.get("memory"))),
        ("bridges", ",".join(device.bridges), ",".join(_bridges(config, len(device.bridges)))),
        ("tags", _tags(device.tags), _tags(_TAG_SEPARATOR.split(str(config.get("tags") or "")))),
    ]
    if wanted_disk is not None:
        options = wanted_disk.options()
        wanted = ",".join([f"{wanted_disk.storage}:{wanted_disk.size_gib}G", *options])
        pairs.append((wanted_disk.slot, wanted, _disk(config.get(wanted_disk.slot), options)))
    differences = [
        Difference(field, str(wanted), "unset" if actual is None else str(actual))
        for field, wanted, actual in pairs
        if wanted != actual
    ]
    if device.hardware is not None:
        differences += _hardware_differences(device, device.hardware, config)
    return tuple(differences)


def _hardware_differences(
    device: Device, hardware: Hardware, config: Mapping[str, Json]
) -> list[Difference]:
    """Only what the device spells out is compared; the rest is the template's business."""
    differences = [
        Difference(key, _shown(wanted), "unset" if config.get(key) is None else str(config[key]))
        for key, wanted in hardware.params().items()
        if not _matches(wanted, config.get(key))
    ]
    if hardware.nic is not None:
        for index in range(len(device.bridges)):
            differences += _nic_differences(f"net{index}", hardware.nic, config)
    return differences


def _nic_differences(key: str, nic: NicSpec, config: Mapping[str, Json]) -> list[Difference]:
    actual = str(config.get(key, ""))
    found = dict(part.partition("=")[::2] for part in _parts(actual))
    wanted: dict[str, tuple[object, bool]] = {}
    if nic.model is not None:
        wanted["model"] = (nic.model.value, nic.model.value in found)
    if nic.firewall is not None:
        wanted["firewall"] = (int(nic.firewall), found.get("firewall", "0") == _shown(nic.firewall))
    if nic.mtu is not None:
        wanted["mtu"] = (nic.mtu, found.get("mtu") == str(nic.mtu))
    if all(matches for _, matches in wanted.values()):
        return []
    written = ",".join(f"{name}={value}" for name, (value, _) in wanted.items())
    return [Difference(key, written, actual or "unset")]


def _shown(value: str | int | bool) -> str:
    return str(int(value)) if isinstance(value, bool) else str(value)


def _matches(wanted: str | int | bool, actual: Json) -> bool:
    """Whether the guest has the setting, however Proxmox chose to write it down."""
    if actual is None:
        return False
    found = _shown(actual) if isinstance(actual, str | int | bool) else str(actual)
    try:
        return float(_shown(wanted)) == float(found)
    except ValueError:
        pass
    return set(_parts(_shown(wanted))) <= set(_parts(found))


def _parts(value: str) -> list[str]:
    return [part.strip() for part in value.strip().split(",")]


def _int(value: Json) -> int | None:
    try:
        return int(str(value))
    except ValueError:
        return None


def _bridges(config: Mapping[str, Json], count: int) -> list[str]:
    """Bridges of the first `count` NICs; extra NICs on the guest are not ours to judge."""
    bridges = []
    for index in range(count):
        match = _BRIDGE.search(str(config.get(f"net{index}", "")))
        bridges.append(match.group(1) if match else "none")
    return bridges


def _tags(tags: Iterable[str]) -> str:
    """Tags in one fixed order, because Proxmox may store them sorted."""
    return ";".join(sorted({tag for tag in tags if tag})) or "none"


def _disk(value: Json, options: Sequence[str] = ()) -> str | None:
    """`local-lvm:vm-110-disk-1,size=50G,ssd=1` -> `local-lvm:50G`, and `,ssd=1` if asked about.

    `options` are the settings the device spells out; others on the guest are left out.
    """
    if value is None:
        return None
    storage, size = _STORAGE.search(str(value)), _SIZE.search(str(value))
    if storage is None or size is None:
        return str(value)
    gib = int(size.group(1)) * _GIB_PER_UNIT[size.group(2)]
    found = dict(part.partition("=")[::2] for part in _parts(str(value)))
    names = [option.partition("=")[0] for option in options]
    kept = [f"{name}={found[name]}" for name in names if name in found]
    return ",".join([f"{storage.group(1)}:{gib:g}G", *kept])
