"""
ComputePrices.com fetcher.
Pulls GPU pricing for providers NOT already covered by direct scrapers.
API docs: https://computeprices.com/docs/api

AUTH (changed upstream ~2026-07-09): the keyless tier was removed — every
/api/v1 call returns 401 without a key. Keys are free (email magic link at
https://computeprices.com/account/api-keys, 750 req/day; this fetcher uses
~16/run) and MUST be sent as an "Authorization: Bearer cp_live_..." header —
the API does not accept ?api_key= query params or X-API-Key headers.
Set the COMPUTEPRICES_API_KEY env var; without it every call 401s and the
pipeline serves the peer cache until the 7-day hard-stale drop.
"""
import json
import logging
import os
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from typing import Dict, List, Optional

from schema import PriceRecord

logger = logging.getLogger(__name__)

API_BASE = "https://computeprices.com/api/v1/gpu-prices"
SOURCE_URL = "https://computeprices.com"

# Provider/fetch key this fetcher registers under (matches the entry in
# config.PROVIDERS and the peer_cache.json key). Peer records produced here are
# prefixed "cp_" and stamped data_source="aggregator". When a live fetch fails the
# pipeline falls back to the cached copy under this key (store.get_cached_records),
# so freshness of THIS source must be guarded before it reaches the exec headline.
# main.py applies store.apply_cache_staleness_guard to cached records for this key.
FETCH_KEY = "computeprices"

# data_source label every record from this aggregator carries when LIVE/fresh.
# The staleness guard rewrites this to store.STALE_AGGREGATOR_DATA_SOURCE on
# SOFT-stale cache fallbacks so downstream can tell fresh aggregator data apart
# from stale-cached aggregator data.
DATA_SOURCE = "aggregator"

# Providers already scraped directly — skip them to avoid double-counting
SKIP_PROVIDERS = {
    "amazon aws",
    "google cloud",
    "microsoft azure",
    "coreweave",
    "lambda labs",
    "crusoe",
    "nebius",
    "hyperstack",
    "nexgencloud",
    "runpod",   # direct fetcher via runpod.py — skip to avoid double-counting
    "oracle cloud",   # direct fetcher via oracle.py (official_api) — prefer it over aggregator
    "oracle",
    "together ai",    # direct fetcher via together.py — aggregator mislabels its cluster rates
    "together",
    # Serverless / per-second inference platforms — NOT cluster-GPU competitors. Their
    # per-second or fractional rates read as implausible $/GPU-hr (Modal: H100 $0.07,
    # B200 $0.10), polluting the set and tripping the anomaly guard daily.
    "modal",
    "modal labs",
    # Cloud-DESKTOP product (browser Linux workstation with a dedicated GPU),
    # not GPU-compute rental — its per-seat $8.48 RTX PRO 6000 price polluted
    # the RTX market set and its tier rows flapped as fake ±24-60% "moves"
    # (Krenev's catch, 2026-08-24). Same category exclusion as Modal.
    "hinode",
    # Distressed — prices unreliable, would pollute the benchmark:
    "genesis cloud",   # in liquidation ("GmbH i.L." since Aug 2025) yet still lists prices
    "genesis",
}

# ComputePrices GPU name → our normalized model name
# Only include GPUs we track; everything else is ignored.
GPU_NAME_MAP = {
    "h100 sxm":  "H100",
    "h100 pcie": "H100",
    "h100 nvl":  "H100",
    "gh200":     "H100",   # Grace Hopper = H100 architecture
    "h200":      "H200",
    "b200":      "B200",
    "hgx b300":  "B300",
    "gb200":     "GB200",
    "gb300":     "GB300",
    "l40s":      "L40S",
    "rtx pro 6000":          "RTX6000",   # Blackwell PRO 6000 96GB — match Nebius's card,
    "rtx pro 6000 blackwell":"RTX6000",   # NOT RTX 6000 Ada (different/older 48GB card)
}

# GPU slugs to query (one request per slug keeps responses small)
# Note: B300 uses slug "hgx-b300" on ComputePrices, not "b300"
GPU_SLUGS = ["h100", "h200", "b200", "hgx-b300", "gb200", "gb300", "l40s", "rtx-pro-6000"]


