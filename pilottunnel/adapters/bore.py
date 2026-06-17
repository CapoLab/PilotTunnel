from .base import AdapterMetadata
from .common import DryRunAdapter


class BoreAdapter(DryRunAdapter):
    ADAPTER_METADATA = AdapterMetadata(name="bore", layer="layer4", transports=("tcp",), notes="Dry-run template only in v0.1")
