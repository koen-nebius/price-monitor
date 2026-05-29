from dataclasses import dataclass, asdict, field
from typing import Optional
import json


@dataclass
class PriceRecord:
    provider: str           # aws | gcp | azure | coreweave | lambda | crusoe | nebius
    gpu_model: str          # H100 | H200 | B200 | B300 | GB200 | GB300 | L40S
    gpu_count: int
    instance_type: str      # provider-specific SKU
    region: str
    consumption_type: str   # on_demand | reserved_1yr | reserved_3yr | spot | preemptible | committed_1yr | committed_3yr
    price_per_hour_usd: float
    price_per_gpu_hour_usd: float
    vcpu: Optional[int] = None
    ram_gb: Optional[float] = None
    fetched_at: str = ""
    source_url: str = ""
    data_source: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PriceRecord":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class DiffEntry:
    provider: str
    gpu_model: str
    region: str
    consumption_type: str
    instance_type: str
    change_type: str        # price_change | added | removed
    old_price: Optional[float] = None
    new_price: Optional[float] = None
    delta_pct: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)
