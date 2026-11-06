"""Destroy guests and the pools they leave empty."""

from collections.abc import Callable, Sequence

from pve_mgr.client import PveClient
from pve_mgr.errors import DestroyError
from pve_mgr.models import Device
from pve_mgr.services.apply import Report
from pve_mgr.services.status import DeviceStatus, device_statuses

type Forget = Callable[[Sequence[str], int], None]


def find_targets(client: PveClient, devices: Sequence[Device]) -> list[DeviceStatus]:
    """The devices that exist, in destroy order (reverse file order).

    Raises before anything is touched if a VMID belongs to something other than
    the device that claims it: destroying the wrong guest cannot be undone.
    """
    targets = [s for s in reversed(device_statuses(client, devices)) if s.guest is not None]
    mismatches = [
        f"VMID {s.device.id} is {s.guest.type} '{s.guest.name}'"
        f"{' (a template)' if s.guest.template else ''}, not {s.device.type} '{s.device.name}'"
        for s in targets
        if s.guest is not None
        and (s.guest.name != s.device.name or s.guest.type is not s.device.type or s.guest.template)
    ]
    if mismatches:
        raise DestroyError(
            "Refusing to destroy guests that do not match the devices file:\n"
            + "\n".join(f"  - {line}" for line in mismatches)
        )
    return targets


def destroy(
    client: PveClient, targets: Sequence[DeviceStatus], *, report: Report, forget: Forget
) -> None:
    """Stop and delete each target, then delete the pools that are left empty.

    Stops at the first failure, before any pool is removed.
    """
    destroyed: set[str] = set()
    for target in targets:
        device, guest = target.device, target.guest
        if guest is None:
            continue
        hosts = [device.name] + ([str(target.address)] if target.address else [])
        report(device.name, f"forgetting SSH host keys of {', '.join(hosts)}")
        forget(hosts, device.ssh_port)
        if guest.running:
            report(device.name, "stopping")
            client.stop(guest)
        report(device.name, f"destroying on {guest.node}")
        client.delete(guest)
        destroyed.add(guest.ref)

    for pool in dict.fromkeys(target.device.pool for target in targets):
        members = client.pool_members(pool)
        if members is None:
            continue
        remaining = [member for member in members if member not in destroyed]
        if remaining:
            report(pool, f"pool kept, it still contains {', '.join(remaining)}")
        else:
            report(pool, "deleting empty pool")
            client.delete_pool(pool)
