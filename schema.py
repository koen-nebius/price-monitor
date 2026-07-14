from dataclasses import dataclass, asdict, field
from typing import Optional
import json

# Bump when a fetcher's parsing logic changes in a way that could affect prices.
# Stamped onto records so a snapshot can be traced to the code that produced it.
PARSER_VERSION = "2.1"   # 2026-07-14: AWS RI upfront amortization; AWS boto3 spot
                          # latest-per-AZ (was min-over-history); Lambda specs.gpus count

# data_source (how the value was obtained) → source_type (provenance tier used for
# the source-priority rule). data_source is the raw fetcher label; source_type is the
# normalized trust tier surfaced in executive tables.
_SOURCE_TYPE_MAP = {
    "official_api":  "api",
    "api":           "api",
    "web_scrape":    "provider_page",
    "provider_page": "provider_page",
    "aggregator":    "aggregator",
    "manual":        "manual",
    "field_quote":   "field_quote",
}

# Default confidence by provenance tier (a cross-check job may downgrade individual
# cells; fetchers may override when they know better).
_DEFAULT_CONFIDENCE = {
    "api":           "high",
    "provider_page": "high",
    "aggregator":    "med",
    "manual":        "low",
    "field_quote":   "low",
}


@dataclass
class PriceRecord:
    provider: str           # aws | gcp | azure | coreweave | lambda | crusoe | nebius
    gpu_model: str          # H100 | H200 | B200 | B300 | GB200 | GB300 | L40S
    gpu_count: int          # GPUs in THIS priced SKU
    instance_type: str      # provider-specific SKU
    region: str
    consumption_type: str   # on_demand | reserved_1yr | reserved_3yr | spot | preemptible | committed_1yr | committed_3yr
    price_per_hour_usd: float
    price_per_gpu_hour_usd: float
    vcpu: Optional[int] = None
    ram_gb: Optional[float] = None
    fetched_at: str = ""
    source_url: str = ""
    data_source: str = ""           # raw fetcher label: official_api | web_scrape | aggregator | manual | field_quote

    # ── Provenance / trust layer (Phase 1.1) ────────────────────────────────
    source_type: str = ""           # api | provider_page | aggregator | manual | field_quote (derived from data_source if unset)
    confidence: str = ""            # high | med | low (derived from source_type if unset; cross-check may downgrade)
    last_changed_at: str = ""       # ISO date this (provider,gpu,ct,region) price last changed (set by diff/history)
    parser_version: str = ""        # PARSER_VERSION that produced this record

    # ── Comparability layer (Phase 1.1 / 2.6) ───────────────────────────────
    interconnect: str = ""          # IB | RoCE | Ethernet | NVLink | unknown
    form_factor: str = ""           # SXM | PCIe | NVL | unknown
    node_gpus: Optional[int] = None # GPUs in the full physical node (defaults to gpu_count)

    def __post_init__(self):
        # Derive provenance fields from data_source so existing fetchers and old
        # snapshots get source_type/confidence for free (idempotent — only fills blanks).
        if not self.source_type and self.data_source:
            self.source_type = _SOURCE_TYPE_MAP.get(self.data_source, "")
        if not self.confidence and self.source_type:
            self.confidence = _DEFAULT_CONFIDENCE.get(self.source_type, "med")
        if not self.parser_version:
            self.parser_version = PARSER_VERSION
        if self.node_gpus is None:
            self.node_gpus = self.gpu_count

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
