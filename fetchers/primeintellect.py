"""Opt-in Prime Intellect availability pilot; no production PriceRecord output.

Read only the two documented availability endpoints. Retain configurations and
raw commercial fields for review; never infer a per-GPU denominator, a reserved
term from prepaid hours, or cluster inventory from a configuration's GPU count.
See analysis/primeintellect_pilot.md for the verified contract and live boundary.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone


SOURCE_FEED = "primeintellect"
PARSER_VERSION = "primeintellect-pilot-1"
API_BASE = "https://api.primeintellect.ai/api/v1/availability/"
ENDPOINTS = {"single_node": "gpus", "multi_node": "multi-node"}
GPU_MAP = {
    "H100_80GB": "H100", "H200_141GB": "H200", "B200_180GB": "B200",
    "B300_262GB": "B300", "GB200": "GB200", "GB300": "GB300",
    "L40S_48GB": "L40S", "RTX_PRO_6000B_96GB": "RTX6000",
}
STOCK_LABELS = {"Available", "Low", "Medium", "High", "Unavailable"}
SOCKETS = {"PCIe", "SXM2", "SXM3", "SXM4", "SXM5", "SXM6"}
RESOURCE_FIELDS = ("disk", "sharedDisk", "vcpu", "memory")
MAX_RESPONSE_BYTES = 20 * 1024 * 1024


class PilotError(ValueError):
    """Only controlled codes cross the transport boundary; no response bodies."""


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _number(value, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number < 0 or (positive and number == 0):
        return None
    return number


def _integer(value, positive=False):
    return value if type(value) is int and value >= (1 if positive else 0) else None


def _string(value):
    return value.strip() if isinstance(value, str) else ""


def _identity(item, endpoint):
    # The source's cloudId is not globally unique: dataCenter and configuration
    # dimensions must accompany it. Prices and stock states are observations.
    dimensions = {key: item.get(key) for key in (
        "provider", "cloudId", "gpuType", "socket", "gpuCount", "gpuMemory",
        "region", "dataCenter", "country", "security", "interconnect",
        "interconnectType", "isSpot", "prepaidTime")}
    dimensions["endpoint_scope"] = endpoint
    dimensions["resources"] = {
        name: {key: (item.get(name) or {}).get(key)
               for key in ("minCount", "defaultCount", "maxCount", "step")}
        for name in RESOURCE_FIELDS if isinstance(item.get(name), dict)
    }
    encoded = json.dumps(dimensions, sort_keys=True, separators=(",", ":"), default=str)
    return "primeintellect:" + hashlib.sha256(encoded.encode()).hexdigest()[:24]


def audit_offer(item, endpoint, fetched_at=None, row_index=0):
    """Preserve one observed catalog configuration; all prices stay quarantined.

    OpenAPI confirms hourly prices but not the GPU/configuration denominator.
    isSpot describes provisioning capability, not which price field is a spot
    quote. These gaps prevent honest construction of the shared PriceRecord.
    """
    if endpoint not in ENDPOINTS:
        raise PilotError("unsupported_endpoint")
    if not isinstance(item, dict):
        return {"row_index": row_index, "source_feed": SOURCE_FEED,
                "endpoint_scope": endpoint, "comparison_eligible": False,
                "quarantine_reasons": ["item_not_object"]}
    reasons = ["price_denominator_unverified", "source_observation_time_unavailable"]
    if endpoint == "multi_node":
        reasons.append("multinode_topology_and_node_count_unverified")
    for field in ("provider", "cloudId", "gpuType"):
        if not _string(item.get(field)):
            reasons.append("missing_" + field)
    count = _integer(item.get("gpuCount"), positive=True)
    if count is None:
        reasons.append("invalid_gpu_count")
    gpu = GPU_MAP.get(_string(item.get("gpuType")))
    if gpu is None:
        reasons.append("unmapped_gpu_type")
    socket = _string(item.get("socket"))
    if socket not in SOCKETS:
        reasons.append("unknown_socket")
    stock = _string(item.get("stockStatus")) or None
    if stock not in STOCK_LABELS:
        reasons.append("unknown_stock_status")
    security = _string(item.get("security"))
    prices = item.get("prices") if isinstance(item.get("prices"), dict) else {}
    if prices.get("currency") != "USD":
        reasons.append("non_usd_or_missing_currency")
    selected_field = {"secure_cloud": "onDemand", "community_cloud": "communityPrice"}.get(security)
    if selected_field is None:
        reasons.append("unknown_security_tier")
    elif _number(prices.get(selected_field), positive=True) is None:
        reasons.append("missing_or_invalid_tier_price")
    if prices.get("isVariable") is True:
        reasons.append("variable_price")
    if item.get("isSpot") is not None and not isinstance(item.get("isSpot"), bool):
        reasons.append("invalid_spot_capability")
    prepaid = item.get("prepaidTime")
    if prepaid is not None and _number(prepaid) is None:
        reasons.append("invalid_prepaid_hours")
    for field in RESOURCE_FIELDS:
        spec = item.get(field)
        if spec is not None and not isinstance(spec, dict):
            reasons.append("invalid_" + field + "_spec")
        elif isinstance(spec, dict) and spec:
            if spec.get("defaultIncludedInPrice") is False:
                reasons.append(field + "_default_cost_is_additional")
            elif spec.get("defaultIncludedInPrice") is not True:
                reasons.append(field + "_default_cost_inclusion_unknown")
    return {
        "row_index": row_index, "source_feed": SOURCE_FEED,
        "provider": _string(item.get("provider")),
        "cloud_id": _string(item.get("cloudId")),
        "offer_id": _identity(item, endpoint), "endpoint_scope": endpoint,
        "product_scope": "gpu_instance_catalog" if endpoint == "single_node" else "multinode_catalog",
        "gpu_model": gpu, "gpu_variant": item.get("gpuType"), "socket": socket,
        "gpu_count": count, "gpu_memory_gb": item.get("gpuMemory"),
        "region": item.get("region"), "data_center": item.get("dataCenter"),
        "country": item.get("country"), "security": security,
        "resource_specs": {field: copy.deepcopy(item.get(field)) for field in RESOURCE_FIELDS},
        "interconnect_gbps": item.get("interconnect"),
        "interconnect_type": item.get("interconnectType"),
        "internet_speed_mbps": item.get("internetSpeed"),
        "provisioning_minutes": item.get("provisioningTime"),
        "stock_status": stock, "availability_evidence": "aggregator_stock_label",
        "available": True if stock == "Available" else False if stock == "Unavailable" else None,
        "stock_quantity": None, "node_count": None,
        "spot_supported": item.get("isSpot") if isinstance(item.get("isSpot"), bool) else None,
        "prices_raw": copy.deepcopy(prices), "price_time_unit": "hour",
        "price_unit": None, "price_field_for_security": selected_field,
        "price_per_gpu_hour_usd": None, "consumption_type": None,
        "prepaid_hours": _number(prepaid), "commitment_months": None,
        "source_observed_at": None, "fetched_at": fetched_at,
        "source_url": API_BASE + ENDPOINTS[endpoint], "parser_version": PARSER_VERSION,
        "comparison_eligible": False, "quarantine_reasons": reasons,
    }


def validate_page(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise PilotError("invalid_items_envelope")
    total = _integer(payload.get("totalCount"))
    if total is None:
        raise PilotError("invalid_total_count")
    return payload["items"], total


def decode_payload(raw):
    def reject_constant(_):
        raise ValueError("non_finite_json_number")
    return json.loads(raw, parse_constant=reject_constant)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise PilotError("redirect_rejected")


def read_page(url, token):
    """GET only, fixed API host, bounded body, no credential-bearing redirects."""
    parsed = urllib.parse.urlsplit(url)
    allowed_paths = {"/api/v1/availability/" + suffix for suffix in ENDPOINTS.values()}
    if parsed.scheme != "https" or parsed.netloc != "api.primeintellect.ai" or parsed.path not in allowed_paths:
        raise PilotError("unsupported_request_url")
    request = urllib.request.Request(url, method="GET", headers={
        "Authorization": "Bearer " + token, "Accept": "application/json",
        "User-Agent": "price-monitor/primeintellect-read-only-pilot",
    })
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=30) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise PilotError("http_" + str(exc.code)) from None
    except PilotError:
        raise
    except Exception:
        raise PilotError("transport_failed") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise PilotError("response_too_large")
    return raw


def collect(token, endpoints=("single_node", "multi_node"), *, page_size=100,
            max_pages=100, request_page=read_page, on_page=None):
    """Collect complete pages or mark the endpoint incomplete; never publish.

    ``on_page(endpoint, page, bytes, metadata)`` lets the caller persist raw data
    to an explicit audit directory. Credentials and HTTP headers are excluded.
    """
    if not 1 <= page_size <= 100 or not 1 <= max_pages <= 1000:
        raise PilotError("invalid_page_bounds")
    if not endpoints or any(endpoint not in ENDPOINTS for endpoint in endpoints):
        raise PilotError("unsupported_endpoint")
    audit = {"source_feed": SOURCE_FEED, "parser_version": PARSER_VERSION,
             "mode": "live_pilot", "credential_present": bool(token),
             "live_attempted": False, "production_eligible": False,
             "price_record_count": 0, "endpoints": {}, "offers": []}
    if not token:
        audit.update(status="blocked", blocked_stage="authentication",
                     reason="missing_availability_read_credential")
        return audit
    audit["live_attempted"] = True
    for endpoint in endpoints:
        info = {"complete": False, "pages": [], "observed_rows": 0, "total_count": None}
        audit["endpoints"][endpoint] = info
        seen = set()
        for page in range(1, max_pages + 1):
            url = API_BASE + ENDPOINTS[endpoint] + "?" + urllib.parse.urlencode({"page": page, "page_size": page_size})
            try:
                raw = request_page(url, token)
            except PilotError as exc:
                # Only our bounded codes are permitted in output. A custom test
                # transport or future adapter may raise an arbitrary message.
                code = str(exc)
                info["error"] = code if code in {"redirect_rejected", "transport_failed", "response_too_large"} or (
                    code.startswith("http_") and code[5:].isdigit()) else "transport_failed"
                break
            except Exception:
                info["error"] = "transport_failed"
                break
            metadata = {"url": url, "fetched_at": _utc_now(),
                        "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
            info["pages"].append(metadata)
            if on_page is not None:
                on_page(endpoint, page, raw, metadata)
            try:
                payload = decode_payload(raw)
                items, total = validate_page(payload)
            except (ValueError, UnicodeDecodeError):
                info["error"] = "invalid_response_schema"
                break
            if info["total_count"] is None:
                info["total_count"] = total
            elif total != info["total_count"]:
                info["error"] = "total_count_changed_during_pagination"
                break
            expected = min(page_size, total - info["observed_rows"])
            if len(items) != expected:
                info["error"] = "incomplete_or_oversized_page"
                break
            rows = [audit_offer(item, endpoint, metadata["fetched_at"], info["observed_rows"] + index)
                    for index, item in enumerate(items)]
            identities = [row.get("offer_id") for row in rows if row.get("offer_id")]
            if seen.intersection(identities) or len(set(identities)) != len(identities):
                info["error"] = "repeated_configuration_during_pagination"
                break
            seen.update(identities)
            audit["offers"].extend(rows)
            info["observed_rows"] += len(items)
            if info["observed_rows"] == total:
                info["complete"] = True
                break
        if not info["complete"] and "error" not in info:
            info["error"] = "page_limit_reached"
    audit["status"] = "collected_for_review" if all(info["complete"] for info in audit["endpoints"].values()) else "incomplete"
    audit["blocked_stage"] = "commercial_semantics_and_live_validation"
    audit["quarantine_counts"] = dict(Counter(reason for row in audit["offers"] for reason in row["quarantine_reasons"]))
    return audit
