"""Public Scaleway stock labels for each exact GPU instance SKU and zone.

The enum describes provider stock, not customer quota, instance/GPU quantities
or multi-node capacity. Sizes and GPU variants are never combined. Only an
explicit ``shortage`` enum establishes sold out for that exact SKU and zone.
"""
import json
import logging
import re
from datetime import datetime, timezone
from typing import List

from capacity.schema import AvailabilityRecord

logger = logging.getLogger(__name__)

ZONES = ["fr-par-1", "fr-par-2", "fr-par-3",
         "nl-ams-1", "nl-ams-2", "nl-ams-3",
         "pl-waw-1", "pl-waw-2", "pl-waw-3"]
API = "https://api.scaleway.com/instance/v1/zones/{zone}/products/servers/availability"
PARSER_VERSION = "scaleway-instance-stock-2.0"
PAGE_SIZE = 100
MAX_PAGES = 20

# Boundaries exclude similarly named, untracked models such as H1000 and L4.
_SKU_GPU = [
    (re.compile(r"^GB300(?=-|$)", re.I), "GB300"),
    (re.compile(r"^GB200(?=-|$)", re.I), "GB200"),
    (re.compile(r"^B300(?=-|$)", re.I), "B300"),
    (re.compile(r"^B200(?=-|$)", re.I), "B200"),
    (re.compile(r"^H200(?=-|$)", re.I), "H200"),
    (re.compile(r"^H100(?=-|$)", re.I), "H100"),
    (re.compile(r"^L40S(?=-|$)", re.I), "L40S"),
    (re.compile(r"^RTX-?PRO-?6000(?=-|$)", re.I), "RTX6000"),
]
_STATE = {"available": "available", "scarce": "limited", "shortage": "sold_out"}
_ERRORS = frozenset((
    "Scaleway response has no servers mapping",
    "Scaleway response has invalid pagination metadata",
    "Scaleway pagination repeated a SKU",
    "Scaleway pagination exceeded the page limit",
    "Scaleway instance has invalid SKU identity",
    "Scaleway instance has invalid GPU count",
    "Scaleway zone is invalid",
))


class ScalewayParseError(ValueError):
    """Static diagnostics do not interpolate a malformed response body."""

    def __init__(self, message):
        super().__init__(message if message in _ERRORS
                         else "Scaleway stock schema validation failed")


def _servers(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("servers"), dict):
        raise ScalewayParseError("Scaleway response has no servers mapping")
    # This API currently paginates via page/per_page and Link headers, not
    # opaque tokens. Do not silently truncate if that contract changes.
    if payload.get("next_page_token") or payload.get("next_token") or payload.get("next"):
        raise ScalewayParseError("Scaleway response has invalid pagination metadata")
    return payload["servers"]


def parse(payload, zone: str, fetched_at: str = "", source_url: str = "") -> List[AvailabilityRecord]:
    """Parse one complete page; every record keeps its exact source URL."""
    if zone not in ZONES:
        raise ScalewayParseError("Scaleway zone is invalid")
    records = []
    for sku, info in sorted(_servers(payload).items(), key=lambda pair: str(pair[0])):
        if not isinstance(sku, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,200}", sku):
            raise ScalewayParseError("Scaleway instance has invalid SKU identity")
        model = next((model for rx, model in _SKU_GPU if rx.match(sku)), None)
        if not model:
            continue
        # Provider SKU suffix is GPU count followed by per-GPU memory. Keeping
        # this distinction avoids treating the final 80G as an 80-GPU shape.
        match = re.search(r"-([1-9][0-9]*)-[1-9][0-9]*G(?:B)?$", sku, flags=re.I)
        if not match or int(match.group(1)) > 2 ** 31 - 1:
            raise ScalewayParseError("Scaleway instance has invalid GPU count")
        count = int(match.group(1))
        raw = info.get("availability") if isinstance(info, dict) else None
        state = _STATE.get(raw, "unknown") if isinstance(raw, str) else "unknown"
        # Preserve provider enums, including future well-formed unknown values,
        # without interpolating arbitrary or multiline response content.
        if isinstance(raw, str) and re.fullmatch(r"[a-z][a-z_]{0,63}", raw):
            evidence = f"Provider stock status: {raw}"
            if state == "unknown":
                evidence += " (unrecognized enum)"
        else:
            evidence = "Provider stock status missing or malformed"
        records.append(AvailabilityRecord(
            provider="scaleway", gpu_model=model, region=zone,
            consumption_type="on_demand", state=state,
            metric_type="instance_stock_status", metric_value=None,
            detail=(f"{evidence}; {count} GPUs per instance; "
                    "customer quota, stock quantity and multi-node availability not established"),
            instance_type=sku, gpu_count=count, product_scope="gpu_instance",
            fetched_at=fetched_at, source_url=source_url or API.format(zone=zone),
            data_source="official_api", parser_version=PARSER_VERSION,
        ))
    return records


def _fetch_zone(zone: str, fetched_at: str, http_get) -> List[AvailabilityRecord]:
    records, seen = [], set()
    for page in range(1, MAX_PAGES + 1):
        url = API.format(zone=zone) + f"?per_page={PAGE_SIZE}&page={page}"
        payload = json.loads(http_get(url, timeout=30))
        servers = _servers(payload)
        if seen.intersection(servers):
            # A server that ignores page numbers must not create a silently
            # partial snapshot or an infinite read loop.
            raise ScalewayParseError("Scaleway pagination repeated a SKU")
        seen.update(servers)
        records.extend(parse(payload, zone, fetched_at, source_url=url))
        if len(servers) < PAGE_SIZE:
            return records
    raise ScalewayParseError("Scaleway pagination exceeded the page limit")


def fetch() -> List[AvailabilityRecord]:
    from fetchers._http import http_get

    now = datetime.now(timezone.utc).isoformat()
    records, zones_ok = [], 0
    for zone in ZONES:
        try:
            zone_records = _fetch_zone(zone, now, http_get)
        except Exception as exc:
            # Do not publish any partial snapshot if one zone/page failed.
            # The pipeline may reuse a clearly stale exact-SKU cache instead.
            if isinstance(exc, ScalewayParseError):
                logger.warning("Scaleway %s: %s", zone, exc)
            else:
                logger.warning("Scaleway %s: request failed (%s)", zone, type(exc).__name__)
            continue
        zones_ok += 1
        records.extend(zone_records)
    if zones_ok != len(ZONES):
        logger.error("Scaleway: incomplete snapshot (%d/%d zones); discarding live rows",
                     zones_ok, len(ZONES))
        return []
    logger.info("Scaleway: %d exact SKU/zone records from %d complete zones", len(records), zones_ok)
    return sorted(records, key=lambda row: (row.gpu_model, row.instance_type, row.region))
