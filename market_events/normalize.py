"""Deterministic normalizers for verified fixtures and public-page candidates."""

from __future__ import annotations

import re

from .models import CapacityAnnouncementEvent, CompetitorOfferEvent, MarketEvent, stable_hash
from .registry import SourceSpec
from .tavily import RawDocument


GPU_RE = re.compile(r"\b(GB300|GB200|B300|B200|H200|H100|L40S|RTX\s*6000)\b", re.I)
PRICE_RE = re.compile(r"\$\s*([0-9]+(?:\.[0-9]+)?)")
GPU_COUNT_RE = re.compile(
    r"([0-9][0-9,]*)\s+(?:NVIDIA\s+)?(?:GB300|GB200|B300|B200|H200|H100|L40S|RTX\s*6000)?\s*GPUs?\b",
    re.I,
)
POWER_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*(GW|MW)\b", re.I)


def _gpu(value: str) -> str:
    cleaned = value.upper().replace(" ", "")
    return "RTX6000" if cleaned == "RTX6000" else cleaned


def _lane(text: str) -> str:
    lowered = text.lower()
    if any(value in lowered for value in ("reserve", "reserved", "commitment", "committed")):
        return "Reserve"
    if any(value in lowered for value in ("spot", "preemptible", "pre-emptible")):
        return "PVM"
    return "PAYG"


def normalize_fixture_row(row: dict) -> MarketEvent:
    """Convert an existing official-source evidence row into the typed schema."""

    common = {
        "source_id": row["source_id"],
        "source_url": row["source_url"],
        "provider": row["provider"],
        "source_title": row.get("source_title", ""),
        "observed_at": row["observed_at"],
        "event_date": row["event_date"],
        "region_scope": row.get("region_scope", "Global"),
        "gpu_model": row.get("gpu_model", "Unspecified"),
        "confidence": row.get("confidence", "high"),
        "review_status": "verified",
        "extraction_method": "existing_official_source_fixture",
        "evidence_text": row["verified_fact"],
    }
    if row["record_type"] == "competitor_price_change":
        event = CompetitorOfferEvent(
            **common,
            product_name=row.get("product_name") or row["gpu_model"],
            configuration=row.get("configuration", "Unspecified"),
            consumption_type=row["consumption_type"],
            price_usd_per_gpu_hour=float(row["current_price_usd_gpu_hour"]),
            previous_price_usd_per_gpu_hour=float(row["previous_price_usd_gpu_hour"]),
            is_price_change=True,
            effective_date=row.get("effective_date"),
            term_months=row.get("term_months"),
            availability_status=row.get("availability_status", "not_publicly_verifiable"),
            availability_checked_at=row["observed_at"],
            availability_evidence=row.get("availability_evidence", row.get("caveat", "")),
            bundle_status=row.get("bundle_status", "not_checked"),
        )
    elif row["record_type"] == "provider_capacity_news":
        announced_gpus = row.get("announced_gpus")
        announced_mw = row.get("announced_mw")
        quantity_basis = (
            "both"
            if announced_gpus is not None and announced_mw is not None
            else "gpu_count"
            if announced_gpus is not None
            else "mw"
            if announced_mw is not None
            else "undisclosed"
        )
        event = CapacityAnnouncementEvent(
            **common,
            event_reference=row.get("evidence_id") or stable_hash(row)[:20],
            status=row["status"],
            quantity_basis=quantity_basis,
            announced_gpus=announced_gpus,
            announced_mw=announced_mw,
            expected_availability_date=row.get("expected_availability_date"),
            location=row.get("location", row.get("region_scope", "")),
            contracted_status=row.get("contracted_status", "unknown"),
            bookability_status=row.get("bookability_status", "not_publicly_verifiable"),
            availability_checked_at=row["observed_at"],
        )
    else:
        raise ValueError(f"unsupported fixture record_type: {row['record_type']}")
    event.validate()
    return event


class PublicDocumentNormalizer:
    """Create low-confidence review candidates from official public content."""

    def normalize(self, source: SourceSpec, document: RawDocument) -> list[MarketEvent]:
        events: list[MarketEvent] = []
        lines = [line.strip() for line in document.content.splitlines() if line.strip()]
        if "pricing" in source.event_kinds:
            events.extend(self._offer_candidates(source, document, lines))
        if "capacity" in source.event_kinds:
            events.extend(self._capacity_candidates(source, document, lines))
        return events

    @staticmethod
    def _offer_candidates(source, document, lines) -> list[CompetitorOfferEvent]:
        events = []
        event_date = document.collected_at[:10]
        for line in lines:
            gpu_match = GPU_RE.search(line)
            prices = [float(value) for value in PRICE_RE.findall(line)]
            if not gpu_match or not prices:
                continue
            previous = prices[0] if len(prices) >= 2 else None
            event = CompetitorOfferEvent(
                source_id=source.source_id,
                source_url=document.url,
                provider=source.provider,
                source_title=document.title,
                observed_at=document.collected_at,
                event_date=event_date,
                region_scope=source.default_region,
                gpu_model=_gpu(gpu_match.group(1)),
                evidence_text=line[:800],
                confidence="low",
                review_status="candidate",
                extraction_method="public_page_regex_candidate",
                raw_document_hash=document.content_hash(),
                product_name=_gpu(gpu_match.group(1)),
                configuration="Unspecified",
                consumption_type=_lane(line),
                price_usd_per_gpu_hour=prices[-1],
                previous_price_usd_per_gpu_hour=previous,
                is_price_change=previous is not None,
                availability_status="listed_only",
                availability_checked_at=document.collected_at,
                availability_evidence="Listed on an official page; bookability was not tested.",
                bundle_status="not_checked",
            )
            event.validate()
            events.append(event)
        return events

    @staticmethod
    def _capacity_candidates(source, document, lines) -> list[CapacityAnnouncementEvent]:
        events = []
        event_date = document.collected_at[:10]
        for line in lines:
            gpu_match = GPU_RE.search(line)
            gpu_count_match = GPU_COUNT_RE.search(line)
            power_match = POWER_RE.search(line)
            if not gpu_count_match and not power_match:
                continue
            announced_gpus = (
                float(gpu_count_match.group(1).replace(",", "")) if gpu_count_match else None
            )
            announced_mw = None
            if power_match:
                announced_mw = float(power_match.group(1))
                if power_match.group(2).upper() == "GW":
                    announced_mw *= 1000
            quantity_basis = (
                "both"
                if announced_gpus is not None and announced_mw is not None
                else "gpu_count"
                if announced_gpus is not None
                else "mw"
            )
            event = CapacityAnnouncementEvent(
                source_id=source.source_id,
                source_url=document.url,
                provider=source.provider,
                source_title=document.title,
                observed_at=document.collected_at,
                event_date=event_date,
                region_scope=source.default_region,
                gpu_model=_gpu(gpu_match.group(1)) if gpu_match else "Unspecified",
                evidence_text=line[:800],
                confidence="low",
                review_status="candidate",
                extraction_method="public_page_regex_candidate",
                raw_document_hash=document.content_hash(),
                event_reference=stable_hash(
                    {"source_id": source.source_id, "url": document.url, "line": line}
                )[:20],
                status="announced_unverified",
                quantity_basis=quantity_basis,
                announced_gpus=announced_gpus,
                announced_mw=announced_mw,
                contracted_status="unknown",
                bookability_status="not_publicly_verifiable",
                availability_checked_at=document.collected_at,
            )
            event.validate()
            events.append(event)
        return events
