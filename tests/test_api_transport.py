import json
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from pve_mgr.errors import ConfigError, TransportError
from pve_mgr.models import Config, Secrets
from pve_mgr.transport import ApiTransport, Method, SshTransport, Transport, build_transport
from tests.test_transport import UPID, Runner

BASE = "https://pve1.example.com:8006"
API = f"{BASE}/api2/json"
SECRET = "00000000-aaaa-bbbb-cccc-000000000000"


@pytest.fixture
def transport() -> ApiTransport:
    return ApiTransport(f"{BASE}/", "automation@pve!pve-mgr", SECRET)


@respx.mock
def test_get_sends_the_token_and_unwraps_data(transport):
    route = respx.get(f"{API}/cluster/resources", params={"type": "vm"}).respond(
        json={"data": [{"vmid": 110}]}
    )
    assert transport.get("/cluster/resources", {"type": "vm"}) == [{"vmid": 110}]
    assert route.calls.last.request.headers["Authorization"] == (
        f"PVEAPIToken=automation@pve!pve-mgr={SECRET}"
    )


@respx.mock
def test_writes_send_form_params_and_wait_for_the_task(transport):
    clone = respx.post(f"{API}/nodes/pve1/qemu/9101/clone").respond(json={"data": UPID})
    status = respx.get(url__regex=r".*/tasks/.*/status$").respond(
        json={"data": {"status": "stopped", "exitstatus": "OK"}}
    )

    result = transport.execute(
        Method.POST, "/nodes/pve1/qemu/9101/clone", {"newid": 110, "full": True}
    )

    assert result == UPID
    assert parse_qs(clone.calls.last.request.content.decode()) == {"newid": ["110"], "full": ["1"]}
    assert status.called


@respx.mock
def test_delete_sends_params_in_the_query(transport):
    route = respx.delete(f"{API}/nodes/pve1/qemu/110", params={"purge": "1"}).respond(
        json={"data": None}
    )
    assert transport.execute(Method.DELETE, "/nodes/pve1/qemu/110", {"purge": True}) is None
    assert route.called


@respx.mock
def test_http_errors_carry_the_reason_and_field_errors_but_not_the_token(transport):
    respx.post(f"{API}/pools").respond(
        400, json={"data": None, "errors": {"poolid": "invalid format"}}
    )
    with pytest.raises(TransportError) as exc:
        transport.execute(Method.POST, "/pools", {"poolid": "bad pool"})
    assert "POST /pools failed: 400" in str(exc.value)
    assert "poolid: invalid format" in str(exc.value)
    assert SECRET not in str(exc.value)


@respx.mock
def test_connection_errors_are_transport_errors(transport):
    respx.get(f"{API}/version").mock(side_effect=httpx.ConnectError("connection refused"))
    with pytest.raises(TransportError, match=f"Cannot reach {BASE}: connection refused"):
        transport.get("/version")


@respx.mock
def test_both_transports_give_the_same_answers(transport):
    """The contract the services rely on: same call in, same result out."""
    guests = [{"vmid": 110, "type": "qemu", "node": "pve1", "status": "running"}]
    stopped = {"status": "stopped", "exitstatus": "OK"}
    respx.get(f"{API}/cluster/resources").respond(json={"data": guests})
    respx.post(f"{API}/nodes/pve1/qemu/110/status/start").respond(json={"data": UPID})
    respx.get(url__regex=r".*/tasks/.*/status$").respond(json={"data": stopped})
    ssh = SshTransport(
        "root@pve1",
        runner=Runner(
            (0, json.dumps(guests), ""), (0, f"{UPID}\n", ""), (0, json.dumps(stopped), "")
        ),
    )

    backends: list[Transport] = [transport, ssh]
    results = [
        (
            backend.get("/cluster/resources", {"type": "vm"}),
            backend.execute(Method.POST, "/nodes/pve1/qemu/110/status/start"),
        )
        for backend in backends
    ]
    assert results[0] == results[1] == (guests, UPID)


def api_config() -> Config:
    return Config.model_validate({"transport": "api", "nodes": {"pve1": {"api": BASE}}})


def test_factory_builds_the_api_transport_from_secrets():
    secrets = Secrets(
        api_token_id="automation@pve!pve-mgr", api_token_secret=SECRET, _env_file=None
    )
    assert isinstance(build_transport(api_config(), secrets), ApiTransport)


def test_factory_needs_the_token(monkeypatch):
    monkeypatch.delenv("PVE_MGR_API_TOKEN_ID", raising=False)
    monkeypatch.delenv("PVE_MGR_API_TOKEN_SECRET", raising=False)
    with pytest.raises(ConfigError, match="PVE_MGR_API_TOKEN_ID"):
        build_transport(api_config(), Secrets(_env_file=None))
