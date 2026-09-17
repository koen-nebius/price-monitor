"""Vultr's public GPU bare-metal catalogue, from the official plans API.

The API quotes an entire bare-metal plan per hour, with an explicit GPU count.
On-demand and preemptible prices are separate fields. A published number does
not mean the corresponding deployment mode is enabled, and ``locations`` lists
valid regions rather than a stock count. Preserve those restrictions through
``price_basis``; downstream ordinary PAYG comparisons must gate on that field.

Only explicitly hourly plans are included. Missing/unknown hardware, prices,
or billing units are never repaired from an old plan table or monthly price.
"""
import json
import logging
import math
import re
from datetime import datetime, timezone
from typing import List, Optional
from urllib.parse import urlencode

from fetchers._http import http_get
from schema import PriceRecord

logger = logging.getLogger(__name__)
API_URL = "https://api.vultr.com/v2/plans-metal"
SOURCE_URL = API_URL
PARSER_VERSION = "vultr-metal-price-1.0"
_MAX_PAGES = 20
_GPU_TYPES = {
    "NVIDIA_H100": ("H100", "unknown"),
    "NVIDIA_H100_SXM": ("H100", "SXM"),
    "NVIDIA_H100_PCIE": ("H100", "PCIe"),
    "NVIDIA_H100_NVL": ("H100", "NVL"),
    "NVIDIA_H200": ("H200", "unknown"),
    "NVIDIA_H200_SXM": ("H200", "SXM"),
    "NVIDIA_H200_NVL": ("H200", "NVL"),
    "NVIDIA_B200": ("B200", "unknown"),
    "NVIDIA_B300": ("B300", "unknown"),
    "NVIDIA_GB200": ("GB200", "unknown"),
    "NVIDIA_GB300": ("GB300", "unknown"),
    "NVIDIA_L40S": ("L40S", "PCIe"),
    "NVIDIA_RTX_PRO_6000_BLACKWELL": ("RTX6000", "PCIe"),
}
_REGION = re.compile(r"^[a-z][a-z0-9-]{1,31}$")


def _positive(value, integer=False):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        return None
    if integer and value != int(value):
        return None
    return int(value) if integer else float(value)


def _basis(plan: dict, flag: str, locations: list) -> str:
    enabled = plan.get(flag)
    if enabled is False:
        mode = "ondemand" if flag == "deploy_ondemand" else "preemptible"
        return "public_catalog_" + mode + "_disabled"
    if enabled is not True:
        return "public_catalog_deployment_unknown"
    if not locations:
        return "public_catalog_no_locations"
    return "public_catalog"


def normalize_plan(plan: dict, fetched_at: str = "") -> List[PriceRecord]:
    """Keep exact plans, deployment tiers and listed regions as distinct rows."""
    if not isinstance(plan, dict) or plan.get("invoice_type") != "hourly":
        return []
    # The documented API uses USD. Do not ignore a future explicit currency
    # field if the provider starts returning differently denominated plans.
    if "currency" in plan and plan["currency"] != "USD":
        return []
    sku = plan.get("id")
    gpu_type = plan.get("gpu_type")
    if not isinstance(sku, str) or not sku.startswith("vbm-"):
        return []
    if not isinstance(gpu_type, str) or gpu_type not in _GPU_TYPES:
        return []
    count = _positive(plan.get("gpu_count"), integer=True)
    if count is None:
        return []
    model, form_factor = _GPU_TYPES[gpu_type]
    # Source GPU count is authoritative. If a count is also present in the
    # plan ID, require agreement instead of manufacturing a fractional GPU.
    named_gpu = re.search(r"-(\d+)-(h100|h200|b200|b300|gb200|gb300|l40s?)-gpu$", sku)
    if named_gpu:
        named_model = named_gpu.group(2).upper()
        if named_model == "L40":  # Vultr's L40S plan retains its historical ID.
            named_model = "L40S"
        if int(named_gpu.group(1)) != count or named_model != model:
            return []
    locations = plan.get("locations")
    if not isinstance(locations, list) or any(
        not isinstance(region, str) or not _REGION.fullmatch(region)
        for region in locations
    ):
        return []
    locations = sorted(set(locations))
    regions = locations or ["unspecified"]
    ram_mb = _positive(plan.get("ram"))
    disk_gb = _positive(plan.get("disk"))
    disks = _positive(plan.get("disk_count"), integer=True)
    records = []
    for consumption, price_field, deploy_flag in (
        ("on_demand", "hourly_cost", "deploy_ondemand"),
        ("preemptible", "hourly_cost_preemptible", "deploy_preemptible"),
    ):
        hourly = _positive(plan.get(price_field))
        if hourly is None:
            continue
        basis = _basis(plan, deploy_flag, locations)
        for region in regions:
            records.append(PriceRecord(
                provider="vultr", gpu_model=model, gpu_count=count,
                instance_type=sku, region=region, consumption_type=consumption,
                price_per_hour_usd=hourly, price_per_gpu_hour_usd=hourly / count,
                # Bare metal: expose CPU threads, not physical cores, in the
                # legacy vcpu field. RAM is converted from the API's MB field.
                vcpu=_positive(plan.get("cpu_threads"), integer=True),
                ram_gb=ram_mb / 1024 if ram_mb is not None else None,
                storage_gb=disk_gb * disks if disk_gb and disks else None,
                node_gpus=count, form_factor=form_factor, interconnect="unknown",
                price_basis=basis, fetched_at=fetched_at, source_url=SOURCE_URL,
                data_source="official_api", parser_version=PARSER_VERSION,
            ))
    return records


