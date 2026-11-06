from ipaddress import IPv4Address, IPv4Network

import pytest

from pve_mgr.client import PveClient
from pve_mgr.errors import TransportError
from pve_mgr.models import Guest
from pve_mgr.services.addresses import pick_address
from tests.fakes import FakeTransport


def guest(kind: str = "qemu", status: str = "running") -> Guest:
    return Guest.model_validate({"vmid": 110, "type": kind, "node": "pve1", "status": status})


def test_guests_are_indexed_by_vmid_and_unknown_fields_ignored():
    transport = FakeTransport(
        {
            "/cluster/resources": [
                {"vmid": 110, "type": "qemu", "node": "pve1", "status": "running", "maxcpu": 2},
                {"vmid": 9101, "type": "qemu", "node": "pve1", "status": "stopped", "template": 1},
            ]
        }
    )
    guests = PveClient(transport).guests()

    assert guests[110].running
    assert guests[9101].template
    assert transport.calls[0].params == {"type": "vm"}


def test_unexpected_guest_list_is_a_transport_error():
    client = PveClient(FakeTransport({"/cluster/resources": [{"vmid": "x"}]}))
    with pytest.raises(TransportError, match="Unexpected reply from /cluster/resources"):
        client.guests()


def test_qemu_addresses_come_from_the_guest_agent():
    reply = {
        "result": [
            {"name": "lo", "ip-addresses": [{"ip-address": "127.0.0.1"}]},
            {
                "name": "eth0",
                "ip-addresses": [
                    {"ip-address": "fe80::1", "ip-address-type": "ipv6"},
                    {"ip-address": "169.254.1.1"},
                    {"ip-address": "192.0.2.20", "ip-address-type": "ipv4"},
                ],
            },
        ]
    }
    client = PveClient(FakeTransport({"/nodes/pve1/qemu/110/agent/network-get-interfaces": reply}))
    assert client.addresses(guest()) == [IPv4Address("192.0.2.20")]


def test_lxc_addresses_come_from_the_interfaces_endpoint():
    reply = [{"name": "lo", "inet": "127.0.0.1/8"}, {"name": "eth0", "inet": "192.0.2.30/24"}]
    client = PveClient(FakeTransport({"/nodes/pve1/lxc/110/interfaces": reply}))
    assert client.addresses(guest("lxc")) == [IPv4Address("192.0.2.30")]


def test_no_addresses_when_stopped_or_agent_missing():
    transport = FakeTransport()
    client = PveClient(transport)
    assert client.addresses(guest(status="stopped")) == []
    assert transport.calls == []
    assert client.addresses(guest()) == []


@pytest.mark.parametrize(
    "reply",
    [{"cidr": "192.0.2.1/24"}, {"address": "192.0.2.1", "netmask": "255.255.255.0"}],
)
def test_bridge_network_is_read_once(reply):
    transport = FakeTransport({"/nodes/pve1/network/vmbr0": reply})
    client = PveClient(transport)
    assert client.bridge_network("pve1", "vmbr0") == IPv4Network("192.0.2.0/24")
    client.bridge_network("pve1", "vmbr0")
    assert len(transport.calls) == 1


def test_bridge_without_an_address_has_no_network():
    client = PveClient(FakeTransport({"/nodes/pve1/network/vmbr10": {"type": "bridge"}}))
    assert client.bridge_network("pve1", "vmbr10") is None


def test_pick_address_prefers_the_bridge_network():
    addresses = [IPv4Address("198.51.100.5"), IPv4Address("192.0.2.20")]
    assert pick_address(addresses, on=IPv4Network("192.0.2.0/24")) == addresses[1]
    assert pick_address(addresses, on=None) == addresses[0]
    assert pick_address([], on=None) is None
