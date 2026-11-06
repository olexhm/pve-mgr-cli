"""Transport that runs `pvesh` on a Proxmox node through the system `ssh`."""

import json
import os
import re
import shlex
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from pve_mgr.errors import TransportError
from pve_mgr.transport.base import Json, Method, Params
from pve_mgr.transport.tasks import wait_for_task

type Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]

SSH_FAILURE = 255
_UPID = re.compile(r"UPID:\S+")


def run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL
    )


def control_dir() -> Path:
    """Directory for SSH connection-sharing sockets, private to the user."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime) if runtime else Path("~/.cache").expanduser()
    return base / "pve-mgr"


class SshTransport:
    """Call the Proxmox API as `ssh <target> [sudo] pvesh <verb> <path> ...`.

    Any node can serve the whole API tree, so one target is enough for a cluster.
    """

    def __init__(
        self,
        target: str,
        *,
        sudo: bool = False,
        control_dir: Path | None = None,
        runner: Runner = run,
    ) -> None:
        self._target = target
        self._sudo = sudo
        self._control_dir = control_dir
        self._runner = runner

    def get(self, path: str, params: Params | None = None) -> Json:
        stdout = self._pvesh(Method.GET, path, params)
        try:
            return json.loads(stdout) if stdout.strip() else None
        except json.JSONDecodeError as exc:
            raise TransportError(f"pvesh get {path} did not return JSON: {stdout[:200]!r}") from exc

    def execute(self, method: Method, path: str, params: Params | None = None) -> Json:
        stdout = self._pvesh(method, path, params)
        if upids := _UPID.findall(stdout):
            upid = upids[-1].strip("\"'")
            wait_for_task(self, upid)
            return upid
        try:
            return json.loads(stdout) if stdout.strip() else None
        except json.JSONDecodeError:
            return None

    def command(self, method: Method, path: str, params: Params | None = None) -> list[str]:
        """The local argv for one call."""
        if not path.startswith("/"):
            raise ValueError(f"API path must start with '/': {path}")
        remote = ["sudo", "-n"] if self._sudo else []
        remote += ["pvesh", method.pvesh, path, "--output-format", "json"]
        for key, value in (params or {}).items():
            remote += [f"--{key}", str(int(value)) if isinstance(value, bool) else str(value)]

        argv = ["ssh", "-o", "BatchMode=yes"]
        if self._control_dir is not None:
            argv += [
                "-o", "ControlMaster=auto",
                "-o", f"ControlPath={self._control_dir}/%C",
                "-o", "ControlPersist=60",
            ]  # fmt: skip
        return [*argv, self._target, shlex.join(remote)]

    def _pvesh(self, method: Method, path: str, params: Params | None) -> str:
        argv = self.command(method, path, params)
        if self._control_dir is not None:
            self._control_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            result = self._runner(argv)
        except OSError as exc:
            raise TransportError(f"Cannot run ssh: {exc.strerror or exc}") from exc

        if result.returncode == 0:
            return result.stdout
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        if result.returncode == SSH_FAILURE:
            raise TransportError(f"SSH to {self._target} failed: {detail}")
        raise TransportError(f"pvesh {method.pvesh} {path} failed on {self._target}: {detail}")
