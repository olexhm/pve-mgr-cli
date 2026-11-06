"""Create the guests that are missing, in the order the files give them."""

from collections.abc import Callable, Sequence

from pve_mgr.client import PveClient
from pve_mgr.errors import ApplyError, PveMgrError
from pve_mgr.models import Device
from pve_mgr.services.plan import Action, Plan, build_plan
from pve_mgr.services.readiness import Waiter

type Report = Callable[[str, str], None]


def apply(
    client: PveClient,
    devices: Sequence[Device],
    *,
    report: Report,
    waiter: Waiter | None = None,
    wait_for_installs: bool = False,
) -> Plan:
    """Create every missing guest. `waiter=None` starts guests without waiting for them.

    A guest created from an ISO has no system to report an address until someone
    has installed one, so it is only waited for when `wait_for_installs` is set.

    Existing guests are left alone, whatever state they are in. Returns the plan
    that was carried out.
    """
    plan = build_plan(client, devices)
    if plan.problems:
        raise ApplyError("Cannot apply:\n" + "\n".join(f"  - {p}" for p in plan.problems))

    for pool in plan.pools_to_create:
        report(pool, "creating pool")
        client.create_pool(pool)

    created: list[str] = []
    for entry in plan.entries:
        device = entry.device
        if entry.action is not Action.CREATE:
            report(device.name, "exists, left as it is")
            continue
        try:
            waited = device.iso is None or wait_for_installs
            _create(client, device, report, waiter if waited else None)
        except PveMgrError as exc:
            done = f" Created before the failure: {', '.join(created)}." if created else ""
            raise ApplyError(
                f"{device.name}: {exc}\n{device.name} may be left half-built; "
                f"check it with 'plan' and destroy it before applying again.{done}"
            ) from exc
        created.append(device.name)
    return plan


def _create(client: PveClient, device: Device, report: Report, waiter: Waiter | None) -> None:
    if device.iso is None:
        report(device.name, f"cloning {device.template} on {device.builder_node}")
        client.clone(device)
    else:
        report(device.name, f"creating on {device.host_node} to install from {device.iso.image}")
        client.create(device, device.iso)
    report(device.name, "configuring")
    client.configure(device, device.builder_node)
    if device.host_node != device.builder_node:
        report(device.name, f"migrating to {device.host_node}")
        client.migrate(device, device.builder_node)
    if (disk := device.additional_disk) is not None:
        report(device.name, f"adding {disk.size} disk on {disk.storage} as {disk.slot}")
        client.attach_disk(device, device.host_node)
    report(device.name, "starting")
    client.start(device, device.host_node)
    if waiter is not None:
        report(device.name, "waiting until it is reachable")
        address = waiter.until_ready(client, device, device.host_node)
        report(device.name, f"ready at {address}")
    elif device.iso is not None:
        report(device.name, "boots the installer; finish the installation from its console")
