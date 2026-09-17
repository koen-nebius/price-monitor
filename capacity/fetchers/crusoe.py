"""
Crusoe authenticated capacity, with a docs footprint when no key is configured.

https://docs.crusoecloud.com/compute/virtual-machines/overview/index.html is
server-rendered HTML (verified 2026-08-12, no JS needed) mapping each
instance type to its zones (e.g. h100-80gb-sxm-ib.8x → us-east1-a,
us-southcentral1-a, eu-iceland1-a). Footprint, not live stock.

GET /v1/capacities returns exact resource types and locations. Its quantity is
retained in provider units; neither GPU totals nor multi-node capacity is
inferred. API errors and partial credentials never fall back to docs.
"""
import logging
import re
from dataclasses import replace
from datetime import datetime, timezone
from typing import List

import crusoe_api
from capacity.schema import AvailabilityRecord, plural
from crusoe_api import API_URL, credentials_configured, fetch_capacities

logger = logging.getLogger(__name__)

URL = "https://docs.crusoecloud.com/compute/virtual-machines/overview/index.html"
SOURCE_URL = URL
PARSER_VERSION = "crusoe-capacity-2.1"

_PARSE_ERROR_REASONS = {
    "Crusoe capacity response has no items array",
    "Crusoe capacity returned unsupported pagination",
    "Crusoe capacity contains an invalid item",
    "Crusoe capacity has an invalid instance type",
    "Crusoe capacity has an invalid location",
    "Crusoe capacity has an invalid quota_type",
    "Crusoe capacity has invalid quantity",
    "Crusoe capacity has invalid num_slices",
}


class CrusoeParseError(ValueError):
    """Only allowlisted static diagnostics may cross the logging boundary."""

    def __init__(self, reason):
        super().__init__(reason if reason in _PARSE_ERROR_REASONS
                         else "Crusoe capacity schema validation failed")

_TYPE_GPU = [
    ("gb300", "GB300"), ("gb200", "GB200"), ("b300", "B300"), ("b200", "B200"),
    ("h200", "H200"), ("h100", "H100"), ("l40s", "L40S"),
]

_ZONE_RE = re.compile(r"\b(?:us|eu|ap|me)-[a-z]+\d-[a-z]\b")
_LIMITED_MAX_ZONES = 1


def _fetch_docs() -> List[AvailabilityRecord]:
    now = datetime.now(timezone.utc).isoformat()
    try:
        from fetchers._http import http_get
        html = http_get(URL, timeout=45).decode("utf-8", "replace")
    except Exception as e:
        logger.error("Crusoe docs fetch failed (%s)", type(e).__name__)
        return []

    # Table rows: instance-type slug cell followed by a zone-list cell
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.DOTALL)
    model_zones: dict = {}
    for row in rows:
        cells = [re.sub(r"<[^>]+>", " ", c) for c in
                 re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.DOTALL)]
        if not cells:
            continue
        row_text = " ".join(cells).lower()
        type_match = re.search(r"\b([a-z0-9]+(?:-[a-z0-9.]+)+x?)\b", row_text)
        gpu_model = None
        for frag, model in _TYPE_GPU:
            if type_match and frag in type_match.group(1):
                gpu_model = model
                break
        if not gpu_model:
            # Fallback: fragment anywhere in a type-looking token
            for frag, model in _TYPE_GPU:
                if re.search(rf"\b{frag}-\d+gb", row_text):
                    gpu_model = model
                    break
        if not gpu_model:
            continue
        zones = set(_ZONE_RE.findall(row_text))
        if zones:
            model_zones.setdefault(gpu_model, set()).update(zones)

    if not model_zones:
        logger.error("Crusoe docs: no GPU type→zone rows parsed — layout changed?")
        return []

    records: List[AvailabilityRecord] = []
    for model, zones in sorted(model_zones.items()):
        n = len(zones)
        state = "limited" if n <= _LIMITED_MAX_ZONES else "available"
        records.append(AvailabilityRecord(
            provider="crusoe", gpu_model=model, region="global",
            consumption_type="on_demand", state=state,
            metric_type="listed_offering", metric_value=float(n),
            detail=f"offered in {plural(n, 'zone')}: {', '.join(sorted(zones))} "
                   f"(footprint, not live stock)",
            fetched_at=now, source_url=SOURCE_URL, data_source="web_scrape",
        ))

    logger.info(f"Crusoe docs: {len(records)} records "
                f"({', '.join(f'{m}:{len(z)}z' for m, z in sorted(model_zones.items()))})")
    return records


