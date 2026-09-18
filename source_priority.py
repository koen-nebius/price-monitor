"""Targeted source selection without dropping uncovered fallback offers."""
import re
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


def _provider_token(value):
    value = str(value or "").strip().lower()
    if value.startswith(("cp_", "sf_")):
        value = value[3:]
    return re.sub(r"[^a-z0-9]", "", value)


_NAME_KEYS = {
    "aws": "aws", "amazonaws": "aws", "amazonwebservices": "aws",
    "gcp": "gcp", "googlecloud": "gcp", "googlecloudplatform": "gcp",
    "azure": "azure", "microsoftazure": "azure", "coreweave": "coreweave",
    "lambda": "lambda", "lambdalabs": "lambda", "lambdacloud": "lambda",
    "crusoe": "crusoe", "crusoecloud": "crusoe", "nebius": "nebius",
    "hyperstack": "hyperstack", "nexgencloud": "hyperstack",
    "oracle": "oracle", "oraclecloud": "oracle", "oci": "oracle",
    "verda": "verda", "datacrunch": "verda", "runpod": "runpod",
    "massedcompute": "massedcompute", "vultr": "vultr", "vultrcloud": "vultr",
    "vast": "vast", "vastai": "vast", "together": "together", "togetherai": "together",
    "voltage": "cp_voltage", "voltagepark": "cp_voltage",
    "gmi": "cp_gmi-cloud", "gmicloud": "cp_gmi-cloud", "scaleway": "cp_scaleway",
    "denvr": "cp_denvr-dataworks", "denvrdataworks": "cp_denvr-dataworks",
    "sfcompute": "sfcompute", "sanfranciscocompute": "sfcompute",
}


def canonical_provider(provider):
    """Normalize supplier naming, never use it to equate their offers.

    Existing cohort keys remain stable even when historically prefixed cp_/sf_.
    Prefix differences between feeds do not create additional competitors.
    """
    token = _provider_token(provider)
    if token in _NAME_KEYS:
        return _NAME_KEYS[token]
    if provider in PROVIDER_ALIASES:
        return PROVIDER_ALIASES[provider]
    from config import PROVIDER_TIERS
    for keys in PROVIDER_TIERS.values():
        for key in keys:
            if _provider_token(key) == token:
                return PROVIDER_ALIASES.get(key, key)
    if str(provider).startswith(("cp_", "sf_")):
        return "cp_" + re.sub(r"[^a-z0-9]+", "-", str(provider)[3:].lower()).strip("-")
    return provider


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
        result.append(replace(row, provider=canonical_provider(row.provider),
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
    """Compatibility entry point: retain all source/configuration observations.

    A live account catalogue cannot supersede a different region, host or
    commercial offer merely because its GPU family and billing tier match.
    Canonicalization merges supplier identity; comparison eligibility and exact
    cross-checks are separate from raw source retention.
    """
    return list(records)
