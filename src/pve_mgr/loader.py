"""Read config and devices files and turn them into validated models."""

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from pve_mgr.errors import ConfigError
from pve_mgr.models import BootDisk, Config, Device, DevicesFile, DeviceSpec, Disk, Iso

CONFIG_NAMES = ("pve-mgr.yml", "pve-mgr.yaml")
USER_CONFIG_DIR = Path("~/.config/pve-mgr")


def find_config(explicit: Path | None = None) -> Path:
    """Return the config file to use: explicit path, then cwd, then the user config dir."""
    if explicit is not None:
        if not explicit.is_file():
            raise ConfigError(f"Config file not found: {explicit}")
        return explicit

    candidates = [
        directory / name
        for directory in (Path.cwd(), USER_CONFIG_DIR.expanduser())
        for name in CONFIG_NAMES
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate

    searched = "\n".join(f"  {candidate}" for candidate in candidates)
    raise ConfigError(f"No config file found. Pass --config or create one of:\n{searched}")


def load_config(path: Path, overrides: Mapping[str, Any] | None = None) -> Config:
    """Read the config; `overrides` (from flags or the environment) win over the file."""
    data = _read_yaml(path)
    if overrides and isinstance(data, dict):
        data = data | overrides
    config = _validate(Config, data, path)
    return config.model_copy(
        update={
            "inventory": _resolve(config.inventory, path.parent),
            "known_hosts": _resolve(config.known_hosts, path.parent),
        }
    )


def load_devices(paths: Sequence[Path], config: Config) -> tuple[Device, ...]:
    """Load one or more devices files, in order, and resolve them against the config."""
    errors: list[str] = []
    devices: list[Device] = []
    seen_ids: dict[int, str] = {}
    seen_names: dict[str, str] = {}
    gateways: dict[str, str] = {}

    for path in paths:
        for spec in _validate(DevicesFile, _read_yaml(path), path).devices:
            where = f"{path}: {spec.name}"

            if spec.id in seen_ids:
                errors.append(f"{where}: id {spec.id} already used by {seen_ids[spec.id]}")
            if spec.name in seen_names:
                errors.append(f"{where}: name already used in {seen_names[spec.name]}")
            seen_ids.setdefault(spec.id, where)
            seen_names.setdefault(spec.name, str(path))
            if (served := spec.served_bridge) is not None:
                if served in gateways:
                    errors.append(f"{where}: {served} already has gateway {gateways[served]}")
                gateways.setdefault(served, spec.name)

            resolved = _resolve_device(spec, config)
            if isinstance(resolved, Device):
                devices.append(resolved)
            else:
                errors.extend(f"{where}: {problem}" for problem in resolved)

    if errors:
        raise ConfigError("Invalid devices:\n" + "\n".join(f"  - {error}" for error in errors))
    return tuple(devices)


def _resolve_device(spec: DeviceSpec, config: Config) -> Device | list[str]:
    """Apply config defaults to a spec, or return every reason it cannot be done."""
    problems: list[str] = []

    if spec.template is not None and spec.template not in config.templates:
        problems.append(f"template: unknown template '{spec.template}'")
    for field in ("builder_node", "host_node"):
        node = getattr(spec, field)
        if node is not None and node not in config.nodes:
            problems.append(f"{field}: unknown node '{node}'")

    disk = None
    if spec.additional_disk is not None:
        storage = spec.additional_disk.storage or config.defaults.disk_storage
        if storage is None:
            problems.append(
                "additional_disk.storage: not set and no defaults.disk_storage in config"
            )
        else:
            disk = Disk(**spec.additional_disk.model_dump(exclude={"storage"}), storage=storage)

    iso = None
    if spec.iso is not None:
        storage = spec.iso.disk.storage or config.defaults.disk_storage
        if storage is None:
            problems.append("iso.disk.storage: not set and no defaults.disk_storage in config")
        else:
            iso = Iso(
                **spec.iso.model_dump(exclude={"disk"}),
                disk=BootDisk(**spec.iso.disk.model_dump(exclude={"storage"}), storage=storage),
            )

    if problems:
        return problems

    host_node = spec.host_node or config.default_host_node
    return Device(
        **spec.model_dump(
            exclude={"builder_node", "host_node", "migration_storage", "additional_disk", "iso"}
        ),
        template_id=None if spec.template is None else config.templates[spec.template],
        iso=iso,
        builder_node=host_node if iso else spec.builder_node or config.default_builder_node,
        host_node=host_node,
        migration_storage=spec.migration_storage or config.default_migration_storage,
        additional_disk=disk,
    )


def _resolve(path: Path, base: Path) -> Path:
    path = path.expanduser()
    return path if path.is_absolute() else base / path


def _read_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"Cannot read {path}: {exc.strerror}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc


def _validate[M: (Config, DevicesFile)](model: type[M], data: Any, path: Path) -> M:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        lines = [
            f"  - {'.'.join(map(str, error['loc'])) or '<root>'}: {error['msg']}"
            for error in exc.errors()
        ]
        raise ConfigError(f"Invalid {path}:\n" + "\n".join(lines)) from exc
