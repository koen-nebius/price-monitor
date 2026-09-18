"""Targeted source selection without dropping uncovered fallback offers."""
import math
from dataclasses import replace

# Keep the established provider keys used by report cohorts. A source prefix is
# provenance, not a second competitor. Do not infer equivalence between offers:
# regional, configuration and commercial differences remain individual records.
PROVIDER_ALIASES = {
    "cp_coreweave": "coreweave", "sf_coreweave": "coreweave",
    "cp_lambda": "lambda", "cp_lambda-labs": "lambda", "sf_lambdalabs": "lambda",
    "sf_crusoe": "crusoe", "cp_crusoe": "crusoe",
    "sf_nebius": "nebius", "cp_nebius": "nebius",
    "cp_hyperstack": "hyperstack", "sf_hyperstack": "hyperstack",
    "cp_oracle": "oracle", "sf_oracle": "oracle",
    "cp_verda": "verda", "sf_verda": "verda", "sf_datacrunch": "verda",
    "sf_scaleway": "cp_scaleway", "sf_voltagepark": "cp_voltage",
    "sf_voltage_park": "cp_voltage", "cp_voltage-park": "cp_voltage",
    "sf_gmi": "cp_gmi-cloud", "sf_gmi-cloud": "cp_gmi-cloud",
    "sf_paperspace": "cp_paperspace", "sf_latitude": "cp_latitude",
    "sf_denvr": "cp_denvr-dataworks", "sf_digitalocean": "cp_digitalocean",
    "cp_vast": "vast", "cp_vast-ai": "vast", "sf_vast": "vast",
}


def canonicalize_provider_sources(records):
    """Retain offers from every feed while giving each supplier one identity.

    In particular, a public rate-card scraper must not suppress CoreWeave
    aggregator offers. Existing comparison functions select one eligible offer
    per canonical provider; raw snapshots retain the source-level observations.
    """
    result = []
    for row in records:
        feed = row.source_feed
        if not feed and row.provider.startswith("cp_"):
            feed = "computeprices"
        elif not feed and row.provider.startswith("sf_"):
            feed = "shadeform"
        result.append(replace(row, provider=PROVIDER_ALIASES.get(row.provider, row.provider),
                              source_feed=feed))
    identities = {}
    for row in result:
        if row.source_feed and row.offer_id:
            identities.setdefault((row.provider, row.source_feed, row.offer_id), set()).add(
                (row.price_per_hour_usd, row.price_per_gpu_hour_usd, row.available))
    for row in result:
        if len(identities.get((row.provider, row.source_feed, row.offer_id), ())) > 1:
            row.comparison_eligible = False
            row.correction_reason = "conflicting observations for the same aggregator offer"
    return result

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