def parse(payload: dict, fetched_at: str = "") -> List[PriceRecord]:
    plans = payload.get("plans_metal") if isinstance(payload, dict) else None
    if not isinstance(plans, list):
        raise ValueError("Vultr response has no plans_metal array")
    records = []
    seen = set()
    for plan in plans:
        for record in normalize_plan(plan, fetched_at):
            key = (record.instance_type, record.region, record.consumption_type)
            if key in seen:
                raise ValueError("Vultr response repeats an exact offer")
            seen.add(key)
            records.append(record)
    return records


def fetch_catalog() -> dict:
    """Read all pages or fail; next is an opaque cursor, never an arbitrary URL."""
    plans = []
    seen_ids, seen_cursors = set(), set()
    cursor = ""
    total = None
    for _ in range(_MAX_PAGES):
        params = {"per_page": 500}
        if cursor:
            params["cursor"] = cursor
        payload = json.loads(http_get(API_URL + "?" + urlencode(params), timeout=30))
        page = payload.get("plans_metal") if isinstance(payload, dict) else None
        meta = payload.get("meta") if isinstance(payload, dict) else None
        if not isinstance(page, list) or not isinstance(meta, dict):
            raise ValueError("Vultr response has no complete catalogue metadata")
        page_total, links = meta.get("total"), meta.get("links")
        if type(page_total) is not int or page_total < 0 or not isinstance(links, dict):
            raise ValueError("Vultr response has invalid pagination metadata")
        if total is not None and total != page_total:
            raise ValueError("Vultr catalogue changed during pagination")
        total = page_total
        for plan in page:
            if not isinstance(plan, dict) or not isinstance(plan.get("id"), str):
                raise ValueError("Vultr catalogue contains a malformed plan")
            if plan["id"] in seen_ids:
                raise ValueError("Vultr pagination repeats a plan")
            seen_ids.add(plan["id"])
            plans.append(plan)
        cursor = links.get("next")
        if not isinstance(cursor, str):
            raise ValueError("Vultr pagination cursor is missing or invalid")
        if not cursor:
            if len(plans) != total:
                raise ValueError("Vultr catalogue is incomplete")
            return {"plans_metal": plans}
        if not page or cursor in seen_cursors:
            raise ValueError("Vultr pagination did not advance")
        seen_cursors.add(cursor)
    raise ValueError("Vultr pagination exceeded page limit")


def fetch(regions: Optional[List[str]] = None) -> List[PriceRecord]:
    try:
        records = parse(fetch_catalog(), datetime.now(timezone.utc).isoformat())
        if regions is not None:
            records = [r for r in records if r.region in regions]
        logger.info("Vultr public bare-metal catalogue: %d price records", len(records))
        return records
    except Exception as exc:
        logger.error("Vultr pricing fetch failed (%s)", type(exc).__name__)
        return []
