"""Transport that calls the Proxmox REST API over HTTPS with an API token."""

import httpx

from pve_mgr.errors import TransportError
from pve_mgr.transport.base import Json, Method, Params
from pve_mgr.transport.tasks import is_upid, wait_for_task


class ApiTransport:
    def __init__(
        self,
        base_url: str,
        token_id: str,
        token_secret: str,
        *,
        verify_tls: bool = True,
        timeout: float = 30,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=f"{self._base_url}/api2/json",
            headers={"Authorization": f"PVEAPIToken={token_id}={token_secret}"},
            verify=verify_tls,
            timeout=timeout,
        )

    def get(self, path: str, params: Params | None = None) -> Json:
        return self._request(Method.GET, path, params)

    def execute(self, method: Method, path: str, params: Params | None = None) -> Json:
        result = self._request(method, path, params)
        if isinstance(result, str) and is_upid(result):
            wait_for_task(self, result)
        return result

    def _request(self, method: Method, path: str, params: Params | None) -> Json:
        if not path.startswith("/"):
            raise ValueError(f"API path must start with '/': {path}")
        values = {
            key: str(int(value)) if isinstance(value, bool) else str(value)
            for key, value in (params or {}).items()
        }
        in_body = method in (Method.POST, Method.PUT)
        try:
            response = self._client.request(
                method.value,
                path,
                params=None if in_body else values,
                data=values if in_body else None,
            )
        except httpx.HTTPError as exc:
            raise TransportError(f"Cannot reach {self._base_url}: {exc}") from exc

        if response.is_error:
            raise TransportError(f"{method} {path} failed: {_describe(response)}")
        try:
            body = response.json()
        except ValueError as exc:
            raise TransportError(f"{method} {path} did not return JSON") from exc
        return body.get("data") if isinstance(body, dict) else None


def _describe(response: httpx.Response) -> str:
    """Proxmox puts the reason in the status line and field errors in the body."""
    detail = f"{response.status_code} {response.reason_phrase}".strip()
    try:
        body = response.json()
    except ValueError:
        return detail
    if not isinstance(body, dict):
        return detail
    extras = [str(body["message"]).strip()] if body.get("message") else []
    if isinstance(body.get("errors"), dict):
        extras += [f"{field}: {str(reason).strip()}" for field, reason in body["errors"].items()]
    return "; ".join([detail, *extras])
