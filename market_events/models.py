"""Typed, validated market-event records.

The main price monitor already owns public prices and internal field evidence.
These records cover the missing public context: offer bookability, bundle
comparability, capacity announcements, and collection health.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar
from urllib.parse import urlsplit


CONFIDENCE_VALUES = {"low", "medium", "high"}
REVIEW_VALUES = {"candidate", "verified", "rejected"}
AVAILABILITY_VALUES = {
    "not_checked",
    "listed_only",
    "bookable_verified",
    "unavailable",
    "not_publicly_verifiable",
    "stale",
}
BUNDLE_VALUES = {"not_checked", "partial", "normalized", "not_comparable"}
LANE_VALUES = {"PAYG", "PVM", "Reserve", "All", "Unknown"}
QUANTITY_BASIS_VALUES = {"gpu_count", "mw", "both", "undisclosed"}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def stable_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _required(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} is required")


def _iso(value: str, field_name: str, *, date_only: bool = False) -> None:
    _required(value, field_name)
    candidate = f"{value}T00:00:00+00:00" if date_only else value
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be ISO-8601: {value}") from exc


def _https_url(value: str, field_name: str = "source_url") -> None:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError(f"{field_name} must be an absolute HTTPS URL")


def _non_negative(value: float | int | None, field_name: str) -> None:
    if value is not None and value < 0:
        raise ValueError(f"{field_name} must be non-negative")


@dataclass(frozen=True, kw_only=True)
class BaseEvent:
    event_type: ClassVar[str] = "base"
    source_id: str
    source_url: str
    provider: str
    observed_at: str
    event_date: str
    evidence_text: str
    source_title: str = ""
    region_scope: str = "Global"
    gpu_model: str = "Unspecified"
    confidence: str = "low"
    review_status: str = "candidate"
    extraction_method: str = "manual"
    raw_document_hash: str = ""

    def validate(self) -> None:
        _required(self.source_id, "source_id")
        _https_url(self.source_url)
        _required(self.provider, "provider")
        _iso(self.observed_at, "observed_at")
        _iso(self.event_date, "event_date", date_only=True)
        _required(self.evidence_text, "evidence_text")
        if self.confidence not in CONFIDENCE_VALUES:
            raise ValueError(f"confidence must be one of {sorted(CONFIDENCE_VALUES)}")
        if self.review_status not in REVIEW_VALUES:
            raise ValueError(f"review_status must be one of {sorted(REVIEW_VALUES)}")

    def natural_key_fields(self) -> dict[str, Any]:
        raise NotImplementedError

    def semantic_key(self) -> str:
        return f"{self.event_type}:{stable_hash(self.natural_key_fields())[:24]}"

    def business_payload(self) -> dict[str, Any]:
        payload = dataclasses.asdict(self)
        # Re-fetching the same fact should not create a new economic version.
        payload.pop("observed_at", None)
        payload.pop("raw_document_hash", None)
        payload.pop("availability_checked_at", None)
        if self.review_status == "candidate" and self.extraction_method.startswith(
            "public_page_regex_candidate"
        ):
            # The collector's observation date is not the event's verified
            # effective date. Keep it for review, but not for candidate diffing.
            payload.pop("event_date", None)
        return payload

    def content_hash(self) -> str:
        return stable_hash(self.business_payload())

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "event_type": self.event_type,
            **dataclasses.asdict(self),
            "semantic_key": self.semantic_key(),
            "content_hash": self.content_hash(),
        }


@dataclass(frozen=True, kw_only=True)
class CompetitorOfferEvent(BaseEvent):
    event_type: ClassVar[str] = "competitor_offer"
    product_name: str
    consumption_type: str
    price_usd_per_gpu_hour: float
    previous_price_usd_per_gpu_hour: float | None = None
    is_price_change: bool = False
    effective_date: str | None = None
    term_months: int | None = None
    configuration: str = "Unspecified"
    currency: str = "USD"
    price_unit: str = "gpu_hour"
    availability_status: str = "not_checked"
    availability_checked_at: str | None = None
    availability_evidence: str = ""
    bundle_status: str = "not_checked"
    local_nvme_included: bool | None = None
    interconnect: str = ""
    gpu_count_per_instance: int | None = None
    vcpu: int | None = None
    ram_gb: float | None = None
    sla_included: bool | None = None

    def validate(self) -> None:
        super().validate()
        _required(self.product_name, "product_name")
        if self.consumption_type not in LANE_VALUES:
            raise ValueError(f"consumption_type must be one of {sorted(LANE_VALUES)}")
        if self.price_usd_per_gpu_hour <= 0:
            raise ValueError("price_usd_per_gpu_hour must be positive")
        for name in (
            "previous_price_usd_per_gpu_hour",
            "term_months",
            "gpu_count_per_instance",
            "vcpu",
            "ram_gb",
        ):
            _non_negative(getattr(self, name), name)
        if self.currency != "USD" or self.price_unit != "gpu_hour":
            raise ValueError("offers must be normalized to USD per GPU-hour")
        if self.is_price_change and self.previous_price_usd_per_gpu_hour is None:
            raise ValueError("price changes require previous_price_usd_per_gpu_hour")
        if self.availability_status not in AVAILABILITY_VALUES:
            raise ValueError(
                f"availability_status must be one of {sorted(AVAILABILITY_VALUES)}"
            )
        if self.bundle_status not in BUNDLE_VALUES:
            raise ValueError(f"bundle_status must be one of {sorted(BUNDLE_VALUES)}")
        if self.effective_date:
            _iso(self.effective_date, "effective_date", date_only=True)
        if self.availability_checked_at:
            _iso(self.availability_checked_at, "availability_checked_at")

    def natural_key_fields(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "provider": self.provider,
            "region_scope": self.region_scope,
            "gpu_model": self.gpu_model,
            "product_name": self.product_name,
            "configuration": self.configuration,
            "consumption_type": self.consumption_type,
            "term_months": self.term_months,
        }


@dataclass(frozen=True, kw_only=True)
class CapacityAnnouncementEvent(BaseEvent):
    event_type: ClassVar[str] = "capacity_announcement"
    event_reference: str
    status: str
    quantity_basis: str
    announced_gpus: float | None = None
    announced_mw: float | None = None
    expected_availability_date: str | None = None
    location: str = ""
    contracted_status: str = "unknown"
    bookability_status: str = "not_publicly_verifiable"
    availability_checked_at: str | None = None

    def validate(self) -> None:
        super().validate()
        _required(self.event_reference, "event_reference")
        _required(self.status, "status")
        if self.quantity_basis not in QUANTITY_BASIS_VALUES:
            raise ValueError(
                f"quantity_basis must be one of {sorted(QUANTITY_BASIS_VALUES)}"
            )
        _non_negative(self.announced_gpus, "announced_gpus")
        _non_negative(self.announced_mw, "announced_mw")
        if self.quantity_basis in {"gpu_count", "both"} and self.announced_gpus is None:
            raise ValueError("quantity_basis requires announced_gpus")
        if self.quantity_basis in {"mw", "both"} and self.announced_mw is None:
            raise ValueError("quantity_basis requires announced_mw")
        if self.bookability_status not in AVAILABILITY_VALUES:
            raise ValueError(
                f"bookability_status must be one of {sorted(AVAILABILITY_VALUES)}"
            )
        if self.expected_availability_date:
            _iso(
                self.expected_availability_date,
                "expected_availability_date",
                date_only=True,
            )
        if self.availability_checked_at:
            _iso(self.availability_checked_at, "availability_checked_at")

    def natural_key_fields(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "event_reference": self.event_reference,
            "region_scope": self.region_scope,
            "gpu_model": self.gpu_model,
        }


@dataclass(frozen=True, kw_only=True)
class SourceHealthEvent(BaseEvent):
    event_type: ClassVar[str] = "source_health"
    run_id: str
    status: str
    operation: str
    documents_discovered: int = 0
    documents_extracted: int = 0
    candidate_events: int = 0
    new_events: int = 0
    changed_events: int = 0
    unchanged_events: int = 0
    failed_urls: int = 0
    error_code: str = ""

    def validate(self) -> None:
        super().validate()
        _required(self.run_id, "run_id")
        _required(self.status, "status")
        _required(self.operation, "operation")
        for name in (
            "documents_discovered",
            "documents_extracted",
            "candidate_events",
            "new_events",
            "changed_events",
            "unchanged_events",
            "failed_urls",
        ):
            _non_negative(getattr(self, name), name)

    def natural_key_fields(self) -> dict[str, Any]:
        return {"source_id": self.source_id, "run_id": self.run_id}


MarketEvent = CompetitorOfferEvent | CapacityAnnouncementEvent | SourceHealthEvent

EVENT_TYPES: dict[str, type[BaseEvent]] = {
    "competitor_offer": CompetitorOfferEvent,
    "capacity_announcement": CapacityAnnouncementEvent,
    "source_health": SourceHealthEvent,
}


def event_from_dict(data: dict[str, Any]) -> MarketEvent:
    payload = dict(data)
    event_type = payload.pop("event_type", None)
    for generated in (
        "semantic_key",
        "content_hash",
        "version_id",
        "version_number",
        "supersedes_version_id",
        "recorded_at",
    ):
        payload.pop(generated, None)
    cls = EVENT_TYPES.get(str(event_type))
    if not cls:
        raise ValueError(f"unknown event_type: {event_type}")
    field_names = {field.name for field in dataclasses.fields(cls)}
    unknown = set(payload) - field_names
    if unknown:
        raise ValueError(f"unknown fields for {event_type}: {sorted(unknown)}")
    event = cls(**payload)
    event.validate()
    return event  # type: ignore[return-value]
