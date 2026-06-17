from .base import AdapterMetadata
from .common import DryRunAdapter


class ChiselAdapter(DryRunAdapter):
    ADAPTER_METADATA = AdapterMetadata(name="chisel", layer="layer4", transports=("tcp",), notes="Dry-run template only in v0.1")
