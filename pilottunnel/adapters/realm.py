from .base import AdapterMetadata
from .common import DryRunAdapter


class RealmAdapter(DryRunAdapter):
    ADAPTER_METADATA = AdapterMetadata(name="realm", layer="layer4", transports=("tcp",), notes="Dry-run template only in v0.1")
