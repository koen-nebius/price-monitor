"""
Verda (ex-DataCrunch, verda.com) fetcher — public no-auth instance-types API.

Added 2026-08-11 from the B300/GB300/Vera-Rubin price research: Verda is the
only provider besides Oracle publishing a machine-readable GB300 price
(OD $8.62 / spot $4.31 per GPU at launch), plus B300, B200, H200, H100, L40S
and RTX PRO 6000 — all in USD, all with spot. Finnish DC footprint
(ex-DataCrunch); listed on getdeploying/gpus.io with matching numbers.

API: GET https://api.verda.com/v1/instance-types  (no auth, JSON list; prices
are strings, gpu.number_of_gpus carries the count; per-instance pricing —
divide by count). Verified 2026-08-11: multi-GPU sizes price linearly, so we
keep the largest config per (gpu, ct) to represent node-scale pricing without
changing the per-GPU rate.
"""
import json
import logging
import urllib.request
from datetime import datetime, timezone
from typing import List

from schema import PriceRecord

logger = logging.getLogger(__name__)

API = "https://api.verda.com/v1/instance-types"
SOURCE_URL = "https://verda.com/pricing"

# Verda `name` → our model. "RTX 6000 Ada" deliberately absent (older 48GB Ada
# card, NOT the Blackwell RTX PRO 6000 — same trap as the ComputePrices map);
# "RTX PRO 6000 CC" (confidential-compute variant) also skipped: same silicon,
# special config, would double-count against the standard card.
GPU_NAME_MAP = {
    "GB300 SXM6 288GB": "GB300",
    "B300 SXM6 268GB":  "B300",
    "B200 SXM6 180GB":  "B200",
    "H200 SXM5 141GB":  "H200",
    "H100 SXM5 80GB":   "H100",
    "L40S 48GB":        "L40S",
    "RTX PRO 6000 96GB": "RTX6000",
}


def fetch(regions: List[str] = None) -> List[PriceRecord]:
    now = datetime.now(timezone.utc).isoformat()
    try:
        req = urllib.request.Request(API, headers={"User-Agent": "Mozilla/5.0 (price-monitor/1.0)"})
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.load(resp)
    except Exception as e:
        logger.error(f"Verda fetch failed: {e}")
        return []

    items = data if isinstance(data, list) else data.get("data", [])
    # Largest config per (gpu_model, ct) — per-GPU rate is linear across sizes.
    best: dict = {}
    node_size: dict = {}   # gpu_model → largest dedicated config in the payload
    for it in items:
        gpu_model = GPU_NAME_MAP.get(it.get("name") or "")
        if not gpu_model:
            continue
        if (it.get("currency") or "usd").lower() != "usd":
            logger.warning(f"Verda: non-USD row skipped ({it.get('instance_type')})")
            continue
        n = (it.get("gpu") or {}).get("number_of_gpus") or 0
        try:
            od = float(it.get("price_per_hour") or 0)
            sp = float(it.get("spot_price") or 0)
        except (ValueError, TypeError):
            continue
        if n <= 0:
            continue
        # Specs from the same row: cpu.number_of_cores is Verda's vCPU count (equals
        # the ".NV" suffix of instance_type, e.g. 8B300.240V → 240; Shadeform lists
        # the same SKU as vcpus=240), so no ×2. memory.size_in_gigabytes = system
        # RAM of this SKU, GB as published. Missing/invalid → None, never inferred.
        try:
            vcpu = int((it.get("cpu") or {}).get("number_of_cores") or 0) or None
            ram_gb = float((it.get("memory") or {}).get("size_in_gigabytes") or 0) or None
        except (ValueError, TypeError, AttributeError):   # AttributeError: cpu/memory not a dict
            vcpu = ram_gb = None
        node_size[gpu_model] = max(node_size.get(gpu_model, 0), n)
        for ct, price in (("on_demand", od), ("spot", sp)):
            if price <= 0:
                continue
            per_gpu = price / n
            if per_gpu < 0.10 or per_gpu > 30:   # unit-error guard
                logger.warning(f"Verda: implausible ${per_gpu:.2f}/GPU-hr for "
                               f"{it.get('instance_type')} {ct} — skipped")
                continue
            key = (gpu_model, ct)
            if key not in best or n > best[key][0]:
                best[key] = (n, per_gpu, price, it.get("instance_type", ""), vcpu, ram_gb)

    records = []
    for (gpu_model, ct), (n, per_gpu, total, itype, vcpu, ram_gb) in best.items():
        records.append(PriceRecord(
            provider="verda",
            gpu_model=gpu_model,
            gpu_count=n,
            instance_type=itype,
            region="fi-01",   # Finnish DCs (ex-DataCrunch); API has no per-region prices
            consumption_type=ct,
            price_per_hour_usd=total,
            price_per_gpu_hour_usd=round(per_gpu, 4),
            vcpu=vcpu,
            ram_gb=ram_gb,
            # Verda sells dedicated hosts; the largest size it lists for this GPU in
            # the payload (8x HGX, 4x GB300 tray) is the node — same premise as the
            # largest-config rule above. Not an explicit node-size field.
            node_gpus=node_size.get(gpu_model) or None,
            fetched_at=now,
            source_url=SOURCE_URL,
            data_source="official_api",
        ))
    if records:
        by_gpu = sorted({f"{g} {'/'.join(c for (gg, c) in best if gg == g)}"
                         for (g, _c) in best})
        logger.info(f"Verda: {len(records)} records ({', '.join(by_gpu)})")
    else:
        logger.warning("Verda: no matching GPU instance types")
    return records
