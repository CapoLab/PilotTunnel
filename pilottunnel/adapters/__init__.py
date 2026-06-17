"""Adapter registry."""

from .backhaul import BackhaulAdapter
from .bore import BoreAdapter
from .chisel import ChiselAdapter
from .frp import FrpAdapter
from .gost import GostAdapter
from .rathole import RatholeAdapter
from .realm import RealmAdapter
from .ssh_reverse import SshReverseAdapter
from .udp2raw import Udp2RawAdapter
from .wstunnel import WSTunnelAdapter

ADAPTERS = {
    "backhaul": BackhaulAdapter,
    "rathole": RatholeAdapter,
    "frp": FrpAdapter,
    "gost": GostAdapter,
    "chisel": ChiselAdapter,
    "realm": RealmAdapter,
    "wstunnel": WSTunnelAdapter,
    "bore": BoreAdapter,
    "ssh_reverse": SshReverseAdapter,
    "udp2raw": Udp2RawAdapter,
}

__all__ = ["ADAPTERS"]
