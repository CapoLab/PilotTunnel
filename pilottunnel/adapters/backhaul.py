from .base import AdapterMetadata
from .common import DryRunAdapter


class BackhaulAdapter(DryRunAdapter):
    ADAPTER_METADATA = AdapterMetadata(name="backhaul", layer="layer4", transports=("tcp", "udp"), notes="Dry-run template only in v0.1")
