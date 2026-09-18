"""Voltage Park's published bare-metal inventory, without synthetic zero stock.

The official GET /api/v1/bare-metal/locations currently returns a paginated
results envelope. Empty locations establish no GPU-specific inventory, while
explicit integer zero counts on a named GPU establish zero for that listing.
No deployments, quote requests, authentication changes or support messages.
"""
import json
import logging
from datetime import datetime, timezone
from typing import List
from urllib.error import HTTPError, URLError

from capacity.schema import AvailabilityRecord

logger = logging.getLogger(__name__)
API = "https://cloud-api.voltagepark.com/api/v1/bare-metal/locations"
SOURCE_URL = API
_GPU_MAP = {"h100": "H100", "h200": "H200", "b200": "B200", "b300": "B300"}
_LIMITED_MAX_GPUS = 64
LAST_FETCH_HEALTH = {}


def _health():
    LAST_FETCH_HEALTH.clear()
    LAST_FETCH_HEALTH.update(status="pending", reason="not started", error_code=None,
                            planned_checks=1, completed_checks=0, failed_checks=0,
                            unqueried_checks=[], requests_attempted=0, pages_fetched=0,
                            pagination_complete=False, reported_locations=None,
                            parsed_locations=0, invalid_locations=0)
    return LAST_FETCH_HEALTH


def _count(value):
    # The official schema requires a nonnegative integer, never missing/null.
    return value if type(value) is int and value >= 0 else None


def fetch() -> List[AvailabilityRecord]:
    health = _health()
    now = datetime.now(timezone.utc).isoformat()
    try:
        from fetchers._http import http_get
        health["requests_attempted"] = 1
        # An access/rate-limit failure is a source state, not a reason to burst
        # repeated requests. The next scheduled run is the retry boundary.
        data = json.loads(http_get(API, timeout=20, retries=1))
        health["pages_fetched"] = 1
    except HTTPError as exc:
        reason = ("inventory endpoint access denied" if exc.code in (401, 403)
                  else "inventory endpoint rate limited" if exc.code == 429
                  else "inventory endpoint HTTP failure")
        health.update(status="failed", reason=reason, error_code=f"http_{exc.code}", failed_checks=1)
        return []
    except (URLError, TimeoutError, OSError) as exc:
        health.update(status="failed", reason="inventory endpoint connection failed",
                      error_code=type(exc).__name__, failed_checks=1)
        return []
    except (ValueError, TypeError):
        health.update(status="failed", reason="inventory response is not valid JSON",
                      error_code="invalid_json", failed_checks=1)
        return []

    if (not isinstance(data, dict) or not isinstance(data.get("results"), list)
            or type(data.get("has_next")) is not bool
            or _count(data.get("total_result_count")) is None):
        health.update(status="failed", reason="inventory envelope does not match the official API schema",
                      error_code="invalid_schema", failed_checks=1)
        return []
    locations = data["results"]
    health["reported_locations"] = data["total_result_count"]
    complete = not data["has_next"] and len(locations) == data["total_result_count"]
    health["pagination_complete"] = complete
    if not complete:
        # OpenAPI publishes no paging request parameters for this endpoint.
        # Do not invent an unverified cursor or treat the first page as global.
        health.update(status="partial", reason="locations response is incomplete; paging parameters are not documented",
                      error_code="incomplete_pagination", failed_checks=1,
                      unqueried_checks=["remaining bare-metal locations"])
    elif not locations:
        health.update(status="empty", reason="endpoint returned no locations; GPU inventory is unknown, not zero",
                      error_code=None, completed_checks=1)
        return []

    per_model = {}
    seen_ids = set()
    for loc in locations:
        if not isinstance(loc, dict) or not isinstance(loc.get("specs_per_node"), dict):
            health["invalid_locations"] += 1
            continue
        ident = loc.get("id")
        if not ident or ident in seen_ids:
            health["invalid_locations"] += 1
            continue
        seen_ids.add(ident)
        model_raw = loc["specs_per_node"].get("gpu_model")
        if not isinstance(model_raw, str):
            health["invalid_locations"] += 1
            continue
        model = next((m for frag, m in _GPU_MAP.items() if frag in model_raw.lower()), None)
        if not model:
            continue  # A listed, untracked GPU is not a parser failure.
        eth, ib = _count(loc.get("gpu_count_ethernet")), _count(loc.get("gpu_count_infiniband"))
        agg = per_model.setdefault(model, {"eth": 0, "ib": 0, "locations": 0, "complete": True})
        if eth is None or ib is None:
            agg["complete"] = False
            health["invalid_locations"] += 1
            continue
        agg["eth"] += eth
        agg["ib"] += ib
        agg["locations"] += 1
        health["parsed_locations"] += 1

    records = []
    for model, agg in per_model.items():
        known = complete and agg["complete"] and not health["invalid_locations"]
        total = agg["eth"] + agg["ib"] if known else None
        if total is None:
            state, detail = "unknown", "incomplete or invalid location counts; no global GPU quantity established"
        elif total == 0:
            state, detail = "sold_out", f"0 GPUs explicitly listed across {agg['locations']} locations (Ethernet and InfiniBand)"
        else:
            state = "limited" if total <= _LIMITED_MAX_GPUS else "available"
            detail = f"{total} GPUs reported ({agg['ib']} IB / {agg['eth']} Eth) across {agg['locations']} locations; no single-cluster guarantee"
        records.append(AvailabilityRecord(
            provider="voltage_park", gpu_model=model, region="global",
            consumption_type="on_demand", state=state,
            metric_type="stock_level", metric_value=float(total) if total is not None else None,
            detail=detail, fetched_at=now, source_url=SOURCE_URL, data_source="official_api",
            parser_version="voltage-inventory-2", product_scope="published_bare_metal_inventory",
        ))
    if health["invalid_locations"]:
        health.update(status="partial" if records else "failed", reason="one or more locations have invalid or missing required fields",
                      error_code="invalid_location_schema", failed_checks=1)
    elif complete:
        health.update(status="live" if records else "empty", completed_checks=1,
                      reason="complete published inventory retrieved" if records else "no tracked GPU models in published locations")
    logger.info("Voltage Park: %s records; %s; %s locations parsed", len(records), health["status"], health["parsed_locations"])
    return records
