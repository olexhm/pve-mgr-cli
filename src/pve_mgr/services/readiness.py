import time
from collections.abc import Callable
from dataclasses import dataclass
from ipaddress import IPv4Address

from pve_mgr.client import PveClient
from pve_mgr.errors import NotReadyError
from pve_mgr.models import Device, Guest
from pve_mgr.services.addresses import pick_address


@dataclass(frozen=True)
class Waiter:
    """How long to wait for guests, with the clock injectable for tests."""

    timeout: float = 300
    interval: float = 3
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic

    def until_ready(self, client: PveClient, device: Device, node: str) -> IPv4Address:
        """Wait until a started guest reports an address, and return it.

        For QEMU this means the guest agent is answering, for LXC that the
        container's network is up: the point from which SSH and guests behind
        this one can work.
        """
        guest = Guest(vmid=device.id, type=device.type, node=node, status="running")
        deadline = self.clock() + self.timeout
        while True:
            if addresses := client.addresses(guest):
                network = client.bridge_network(node, device.bridge)
                address = pick_address(addresses, on=network)
                assert address is not None
                return address
            if self.clock() >= deadline:
                raise NotReadyError(
                    f"{device.name} reported no address within {self.timeout:g}s "
                    "(is the guest agent installed and DHCP available?)"
                )
            self.sleep(self.interval)