def _gpu_model(instance_type):
    for prefix, model in _TYPE_GPU:
        if re.match(rf"^{prefix}(?:[.-]|$)", instance_type, re.I):
            return model
    if re.match(r"^rtx-pro-6000-blackwell(?:[.-]|$)", instance_type, re.I):
        return "RTX6000"
    return None


def _uint32(value, field):
    if type(value) is not int or not 0 <= value <= 2 ** 32 - 1:
        raise CrusoeParseError(f"Crusoe capacity has invalid {field}")
    return value


def parse(payload: dict, fetched_at: str = "") -> List[AvailabilityRecord]:
    """Preserve one exact resource/location observation, never sum shapes."""
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise CrusoeParseError("Crusoe capacity response has no items array")
    if payload.get("next_page_token") or payload.get("next_token"):
        raise CrusoeParseError("Crusoe capacity returned unsupported pagination")
    by_identity = {}
    for item in items:
        if not isinstance(item, dict):
            raise CrusoeParseError("Crusoe capacity contains an invalid item")
        sku = item.get("type")
        # Type is optional in CapacityV1; no hardware identity means no GPU row.
        if sku is None:
            continue
        if not isinstance(sku, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", sku):
            raise CrusoeParseError("Crusoe capacity has an invalid instance type")
        model = _gpu_model(sku)
        if not model:
            continue
        location = item.get("location")
        if not isinstance(location, str) or not re.fullmatch(r"[a-z][a-z0-9-]{1,79}", location):
            raise CrusoeParseError("Crusoe capacity has an invalid location")
        key = (sku, location)
        quantity = _uint32(item.get("quantity"), "quantity")
        parts = [f"API quantity {quantity} (provider units)"]
        if "num_slices" in item:
            slices = _uint32(item["num_slices"], "num_slices")
            parts.append(f"num_slices={slices} per resource")
        if "quota_type" in item:
            quota = item["quota_type"]
            if not isinstance(quota, str) or (quota and not re.fullmatch(r"[A-Z0-9_]{1,256}", quota)):
                raise CrusoeParseError("Crusoe capacity has an invalid quota_type")
            if quota:
                parts.append(f"quota_type={quota}")
        # Reservation context is not part of today's CapacityV1. If introduced,
        # avoid presenting reserved/restricted quantity as open availability.
        reservation = any(item.get(field) not in (None, False, "", [], {}) for field in (
            "reservation", "reservation_id", "reservation_specification",
            "reserved", "is_reserved", "requires_reservation",
        ))
        state = "unknown" if reservation else "available" if quantity > 0 else "sold_out"
        if reservation:
            parts.append("reservation context present; eligibility unverified")
        parts.append("account quota, reservation eligibility and multi-node stock not established")
        # 'global' is a special aggregation sentinel in the existing renderer.
        region = "global (reported location)" if location == "global" else location
        record = AvailabilityRecord(
            provider="crusoe", gpu_model=model, region=region,
            consumption_type="on_demand", state=state,
            metric_type="provider_quantity", metric_value=float(quantity),
            detail="; ".join(parts), instance_type=sku, fetched_at=fetched_at,
            source_url=API_URL, data_source="official_api", parser_version=PARSER_VERSION,
        )
        # The live API can repeat a type/location. Identical normalized evidence
        # is one observation; conflicting quantities, slices, quota categories
        # or reservation context must not be summed or resolved optimistically.
        by_identity.setdefault(key, {})[record.detail] = record
    records = []
    suffix = "; account quota, reservation eligibility and multi-node stock not established"
    for variants in by_identity.values():
        if len(variants) == 1:
            records.append(next(iter(variants.values())))
            continue
        details = sorted(variants)
        candidates = " | ".join("[" + detail.removesuffix(suffix) + "]" for detail in details)
        records.append(replace(
            variants[details[0]], state="unknown", metric_value=None,
            detail="Ambiguous duplicate API observations; candidates: " + candidates
                   + "; no aggregate quantity or stock verdict assigned" + suffix,
        ))
    return records


def fetch() -> List[AvailabilityRecord]:
    if crusoe_api.CAPACITY_ACCESS_PAUSED:
        logger.info("Crusoe capacity paused: %s", crusoe_api.CAPACITY_PAUSE_REASON)
        return []
    if not credentials_configured():
        return _fetch_docs()
    try:
        return parse(fetch_capacities(), datetime.now(timezone.utc).isoformat())
    except CrusoeParseError as exc:
        logger.error("Crusoe API capacity fetch failed (%s)", str(exc))
        return []
    except Exception as exc:
        status = getattr(exc, "http_status", None)
        if type(status) is int and 100 <= status <= 599:
            logger.error("Crusoe API capacity fetch failed (HTTP %d)", status)
        else:
            logger.error("Crusoe API capacity fetch failed (%s)", type(exc).__name__)
        return []
