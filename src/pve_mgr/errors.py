class PveMgrError(Exception):
    """Base class for every error the CLI reports without a traceback."""

    exit_code = 1


class ConfigError(PveMgrError):
    """A config or devices file is missing, malformed or inconsistent."""

    exit_code = 2


class TransportError(PveMgrError):
    """A call to Proxmox failed."""


class TaskError(TransportError):
    """A Proxmox background task failed or did not finish in time."""


class ApplyError(PveMgrError):
    """Creating a guest failed part-way."""


class DestroyError(PveMgrError):
    """A guest must not or could not be destroyed."""


class InventoryError(PveMgrError):
    """The inventory could not be built or written."""


class NotReadyError(PveMgrError):
    """A guest did not become reachable in time."""


class UnsupportedOperationError(TransportError):
    """The active transport cannot perform the requested operation."""
