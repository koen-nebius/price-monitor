"""Exact dedicated-inference replica headroom; never GPU-cluster capacity.

Preserve each instance type, explicit GPU count and provider region. Alternative
instance shapes can share physical capacity, so these quantities are not added
or reduced to a provider/model/global stock verdict.
"""
import logging
import os
import re
from dataclasses import replace
from datetime import datetime, timezone
from typing import List

from capacity.schema import AvailabilityRecord, plural
from together_capacity_api import API_URL, API_KEY_ENV, fetch_instance_types

logger = logging.getLogger(__name__)
API = SOURCE_URL = API_URL
PARSER_VERSION = "together-inference-capacity-2.0"
_RELATIONS = frozenset(("RELATION_EQ", "RELATION_GTE"))
_GPU_MAP = {
    "gb300": "GB300", "gb200": "GB200", "b300": "B300", "b200": "B200",
    "h200": "H200", "h100": "H100", "l40s": "L40S",
}
_ERRORS = frozenset((
    "Together inference response has no data array",
    "Together inference returned unsupported pagination",
    "Together inference instance is not an object",
    "Together inference has invalid GPU type",
    "Together inference has invalid instance identity",
    "Together inference has invalid GPU count",
    "Together inference instance configuration conflicts",
    "Together inference has invalid regions array",
    "Together inference has invalid region identity",
    "Together inference has invalid headroom object",
    "Together inference has invalid headroom value",
    "Together inference has invalid headroom relation",
))


class TogetherParseError(ValueError):
    """Only whitelisted static messages may be logged from parser failures."""

    def __init__(self, message):
        super().__init__(message if message in _ERRORS
                         else "Together inference schema validation failed")


def _match(gpu_type: str):
    low = gpu_type.lower()
    for token, model in _GPU_MAP.items():
        if re.search(r"(?<![a-z0-9])" + token + r"(?![a-z0-9])", low):
            return model
    if re.search(r"\brtx[ _-]+pro[ _-]+6000\b", low):
        return "RTX6000"
    return None


def _identity(value, message):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", value):
        raise TogetherParseError(message)
    return value


def _headroom(head):
    if head is None:
        head = {}
    if not isinstance(head, dict):
        raise TogetherParseError("Together inference has invalid headroom object")
    value, relation = head.get("value"), head.get("relation")
    if value is not None and (type(value) is not int or value < 0 or value > 2 ** 53 - 1):
        raise TogetherParseError("Together inference has invalid headroom value")
    if relation is None:
        relation = ""
    if not isinstance(relation, str) or (relation and not re.fullmatch(r"[A-Z_]{1,80}", relation)):
        raise TogetherParseError("Together inference has invalid headroom relation")
    signature = (value, relation)
    if value is None or relation not in _RELATIONS:
        return "unknown", None, relation, "headroom or its relation not established", signature
    if relation == "RELATION_GTE" and value == 0:
        return "unknown", None, relation, "headroom ≥0 replicas; positive availability not established", signature
    if value == 0:
        state = "sold_out"
    elif relation == "RELATION_EQ" and value <= 2:
        state = "limited"
    else:
        state = "available"
    detail = f"headroom {'≥' if relation == 'RELATION_GTE' else ''}{plural(value, 'replica')}"
    return state, float(value), relation, detail, signature


def parse(payload, fetched_at: str = "") -> List[AvailabilityRecord]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise TogetherParseError("Together inference response has no data array")
    if payload.get("next_page_token") or payload.get("next_token") or payload.get("next"):
        raise TogetherParseError("Together inference returned unsupported pagination")
    configs, grouped = {}, {}
    for item in payload["data"]:
        if not isinstance(item, dict):
            raise TogetherParseError("Together inference instance is not an object")
        gpu_type = item.get("gpuType")
        if not isinstance(gpu_type, str):
            raise TogetherParseError("Together inference has invalid GPU type")
        model = _match(gpu_type)
        if not model:
            continue
        instance = _identity(item.get("id"), "Together inference has invalid instance identity")
        count = item.get("gpuCount")
        if type(count) is not int or count < 1 or count > 2 ** 31 - 1:
            raise TogetherParseError("Together inference has invalid GPU count")
        config = (model, count)
        if instance in configs and configs[instance] != config:
            raise TogetherParseError("Together inference instance configuration conflicts")
        configs[instance] = config
        regions = item.get("regions")
        if not isinstance(regions, list):
            raise TogetherParseError("Together inference has invalid regions array")
        for region in regions:
            if not isinstance(region, dict):
                raise TogetherParseError("Together inference has invalid region identity")
            name = _identity(region.get("name"), "Together inference has invalid region identity")
            # "global" is a downstream aggregate sentinel, not a provider region.
            if name.lower() == "global":
                raise TogetherParseError("Together inference has invalid region identity")
            state, value, relation, detail, signature = _headroom(region.get("headroom"))
            record = AvailabilityRecord(
                provider="together", gpu_model=model, region=name,
                consumption_type="on_demand", state=state,
                metric_type="inference_replicas", metric_value=value,
                detail=f"Dedicated inference: {detail}; {count} GPUs per replica; not GPU-cluster capacity",
                instance_type=instance, fetched_at=fetched_at, source_url=SOURCE_URL,
                data_source="official_api", parser_version=PARSER_VERSION,
                product_scope="dedicated_inference", gpu_count=count,
                quantity_relation=relation,
            )
            grouped.setdefault((instance, name), {})[signature] = record
    records = []
    for key in sorted(grouped):
        variants = grouped[key]
        if len(variants) == 1:
            records.append(next(iter(variants.values())))
        else:
            # Validated but conflicting evidence has no defensible stock verdict.
            base = next(iter(variants.values()))
            candidates = sorted(
                f"{relation or 'relation missing'}:{value if value is not None else 'value missing'}"
                for value, relation in variants)
            records.append(replace(
                base, state="unknown", metric_value=None, quantity_relation="",
                detail=(f"Dedicated inference: conflicting headroom observations ({'; '.join(candidates)}); "
                        f"{base.gpu_count} GPUs per replica; no quantity or stock verdict; not GPU-cluster capacity"),
            ))
    return records


def fetch() -> List[AvailabilityRecord]:
    if not os.environ.get(API_KEY_ENV, "").strip():
        logger.warning("Together: TOGETHER_API_KEY not set — skipping inference capacity")
        return []
    try:
        payload = fetch_instance_types()
        records = parse(payload, datetime.now(timezone.utc).isoformat())
    except Exception as exc:
        status = getattr(exc, "http_status", None)
        if type(status) is int and 100 <= status <= 599:
            logger.error("Together inference fetch failed: HTTP %d", status)
        elif isinstance(exc, TogetherParseError):
            logger.error("Together inference fetch failed: %s", exc)
        else:
            logger.error("Together inference fetch failed (%s)", type(exc).__name__)
        return []
    logger.info("Together inference capacity: %d exact instance/region records", len(records))
    return records
