"""
Vast.ai capacity fetcher — public marketplace search, no auth.

GET https://console.vast.ai/api/v0/bundles/?q=<url-encoded JSON> (the exact
endpoint the vast CLI uses; POST fails validation, full Chrome UA required).
Depth of listed market per GPU model = offers × GPUs rentable right now.
This measures LISTED marketplace supply, not datacenter inventory — falling
depth means demand is absorbing supply OR hosts delisting; both are demand
signals worth seeing.

One query per model, limit 64 (the endpoint's hard page cap, verified).
Depth ≥64 offers is reported as "64+ (capped)" rather than paginated —
day-over-day trend matters more than the exact tail.
"""
import json
import logging
import urllib.parse
from datetime import datetime, timezone
from typing import List

from capacity.schema import AvailabilityRecord, plural

logger = logging.getLogger(__name__)

API = "https://console.vast.ai/api/v0/bundles/"
SOURCE_URL = "https://vast.ai/"

# Vast gpu_name values → our model ("RTX 6000 Ada" is the older Ada card, skipped)
GPU_NAME_MAP = {
    "H100 SXM": "H100",
    "H100 NVL": "H100",
    "H200": "H200",
    "H200 NVL": "H200",
    "B200": "B200",
    "B300": "B300",
    "GB200": "GB200",
    "L40S": "L40S",
    "RTX PRO 6000": "RTX6000",
    "RTX PRO 6000 WS": "RTX6000",
}

_LIMITED_MAX_GPUS = 8


def _search(gpu_names: List[str]) -> list:
    from fetchers._http import http_get
    q = {
        "gpu_name": {"in": gpu_names},
        "rentable": {"eq": True},
        "rented": {"eq": False},
        "type": "ask",
        "order": [["dph_total", "asc"], ["id", "asc"]],
        "limit": 64,
    }
    url = API + "?q=" + urllib.parse.quote(json.dumps(q))
    data = json.loads(http_get(url, timeout=45))
    return data.get("offers", [])


def fetch() -> List[AvailabilityRecord]:
    now = datetime.now(timezone.utc).isoformat()

    # Group vast names by our model so one query per MODEL covers its variants
    by_model: dict = {}
    for vname, model in GPU_NAME_MAP.items():
        by_model.setdefault(model, []).append(vname)

    records: List[AvailabilityRecord] = []
    for model, vnames in by_model.items():
        try:
            offers = _search(vnames)
        except Exception as e:
            logger.error(f"Vast search {model} failed: {e}")
            return []   # transient marketplace failure → serve cache

        n_offers = len(offers)
        n_gpus = sum(int(o.get("num_gpus") or 0) for o in offers)
        countries = sorted({(o.get("geolocation") or "").rsplit(",", 1)[-1].strip()
                            for o in offers if o.get("geolocation")})
        min_price = min((float(o.get("dph_total") or 0) / max(1, int(o.get("num_gpus") or 1))
                         for o in offers), default=0)

        capped = " (64-offer page cap hit — true depth larger)" if n_offers >= 64 else ""
        if n_gpus == 0:
            # Zero listings on a commodity marketplace is a structural absence
            # (nobody rents GB200 racks on Vast), not a sellout — rendering it
            # sold_out manufactured scarcity signal (red-team 2026-08-12).
            state, detail = "not_offered", "no offers listed (not traded on this marketplace)"
        elif n_gpus <= _LIMITED_MAX_GPUS:
            state = "limited"
            detail = (f"only {plural(n_gpus, 'GPU')} across {plural(n_offers, 'offer')}, "
                      f"min ${min_price:.2f}/GPU-hr")
        else:
            state = "available"
            detail = (f"{n_gpus} GPUs / {n_offers} offers{capped}, "
                      f"min ${min_price:.2f}/GPU-hr, "
                      f"{len(countries)} countr{'ies' if len(countries) != 1 else 'y'}")

        records.append(AvailabilityRecord(
            provider="vast", gpu_model=model, region="global",
            consumption_type="on_demand", state=state,
            metric_type="offer_depth_gpus", metric_value=float(n_gpus),
            detail=detail,
            fetched_at=now, source_url=SOURCE_URL, data_source="official_api",
        ))

    logger.info(f"Vast capacity: {len(records)} records")
    return records
