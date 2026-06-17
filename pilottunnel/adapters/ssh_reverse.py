from .base import AdapterMetadata
from .common import DryRunAdapter


class SshReverseAdapter(DryRunAdapter):
    ADAPTER_METADATA = AdapterMetadata(name="ssh_reverse", layer="layer4", transports=("tcp",), notes="Dry-run template only in v0.1")
