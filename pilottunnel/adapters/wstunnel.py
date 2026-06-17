from .base import AdapterMetadata
from .common import DryRunAdapter


class WSTunnelAdapter(DryRunAdapter):
    ADAPTER_METADATA = AdapterMetadata(name="wstunnel", layer="layer7", transports=("ws", "wss"), supported=False, notes="Listed only; layer7 is blocked in v0.1")
