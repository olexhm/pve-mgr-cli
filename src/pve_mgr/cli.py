import json
import sys
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from pve_mgr import __version__
from pve_mgr.client import PveClient
from pve_mgr.errors import PveMgrError
from pve_mgr.known_hosts import KnownHosts
from pve_mgr.loader import find_config, load_config, load_devices
from pve_mgr.models import Config, Device, DevicesFile, TransportKind
from pve_mgr.services import apply as apply_service
from pve_mgr.services import destroy as destroy_service
from pve_mgr.services import lifecycle
from pve_mgr.services.inventory import build_inventory, write_inventory
from pve_mgr.services.plan import Action, Plan, build_plan
from pve_mgr.services.readiness import Waiter
from pve_mgr.services.status import device_statuses
from pve_mgr.transport import Call, DryRunTransport, build_dry_run, build_transport

app = typer.Typer(
    help="Declarative Proxmox VE guest management over the REST API or SSH.",
    no_args_is_help=True,
)
console = Console()

ConfigOption = Annotated[
    Path | None,
    typer.Option("--config", "-c", envvar="PVE_MGR_CONFIG", help="Path to the config file."),
]
DevicesArgument = Annotated[
    list[Path], typer.Argument(help="Devices files, applied in the order given.")
]
DryRunOption = Annotated[
    bool, typer.Option("--dry-run", help="Read the cluster, but only print what would change.")
]
TimeoutOption = Annotated[
    float, typer.Option("--timeout", min=1, help="Seconds to wait for each guest.")
]
WaitOption = Annotated[
    bool, typer.Option("--wait/--no-wait", "-w", help="Wait for each guest before the next one.")
]
ApplyWaitOption = Annotated[
    bool | None,
    typer.Option(
        "--wait/--no-wait",
        "-w",
        help="Wait for each guest before the next one. By default cloned guests are waited "
        "for and guests created from an ISO are not; --wait waits for those as well.",
    ),
]
ShutdownTimeoutOption = Annotated[
    int, typer.Option("--shutdown-timeout", min=1, help="Seconds a guest gets to shut down.")
]
ForceOption = Annotated[
    bool, typer.Option("--force", help="Hard-stop a guest that does not shut down in time.")
]


class SchemaKind(StrEnum):
    CONFIG = "config"
    DEVICES = "devices"


def _version(value: bool) -> None:
    if value:
        console.print(f"pve-mgr {__version__}")
        raise typer.Exit


_overrides: dict[str, str] = {}


@app.callback()
def root(
    version: Annotated[
        bool, typer.Option("--version", callback=_version, is_eager=True, help="Show the version.")
    ] = False,
    transport: Annotated[
        TransportKind | None,
        typer.Option(envvar="PVE_MGR_TRANSPORT", help="Override the config's transport."),
    ] = None,
    entry_node: Annotated[
        str | None,
        typer.Option(envvar="PVE_MGR_ENTRY_NODE", help="Override the node calls go through."),
    ] = None,
) -> None:
    _overrides.clear()
    if transport is not None:
        _overrides["transport"] = transport.value
    if entry_node is not None:
        _overrides["entry_node"] = entry_node


def _load_config(path: Path | None) -> Config:
    return load_config(find_config(path), _overrides)


@app.command()
def validate(devices: DevicesArgument, config: ConfigOption = None) -> None:
    """Check the config and devices files without contacting Proxmox."""
    cfg = _load_config(config)
    table = Table("name", "type", "id", "pool", "source", "host", "cores", "mem_mb", "bridges")
    for device in load_devices(devices, cfg):
        table.add_row(
            device.name,
            device.type,
            str(device.id),
            device.pool,
            device.iso.image if device.iso else f"{device.template} ({device.template_id})",
            device.host_node,
            str(device.cores),
            str(device.memory_mb),
            ",".join(device.bridges),
        )
    console.print(table)


