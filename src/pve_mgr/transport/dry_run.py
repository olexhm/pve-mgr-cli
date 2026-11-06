from pve_mgr.transport.base import Call, Json, Method, Params, Transport


class DryRunTransport:
    """Wrap a transport so reads go through and writes are only recorded."""

    def __init__(self, inner: Transport) -> None:
        self._inner = inner
        self.planned: list[Call] = []

    def get(self, path: str, params: Params | None = None) -> Json:
        return self._inner.get(path, params)

    def execute(self, method: Method, path: str, params: Params | None = None) -> Json:
        self.planned.append(Call(method, path, dict(params or {})))
        return None
