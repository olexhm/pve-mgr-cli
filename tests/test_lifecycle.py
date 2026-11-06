from pve_mgr.client import PveClient
from pve_mgr.services import lifecycle
from pve_mgr.services.readiness import Waiter
from pve_mgr.transport import Call, Method
from tests.fakes import FakeTransport
from tests.test_apply import cluster
from tests.test_plan import device, guest

GATEWAY = device(id=110, name="gw")
WEB = device(id=111, name="web1")
ABSENT = device(id=112, name="web2")


def setup(gw_status: str, web_status: str) -> tuple[FakeTransport, PveClient, list[str]]:
    transport = cluster(
        guest(110, name="gw", status=gw_status), guest(111, name="web1", status=web_status)
    )
    return transport, PveClient(transport), []


def reporter(log: list[str]):
    return lambda subject, message: log.append(f"{subject}: {message}")


def test_start_skips_running_and_missing_guests():
    transport, client, log = setup("running", "stopped")

    lifecycle.start(client, [GATEWAY, WEB, ABSENT], report=reporter(log))

    assert transport.executed == [Call(Method.POST, "/nodes/pve2/qemu/111/status/start", {})]
    assert log == ["gw: already running", "web1: starting on pve2", "web2: does not exist, skipped"]


def test_start_waits_for_each_guest():
    transport, client, log = setup("stopped", "running")
    transport.replies["/nodes/pve2/qemu/110/agent/network-get-interfaces"] = {
        "result": [{"ip-addresses": [{"ip-address": "192.0.2.1"}]}]
    }
    transport.replies["/nodes/pve2/network/vmbr0"] = {"cidr": "192.0.2.1/24"}

    lifecycle.start(client, [GATEWAY], report=reporter(log), waiter=Waiter())

    assert log[-1] == "gw: ready at 192.0.2.1"


def test_shutdown_runs_in_reverse_order_and_skips_stopped_guests():
    transport, client, log = setup("running", "running")

    lifecycle.shutdown(client, [GATEWAY, WEB], report=reporter(log), timeout=60, force=True)

    assert transport.executed == [
        Call(
            Method.POST,
            f"/nodes/pve2/qemu/{vmid}/status/shutdown",
            {"timeout": 60, "forceStop": True},
        )
        for vmid in (111, 110)
    ]

    transport, client, log = setup("stopped", "paused")
    lifecycle.shutdown(client, [GATEWAY, WEB], report=reporter(log))
    assert transport.executed == []
    assert log == ["web1: already paused", "gw: already stopped"]


def test_restart_stops_in_reverse_then_starts_in_order():
    transport, client, log = setup("running", "stopped")

    lifecycle.restart(client, [GATEWAY, WEB, ABSENT], report=reporter(log))

    assert [call.path.removeprefix("/nodes/pve2/qemu/") for call in transport.executed] == [
        "110/status/shutdown",
        "110/status/start",
        "111/status/start",
    ]
