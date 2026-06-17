from .base import AdapterMetadata
from .common import DryRunAdapter


class RatholeAdapter(DryRunAdapter):
    ADAPTER_METADATA = AdapterMetadata(name="rathole", layer="layer4", transports=("tcp",), notes="Dry-run template only in v0.1")
