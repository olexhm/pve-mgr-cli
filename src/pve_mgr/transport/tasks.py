"""Waiting for Proxmox background tasks, shared by every transport."""

import time
from collections.abc import Callable

from pve_mgr.errors import TaskError
from pve_mgr.transport.base import Json, Transport

UPID_PREFIX = "UPID:"


def is_upid(value: Json) -> bool:
    return isinstance(value, str) and value.startswith(UPID_PREFIX)


def wait_for_task(
    transport: Transport,
    upid: str,
    *,
    timeout: float = 1800,
    interval: float = 1,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Block until the task has stopped; raise `TaskError` unless it succeeded."""
    parts = upid.split(":")
    if len(parts) < 8:
        raise TaskError(f"Malformed task ID: {upid}")
    node, kind = parts[1], parts[5]
    path = f"/nodes/{node}/tasks/{upid}/status"

    deadline = clock() + timeout
    while True:
        status = transport.get(path)
        if not isinstance(status, dict):
            raise TaskError(f"Unexpected status reply for task {kind} on {node}: {status!r}")
        if status.get("status") == "stopped":
            exit_status = str(status.get("exitstatus", "unknown"))
            if exit_status == "OK" or exit_status.startswith("WARNINGS"):
                return
            raise TaskError(f"Task {kind} on {node} failed: {exit_status}")
        if clock() >= deadline:
            raise TaskError(f"Task {kind} on {node} did not finish within {timeout:g}s")
        sleep(interval)
