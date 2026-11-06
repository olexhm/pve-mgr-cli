from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

type Json = dict[str, Json] | list[Json] | str | int | float | bool | None
type Params = Mapping[str, str | int | bool]


class Method(StrEnum):
    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    DELETE = "DELETE"

    @property
    def pvesh(self) -> str:
        """The `pvesh` subcommand that performs this HTTP method."""
        return {"GET": "get", "POST": "create", "PUT": "set", "DELETE": "delete"}[self.value]


@dataclass(frozen=True)
class Call:
    method: Method
    path: str
    params: Params = field(default_factory=dict)


class Transport(Protocol):
    """One way of reaching the Proxmox API tree.

    Paths are API paths without the `/api2/json` prefix, e.g.
    `/nodes/pve1/qemu/100/status/current`. Both methods return the `data`
    payload of the response and raise `TransportError` on failure.
    """

    def get(self, path: str, params: Params | None = None) -> Json:
        """Read state. Never changes anything, so it also runs during a dry run."""
        ...

    def execute(self, method: Method, path: str, params: Params | None = None) -> Json:
        """Change state. Blocks until any task the call started has finished."""
        ...
