from .base import AdapterMetadata
from .common import DryRunAdapter


class Udp2RawAdapter(DryRunAdapter):
    ADAPTER_METADATA = AdapterMetadata(
        name="udp2raw",
        layer="experimental",
        transports=("udp",),
        experimental=True,
        supported=False,
        notes="Experimental only; blocked in v0.1",
    )
