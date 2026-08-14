"""
Verda (ex-DataCrunch) capacity fetcher — per-location instance availability.

OAuth2 client-credentials (free account → API keys → VERDA_CLIENT_ID /
VERDA_CLIENT_SECRET secrets; the old unauthenticated read closed — verified
401 on 2026-08-12, shape confirmed from the live openapi.json):
  POST /v1/oauth2/token {grant_type: client_credentials, ...} → bearer
  GET  /v1/instance-availability            → on-demand
  GET  /v1/instance-availability?is_spot=true → spot
Response: [{"location_code": "FIN-01", "availabilities": ["8H100.80S.176V",
...]}] — slugs = deployable NOW. Slug grammar: <count><FAMILY>.<variant>;
".CC" confidential-compute variants collapse into the base family.
Skips cleanly until the secrets are set.
"""
import json
import logging
import os
import re
import urllib.request
from datetime import datetime, timezone
from typing import List

from capacity.schema import AvailabilityRecord, plural

logger = logging.getLogger(__name__)

BASE = "https://api.verda.com/v1"
SOURCE_URL = "https://verda.com/"

_FAMILY_MAP = [
    ("GB300", "GB300"), ("GB200", "GB200"), ("B300", "B300"), ("B200", "B200"),
    ("H200", "H200"), ("H100", "H100"), ("L40S", "L40S"), ("RTXPRO6000", "RTX6000"),
]
_SLUG_RE = re.compile(r"^(\d+)([A-Z0-9]+)")


def _parse_slug(slug: str):
    m = _SLUG_RE.match(slug.upper())
    if not m:
        return None, 0
    count = int(m.group(1))
    body = slug.upper()
    if "RTX6000ADA" in body:   # older Ada card, not the Blackwell RTX PRO
        return None, 0
    for frag, model in _FAMILY_MAP:
        if frag in body:
            return model, count
    return None, 0


def _token() -> str:
    cid = os.environ.get("VERDA_CLIENT_ID", "")
    secret = os.environ.get("VERDA_CLIENT_SECRET", "")
    if not cid or not secret:
        return ""
    body = json.dumps({"grant_type": "client_credentials",
                       "client_id": cid, "client_secret": secret}).encode()
    req = urllib.request.Request(f"{BASE}/oauth2/token", data=body, headers={
        "Content-Type": "application/json", "User-Agent": "Mozilla/5.0 (capacity-monitor/1.0)",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read()).get("access_token", "")


def _availability(token: str, spot: bool) -> list:
    from fetchers._http import http_get
    url = f"{BASE}/instance-availability" + ("?is_spot=true" if spot else "")
    return json.loads(http_get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30))


def fetch() -> List[AvailabilityRecord]:
    now = datetime.now(timezone.utc).isoformat()
    try:
        token = _token()
    except Exception as e:
        logger.error(f"Verda token request failed: {e}")
        return []
    if not token:
        logger.warning("Verda: VERDA_CLIENT_ID/VERDA_CLIENT_SECRET not set — skipping "
                       "(free account: console.verda.com → API keys)")
        return []

    records: List[AvailabilityRecord] = []
    for spot, ct in ((False, "on_demand"), (True, "spot")):
        try:
            locations = _availability(token, spot)
        except Exception as e:
            logger.error(f"Verda instance-availability (spot={spot}) failed: {e}")
            continue

        # location → model → set of deployable sizes (deduped: the API can
        # return a location twice; verbatim duplicate rows polluted the
        # 2026-08-14 diff with a "0 sellouts / 0 restocks" churn line)
        seen_models = set()
        model_best: dict = {}   # model → largest size across ALL locations
        emitted = set()
        for loc in locations:
            code = loc.get("location_code", "unknown")
            sizes: dict = {}
            for slug in loc.get("availabilities") or []:
                model, count = _parse_slug(slug)
                if model:
                    sizes.setdefault(model, set()).add(count)
            for model, counts in sizes.items():
                if (model, code, ct) in emitted:
                    continue
                emitted.add((model, code, ct))
                seen_models.add(model)
                largest = max(counts)
                model_best[model] = max(model_best.get(model, 0), largest)
                state = "available" if largest >= 8 else "limited"
                detail = (f"deployable sizes: {', '.join(f'{c}x' for c in sorted(counts))}"
                          + ("" if largest >= 8 else " (no 8x node)"))
                records.append(AvailabilityRecord(
                    provider="verda", gpu_model=model, region=code,
                    consumption_type=ct, state=state,
                    metric_type="binary", metric_value=float(largest),
                    detail=detail,
                    fetched_at=now, source_url=SOURCE_URL, data_source="official_api",
                ))

        # GLOBAL rollup per model — without it the renderer had no direct
        # global row and fell back to Shadeform booleans, publishing
        # "Verda H100 sold out" while this API showed an 8x node deployable
        # (red-team catch 2026-08-14).
        for model, largest in model_best.items():
            n_locs = sum(1 for (m, _c, c2) in emitted if m == model and c2 == ct)
            state = "available" if largest >= 8 else "limited"
            records.append(AvailabilityRecord(
                provider="verda", gpu_model=model, region="global",
                consumption_type=ct, state=state,
                metric_type="regions_with_capacity", metric_value=float(n_locs),
                detail=(f"deployable in {plural(n_locs, 'location')}, largest node {largest}x"
                        + ("" if largest >= 8 else " (no 8x node)")),
                fetched_at=now, source_url=SOURCE_URL, data_source="official_api",
            ))

        # Models in the catalog but in NO location's availability list = sold out.
        # Catalog is keyless — reuse the pricing fetcher's endpoint.
        if not spot:
            try:
                from fetchers._http import http_get
                catalog = json.loads(http_get(f"{BASE}/instance-types", timeout=30))
                catalog_models = set()
                for it in (catalog if isinstance(catalog, list) else []):
                    model, _ = _parse_slug(it.get("instance_type", ""))
                    if model:
                        catalog_models.add(model)
                for model in catalog_models - seen_models:
                    records.append(AvailabilityRecord(
                        provider="verda", gpu_model=model, region="global",
                        consumption_type="on_demand", state="sold_out",
                        metric_type="binary", metric_value=0.0,
                        detail="in catalog but deployable in no location",
                        fetched_at=now, source_url=SOURCE_URL, data_source="official_api",
                    ))
            except Exception as e:
                logger.warning(f"Verda catalog cross-check failed: {e}")

    logger.info(f"Verda capacity: {len(records)} records")
    return records
