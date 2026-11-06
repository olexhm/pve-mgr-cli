"""Start, shut down and restart existing guests."""

from collections.abc import Sequence

from pve_mgr.client import PveClient
from pve_mgr.models import Device, Guest
from pve_mgr.services.apply import Report
from pve_mgr.services.readiness import Waiter


def start(
    client: PveClient, devices: Sequence[Device], *, report: Report, waiter: Waiter | None = None
) -> None:
    """Start stopped guests in file order, each one ready before the next."""
    guests = client.guests()
    for device in devices:
        guest = _existing(device, guests, report)
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
) -> None:
    """Shut running guests down in reverse file order, so gateways go last."""
    guests = client.guests()
    for device in reversed(devices):
        guest = _existing(device, guests, report)
        if guest is not None:
            _shutdown(client, device, guest, report, timeout, force)


def restart(
    client: PveClient,
    devices: Sequence[Device],
    *,
    report: Report,
    waiter: Waiter | None = None,
    timeout: int = 180,
    force: bool = False,
) -> None:
    """Shut everything down in reverse order, then start it all in order."""
    guests = client.guests()
    existing = [(d, g) for d in devices if (g := _existing(d, guests, report)) is not None]
    for device, guest in reversed(existing):
        _shutdown(client, device, guest, report, timeout, force)
    for device, guest in existing:
        _start(client, device, guest, report, waiter)


def _existing(device: Device, guests: dict[int, Guest], report: Report) -> Guest | None:
    guest = guests.get(device.id)
    if guest is None:
        report(device.name, "does not exist, skipped")
    return guest


def _start(
    client: PveClient, device: Device, guest: Guest, report: Report, waiter: Waiter | None
) -> None:
    report(device.name, f"starting on {guest.node}")
    client.start(device, guest.node)
    if waiter is not None:
        report(device.name, "waiting until it is reachable")
        report(device.name, f"ready at {waiter.until_ready(client, device, guest.node)}")


def _shutdown(
    client: PveClient, device: Device, guest: Guest, report: Report, timeout: int, force: bool
) -> None:
    if not guest.running:
        report(device.name, f"already {guest.status}")
        return
    report(device.name, "shutting down")
    client.shutdown(guest, timeout=timeout, force=force)
