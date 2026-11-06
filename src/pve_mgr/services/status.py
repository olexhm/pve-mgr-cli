from collections.abc import Sequence
from dataclasses import dataclass
from ipaddress import IPv4Address

from pve_mgr.client import PveClient
from pve_mgr.models import Device, Guest
from pve_mgr.services.addresses import guest_address


@dataclass(frozen=True)
class DeviceStatus:
    device: Device
    guest: Guest | None
    address: IPv4Address | None = None


def device_statuses(client: PveClient, devices: Sequence[Device]) -> list[DeviceStatus]:
    """Where each device is and what state it is in; `guest` is None if it does not exist."""
    guests = client.guests()
    statuses: list[DeviceStatus] = []
    for device in devices:
        guest = guests.get(device.id)
        address = guest_address(client, device, guest) if guest is not None else None
        statuses.append(DeviceStatus(device, guest, address))
    return statuses
