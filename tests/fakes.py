from typing import Any

from pve_mgr.errors import TransportError
from pve_mgr.transport import Call, Json, Method, Params

type Reply = Any


class FakeTransport:
    """In-memory transport: canned replies per (method, path), every call recorded."""

    def __init__(self, replies: dict[str, Reply] | None = None) -> None:
        self.replies: dict[str, Reply] = dict(replies or {})
        self.calls: list[Call] = []

    @property
    def executed(self) -> list[Call]:
        return [call for call in self.calls if call.method is not Method.GET]

    def get(self, path: str, params: Params | None = None) -> Json:
        return self._reply(Call(Method.GET, path, dict(params or {})))

    def execute(self, method: Method, path: str, params: Params | None = None) -> Json:
        return self._reply(Call(method, path, dict(params or {})))

    def _reply(self, call: Call) -> Json:
        self.calls.append(call)
        for key in (f"{call.method} {call.path}", call.path):
            if key in self.replies:
                reply = self.replies[key]
                return reply(call) if callable(reply) else reply
        if call.method is Method.GET:
            raise TransportError(f"no fake reply for GET {call.path}")
        return None
