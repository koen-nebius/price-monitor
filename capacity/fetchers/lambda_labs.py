"""Lambda exact-instance launchability, without model/size or cluster inference.

The API lists regions where one exact instance type is currently launchable.
It reports no count of instances and no multi-node cluster availability. A
global summary counts regions only for that exact SKU; alternative shapes are
never combined. Only an explicit valid empty region list establishes sold out.
"""
import logging
import os
import re
from datetime import datetime, timezone
from typing import List

from capacity.schema import AvailabilityRecord, plural
from lambda_capacity_api import API_URL, API_KEY_ENV, LambdaAPIError, fetch_instance_types

logger = logging.getLogger(__name__)
SOURCE_URL = API_URL
PARSER_VERSION = "lambda-instance-capacity-2.0"
_GPU_FRAGMENTS = [
    ("gb300", "GB300"), ("gb200", "GB200"), ("b300", "B300"), ("b200", "B200"),
    ("h200", "H200"), ("h100", "H100"), ("l40s", "L40S"),
    ("rtx_pro_6000", "RTX6000"), ("rtxpro6000", "RTX6000"),
]
_ERRORS = frozenset((
    "Lambda instance response has no data mapping",
    "Lambda instance response returned unsupported pagination",
    "Lambda instance has invalid SKU identity",
    "Lambda instance entry is not an object",
    "Lambda instance has invalid type object",
    "Lambda instance name differs from SKU identity",
    "Lambda instance has invalid specs object",
    "Lambda instance has invalid GPU count",
    "Lambda instance GPU count differs from SKU identity",
))


class LambdaParseError(ValueError):
    """Static schema diagnostics that never interpolate response values."""

    def __init__(self, message):
        super().__init__(message if message in _ERRORS
                         else "Lambda instance schema validation failed")


def _valid_identity(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", value) is not None


def _match_gpu(instance_id: str):
    low = instance_id.lower()
    for token, model in _GPU_FRAGMENTS:
        if re.search(r"(?<![a-z0-9])" + re.escape(token) + r"(?![a-z0-9])", low):
            return model
    return None


def _regions(info):
    """Preserve valid positive regions; flag whether the complete list is usable."""
    raw = info.get("regions_with_capacity_available")
    if not isinstance(raw, list):
        return [], False
    names, complete = set(), True
    for entry in raw:
        name = entry.get("name") if isinstance(entry, dict) else None
        if not _valid_identity(name) or name.lower() == "global":
            complete = False
        else:
            names.add(name)
    return sorted(names), complete


def parse(payload, fetched_at: str = "") -> List[AvailabilityRecord]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise LambdaParseError("Lambda instance response has no data mapping")
    if payload.get("next_page_token") or payload.get("next_token") or payload.get("next"):
        raise LambdaParseError("Lambda instance response returned unsupported pagination")
    records = []
    for name in sorted(payload["data"], key=str):
        if not _valid_identity(name):
            raise LambdaParseError("Lambda instance has invalid SKU identity")
        gpu_model = _match_gpu(name)
        if not gpu_model:
            continue
        info = payload["data"][name]
        if not isinstance(info, dict):
            raise LambdaParseError("Lambda instance entry is not an object")
        instance = info.get("instance_type")
        if not isinstance(instance, dict):
            raise LambdaParseError("Lambda instance has invalid type object")
        if instance.get("name") != name:
            raise LambdaParseError("Lambda instance name differs from SKU identity")
        specs = instance.get("specs")
        if not isinstance(specs, dict):
            raise LambdaParseError("Lambda instance has invalid specs object")
        count = specs.get("gpus")
        if type(count) is not int or not 1 <= count <= 2 ** 31 - 1:
            raise LambdaParseError("Lambda instance has invalid GPU count")
        sku_count = re.match(r"gpu_(\d+)x_", name, flags=re.IGNORECASE)
        if not sku_count or int(sku_count.group(1)) != count:
            raise LambdaParseError("Lambda instance GPU count differs from SKU identity")
        regions, complete = _regions(info)
        common = dict(
            provider="lambda", gpu_model=gpu_model, consumption_type="on_demand",
            instance_type=name, fetched_at=fetched_at, source_url=SOURCE_URL,
            data_source="official_api", parser_version=PARSER_VERSION,
            product_scope="on_demand_instance", gpu_count=count,
        )
        if complete:
            state = "available" if regions else "sold_out"
            value = float(len(regions))
            evidence = (f"launchable in {plural(len(regions), 'region')}: {', '.join(regions)}"
                        if regions else "explicit empty launchable-region list")
        else:
            state, value = "unknown", None
            evidence = "launchable-region list missing, null or malformed; complete region count unknown"
        caveat = f"{count} GPUs per instance; instance count and multi-node availability not established"
        records.append(AvailabilityRecord(
            **common, region="global", state=state, metric_type="launchable_regions",
            metric_value=value, detail=f"Exact instance: {evidence}; {caveat}",
        ))
        for region in regions:
            records.append(AvailabilityRecord(
                **common, region=region, state="available", metric_type="instance_launchability",
                metric_value=1.0,
                detail=f"API lists this exact instance as launchable in this region; {caveat}",
            ))
    return records


def fetch() -> List[AvailabilityRecord]:
    if not os.environ.get(API_KEY_ENV, "").strip():
        logger.warning("Lambda capacity: LAMBDA_API_KEY not set — skipping")
        return []
    try:
        payload = fetch_instance_types()
        records = parse(payload, datetime.now(timezone.utc).isoformat())
    except Exception as exc:
        status = getattr(exc, "http_status", None)
        if type(status) is int and 100 <= status <= 599:
            logger.error("Lambda instance capacity fetch failed: HTTP %d", status)
        elif isinstance(exc, (LambdaParseError, LambdaAPIError)):
            logger.error("Lambda instance capacity fetch failed: %s", exc)
        else:
            logger.error("Lambda instance capacity fetch failed (%s)", type(exc).__name__)
        return []
    logger.info("Lambda instance capacity: %d exact instance/region records", len(records))
    return records
