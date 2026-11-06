from collections.abc import Sequence
from ipaddress import IPv4Address, IPv4Network

from pve_mgr.client import PveClient
from pve_mgr.models import Device, Guest


def guest_address(client: PveClient, device: Device, guest: Guest) -> IPv4Address | None:
    """The address to reach a guest on: the one on its first bridge when we can tell."""
    return pick_address(
        client.addresses(guest), on=client.bridge_network(guest.node, device.bridge)
    )


def pick_address(addresses: Sequence[IPv4Address], *, on: IPv4Network | None) -> IPv4Address | None:
    if on is not None:
        for address in addresses:
            if address in on:
                return address
    return addresses[0] if addresses else None
