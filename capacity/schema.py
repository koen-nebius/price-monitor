"""
Availability record schema for the GPU capacity monitor.

A record answers: "for provider P, GPU model G, region R, consumption type C —
can a customer get capacity right now, and how much / how fast?"

Availability signals are heterogeneous across providers (binary flags, stock
counts, marketplace offer depth, reservation lead times), so each record
carries BOTH a normalized state (comparable across providers) and the raw
quantitative metric it was derived from (auditable, provider-specific).
"""
from dataclasses import dataclass, asdict
from typing import Optional

# Bump when a fetcher's availability-derivation logic changes.
PARSER_VERSION = "1.0"

# Normalized availability states, ordered from most to least available.
# not_offered = the provider does not sell this GPU/region at all (structural,
# not a capacity signal); unknown = tracked but today's signal was unreadable.
STATES = ["available", "limited", "sold_out", "not_offered", "unknown"]

# metric_type values and their semantics:
#   regions_with_capacity — count of regions listing live capacity (Lambda)
#   stock_level           — provider-reported stock label or count (Hyperstack)
#   offer_depth_gpus      — marketplace GPUs listed right now (Vast)
#   stock_status_label    — provider enum e.g. High/Low/None (RunPod, Scaleway)
#   lead_time_days        — days until earliest reservable block (AWS CB)
#   clearing_price_usd    — market-clearing $/GPU-hr on an exchange (SFCompute)
#   binary                — available true/false (Verda, DataCrunch-style)
#   listed_offering       — GPU is listed for sale, no live stock signal (docs)


@dataclass
class AvailabilityRecord:
    provider: str            # same provider keys as the price monitor
    gpu_model: str           # H100 | H200 | B200 | B300 | GB200 | GB300 | L40S | RTX6000 | VR
    region: str              # provider-native region label ("global" if none)
    consumption_type: str    # on_demand | spot | reserved_short | committed
    state: str               # available | limited | sold_out | not_offered | unknown
    metric_type: str         # see semantics table above
    metric_value: Optional[float] = None   # numeric metric when one exists
    detail: str = ""         # human-readable evidence, e.g. "3 of 8 regions have stock"
    instance_type: str = ""  # provider SKU the signal came from
    fetched_at: str = ""
    source_url: str = ""
    data_source: str = ""    # official_api | web_scrape | aggregator | manual
    parser_version: str = ""

    def __post_init__(self):
        if self.state not in STATES:
            raise ValueError(f"invalid state {self.state!r}")
        if not self.parser_version:
            self.parser_version = PARSER_VERSION

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "AvailabilityRecord":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class CapacityDiffEntry:
    provider: str
    gpu_model: str
    region: str
    consumption_type: str
    change_type: str         # state_change | metric_move | added | removed
    old_state: Optional[str] = None
    new_state: Optional[str] = None
    old_value: Optional[float] = None
    new_value: Optional[float] = None
    detail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)
