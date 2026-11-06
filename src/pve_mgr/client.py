"""Typed Proxmox operations. The only module that knows API paths."""

from ipaddress import IPv4Address, IPv4Interface, IPv4Network
from typing import Any

from pydantic import TypeAdapter, ValidationError

from pve_mgr.errors import TransportError
from pve_mgr.models import (
    Bios,
    Device,
    Guest,
    GuestType,
    Hardware,
    Iso,
    OsType,
    ScsiController,
)
from pve_mgr.transport import Json, Method, Params, Transport

_GUESTS = TypeAdapter(list[Guest])


class PveClient:
    def __init__(self, transport: Transport) -> None:
        self._transport = transport
        self._networks: dict[tuple[str, str], IPv4Network | None] = {}

    def guests(self) -> dict[int, Guest]:
        """Every guest in the cluster by VMID, from a single call."""
        path = "/cluster/resources"
        try:
            guests = _GUESTS.validate_python(self._transport.get(path, {"type": "vm"}))
        except ValidationError as exc:
            raise TransportError(f"Unexpected reply from {path}: {exc.errors()[0]['msg']}") from exc
        return {guest.vmid: guest for guest in guests}

    def guest_config(self, guest: Guest) -> dict[str, Json]:
        """The guest's settings as Proxmox stores them (`cores`, `net0`, `scsi1`, ...)."""
        path = _path(guest, "config")
        reply = self._transport.get(path)
        if not isinstance(reply, dict):
            raise TransportError(f"Unexpected reply from {path}: {reply!r}")
        return reply

    def pools(self) -> set[str]:
        reply = self._transport.get("/pools")
        if not isinstance(reply, list):
            raise TransportError(f"Unexpected reply from /pools: {reply!r}")
        return {
            str(pool["poolid"]) for pool in reply if isinstance(pool, dict) and "poolid" in pool
        }

    def create_pool(self, pool: str) -> None:
        self._transport.execute(Method.POST, "/pools", {"poolid": pool})

    def clone(self, device: Device) -> None:
        """Full clone of the device's template, on the builder node."""
        name_key = "name" if device.type is GuestType.QEMU else "hostname"
        self._transport.execute(
            Method.POST,
            f"/nodes/{device.builder_node}/{device.type}/{device.template_id}/clone",
            {"newid": device.id, name_key: device.name, "pool": device.pool, "full": True},
        )

    def create(self, device: Device, iso: Iso) -> None:
        """Empty QEMU guest on the host node that boots the installer image.

        Only what has to be there from the start; `configure` sets the rest.
        """
        hardware = device.hardware or Hardware()
        bios = hardware.bios or Bios.SEABIOS
        params: dict[str, str | int | bool] = {
            "vmid": device.id,
            "name": device.name,
            "pool": device.pool,
            "ostype": (hardware.ostype or OsType.LINUX_26).value,
            "bios": bios.value,
            "scsihw": (hardware.scsihw or ScsiController.VIRTIO_SCSI_SINGLE).value,
            iso.disk.slot: _new_volume(iso.disk.storage, iso.disk.size_gib, iso.disk.options()),
            "ide2": f"{iso.image},media=cdrom",
            "boot": f"order={iso.disk.slot};ide2",
        }
        if hardware.machine is not None:
            params["machine"] = hardware.machine.value
        if bios is Bios.OVMF:
            params["efidisk0"] = f"{iso.disk.storage}:1,efitype=4m"
        if iso.tpm is not None:
            params["tpmstate0"] = f"{iso.disk.storage}:1,version={iso.tpm}"
        self._transport.execute(Method.POST, f"/nodes/{device.host_node}/qemu", params)

    def iso_images(self, node: str, storage: str) -> set[str]:
        """Volume IDs (`local:iso/debian-13.iso`) of the ISO images a node sees on a storage."""
        path = f"/nodes/{node}/storage/{storage}/content"
        reply = self._transport.get(path, {"content": "iso"})
        if not isinstance(reply, list):
            raise TransportError(f"Unexpected reply from {path}: {reply!r}")
        return {str(item["volid"]) for item in reply if isinstance(item, dict) and "volid" in item}

    def configure(self, device: Device, node: str) -> None:
        """Set cores, memory, tags and one NIC per bridge.

        QEMU guests also get the agent and whatever hardware the device spells out.
        """
        params: dict[str, str | int | bool] = {"cores": device.cores, "memory": device.memory_mb}
        for index, bridge in enumerate(device.bridges):
            if device.type is GuestType.QEMU:
                params[f"net{index}"] = _qemu_nic(device, bridge)
            else:
                params[f"net{index}"] = f"name=eth{index},bridge={bridge},ip=dhcp,firewall=0"
        if device.type is GuestType.QEMU:
            params["agent"] = "enabled=1"
        if device.hardware is not None:
            params |= device.hardware.params()
        if device.tags:
            params["tags"] = ";".join(device.tags)
        else:
            params["delete"] = "tags"
        self._transport.execute(Method.PUT, _guest_path(device, node, "config"), params)

    def migrate(self, device: Device, source: str) -> None:
        params: dict[str, str | int | bool] = {"target": device.host_node}
        if device.migration_storage is not None:
            if device.type is GuestType.QEMU:
                params |= {"targetstorage": device.migration_storage, "with-local-disks": True}
            else:
                params["target-storage"] = device.migration_storage
        self._transport.execute(Method.POST, _guest_path(device, source, "migrate"), params)

    def attach_disk(self, device: Device, node: str) -> None:
        disk = device.additional_disk
        if disk is None:
            return
        params: Params = {disk.slot: _new_volume(disk.storage, disk.size_gib, disk.options())}
        self._transport.execute(Method.PUT, _guest_path(device, node, "config"), params)

    def start(self, device: Device, node: str) -> None:
        self._transport.execute(Method.POST, _guest_path(device, node, "status/start"))

    def shutdown(self, guest: Guest, *, timeout: int, force: bool = False) -> None:
        """Ask the guest to shut down and wait; `force` hard-stops it after the timeout."""
        params: dict[str, str | int | bool] = {"timeout": timeout}
        if force:
            params["forceStop"] = True
        self._transport.execute(Method.POST, _path(guest, "status/shutdown"), params)

    def stop(self, guest: Guest) -> None:
        self._transport.execute(Method.POST, _path(guest, "status/stop"))

    def delete(self, guest: Guest) -> None:
        """Delete a stopped guest with its disks, and drop it from backup and HA config."""
        params: dict[str, str | int | bool] = {"purge": True}
        if guest.type is GuestType.QEMU:
            params["destroy-unreferenced-disks"] = True
        self._transport.execute(Method.DELETE, _path(guest), params)

    def pool_members(self, pool: str) -> list[str] | None:
        """IDs of a pool's members (`qemu/110`, `storage/...`); None if there is no such pool."""
        if pool not in self.pools():
            return None
        reply = self._transport.get(f"/pools/{pool}")
        members = reply.get("members") if isinstance(reply, dict) else None
        if not isinstance(members, list):
            raise TransportError(f"Unexpected reply from /pools/{pool}: {reply!r}")
        return [str(member.get("id")) for member in members if isinstance(member, dict)]

    def delete_pool(self, pool: str) -> None:
        self._transport.execute(Method.DELETE, f"/pools/{pool}")

    def addresses(self, guest: Guest) -> list[IPv4Address]:
        """Usable IPv4 addresses of a running guest; empty if it cannot tell us yet."""
        if not guest.running:
            return []
        base = _path(guest)
        try:
            if guest.type is GuestType.QEMU:
                reply = self._transport.get(f"{base}/agent/network-get-interfaces")
                interfaces = reply.get("result") if isinstance(reply, dict) else reply
            else:
                interfaces = self._transport.get(f"{base}/interfaces")
        except TransportError:
            return []
        found = [address for raw in _raw_addresses(interfaces) if (address := _usable(raw))]
        return list(dict.fromkeys(found))

    def bridge_network(self, node: str, bridge: str) -> IPv4Network | None:
        """The IPv4 network configured on a node's bridge, if it has an address."""
        key = (node, bridge)
        if key not in self._networks:
            self._networks[key] = self._bridge_network(node, bridge)
        return self._networks[key]

    def _bridge_network(self, node: str, bridge: str) -> IPv4Network | None:
        try:
            reply = self._transport.get(f"/nodes/{node}/network/{bridge}")
        except TransportError:
            return None
        if not isinstance(reply, dict):
            return None
        cidr = reply.get("cidr")
        if not cidr and reply.get("address") and reply.get("netmask"):
            cidr = f"{reply['address']}/{reply['netmask']}"
        try:
            return IPv4Interface(str(cidr)).network
        except ValueError:
            return None


