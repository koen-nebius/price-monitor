"""
Shadeform multi-cloud GPU marketplace -> aggregator records (provider prefix "sf_").

Source (verified 2026-09-15): GET https://api.shadeform.ai/v1/instances/types returns
JSON {"instance_types": [...]} with 298 instance types across 19 clouds; each item
carries cloud, gpu_type, num_gpus, hourly_price (integer USD CENTS per INSTANCE-hour,
on-demand), interconnect (pcie|sxm4|sxm5|sxm6|""), nvlink, vcpus, memory_in_gb,
storage_in_gb, and availability[] entries {region, display_name, available,
rental_type (on_demand|spot), hourly_price (spot only, string USD per instance-hour)}.

Gating (terms of use): Shadeform's Terms of Service s.4.2(1) prohibit "systematically
retrieving data ... to create databases" without written permission, and the docs
list X-API-KEY as required even though the endpoint answers without one today. So
this fetcher runs ONLY when SHADEFORM_API_KEY is set (a free key issued after asking
support@shadeform.ai for permission) and sends it on every call; without the key the
provider is not even registered (config.PROVIDERS mirrors the sfcompute pattern).

Comparability: prices are the underlying providers' public list prices (Shadeform
charges the provider's rate; Nebius rows carry a flat +$0.13/instance-hr fee). Clouds
we already fetch directly or via ComputePrices are SUPERSEDED in main.py so each
provider is counted once; the net-new coverage is the long tail (BoostRun, IMWT,
Horizon, Phyntec, Amaya) plus per-region LIVE availability, which the capacity
monitor can consume later. "excesssupply" is Shadeform's own resale pool of partner
capacity (duplicates other clouds' configs) and is skipped.
"""
import json
import logging
import os
import urllib.request
from datetime import datetime, timezone
from typing import Dict, List, Optional

from schema import PriceRecord

logger = logging.getLogger(__name__)

API_URL = "https://api.shadeform.ai/v1/instances/types"
DIRECTORY_URL = "https://shadeform.com/directory/clouds/{cloud}"
DATA_SOURCE = "aggregator"

SKIP_CLOUDS = {"excesssupply"}          # Shadeform resale pool, not a distinct provider

# Shadeform gpu_type -> our gpu_model. Unlisted (A100_80G, H100_nvl, GH200, RTX6000Ada,
# RTX5090, L40, CPU ...) are ignored: different cards or not tracked.
GPU_MAP = {
    "H100": "H100",
    "H200": "H200",
    "B200": "B200",
    "B300": "B300",
    "GB200": "GB200",
    "GB300": "GB300",
    "L40S": "L40S",
    "RTXPro6000": "RTX6000",
}


def _form_factor(interconnect: str) -> str:
    ic = (interconnect or "").lower()
    if ic.startswith("sxm"):
        return "SXM"
    if ic.startswith("pcie"):
        return "PCIe"
    return "unknown"


def parse(items: List[dict], now: str) -> List[PriceRecord]:
    """Marketplace items -> cheapest record per (provider, gpu_model, consumption_type)."""
    best: Dict[tuple, PriceRecord] = {}

    def offer(cloud, gpu, ct, per_gpu, count, item, region, avail_url):
        if not (0.10 <= per_gpu <= 100):
            return
        rec = PriceRecord(
            provider=f"sf_{cloud}",
            gpu_model=gpu,
            gpu_count=count,
            instance_type=str(item.get("shade_instance_type") or item.get("cloud_instance_type") or f"{gpu}x{count}"),
            region=region,
            consumption_type=ct,
            price_per_hour_usd=round(per_gpu * count, 4),
            price_per_gpu_hour_usd=round(per_gpu, 4),
            vcpu=item.get("vcpus"),
            ram_gb=item.get("memory_in_gb"),
            fetched_at=now,
            source_url=avail_url,
            data_source=DATA_SOURCE,
            interconnect="NVLink" if item.get("nvlink") else "unknown",
            form_factor=_form_factor(item.get("interconnect", "")),
            node_gpus=count,
        )
        k = (rec.provider, gpu, ct)
        if k not in best or per_gpu < best[k].price_per_gpu_hour_usd:
            best[k] = rec

    for item in items:
        cloud = str(item.get("cloud") or "").strip().lower()
        if not cloud or cloud in SKIP_CLOUDS:
            continue
        gpu = GPU_MAP.get(str(item.get("gpu_type") or ""))
        if not gpu:
            continue
        try:
            count = int(item.get("num_gpus") or 0)
            cents = float(item.get("hourly_price") or 0)
        except (TypeError, ValueError):
            continue
        if count <= 0:
            continue
        avail = item.get("availability") or []
        regions = [a.get("display_name") or a.get("region") for a in avail if a.get("rental_type", "on_demand") == "on_demand"]
        region = regions[0] if regions else "global"
        url = DIRECTORY_URL.format(cloud=cloud)
        if cents > 0:
            offer(cloud, gpu, "on_demand", cents / 100.0 / count, count, item, region, url)
        for a in avail:
            if a.get("rental_type") != "spot":
                continue
            try:
                spot_instance = float(a.get("hourly_price") or 0)   # dollars per instance-hour
            except (TypeError, ValueError):
                continue
            if spot_instance > 0:
                offer(cloud, gpu, "spot", spot_instance / count, count, item,
                      a.get("display_name") or a.get("region") or region, url)
    return list(best.values())


def fetch(regions: Optional[List[str]] = None) -> List[PriceRecord]:
    api_key = os.environ.get("SHADEFORM_API_KEY")
    if not api_key:
        logger.warning("SHADEFORM_API_KEY not set — Shadeform fetch skipped (ToS s.4.2 requires "
                       "permission/key for systematic retrieval; ask support@shadeform.ai)")
        return []
    now = datetime.now(timezone.utc).isoformat()
    req = urllib.request.Request(API_URL, headers={
        "X-API-KEY": api_key,
        "User-Agent": "nebius-price-monitor/1.0 (koen@nebius.com)",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read().decode("utf-8"))
    items = data.get("instance_types", data if isinstance(data, list) else [])
    records = parse(items, now)
    clouds = sorted({r.provider for r in records})
    logger.info(f"Shadeform: {len(records)} records from {len(items)} instance types, "
                f"{len(clouds)} clouds: {', '.join(clouds)}")
    return records
