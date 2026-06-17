from .base import AdapterMetadata
from .common import DryRunAdapter


class FrpAdapter(DryRunAdapter):
    ADAPTER_METADATA = AdapterMetadata(name="frp", layer="layer4", transports=("tcp", "udp"), notes="Dry-run template only in v0.1")