# ComputePrices provider name → our direct-fetch provider key. Used by the 1.9
# cross-check: these providers are skipped in the benchmark (we fetch them directly),
# but ComputePrices is a useful INDEPENDENT second source to validate our numbers.
_XCHECK_NAME_MAP = {
    "amazon aws": "aws", "aws": "aws",
    "google cloud": "gcp", "gcp": "gcp",
    "microsoft azure": "azure", "azure": "azure",
    "coreweave": "coreweave",
    "lambda labs": "lambda",
    "crusoe": "crusoe",
    "nebius": "nebius",
    "oracle cloud": "oracle", "oracle": "oracle",
    "hyperstack": "hyperstack", "nexgencloud": "hyperstack",
    "runpod": "runpod",
}


# Cross-check rows older than this are ignored: ComputePrices relays some prices
# from third-party directories (e.g. shadeform referral links) that can freeze for
# days while the provider's own page moves. A stale relay is not a valid check —
# on 2026-07-14 frozen $1.90 shadeform rows (last_updated 07-08) flagged our
# CORRECT $2.50 Hyperstack scrape for a week after Hyperstack repriced +30%.
_CROSSCHECK_MAX_AGE_DAYS = 3


def fetch_crosscheck() -> Dict[tuple, float]:
    """
    Phase 1.9: return {(direct_provider_key, gpu_model): cheapest FRESH on_demand
    $/GPU-hr} from ComputePrices for the providers we fetch DIRECTLY — an
    independent second source to validate our primary numbers. Rows with a
    last_updated older than _CROSSCHECK_MAX_AGE_DAYS are skipped (stale relays);
    rows without the field are kept. Does NOT enter the benchmark. Graceful:
    returns {} on any network/parse failure.
    """
    api_key = os.environ.get("COMPUTEPRICES_API_KEY")
    out: Dict[tuple, float] = {}
    for slug in GPU_SLUGS:
        try:
            url = f"{API_BASE}?{urllib.parse.urlencode({'gpu': slug})}"
            req = urllib.request.Request(url, headers=_headers(api_key))
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
        except Exception as e:
            logger.warning(f"crosscheck slug={slug} failed: {e}")
            continue
        for item in data.get("data", []):
            key_prov = _XCHECK_NAME_MAP.get((item.get("provider", "") or "").lower())
            if not key_prov:
                continue
            if (item.get("pricing_type") or "on_demand") != "on_demand":
                continue
            lu = item.get("last_updated")
            if lu:
                try:
                    age = datetime.now(timezone.utc) - datetime.fromisoformat(lu)
                    if age.days > _CROSSCHECK_MAX_AGE_DAYS:
                        continue
                except (ValueError, TypeError):
                    pass
            gpu_model = GPU_NAME_MAP.get((item.get("gpu", "") or "").lower())
            if not gpu_model:
                continue
            gc = item.get("gpu_count") or 1
            total = item.get("total_hourly_usd") or 0
            pph = item.get("price_per_hour_usd") or 0
            px = (total / gc) if total > 0 else pph
            if px <= 0:
                continue
            k = (key_prov, gpu_model)
            if k not in out or px < out[k]:
                out[k] = px
    return out


