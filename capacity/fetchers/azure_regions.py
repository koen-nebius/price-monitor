"""
Azure GPU offering fetcher — Retail Prices API region footprint per SKU.

For each tracked ND-series SKU, the (no-auth) Retail Prices API returns the
set of armRegionNames with a published on-demand or spot meter. Semantics:
STATIC OFFERING footprint — where the SKU is commercially listed, which can
overstate physical deployment. Azure's genuine live signals (Resource SKUs
restrictions, Spot Placement Scores) both need a subscription + AAD token;
this fetcher upgrades transparently if that access ever lands.

Also runs a new-SKU tripwire: a contains() query for GB300/B300 SKU names
(zero rows as of 2026-08-12) so the day Azure lists them we emit records.
"""
import json
import logging
import urllib.parse
from datetime import datetime, timezone
from typing import List, Set

from capacity.schema import AvailabilityRecord, plural

logger = logging.getLogger(__name__)

API = "https://prices.azure.com/api/retail/prices"
SOURCE_URL = "https://azure.microsoft.com/en-us/pricing/details/virtual-machines/"

SKU_GPU_MAP = {
    "Standard_ND96isr_H100_v5": "H100",
    "Standard_ND96isr_H200_v5": "H200",
    "Standard_ND96isrf_H200_v5": "H200",
    "Standard_ND128isr_NDR_GB200_v6": "GB200",
    "Standard_ND128isrf_NDR_GB200_v6": "GB200",
}

# armSkuName substrings that would signal a newly listed generation
TRIPWIRE_FRAGMENTS = {"GB300": "GB300", "B300": "B300", "Rubin": "VR"}

_LIMITED_MAX_REGIONS = 2


def _query(filt: str) -> list:
    from fetchers._http import http_get
    items, url = [], f"{API}?$filter={urllib.parse.quote(filt)}"
    for _ in range(5):   # page cap — footprint queries are small
        data = json.loads(http_get(url, timeout=45))
        items.extend(data.get("Items", []))
        url = data.get("NextPageLink")
        if not url:
            break
    return items


def fetch() -> List[AvailabilityRecord]:
    now = datetime.now(timezone.utc).isoformat()
    records: List[AvailabilityRecord] = []

    for sku, gpu_model in SKU_GPU_MAP.items():
        try:
            items = _query(f"armSkuName eq '{sku}' and priceType eq 'Consumption'")
        except Exception as e:
            logger.error(f"Azure retail prices {sku}: {e}")
            return []   # transient API failure → let the cache serve

        od_regions: Set[str] = set()
        spot_regions: Set[str] = set()
        for it in items:
            region = it.get("armRegionName") or ""
            if not region or region == "Global":
                continue
            if "Spot" in (it.get("skuName") or "") or "Spot" in (it.get("meterName") or ""):
                spot_regions.add(region)
            elif "Low Priority" not in (it.get("skuName") or ""):
                od_regions.add(region)

        for ct, regions in (("on_demand", od_regions), ("spot", spot_regions)):
            if not regions:
                continue
            n = len(regions)
            state = "limited" if n <= _LIMITED_MAX_REGIONS else "available"
            records.append(AvailabilityRecord(
                provider="azure", gpu_model=gpu_model, region="global",
                consumption_type=ct, state=state,
                metric_type="listed_offering", metric_value=float(n),
                detail=f"{sku}: priced in {plural(n, 'region')} (offering, can overstate deployment)",
                instance_type=sku,
                fetched_at=now, source_url=SOURCE_URL, data_source="official_api",
            ))

    # New-generation tripwire (cheap: one contains() query per fragment)
    for frag, gpu_model in TRIPWIRE_FRAGMENTS.items():
        try:
            items = _query(f"contains(armSkuName, '{frag}') and priceType eq 'Consumption'")
        except Exception as e:
            logger.warning(f"Azure tripwire {frag}: {e}")
            continue
        nd_items = [it for it in items if (it.get("armSkuName") or "").startswith("Standard_ND")]
        if nd_items:
            skus = sorted({it["armSkuName"] for it in nd_items})
            regions = sorted({it.get("armRegionName") for it in nd_items if it.get("armRegionName")})
            records.append(AvailabilityRecord(
                provider="azure", gpu_model=gpu_model, region="global",
                consumption_type="on_demand", state="limited",
                metric_type="listed_offering", metric_value=float(len(regions)),
                detail=f"NEW SKU LISTED: {', '.join(skus[:3])} in {plural(len(regions), 'region')}",
                instance_type=skus[0],
                fetched_at=now, source_url=SOURCE_URL, data_source="official_api",
            ))
            logger.warning(f"Azure tripwire hit: {frag} → {skus}")

    # De-duplicate same (gpu, ct) from isr/isrf twins — keep the wider footprint
    best = {}
    for r in records:
        key = (r.gpu_model, r.consumption_type, r.region)
        if key not in best or (r.metric_value or 0) > (best[key].metric_value or 0):
            best[key] = r
    out = list(best.values())
    logger.info(f"Azure regions: {len(out)} records")
    return out
