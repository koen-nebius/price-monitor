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
from typing import Dict, List, NamedTuple, Optional, Tuple

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


class _Sku(NamedTuple):
    """One kept SKU per family. vcpu/ram_gb/node_gpus come from the docs-table
    row itself; the marketing-page fallback lists only VRAM, so they stay None."""
    per_gpu: float
    gpu_count: int
    sku: str
    vcpu: Optional[int] = None       # table "vCPU" column — already threads
    ram_gb: Optional[float] = None   # table "RAM" column — GiB as published
    node_gpus: Optional[int] = None  # Baseten states no host size → always None;
                                     # schema.__post_init__ falls back to gpu_count


def _parse_docs_table(page: str) -> Dict[str, _Sku]:
    """model → _Sku for the largest config in the family.

    Docs table columns (verified 2026-09-02): Instance | $/min | vCPU | RAM |
    GPU | VRAM, where the GPU cell reads "1 NVIDIA H200" / "8 NVIDIA B200s" /
    "Fractional NVIDIA H100" (MIG — skipped). Per-GPU rate is linear across
    sizes; keep the LARGEST config per family to represent node scale (same
    convention as verda.py)."""
    best: Dict[str, _Sku] = {}
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
        # Same row, columns 3-4: whole-instance vCPU (threads) and system RAM
        # ("944 GiB" — GiB as published; VRAM is a separate column, ignored).
        m_vcpu = re.fullmatch(r"\d+", cells[2])
        m_ram = re.match(r"([0-9]*\.?[0-9]+)\s*Gi?B\b", cells[3])
        if model not in best or count > best[model].gpu_count:
            best[model] = _Sku(round(per_gpu, 4), count, sku,
                               vcpu=int(m_vcpu.group()) if m_vcpu else None,
                               ram_gb=float(m_ram.group(1)) if m_ram else None)
    return best


def _parse_pricing_rsc(page: str) -> Dict[str, _Sku]:
    """Fallback: base single-GPU configs from the marketing page RSC payload.
    Its records carry only "specs":"180 GiB VRAM" — no vCPU/RAM/node size."""
    best: Dict[str, _Sku] = {}
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
            best[model] = _Sku(round(per_gpu, 4), 1, name)
    return best


def fetch(regions: List[str] = None) -> List[PriceRecord]:
    now = datetime.now(timezone.utc).isoformat()
    best: Dict[str, _Sku] = {}
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
    for model, (per_gpu, count, sku, vcpu, ram_gb, node_gpus) in best.items():
        records.append(PriceRecord(
            provider="baseten",
            gpu_model=model,
            gpu_count=count,
            instance_type=sku,
            region="us (managed)",
            consumption_type="on_demand",
            price_per_hour_usd=round(per_gpu * count, 4),
            price_per_gpu_hour_usd=per_gpu,
            vcpu=vcpu,
            ram_gb=ram_gb,
            node_gpus=node_gpus,   # None on fallback → schema defaults to gpu_count
            fetched_at=now,
            source_url=PRICING_URL,
            data_source="web_scrape",
        ))
    if records:
        logger.info(f"Baseten: {len(records)} records ({', '.join(sorted(best))})")
    else:
        logger.warning("Baseten: no GPU prices parsed — both sources failed or reshaped")
    return records
