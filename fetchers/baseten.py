"""
Baseten (baseten.co) fetcher — managed-inference dedicated deployments,
per-minute GPU instance pricing converted to $/GPU-hr.

Added 2026-09-02 (Koen: "add Modal and Baseten to the tracked list").

Two public sources, both plain-HTTP friendly (verified 2026-09-02):
  1. PRIMARY: https://docs.baseten.co/performance/instances — the only public
     source with the FULL SKU table (H200 and RTX PRO 6000 are here but NOT on
     the marketing page), one <table> row per SKU: name, $/min, vCPU, RAM,
     GPU count, VRAM. Per-instance price → divide by GPU count.
  2. FALLBACK: https://www.baseten.co/pricing/ — Next.js App Router page whose
     RSC payload embeds {"instanceType":"gpu","name":...,"pricePerHour":N}
     records for the 7 base (single-GPU) configs. No __NEXT_DATA__, no public
     pricing API.

Watch out: Baseten's historic H100 rate ($0.16632/min) is now the B200 price
($0.16633/min); H100 is $0.10833/min — don't "recognize" old numbers.

Comparability (why this renders ONLY in the platform section): per-minute
billing of active replica time (autoscaling, scale-to-zero — includes
deploy/scale-up time), vCPU + RAM bundled per GPU. A platform rate, not a
24/7 IaaS list price. H100 MIG (fractional 40GiB slice) is skipped — not a
whole GPU. A100/A10G/L4/T4 skipped (prior-gen / non-tracked models).
"""
import html as _html
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from schema import PriceRecord

logger = logging.getLogger(__name__)

# /performance/instances 308-redirects here since ~Aug 2026; urllib on Python
# 3.11 does NOT follow 308, so _get() follows redirects manually.
DOCS_URL = "https://docs.baseten.co/deployment/resources"
PRICING_URL = "https://www.baseten.co/pricing/"

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# SKU family prefix → canonical model. Order matters: "H100 MIG" (skipped)
# must match before "H100". Families not listed are deliberately untracked.
_FAMILY_MAP: List[Tuple[str, Optional[str]]] = [
    ("B300", "B300"),
    ("B200", "B200"),
    ("H200", "H200"),
    ("H100 MIG", None),          # fractional 40GiB slice — not a whole GPU
    ("H100MIG", None),
    ("H100", "H100"),
    ("RTX PRO 6000", "RTX6000"),
    ("RTX-PRO-6000", "RTX6000"),
    ("RTXPRO6000", "RTX6000"),
    ("L40S", "L40S"),
]


def _family(sku: str) -> Optional[str]:
    s = sku.upper().replace("_", " ").strip()
    for prefix, model in _FAMILY_MAP:
        if s.startswith(prefix.upper()):
            return model
    return None


def _get(url: str, max_redirects: int = 3) -> str:
    for _ in range(max_redirects + 1):
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 307, 308) and e.headers.get("Location"):
                url = urllib.parse.urljoin(url, e.headers["Location"])
                continue
            raise
    raise RuntimeError(f"too many redirects for {url}")


def _parse_docs_table(page: str) -> Dict[str, Tuple[float, int, str]]:
    """model → (per_gpu_hr, gpu_count, sku).

    Docs table columns (verified 2026-09-02): Instance | $/min | vCPU | RAM |
    GPU | VRAM, where the GPU cell reads "1 NVIDIA H200" / "8 NVIDIA B200s" /
    "Fractional NVIDIA H100" (MIG — skipped). Per-GPU rate is linear across
    sizes; keep the LARGEST config per family to represent node scale (same
    convention as verda.py)."""
    best: Dict[str, Tuple[float, int, str]] = {}
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S):
        cells = [_html.unescape(re.sub(r"<[^>]+>", "", c)).strip()
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if len(cells) < 6:
            continue
        sku = cells[0]
        model = _family(sku)
        if not model:
            continue
        m_price = re.search(r"\$\s*([0-9]*\.?[0-9]+)", cells[1])
        m_count = re.match(r"(\d+)\s+NVIDIA", cells[4])
        if not m_price or not m_count:   # "Fractional NVIDIA H100" → no match
            continue
        count = int(m_count.group(1))
        per_gpu = float(m_price.group(1)) * 60.0 / max(count, 1)
        if per_gpu < 0.10 or per_gpu > 30:
            logger.warning(f"Baseten: implausible ${per_gpu:.2f}/GPU-hr for "
                           f"{sku} — skipped")
            continue
        if model not in best or count > best[model][1]:
            best[model] = (round(per_gpu, 4), count, sku)
    return best


def _parse_pricing_rsc(page: str) -> Dict[str, Tuple[float, int, str]]:
    """Fallback: base single-GPU configs from the marketing page RSC payload."""
    best: Dict[str, Tuple[float, int, str]] = {}
    pat = re.compile(
        r'\\"instanceType\\":\\"gpu\\",\\"name\\":\\"([^"\\\\]+?)\\",'
        r'[^{}]*?\\"pricePerHour\\":([0-9]*\.?[0-9]+)')
    for name, hourly in pat.findall(page):
        model = _family(name)
        if not model:
            continue
        per_gpu = float(hourly)
        if per_gpu < 0.10 or per_gpu > 30:
            continue
        if model not in best or per_gpu < best[model][0]:
            best[model] = (round(per_gpu, 4), 1, name)
    return best


def fetch(regions: List[str] = None) -> List[PriceRecord]:
    now = datetime.now(timezone.utc).isoformat()
    best: Dict[str, Tuple[float, int, str]] = {}
    try:
        best = _parse_docs_table(_get(DOCS_URL))
    except Exception as e:
        logger.warning(f"Baseten docs-table fetch failed: {e}")
    if len(best) < 2:   # docs table gone/reshaped → marketing page fallback
        try:
            fb = _parse_pricing_rsc(_get(PRICING_URL))
            for model, v in fb.items():
                best.setdefault(model, v)
        except Exception as e:
            logger.error(f"Baseten fallback fetch failed: {e}")

    records = []
    for model, (per_gpu, count, sku) in best.items():
        records.append(PriceRecord(
            provider="baseten",
            gpu_model=model,
            gpu_count=count,
            instance_type=sku,
            region="us (managed)",
            consumption_type="on_demand",
            price_per_hour_usd=round(per_gpu * count, 4),
            price_per_gpu_hour_usd=per_gpu,
            fetched_at=now,
            source_url=PRICING_URL,
            data_source="web_scrape",
        ))
    if records:
        logger.info(f"Baseten: {len(records)} records ({', '.join(sorted(best))})")
    else:
        logger.warning("Baseten: no GPU prices parsed — both sources failed or reshaped")
    return records