@app.command()
def status(
    devices: DevicesArgument,
    config: ConfigOption = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON instead of a table.")] = False,
) -> None:
    """Show where each device runs, its state and its address."""
    session = _Session(devices, config)
    entries = device_statuses(session.client, session.devices)
    if as_json:
        rows = [
            {
                "name": entry.device.name,
                "id": entry.device.id,
                "type": entry.device.type.value,
                "node": entry.guest.node if entry.guest else None,
                "state": entry.guest.status if entry.guest else "absent",
                "address": str(entry.address) if entry.address else None,
            }
            for entry in entries
        ]
        print(json.dumps(rows, indent=2))
        return

    table = Table("name", "type", "id", "node", "state", "address")
    for entry in entries:
        device, guest = entry.device, entry.guest
        if guest is None:
            table.add_row(device.name, device.type, str(device.id), "-", "[dim]absent[/]", "-")
            continue
        state = f"[green]{guest.status}[/]" if guest.running else guest.status
        table.add_row(
            device.name, guest.type, str(device.id), guest.node, state, str(entry.address or "-")
        )
    console.print(table)


_ACTION_STYLE = {Action.CREATE: "green", Action.UNCHANGED: "dim", Action.DRIFT: "yellow"}


def _render_plan(plan: Plan) -> None:
    table = Table("name", "id", "action", "details")
    for entry in plan.entries:
        device = entry.device
        match entry.action:
            case Action.CREATE if device.iso is not None:
                details = f"create on {device.host_node}, install from {device.iso.image}"
            case Action.CREATE:
                details = f"clone {device.template} on {device.builder_node} -> {device.host_node}"
            case Action.UNCHANGED:
                details = ""
            case Action.DRIFT:
                details = "\n".join(
                    f"{d.field}: wanted {d.wanted}, is {d.actual}" for d in entry.differences
                )
        style = _ACTION_STYLE[entry.action]
        table.add_row(device.name, str(device.id), f"[{style}]{entry.action}[/]", details)
    console.print(table)
    for pool in plan.pools_to_create:
        console.print(f"pool to create: {pool}", highlight=False)
    for problem in plan.problems:
        console.print(f"[bold red]problem:[/] {problem}", highlight=False)
    counts = ", ".join(f"{len(plan.with_action(action))} {action}" for action in Action)
    console.print(f"{counts}. Existing guests are never modified.", highlight=False)


@app.command()
def plan(devices: DevicesArgument, config: ConfigOption = None) -> None:
    """Show what `apply` would create and how existing guests differ from the files."""
    session = _Session(devices, config)
    _render_plan(build_plan(session.client, session.devices))


def _report(subject: str, message: str) -> None:
    console.print(f"[bold]{subject}[/]: {message}", highlight=False)


def _render_calls(calls: list[Call]) -> None:
    console.print("\n[bold]Dry run[/] - these calls were not made:", highlight=False)
    for call in calls:
        params = " ".join(f"{key}={value}" for key, value in call.params.items())
        console.print(f"  {call.method:<6} {call.path} {params}".rstrip(), highlight=False)


class _Session:
    """What every command that talks to the cluster needs, built from the CLI options."""

    def __init__(self, devices: list[Path], config: Path | None, dry_run: bool = False) -> None:
        self.config = _load_config(config)
        self.devices: tuple[Device, ...] = load_devices(devices, self.config)
        self.recorder: DryRunTransport | None = build_dry_run(self.config) if dry_run else None
        self.client = PveClient(self.recorder or build_transport(self.config))

    def waiter(self, wait: bool, timeout: float) -> Waiter | None:
        return Waiter(timeout=timeout) if wait and self.recorder is None else None

    def finish(self) -> None:
        if self.recorder is not None:
            _render_calls(self.recorder.planned)


@app.command()
def apply(
    devices: DevicesArgument,
    config: ConfigOption = None,
    dry_run: DryRunOption = False,
    wait: ApplyWaitOption = None,
    timeout: TimeoutOption = 300,
) -> None:
    """Create the guests that do not exist yet, in order. Existing guests are untouched."""
    session = _Session(devices, config, dry_run)
    apply_service.apply(
        session.client,
        session.devices,
        report=_report,
        waiter=session.waiter(wait is not False, timeout),
        wait_for_installs=wait is True,
    )
    session.finish()


