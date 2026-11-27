"""Start, shut down and restart existing guests."""

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from pve_mgr.client import PveClient
from pve_mgr.errors import TransportError
from pve_mgr.models import Device, Guest, GuestType
from pve_mgr.services.apply import Report
from pve_mgr.services.readiness import Waiter


@dataclass(frozen=True)
class Settle:
    """After a QEMU guest shuts down, wait until Proxmox has finished with it."""

    grace: float = 2
    timeout: float = 60
    interval: float = 1
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic

    def after_shutdown(self, client: PveClient, device: Device, guest: Guest) -> None:
        if guest.type is not GuestType.QEMU:
            return
        deadline = self.clock() + self.timeout
        while client.refresh(guest).running:
            self._check(deadline, device, "still running")
            self.sleep(self.interval)
        self.sleep(self.grace)
        while not client.try_lock(guest):
            self._check(deadline, device, "still locked by Proxmox")
            self.sleep(self.interval)

    def _check(self, deadline: float, device: Device, state: str) -> None:
        if self.clock() >= deadline:
            raise TransportError(
                f"{device.name} is {state} {self.timeout:g}s after it was shut down"
            )


def start(
    client: PveClient, devices: Sequence[Device], *, report: Report, waiter: Waiter | None = None
) -> None:
    """Start stopped guests in file order, each one ready before the next."""
    guests = client.guests()
    for device in devices:
        guest = _existing(client, device, guests, report)
        if guest is None:
            continue
        if guest.running:
            report(device.name, "already running")
            continue
        _start(client, device, guest, report, waiter)


def shutdown(
    client: PveClient,
    devices: Sequence[Device],
    *,
    report: Report,
    timeout: int = 180,
    force: bool = False,
    settle: Settle | None = None,
) -> None:
    """Shut running guests down in reverse file order, so gateways go last."""
    guests = client.guests()
    for device in reversed(devices):
        guest = _existing(client, device, guests, report)
        if guest is not None:
            _shutdown(client, device, guest, report, timeout, force, settle)


def restart(
    client: PveClient,
    devices: Sequence[Device],
    *,
    report: Report,
    waiter: Waiter | None = None,
    timeout: int = 180,
    force: bool = False,
    settle: Settle | None = None,
) -> None:
    """Shut everything down in reverse order, then start it all in order."""
    guests = client.guests()
    existing = [(d, g) for d in devices if (g := _existing(client, d, guests, report)) is not None]
    for device, guest in reversed(existing):
        _shutdown(client, device, guest, report, timeout, force, settle)
    for device, guest in existing:
        _start(client, device, guest, report, waiter)


def _existing(
    client: PveClient, device: Device, guests: dict[int, Guest], report: Report
) -> Guest | None:
    """The device's guest with its current state, or None if it does not exist."""
    guest = guests.get(device.id)
    if guest is None:
        report(device.name, "does not exist, skipped")
        return None
    return client.refresh(guest)


def _start(
    client: PveClient, device: Device, guest: Guest, report: Report, waiter: Waiter | None
) -> None:
    report(device.name, f"starting on {guest.node}")
    client.start(device, guest.node)
    if waiter is not None:
        report(device.name, "waiting until it is reachable")
        report(device.name, f"ready at {waiter.until_ready(client, device, guest.node)}")


def _shutdown(
    client: PveClient,
    device: Device,
    guest: Guest,
    report: Report,
    timeout: int,
    force: bool,
    settle: Settle | None,
) -> None:
    if not guest.running:
        report(device.name, f"already {guest.status}")
        return
    report(device.name, "shutting down")
    client.shutdown(guest, timeout=timeout, force=force)
    if settle is not None:
        settle.after_shutdown(client, device, guest)
