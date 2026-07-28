"""
Comparability tagging (Phase 1.3 / 2.6).

gpu_count is NOT a reliable cluster signal — many enterprise neoclouds (Nebius,
Crusoe, Voltage, GMI, Scaleway) record their 8×SXM HGX clusters as per-GPU rows
with gpu_count=1, while AWS/GCP/Oracle use gpu_count=8. So form factor is assigned
from provider + SKU + GPU model, not node size.

Why it matters: the headline "cheapest hyperscaler H100" must not be Azure's
NC40ads ($6.98, a single H100 NVL on PCIe with no InfiniBand) compared against a
competitor's 8×SXM InfiniBand training node ($12.29). is_cluster_class() lets the
exec tables compare like for like (SXM HGX cluster SKUs) and label the rest.
"""
import re
from typing import List, Tuple

from schema import PriceRecord

# Datacenter GPUs that ship in SXM (HGX baseboard, high-speed fabric) by default.
_SXM_MODELS = {"H100", "H200", "B200", "B300", "GB200", "GB300", "H800", "A100"}
# GPUs that are inherently PCIe (no NVLink fabric at cluster scale).
_PCIE_MODELS = {"L40S", "L40", "L4", "A10", "A40", "RTX", "V100", "T4"}

# Explicit (provider-prefix, instance_type regex) → (form_factor, interconnect).
# Only needed where the model-based default is wrong, or to assert a known fabric.
# Order matters: first match wins.
_RULES: List[Tuple[str, str, str, str]] = [
    ("azure",  r"nc\d+ads",   "NVL",  "none"),        # NC40ads = 1× H100 NVL, PCIe, no IB
    ("azure",  r"nd\d+is",    "SXM",  "InfiniBand"),  # ND96isr / ND96is = 8× SXM + IB
    ("aws",    r"g6e",        "PCIe", "Ethernet"),    # L40S
    ("aws",    r"p5|p6",      "SXM",  "EFA"),          # p5/p5e/p5en/p6-b200/p6-b300
    ("gcp",    r"a3-|a4-",    "SXM",  "GPUDirect"),
    ("lambda", r"pcie",       "PCIe", "Ethernet"),     # Lambda's 1× H100 PCIe SKU ($3.29)
]

# Fabric for SXM defaults, by provider, where we're confident. Others → unknown
# (form_factor=SXM is what drives cluster-class; interconnect label is informational).
_SXM_FABRIC = {
    "aws": "EFA", "gcp": "GPUDirect", "azure": "InfiniBand", "nebius": "InfiniBand",
    "crusoe": "InfiniBand",   # all Crusoe SXM capacity runs on HGX nodes with IB fabric
}

# Providers whose DIRECT-fetch records price 8×SXM HGX cluster products as per-GPU
# rows (gpu_count=1) — the exact pattern the module docstring warns about. For these,
# enrichment stamps node_gpus=8 on SXM records so diff._is_cluster_peer treats them
# as the like-for-like cluster set (fix 2026-07-06: Crusoe H100 $3.90 / H200 $4.29,
# high-confidence direct prices, were silently excluded from the peer median by the
# gpu_count>=8 gate).
# Deliberately NOT included:
#   - hyperstack: SXM-priced VMs, but IB fabric is not guaranteed on the on-demand
#     tier — enterprise peer (see config PROVIDER_TIERS), not cluster-class.
#   - voltage / gmi-cloud aggregator rows: Ethernet/unknown entry SKUs — the
#     _is_cluster_peer docstring's own counter-examples. Revisit only with evidence.
_PER_GPU_CLUSTER_PROVIDERS = {"crusoe"}

# ── Local storage bundling (product-attribute normalization, attribute #1) ──
# The 2026-07-14 external review flagged that the report normalizes form factor /
# interconnect but no product attributes. This is the first: whether the $/GPU-hr
# list price INCLUDES local NVMe scratch. Near-static product config verified by
# hand from provider pages/docs (not scraped) — same pattern as
# _PER_GPU_CLUSTER_PROVIDERS. Keys are base provider names (cp_ prefix stripped).
# "note" is the per-8-GPU-node detail rendered verbatim in footnotes; dict order
# is display order (bundled entries roughly largest-first).
LOCAL_STORAGE_VERIFIED = "2026-07-22"
LOCAL_STORAGE_BUNDLED = {
    "coreweave":  {"included": True,  "note": "61.44TB"},
    "hyperstack": {"included": True,  "note": "32-48TB"},
    "aws":        {"included": True,  "note": "30.72TB (p5 instance store)"},
    "azure":      {"included": True,  "note": "28TiB (ND H100 v5)"},
    "lambda":     {"included": True,  "note": "22TiB"},
    "vultr":      {"included": True,  "note": "13TB VM / 30.72TB bare metal"},
    "crusoe":     {"included": True,  "note": "7.7TB H100 / 15.4TB H200-B200"},
    "gcp":        {"included": True,  "note": "6TB (a3, mandatory; a3-ultra 12TB)"},
    "scaleway":   {"included": True,  "note": "3.2-12.8TB (24TB B300)"},
    "together":   {"included": True,  "note": "size unpublished"},
    "runpod":     {"included": False, "note": "all disk metered separately"},
    "nebius":     {"included": False,
                   "note": "H100/H200/B200 hosts have no local NVMe; all storage is "
                           "network-attached, billed separately per GiB. B300 8-GPU "
                           "preset has an opt-in 6×3.84TB local pack at "
                           "$0.065/GiB-mo ≈ +$0.24/GPU-hr"},
}


def local_storage_info(provider: str) -> dict:
    """Bundled-local-storage entry for a provider code (cp_ prefix ok); {} if unverified."""
    p = provider.lower()
    base = p[3:] if p.startswith("cp_") else p
    return LOCAL_STORAGE_BUNDLED.get(base, {})


def _classify(r: PriceRecord) -> Tuple[str, str]:
    prov = r.provider.lower()
    base = prov[3:] if prov.startswith("cp_") else prov
    it = (r.instance_type or "").lower()
    model = (r.gpu_model or "").upper()

    for p, rx, ff, ic in _RULES:
        if base.startswith(p) and re.search(rx, it):
            return ff, ic

    if model in _PCIE_MODELS:
        return "PCIe", "Ethernet"
    if model in _SXM_MODELS:
        return "SXM", _SXM_FABRIC.get(base, "unknown")
    return "unknown", "unknown"


def enrich_comparability(records: List[PriceRecord]) -> List[PriceRecord]:
    """Stamp form_factor/interconnect on every record (idempotent — fills blanks only)."""
    for r in records:
        if not r.form_factor or r.form_factor == "unknown":
            ff, ic = _classify(r)
            r.form_factor = ff
            if not r.interconnect or r.interconnect == "unknown":
                r.interconnect = ic
        # Known per-GPU-priced cluster products: stamp node size so the cluster-class
        # peer gate (form_factor SXM AND node_gpus>=8) sees them like-for-like.
        prov = r.provider.lower()
        base = prov[3:] if prov.startswith("cp_") else prov
        if base in _PER_GPU_CLUSTER_PROVIDERS and r.form_factor == "SXM" \
                and (getattr(r, "node_gpus", 0) or 0) < 8:
            r.node_gpus = 8
    return records


def is_cluster_class(r: PriceRecord) -> bool:
    """
    True for SXM HGX cluster SKUs — the like-for-like set for training-cluster
    price comparison. Excludes single-GPU NVL/PCIe entry SKUs (Azure NC40ads,
    Lambda 1× PCIe) and PCIe inference cards (L40S, L4).
    """
    return getattr(r, "form_factor", "") == "SXM"