@app.command()
def start(
    devices: DevicesArgument,
    config: ConfigOption = None,
    dry_run: DryRunOption = False,
    wait: WaitOption = True,
    timeout: TimeoutOption = 300,
) -> None:
    """Start stopped guests, in order."""
    session = _Session(devices, config, dry_run)
    lifecycle.start(
        session.client, session.devices, report=_report, waiter=session.waiter(wait, timeout)
    )
    session.finish()


@app.command()
def shutdown(
    devices: DevicesArgument,
    config: ConfigOption = None,
    dry_run: DryRunOption = False,
    shutdown_timeout: ShutdownTimeoutOption = 180,
    force: ForceOption = False,
) -> None:
    """Shut running guests down cleanly, in reverse order."""
    session = _Session(devices, config, dry_run)
    lifecycle.shutdown(
        session.client, session.devices, report=_report, timeout=shutdown_timeout, force=force
    )
    session.finish()


@app.command()
def restart(
    devices: DevicesArgument,
    config: ConfigOption = None,
    dry_run: DryRunOption = False,
    wait: WaitOption = True,
    timeout: TimeoutOption = 300,
    shutdown_timeout: ShutdownTimeoutOption = 180,
    force: ForceOption = False,
) -> None:
    """Shut guests down in reverse order, then start them again in order."""
    session = _Session(devices, config, dry_run)
    lifecycle.restart(
        session.client,
        session.devices,
        report=_report,
        waiter=session.waiter(wait, timeout),
        timeout=shutdown_timeout,
        force=force,
    )
    session.finish()


@app.command()
def destroy(
    devices: DevicesArgument,
    config: ConfigOption = None,
    dry_run: DryRunOption = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")] = False,
) -> None:
    """Stop and delete guests, in reverse order, and the pools they leave empty."""
    session = _Session(devices, config, dry_run)
    targets = destroy_service.find_targets(session.client, session.devices)
    if not targets:
        console.print("None of these devices exist; nothing to destroy.")
        return

    table = Table("name", "type", "id", "node", "state", title="To be destroyed, with their disks")
    for target in targets:
        if (guest := target.guest) is not None:
            table.add_row(target.device.name, guest.type, str(guest.vmid), guest.node, guest.status)
    console.print(table)
    if not (yes or dry_run):
        typer.confirm(f"Destroy {len(targets)} guest(s)? This cannot be undone", abort=True)

    known_hosts = KnownHosts(session.config.known_hosts)
    destroy_service.destroy(
        session.client,
        targets,
        report=_report,
        forget=(lambda hosts, port: None) if dry_run else known_hosts.forget,
    )
    session.finish()


@app.command()
def inventory(
    devices: DevicesArgument,
    config: ConfigOption = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="File to write; '-' prints it. Default: from config."),
    ] = None,
) -> None:
    """Write an Ansible inventory for the guests of all the given files."""
    session = _Session(devices, config)
    result = build_inventory(session.client, session.devices, session.config)
    errors = Console(stderr=True)
    for name, reason in result.skipped:
        errors.print(f"[yellow]skipped[/] {name}: {reason}", highlight=False)

    if output == Path("-"):
        print(result.render(), end="")
        return
    path = output or session.config.inventory
    write_inventory(path, result.render())
    errors.print(f"Wrote {len(result.hosts)} host(s) to {path}", highlight=False)


@app.command()
def schema(kind: SchemaKind) -> None:
    """Print the JSON Schema of a file format, for editor completion."""
    model = Config if kind is SchemaKind.CONFIG else DevicesFile
    print(json.dumps(model.model_json_schema(), indent=2))


def main() -> None:
    try:
        app()
    except PveMgrError as exc:
        Console(stderr=True).print(f"[bold red]error:[/] {exc}", highlight=False, soft_wrap=True)
        sys.exit(exc.exit_code)