def _headers(api_key: Optional[str]) -> dict:
    headers = {"User-Agent": "Mozilla/5.0 (price-monitor/1.0)"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def fetch(regions: List[str] = None) -> List[PriceRecord]:
    now = datetime.now(timezone.utc).isoformat()
    api_key = os.environ.get("COMPUTEPRICES_API_KEY")
    if not api_key:
        logger.warning(
            "COMPUTEPRICES_API_KEY not set — the API requires a key since 2026-07-09, "
            "all calls will 401 (free key: https://computeprices.com/account/api-keys)"
        )

    records = []
    seen: set = set()

    for slug in GPU_SLUGS:
        try:
            slug_records = _fetch_slug(slug, api_key, now, seen)
            records.extend(slug_records)
        except Exception as e:
            logger.warning(f"ComputePrices slug={slug} failed: {e}")

    # Deduplicate: for each (provider, gpu_model, ct), keep the cheapest per-GPU price.
    # Some providers have incorrect total_hourly_usd values that scale non-linearly with
    # gpu_count (e.g. UpCloud H100), causing inflated per-GPU prices for multi-GPU nodes.
    # Keeping the minimum ensures the executive table and diff log reflect the real price.
    best: Dict[tuple, PriceRecord] = {}
    for r in records:
        key = (r.provider, r.gpu_model, r.consumption_type)
        if key not in best or r.price_per_gpu_hour_usd < best[key].price_per_gpu_hour_usd:
            best[key] = r
    records = list(best.values())

    # Sanity filter: drop reserved records where price > on_demand for the same provider+GPU.
    # ComputePrices occasionally returns inverted reserved pricing (e.g. Gcore H100 reserved_3yr
    # at $16.24 vs on_demand $1.78). These are data quality issues in the upstream source.
    od_prices: Dict[tuple, float] = {
        (r.provider, r.gpu_model): r.price_per_gpu_hour_usd
        for r in records if r.consumption_type == "on_demand"
    }
    filtered = []
    for r in records:
        if "reserved" in r.consumption_type or "committed" in r.consumption_type:
            od = od_prices.get((r.provider, r.gpu_model))
            if od is not None and r.price_per_gpu_hour_usd > od:
                logger.warning(
                    f"  computeprices: dropping inverted reserved price — "
                    f"{r.provider} {r.gpu_model} {r.consumption_type} "
                    f"${r.price_per_gpu_hour_usd:.2f} > on_demand ${od:.2f}"
                )
                continue
        filtered.append(r)
    records = filtered

    logger.info(f"ComputePrices: {len(records)} records from {len(GPU_SLUGS)} GPU slugs")
    return records


def _fetch_slug(
    slug: str,
    api_key: Optional[str],
    now: str,
    seen: set,
) -> List[PriceRecord]:
    url = f"{API_BASE}?{urllib.parse.urlencode({'gpu': slug})}"

    req = urllib.request.Request(url, headers=_headers(api_key))
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())

    records = []
    for item in data.get("data", []):
        provider_name = item.get("provider", "")
        if provider_name.lower() in SKIP_PROVIDERS:
            continue

        gpu_label = item.get("gpu", "").lower()
        gpu_model = GPU_NAME_MAP.get(gpu_label)
        if gpu_model is None:
            continue

        # Use total_hourly_usd / gpu_count as the authoritative per-GPU price.
        # ComputePrices `price_per_hour_usd` is per-GPU for most providers but
        # some (e.g. UpCloud) return total node price, causing it to scale
        # linearly with gpu_count. total_hourly_usd / gpu_count is always correct.
        gpu_count = item.get("gpu_count") or 1
        total_usd = item.get("total_hourly_usd") or 0
        price_per_hour_usd_field = item.get("price_per_hour_usd") or 0

        if total_usd > 0:
            price_usd = total_usd / gpu_count   # true per-GPU price
        elif price_per_hour_usd_field > 0:
            price_usd = price_per_hour_usd_field
        else:
            continue

        if price_usd <= 0:
            continue
        pricing_type = item.get("pricing_type", "on_demand")
        commitment_months = item.get("commitment_months")

        ct = _map_consumption_type(pricing_type, commitment_months)
        if ct is None:
            continue

        provider_slug = item.get("provider_slug", provider_name.lower().replace(" ", "_"))
        source = item.get("source_url") or SOURCE_URL

        # Region: ComputePrices doesn't expose region per-record, use provider slug as proxy
        region = "global"

        key = (provider_slug, gpu_model, gpu_count, ct)
        if key in seen:
            # Keep cheapest when same provider/gpu/count/type appears twice
            continue
        seen.add(key)

        records.append(PriceRecord(
            provider=f"cp_{provider_slug}",   # prefix to distinguish from direct scrapers
            gpu_model=gpu_model,
            gpu_count=gpu_count,
            instance_type=f"{provider_slug}-{gpu_model.lower()}-{gpu_count}x",
            region=region,
            consumption_type=ct,
            price_per_hour_usd=price_usd * gpu_count,
            price_per_gpu_hour_usd=price_usd,
            fetched_at=now,
            source_url=source,
            data_source=DATA_SOURCE,
        ))

    return records


def _map_consumption_type(pricing_type: str, commitment_months: Optional[int]) -> Optional[str]:
    pt = pricing_type.lower()
    if pt == "spot":
        return "spot"
    if pt == "on_demand":
        return "on_demand"
    if pt == "reserved":
        if commitment_months is None:
            return "on_demand"
        # Short-term committed capacity (<= 6mo): meaningful data but not comparable
        # to standard 1yr/2yr/3yr buckets — store separately.
        if commitment_months <= 6:
            return "committed_short_term"
        if commitment_months <= 12:
            return "reserved_1yr"    # canonical 1yr bucket
        if commitment_months <= 24:
            return "committed_2yr"   # canonical 2yr bucket (Nebius also uses this)
        if commitment_months <= 36:
            return "reserved_3yr"    # canonical 3yr bucket
        if commitment_months <= 48:
            return "committed_4yr"   # kept for reference (e.g. Vultr B200 48mo)
        return None
    return None
