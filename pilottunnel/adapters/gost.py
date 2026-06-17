from .base import AdapterMetadata
from .common import DryRunAdapter


class GostAdapter(DryRunAdapter):
    ADAPTER_METADATA = AdapterMetadata(name="gost", layer="layer4", transports=("tcp", "udp"), notes="Dry-run template only in v0.1")
