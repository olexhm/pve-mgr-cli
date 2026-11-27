import pytest

from pve_mgr.client import PveClient
from pve_mgr.errors import TransportError
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


def current(transport: FakeTransport, vmid: int, status: str) -> None:
    """What the guest itself says, when /cluster/resources has not caught up yet."""
    transport.replies[f"/nodes/pve2/qemu/{vmid}/status/current"] = {"status": status}


def test_shutdown_acts_on_the_guests_state_not_the_cluster_cache():
    """/cluster/resources lags behind by up to ~10 s and shows new guests as 'unknown'."""
    transport, client, log = setup("unknown", "running")
    current(transport, 110, "running")  # just created
    current(transport, 111, "stopped")  # just shut down

    lifecycle.shutdown(client, [GATEWAY, WEB], report=reporter(log))

    assert transport.executed == [
        Call(Method.POST, "/nodes/pve2/qemu/110/status/shutdown", {"timeout": 180})
    ]
    assert log == ["web1: already stopped", "gw: shutting down"]


def test_start_acts_on_the_guests_state_not_the_cluster_cache():
    transport, client, log = setup("running", "stopped")
    current(transport, 110, "stopped")  # just shut down
    current(transport, 111, "running")  # just started

    lifecycle.start(client, [GATEWAY, WEB], report=reporter(log))

    assert transport.executed == [Call(Method.POST, "/nodes/pve2/qemu/110/status/start", {})]
    assert log == ["gw: starting on pve2", "web1: already running"]


class Clock:
    """Fake time for Settle: sleeping moves the clock, nothing waits."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def settled(transport: FakeTransport, clock: Clock, vmid: int = 110, lock=None):
    """A guest that reports stopped once its shutdown has been sent."""
    transport.replies[f"/nodes/pve2/qemu/{vmid}/status/current"] = lambda call: {
        "status": "stopped"
        if any(c.path.endswith(f"/{vmid}/status/shutdown") for c in transport.executed)
        else "running"
    }
    transport.replies[f"/nodes/pve2/qemu/{vmid}/config"] = {"name": "gw", "tablet": 1}
    if lock is not None:
        transport.replies[f"PUT /nodes/pve2/qemu/{vmid}/config"] = lock
    return lifecycle.Settle(sleep=clock.sleep, clock=lambda: clock.now)


def test_restart_lets_proxmox_finish_with_a_guest_before_starting_it():
    """Starting right after the shutdown races qmeventd's cleanup for the guest's lock."""
    transport, client, log = setup("running", "stopped")
    clock = Clock()

    lifecycle.restart(client, [GATEWAY], report=reporter(log), settle=settled(transport, clock))

    assert transport.executed == [
        Call(Method.POST, "/nodes/pve2/qemu/110/status/shutdown", {"timeout": 180}),
        # Deleting an option the guest does not have: takes the lock, changes nothing.
        Call(Method.PUT, "/nodes/pve2/qemu/110/config", {"delete": "localtime"}),
        Call(Method.POST, "/nodes/pve2/qemu/110/status/start", {}),
    ]
    assert clock.slept == [2]  # the cleanup starts ~0.6 s after the shutdown


def test_settle_waits_while_proxmox_holds_the_lock():
    transport, client, log = setup("running", "stopped")
    clock = Clock()
    attempts: list[Call] = []

    def lock(call: Call):
        attempts.append(call)
        if len(attempts) < 3:
            raise TransportError("can't lock file '/var/lock/qemu-server/lock-110.conf'")

    settle = settled(transport, clock, lock=lock)
    lifecycle.shutdown(client, [GATEWAY], report=reporter(log), settle=settle)

    assert len(attempts) == 3
    assert clock.slept == [2, 1, 1]


def test_settle_gives_up_after_its_timeout():
    transport, client, log = setup("running", "stopped")
    clock = Clock()

    def always_locked(call: Call):
        raise TransportError("can't lock file '/var/lock/qemu-server/lock-110.conf'")

    settle = settled(transport, clock, lock=always_locked)
    with pytest.raises(TransportError, match="gw is still locked by Proxmox 60s after"):
        lifecycle.shutdown(client, [GATEWAY], report=reporter(log), settle=settle)


def test_settle_falls_back_to_the_grace_period_without_permission():
    transport, client, log = setup("running", "stopped")
    clock = Clock()

    def forbidden(call: Call):
        raise TransportError("403 Permission check failed (/vms/110, VM.Config.Options)")

    settle = settled(transport, clock, lock=forbidden)
    lifecycle.shutdown(client, [GATEWAY], report=reporter(log), settle=settle)
    assert clock.slept == [2]


def test_containers_are_not_settled():
    """qmeventd and its cleanup are QEMU's; containers never showed the race."""
    transport = cluster(guest(110, type="lxc", name="gw", status="running"))
    clock = Clock()
    settle = lifecycle.Settle(sleep=clock.sleep, clock=lambda: clock.now)

    lifecycle.shutdown(
        PveClient(transport),
        [device(id=110, name="gw", type="lxc", hardware=None, additional_disk=None)],
        report=lambda *_: None,
        settle=settle,
    )

    assert [call.path for call in transport.executed] == ["/nodes/pve2/lxc/110/status/shutdown"]
    assert clock.slept == []


def test_the_lock_probe_never_deletes_an_option_the_guest_has():
    transport, client, log = setup("running", "stopped")
    clock = Clock()
    settle = settled(transport, clock)
    transport.replies["/nodes/pve2/qemu/110/config"] = {"localtime": 1, "freeze": 0}

    lifecycle.shutdown(client, [GATEWAY], report=reporter(log), settle=settle)

    assert transport.executed[-1] == Call(
        Method.PUT, "/nodes/pve2/qemu/110/config", {"delete": "tablet"}
    )
