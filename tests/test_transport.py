import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from pve_mgr.errors import TaskError, TransportError
from pve_mgr.models import Config
from pve_mgr.transport import (
    Call,
    DryRunTransport,
    Method,
    SshTransport,
    Transport,
    build_transport,
    wait_for_task,
)
from tests.fakes import FakeTransport

UPID = "UPID:pve1:0001:0002:0003:qmclone:110:root@pam:"
TASK = f"/nodes/pve1/tasks/{UPID}/status"


class Runner:
    """Stand-in for subprocess.run that replays queued results."""

    def __init__(self, *results: tuple[int, str, str]) -> None:
        self.results = list(results)
        self.argvs: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.argvs.append(list(argv))
        code, stdout, stderr = self.results.pop(0)
        return subprocess.CompletedProcess(argv, code, stdout, stderr)


def test_dry_run_reads_but_does_not_write():
    inner = FakeTransport({"/cluster/resources": []})
    transport: Transport = DryRunTransport(inner)

    assert transport.get("/cluster/resources") == []
    assert transport.execute(Method.POST, "/nodes/pve1/qemu/9101/clone", {"newid": 110}) is None

    assert inner.executed == []
    assert transport.planned == [Call(Method.POST, "/nodes/pve1/qemu/9101/clone", {"newid": 110})]


def test_methods_map_to_pvesh_verbs():
    assert [m.pvesh for m in Method] == ["get", "create", "set", "delete"]


def test_ssh_get_runs_pvesh_and_parses_json():
    runner = Runner((0, '[{"vmid": 110}]', ""))
    transport = SshTransport("root@pve1", runner=runner)

    assert transport.get("/cluster/resources", {"type": "vm"}) == [{"vmid": 110}]
    assert runner.argvs == [
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "root@pve1",
            "pvesh get /cluster/resources --output-format json --type vm",
        ]
    ]


def test_ssh_quotes_params_and_uses_sudo_and_connection_sharing(tmp_path: Path):
    transport = SshTransport("admin@pve1", sudo=True, control_dir=tmp_path / "ctl")
    argv = transport.command(
        Method.PUT, "/nodes/pve1/qemu/110/config", {"name": "a b", "kvm": True}
    )

    assert argv[-1] == (
        "sudo -n pvesh set /nodes/pve1/qemu/110/config --output-format json --name 'a b' --kvm 1"
    )
    assert f"ControlPath={tmp_path / 'ctl'}/%C" in argv
    assert "ControlMaster=auto" in argv


def test_ssh_creates_a_private_control_dir(tmp_path: Path):
    control = tmp_path / "ctl"
    SshTransport("root@pve1", control_dir=control, runner=Runner((0, "null", ""))).get("/version")
    assert control.stat().st_mode & 0o777 == 0o700


def test_ssh_execute_waits_for_the_task():
    runner = Runner(
        (0, f'"{UPID}"\n', ""),
        (0, '{"status": "stopped", "exitstatus": "OK"}', ""),
    )
    transport = SshTransport("root@pve1", runner=runner)

    assert transport.execute(Method.POST, "/nodes/pve1/qemu/9101/clone", {"newid": 110}) == UPID
    assert f"pvesh get {TASK}" in runner.argvs[1][-1].replace("'", "")


def test_ssh_execute_without_a_task_returns_the_reply():
    transport = SshTransport("root@pve1", runner=Runner((0, "", "")))
    assert transport.execute(Method.POST, "/pools", {"poolid": "lab"}) is None


@pytest.mark.parametrize(
    ("result", "message"),
    [
        ((255, "", "Permission denied (publickey)."), "SSH to root@pve1 failed: Permission denied"),
        ((2, "", "no such VM ('110')"), "pvesh get /x failed on root@pve1: no such VM"),
        ((0, "not json", ""), "did not return JSON"),
    ],
)
def test_ssh_errors(result, message):
    with pytest.raises(TransportError, match=message.replace("(", r"\(")):
        SshTransport("root@pve1", runner=Runner(result)).get("/x")


def test_ssh_missing_binary_is_a_transport_error():
    def runner(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(2, "No such file or directory")

    with pytest.raises(TransportError, match="Cannot run ssh"):
        SshTransport("root@pve1", runner=runner).get("/x")


def test_wait_for_task_polls_until_stopped():
    replies = iter([{"status": "running"}, {"status": "stopped", "exitstatus": "WARNINGS: 1"}])
    transport = FakeTransport({TASK: lambda call: next(replies)})
    naps: list[float] = []

    wait_for_task(transport, UPID, sleep=naps.append)

    assert naps == [1]
    assert [call.path for call in transport.calls] == [TASK, TASK]


def test_wait_for_task_reports_failure():
    transport = FakeTransport({TASK: {"status": "stopped", "exitstatus": "clone failed"}})
    with pytest.raises(TaskError, match="qmclone on pve1 failed: clone failed"):
        wait_for_task(transport, UPID)


def test_wait_for_task_times_out():
    transport = FakeTransport({TASK: {"status": "running"}})
    ticks = iter(range(100))
    with pytest.raises(TaskError, match="did not finish within 5s"):
        wait_for_task(
            transport, UPID, timeout=5, sleep=lambda _: None, clock=lambda: float(next(ticks))
        )


def test_wait_for_task_rejects_a_malformed_id():
    with pytest.raises(TaskError, match="Malformed"):
        wait_for_task(FakeTransport(), "UPID:nope")


def test_factory_builds_the_ssh_transport_for_the_entry_node():
    config = Config.model_validate(
        {"nodes": {"pve1": "root@pve1", "pve2": "admin@pve2"}, "defaults": {"host_node": "pve2"}}
    )
    transport = build_transport(config)
    assert isinstance(transport, SshTransport)
    assert transport.command(Method.GET, "/version")[-2:] == [
        "admin@pve2",
        "sudo -n pvesh get /version --output-format json",
    ]
