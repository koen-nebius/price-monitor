"""Completeness metrics that count only fields applicable to each record."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Callable

from .models import MarketEvent


MISSING_SENTINELS = {None, "", "unknown", "not_checked", "Unspecified"}


@dataclass(frozen=True)
class CoverageMetric:
    event_type: str
    field: str
    applicable_count: int
    populated_count: int
    missing_count: int
    coverage_pct: float | None

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


Rule = tuple[str, Callable[[MarketEvent], bool]]


def _present(value: object) -> bool:
    try:
        return value not in MISSING_SENTINELS
    except TypeError:
        return value is not None


RULES: dict[str, list[Rule]] = {
    "competitor_offer": [
        ("price_usd_per_gpu_hour", lambda event: True),
        ("previous_price_usd_per_gpu_hour", lambda event: bool(event.is_price_change)),
        ("term_months", lambda event: event.consumption_type == "Reserve"),
        ("availability_status", lambda event: True),
        ("availability_checked_at", lambda event: event.availability_status != "not_checked"),
        ("bundle_status", lambda event: True),
        ("local_nvme_included", lambda event: event.bundle_status in {"partial", "normalized"}),
        ("interconnect", lambda event: event.bundle_status in {"partial", "normalized"}),
        ("gpu_count_per_instance", lambda event: event.bundle_status in {"partial", "normalized"}),
        ("vcpu", lambda event: event.bundle_status in {"partial", "normalized"}),
        ("ram_gb", lambda event: event.bundle_status in {"partial", "normalized"}),
        ("sla_included", lambda event: event.bundle_status in {"partial", "normalized"}),
    ],
    "capacity_announcement": [
        ("announced_gpus", lambda event: event.quantity_basis in {"gpu_count", "both"}),
        ("announced_mw", lambda event: event.quantity_basis in {"mw", "both"}),
        (
            "expected_availability_date",
            lambda event: event.status
            not in {"ga", "online", "partly_online", "contracted_infrastructure"},
        ),
        ("contracted_status", lambda event: True),
        ("bookability_status", lambda event: True),
        ("availability_checked_at", lambda event: event.bookability_status != "not_checked"),
    ],
    "source_health": [
        ("documents_discovered", lambda event: event.operation in {"search", "crawl", "weekly"}),
        ("documents_extracted", lambda event: True),
        ("error_code", lambda event: event.status in {"partial", "error"}),
    ],
}


def coverage_report(events: list[MarketEvent]) -> list[CoverageMetric]:
    output = []
    for event_type, rules in RULES.items():
        typed = [event for event in events if event.event_type == event_type]
        for field, applies in rules:
            applicable = [event for event in typed if applies(event)]
            populated = sum(_present(getattr(event, field)) for event in applicable)
            count = len(applicable)
            output.append(
                CoverageMetric(
                    event_type=event_type,
                    field=field,
                    applicable_count=count,
                    populated_count=populated,
                    missing_count=count - populated,
                    coverage_pct=round(populated / count * 100, 1) if count else None,
                )
            )
    return output
