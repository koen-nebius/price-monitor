"""
ComputePrices.com fetcher.
Pulls distinct GPU offers, retaining their source and comparison dimensions.
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
import hashlib
import logging
import math
import os
import re
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

# Known product exclusions and existing direct-source exclusions. CoreWeave is
# retained as an independent offer source; assembly resolves exact offer overlap.
SKIP_PROVIDERS = {
    "amazon aws",
    "google cloud",
    "microsoft azure",
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
    "vultr",         # public direct API preserves node prices and deployment restrictions
    "vultr cloud",   # aggregator starting-at rates are not verified PAYG offers
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
    "h100":      "H100",
    "h100 80gb": "H100",   # API documentation label; variant remains explicit/unknown
    "h100 sxm":  "H100",
    "h100 pcie": "H100",
    "h100 nvl":  "H100",
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
            amounts = _price_amounts(item)
            if amounts is None:
                continue
            _, _, px = amounts
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

    # Keep offers, not a cheapest-provider summary. Different regions, sizes,
    # offering tiers and commitment terms are distinct comparison populations.
    # A more expensive reserved offer is retained: price ordering alone cannot
    # establish an upstream error or justify deleting a different configuration.
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

    return parse(data.get("data", []), now, seen)


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _positive_number(value) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _positive_integer(value) -> Optional[int]:
    number = _positive_number(value)
    return int(number) if number is not None and number.is_integer() else None


def _price_amounts(item):
    """Validate the documented integer count and USD amounts without defaults."""
    count = _positive_integer(item.get("gpu_count"))
    if count is None:
        return None
    raw_per_gpu = item.get("price_per_hour_usd")
    if raw_per_gpu is not None and _positive_number(raw_per_gpu) is None:
        return None
    raw_total = item.get("total_hourly_usd")
    if raw_total is not None:
        total = _positive_number(raw_total)
        if total is None:
            return None
        per_gpu = total / count
    else:
        per_gpu = _positive_number(raw_per_gpu)
        if per_gpu is None:
            return None
        total = per_gpu * count
    if not math.isfinite(total) or not math.isfinite(per_gpu) or per_gpu <= 0:
        return None
    return count, total, per_gpu


def parse(items: list, now: str, seen: Optional[set] = None) -> List[PriceRecord]:
    """Normalize offer rows without pooling shapes, regions, tiers or terms.

    The public OpenAPI defines ``variant`` as the provider's offering tier, not
    GPU form factor. ``last_updated`` is upstream observation time, while ``now``
    records our retrieval time. Neither a fresh fetch nor a missing stock signal
    establishes availability.
    """
    seen = set() if seen is None else seen
    records = []
    for item in items:
        if not isinstance(item, dict):
            continue
        provider_name = _text(item.get("provider"))
        if not provider_name:
            continue
        if provider_name.lower() in SKIP_PROVIDERS:
            continue

        gpu_label = _text(item.get("gpu")).lower()
        gpu_model = GPU_NAME_MAP.get(gpu_label)
        if gpu_model is None:
            continue

        amounts = _price_amounts(item)
        if amounts is None:
            continue
        gpu_count, total_usd, price_usd = amounts
        pricing_type = _text(item.get("pricing_type"))
        commitment_months = _positive_integer(item.get("commitment_months"))

        ct = _map_consumption_type(pricing_type, commitment_months)
        if ct is None:
            continue

        provider_slug = _text(item.get("provider_slug")) or provider_name.lower().replace(" ", "_")
        source = _text(item.get("source_url")) or SOURCE_URL
        region = _text(item.get("region")) or "unspecified"
        variant = _text(item.get("variant"))
        gpu_variant = _text(item.get("gpu"))

        # Node size comes from the row's own max_gpus_per_node — GPUs in the physical
        # node this SKU is carved from (a 1-GPU slice of an 8-GPU host carries 8; a
        # whole host carries its own count). The payload has NO vCPU or system-RAM
        # field (vram_gb is per-GPU memory, not RAM), so vcpu/ram_gb stay None.
        node_gpus = _positive_integer(item.get("max_gpus_per_node"))
        # A node can't be smaller than the slice priced from it; treat an
        # upstream contradiction as unknown so schema falls back to gpu_count.
        if node_gpus is not None and isinstance(gpu_count, (int, float)) and node_gpus < gpu_count:
            node_gpus = None

        # No price, retrieval timestamp or stock signal in identity: updates to
        # an existing offer must not masquerade as a newly added configuration.
        identity = (provider_slug, gpu_label, variant, region, gpu_count, ct, commitment_months)
        offer_id = "computeprices:" + hashlib.sha256(
            json.dumps(identity, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:24]
        # Remove exact repeated observations only, not competing offers or a
        # distinct upstream observation of the same offer.
        observed_at = _text(item.get("last_updated"))
        available = item.get("available") if isinstance(item.get("available"), bool) else None
        key = (offer_id, observed_at, total_usd, available)
        if key in seen:
            continue
        seen.add(key)

        # CoreWeave has distinct Standard/High Memory RTX hosts. The feed's
        # cheapest OD and spot observations can describe different hosts; price
        # coincidence cannot establish the missing configuration.
        unknown_rtx_host = (
            provider_slug.lower() == "coreweave" and gpu_model == "RTX6000"
            and not re.search(r"\b(?:high|standard)[ -]memory\b", variant, re.I)
        )
        records.append(PriceRecord(
            provider=f"cp_{provider_slug}",   # prefix to distinguish from direct scrapers
            gpu_model=gpu_model,
            gpu_count=gpu_count,
            instance_type=f"{provider_slug}-{re.sub(r'[^a-z0-9]+', '-', gpu_label).strip('-')}-{gpu_count}x",
            region=region,
            consumption_type=ct,
            price_per_hour_usd=total_usd,
            price_per_gpu_hour_usd=price_usd,
            fetched_at=now,
            source_url=source,
            data_source=DATA_SOURCE,
            node_gpus=node_gpus,
            source_feed=FETCH_KEY,
            source_observed_at=observed_at,
            offer_id=offer_id,
            commitment_months=commitment_months,
            available=available,
            gpu_variant=gpu_variant,
            offer_variant=variant,
            form_factor=("SXM" if "sxm" in gpu_label else "PCIe" if "pcie" in gpu_label
                         else "NVL" if "nvl" in gpu_label else "unknown"),
            parser_version="aggregator-offers-1",
            comparison_eligible=not unknown_rtx_host,
            correction_reason=("CoreWeave RTX host memory variant is unspecified; "
                               "on-demand and spot may describe different configurations"
                               if unknown_rtx_host else ""),
        ))

    return records


def _map_consumption_type(pricing_type: str, commitment_months: Optional[int]) -> Optional[str]:
    pt = _text(pricing_type).lower()
    if pt == "spot":
        return "spot"
    if pt == "on_demand":
        return "on_demand"
    if pt == "reserved":
        if commitment_months is None:
            return "reserved_unknown"
        return {12: "reserved_1yr", 24: "committed_2yr", 36: "reserved_3yr",
                48: "committed_4yr"}.get(commitment_months, f"committed_{commitment_months}mo")
    return None
