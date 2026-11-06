"""Forget the SSH host keys of destroyed guests, so a recreated one is not rejected."""

import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from pve_mgr.errors import PveMgrError

type Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


class KnownHosts:
    def __init__(self, path: Path, runner: Runner = _run) -> None:
        self._path = path
        self._runner = runner

    def forget(self, hosts: Sequence[str], port: int = 22) -> None:
        """Remove every entry for the given names or addresses."""
        if not self._path.is_file():
            return
        targets = list(hosts) if port == 22 else [f"[{host}]:{port}" for host in hosts]
        for target in targets:
            try:
                result = self._runner(["ssh-keygen", "-f", str(self._path), "-R", target])
            except OSError as exc:
                raise PveMgrError(f"Cannot run ssh-keygen: {exc.strerror or exc}") from exc
            if result.returncode != 0:
                raise PveMgrError(
                    f"Could not remove {target} from {self._path}: {result.stderr.strip()}"
                )
