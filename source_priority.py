"""Targeted source selection without dropping uncovered fallback offers."""
import math

MASSED_ALIASES = {"cp_massedcompute", "cp_massed-compute", "sf_massedcompute"}
VULTR_ALIASES = {"cp_vultr", "cp_vultr-cloud", "cp_vultr_cloud", "sf_vultr"}


def exclude_superseded_vultr(records):
    """Do not revive unqualified Vultr starting-at prices on API/cache failure.

    The public bare-metal API qualifies deployment rights and full-node hourly
    prices. Historical aggregator observations do neither, so they cannot act
    as PAYG fallbacks. Preserve direct catalogue rows and unrelated providers.
    """
    return [r for r in records if r.provider not in VULTR_ALIASES]


def prefer_massed_direct(records, direct_live):
    """One Massed source per GPU/tier, preserving every SKU in that source.

    Prefer today's direct account catalogue when it has plausible accepted
    prices. Otherwise prefer ComputePrices, then Shadeform, then direct cache.
    Account catalogue is labelled separately from public-list evidence in the
    rendered output; registration does not promote it into enterprise medians.
    """
    groups = {}
    for record in records:
        if record.provider in MASSED_ALIASES | {"massedcompute"}:
            key = (record.gpu_model, record.consumption_type)
            groups.setdefault(key, set()).add(record.provider)
    chosen = {}
    for key, providers in groups.items():
        direct = [r for r in records if r.provider == "massedcompute"
                  and (r.gpu_model, r.consumption_type) == key]
        valid = bool(direct) and all(
            math.isfinite(r.price_per_gpu_hour_usd)
            and 0.20 <= r.price_per_gpu_hour_usd <= 200 for r in direct
        )
        priority = (["massedcompute"] if direct_live and valid else []) + [
            "cp_massedcompute", "cp_massed-compute", "sf_massedcompute", "massedcompute"
        ]
        chosen[key] = next(p for p in priority if p in providers)
    return [r for r in records if r.provider not in MASSED_ALIASES | {"massedcompute"}
            or chosen[(r.gpu_model, r.consumption_type)] == r.provider]