def _new_volume(storage: str, size_gib: int, options: tuple[str, ...]) -> str:
    return ",".join([f"{storage}:{size_gib}", *options])


def _qemu_nic(device: Device, bridge: str) -> str:
    nic = device.hardware.nic if device.hardware else None
    model = nic.model.value if nic and nic.model else "virtio"
    parts = [f"model={model}", f"bridge={bridge}", f"firewall={int(bool(nic and nic.firewall))}"]
    if nic and nic.mtu is not None:
        parts.append(f"mtu={nic.mtu}")
    return ",".join(parts)


def _guest_path(device: Device, node: str, suffix: str) -> str:
    return f"/nodes/{node}/{device.type}/{device.id}/{suffix}"


def _path(guest: Guest, suffix: str = "") -> str:
    return f"/nodes/{guest.node}/{guest.type}/{guest.vmid}/{suffix}".rstrip("/")


def _raw_addresses(interfaces: Json) -> list[Any]:
    """Pull address strings out of a guest-agent or LXC interface list."""
    raw: list[Any] = []
    if not isinstance(interfaces, list):
        return raw
    for interface in interfaces:
        if not isinstance(interface, dict):
            continue
        raw.append(interface.get("inet"))
        entries = interface.get("ip-addresses")
        if isinstance(entries, list):
            raw.extend(entry.get("ip-address") for entry in entries if isinstance(entry, dict))
    return raw


def _usable(raw: Any) -> IPv4Address | None:
    try:
        address = IPv4Interface(str(raw)).ip
    except ValueError:
        return None
    return None if address.is_loopback or address.is_link_local else address
