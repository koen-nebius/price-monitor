"""
Modal (modal.com) fetcher — serverless per-second GPU pricing, converted to
$/GPU-hr (rate/sec × 3600).

Added 2026-09-02 (Koen: "add Modal and Baseten to the tracked list"). Direct
fetcher because the ComputePrices aggregator misreads Modal's per-second rates
as $/hr (H100 "$0.07") — cp_modal stays in SKIP_PROVIDERS.

The pricing page is SvelteKit SERVER-side rendered: every GPU name and $/sec
price is present in the static HTML (verified 2026-09-02 three ways: plain
curl, WebFetch, Tavily extract — identical). There is NO JSON embed or pricing
API (/pricing/__data.json carries only the announcement banner); the page's
per-hour toggle is computed client-side, so only $/sec values exist in the
HTML. Parse strategy: strip tags, then pair each "Nvidia <name>" line with the
next "$X / sec" line. Deliberately NOT anchored on svelte class hashes
(e.g. svelte-1sd3zzt) — they change on every deploy.

Comparability (why this renders ONLY in the platform section, never in peer
medians): CPU ($0.0000131/core/sec ≈ $0.047/core-hr, min 0.125 cores) and
memory ($0.00000222/GiB/sec ≈ $0.008/GiB-hr) are billed ON TOP of the GPU
rate, and billable time includes cold-start load plus a default 60s
keep-alive. Serverless per-second billing is not a 24/7 IaaS list price.
"""
import html as _html
import logging
import re
import urllib.request
from datetime import datetime, timezone
from typing import List

from schema import PriceRecord

logger = logging.getLogger(__name__)

PRICING_URL = "https://modal.com/pricing"

# Page GPU label (after the "Nvidia " prefix) → our canonical model.
# A100 (prior-gen, excluded from coverage by decision), A10, L4, T4 are
# deliberately absent — same scope as the rest of the monitor.
GPU_NAME_MAP = {
    "B300": "B300",
    "B200": "B200",
    "H200 SXM": "H200",
    "H200": "H200",
    "H100 SXM5": "H100",
    "H100": "H100",
    "RTX PRO 6000": "RTX6000",
    "L40S": "L40S",
}

# Raw-HTML row pattern (verified 2026-09-02):
#   <p class="...">Nvidia B300</p> <!--[!--><p class="price svelte-...">
#   $0.001972 <span class="...">/ sec</span></p>
# Anchored on the "Nvidia <name>" text, the 'class="price' prefix, and the
# "/ sec" unit — NOT on svelte class hashes (they change per deploy). The
# unit anchor also guarantees we never convert a $/hr toggle value.
_ROW = re.compile(
    r"Nvidia\s+([^<]+?)</p>\s*(?:<!--.*?-->\s*)*"
    r"<p class=\"price[^\"]*\">\s*\$([0-9]*\.?[0-9]+)\s*"
    r"(?:<span[^>]*>)?\s*/\s*sec",
    re.S)


def fetch(regions: List[str] = None) -> List[PriceRecord]:
    now = datetime.now(timezone.utc).isoformat()
    try:
        req = urllib.request.Request(
            PRICING_URL,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                                   "Chrome/126.0 Safari/537.36"})
        with urllib.request.urlopen(req, timeout=45) as resp:
            page = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        logger.error(f"Modal fetch failed: {e}")
        return []

    found: dict = {}
    for m in _ROW.finditer(page):
        raw_name = _html.unescape(m.group(1)).strip().rstrip(",")
        model = GPU_NAME_MAP.get(raw_name)
        if not model:
            continue
        per_gpu = float(m.group(2)) * 3600.0
        if per_gpu < 0.10 or per_gpu > 30:   # unit-error guard
            logger.warning(f"Modal: implausible ${per_gpu:.2f}/GPU-hr "
                           f"for {raw_name} — skipped")
            continue
        # first occurrence wins (main Resource costs table comes first)
        if model not in found:
            found[model] = (per_gpu, raw_name)

    records = []
    for model, (per_gpu, raw_name) in found.items():
        records.append(PriceRecord(
            provider="modal",
            gpu_model=model,
            gpu_count=1,
            instance_type=f"serverless {raw_name}",
            region="us (serverless)",
            consumption_type="on_demand",
            price_per_hour_usd=round(per_gpu, 4),
            price_per_gpu_hour_usd=round(per_gpu, 4),
            fetched_at=now,
            source_url=PRICING_URL,
            data_source="web_scrape",
        ))
    if records:
        logger.info(f"Modal: {len(records)} records "
                    f"({', '.join(sorted(found))})")
    else:
        logger.warning("Modal: no GPU prices parsed — page layout may have changed")
    return records
