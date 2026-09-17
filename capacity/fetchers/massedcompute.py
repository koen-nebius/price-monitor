"""Massed Compute inventory, preserving exact SKU and observed region.

The official MCP inventory separates listed instance configurations from
regions with capacity. Listing a SKU or a true catalogue-level capacity flag
alone does not establish stock in any region. No global rollup is emitted:
the current capacity renderer would otherwise infer eight-GPU/cluster stock
from a single available SKU. Multi-node capability is not established here.
"""
import logging
import math
from datetime import datetime, timezone
from typing import List

from capacity.schema import AvailabilityRecord
from fetchers.massedcompute import normalize_offer
from massedcompute_api import fetch_inventory

logger = logging.getLogger(__name__)
SOURCE_URL = "https://vm.massedcompute.com/api/mcp"
PARSER_VERSION = "massedcompute-capacity-1.0"


def _regions(value):
    """Return validated, deduplicated region labels, or None for bad data."""
    if not isinstance(value, list):
        return None
    result = set()
    for item in value:
        if isinstance(item, str):
            name = item
        elif isinstance(item, dict):
            name = item.get("name") or item.get("id") or item.get("region")
        else:
            return None
        if not isinstance(name, str) or not name.strip():
            return None
        name = name.strip()
        # 'global' is an aggregation sentinel elsewhere in this monitor.
        result.add("global (SKU region)" if name.lower() == "global" else name)
    return sorted(result)


def _capacity_flag(entry):
    """Interpret an explicit flag/zero only; never turn it into a stock count."""
    if "capacity_available" not in entry:
        return None, False
    value = entry["capacity_available"]
    if isinstance(value, bool):
        return value, False
    if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
        return value > 0, False
    return None, True


def parse(payload: dict, fetched_at: str) -> List[AvailabilityRecord]:
    """Parse a successful inventory response without guessing missing stock."""
    inventory = payload.get("gpu_inventory") if isinstance(payload, dict) else None
    if not isinstance(inventory, dict):
        raise ValueError("Massed inventory response has no gpu_inventory object")
    records = []
    for sku, entry in inventory.items():
        if not isinstance(sku, str) or not isinstance(entry, dict):
            continue
        offer = normalize_offer(sku, entry)
        if offer is None:
            continue
        regions = _regions(entry.get("regions_with_capacity_available"))
        capacity, invalid_capacity = _capacity_flag(entry)
        raw_capacity = entry.get("capacity_available")
        stock_note = (f"; capacity_available={raw_capacity} (unit unverified)"
                      if type(raw_capacity) in (int, float) and not invalid_capacity else "")
        base = dict(
            provider="massedcompute", gpu_model=offer.gpu_model,
            consumption_type=offer.consumption_type,
            instance_type=offer.instance_type,
            fetched_at=fetched_at, source_url=SOURCE_URL,
            data_source="official_api", parser_version=PARSER_VERSION,
        )
        if not regions:
            valid_empty = regions == []
            detail = ("Listed SKU; no region stock reported" if valid_empty else
                      "Listed SKU; regional stock missing or unreadable")
            if capacity is True:
                detail += "; positive capacity flag has no confirmed region"
            detail += stock_note
            records.append(AvailabilityRecord(
                **base, region="unreported", state="unknown",
                metric_type="regions_with_capacity",
                metric_value=0.0 if valid_empty else None, detail=detail,
            ))
            continue
        # An explicit false/zero contradicts a list of available regions.
        # Preserve that conflict rather than choosing the optimistic signal.
        conflict = capacity is False or invalid_capacity
        for region in regions:
            records.append(AvailabilityRecord(
                **base, region=region, state="unknown" if conflict else "available",
                metric_type="binary", metric_value=None if conflict else 1.0,
                detail=("Regional stock conflicts with capacity_available; verify source"
                        if conflict else
                        f"API lists this {offer.gpu_count}-GPU SKU in region"
                        + stock_note + "; quantity and multi-node availability not established"),
            ))
    return records


def fetch() -> List[AvailabilityRecord]:
    try:
        payload = fetch_inventory()
        return parse(payload, datetime.now(timezone.utc).isoformat())
    except Exception as exc:
        # Do not print authenticated request details or credentials.
        logger.error("Massed capacity fetch failed (%s)", type(exc).__name__)
        return []
