"""
Nebius committed / reserved pricing fetcher.
Reads from the internal pricing model in config.NEBIUS_COMMITTED_PRICES.
Prices are in $/GPU/hr.

Consumption type mapping:
  9-month  → committed_9mo
  12-month → committed_1yr    (canonical; picked up by RESERVED_1YR_CTS in diff.py)
  18-month → committed_18mo
  24-month → committed_2yr
  36-month → committed_3yr    (canonical; picked up by RESERVED_3YR_CTS in diff.py)

For each period, three prepayment tiers are emitted:
  100% upfront → canonical CT (e.g. committed_1yr)
  50% upfront  → CT + "_50pct" (e.g. committed_1yr_50pct)
  30% upfront  → CT + "_30pct" (e.g. committed_1yr_30pct)

Two volume tiers are emitted:
  below_512  → standard, accessible to all customers
  above_512  → enterprise tier, available for 512+ GPU commitments

The committed gap table in diff.py uses min() per provider+gpu+column so it
naturally picks the most favourable price (above_512, 100% upfront).
The footnote clarifies the terms.
"""
import logging
from datetime import datetime, timezone
from typing import List

from schema import PriceRecord
from config import (
    NEBIUS_COMMITTED_PRICES,
    NEBIUS_COMMITTED_CT_MAP,
    NEBIUS_COMMITTED_CT_SUFFIX,
)

logger = logging.getLogger(__name__)

SOURCE_URL = "https://nebius.com/prices"

# Volume tier label used in instance_type slug
_TIER_SLUG = {
    "below_512":  "std",
    "above_512":  "ent",
}


def fetch(regions: List[str] = None) -> List[PriceRecord]:
    now = datetime.now(timezone.utc).isoformat()
    records: List[PriceRecord] = []

    for gpu_model, tiers in NEBIUS_COMMITTED_PRICES.items():
        gpu_lower = gpu_model.lower()
        for tier_name, periods in tiers.items():
            tier_slug = _TIER_SLUG[tier_name]
            for months, prepay_options in periods.items():
                base_ct = NEBIUS_COMMITTED_CT_MAP.get(months)
                if base_ct is None:
                    logger.warning(f"No CT mapping for {months}-month commitment — skipping")
                    continue
                for prepay_label, price in prepay_options.items():
                    ct_suffix = NEBIUS_COMMITTED_CT_SUFFIX.get(prepay_label, f"_{prepay_label}")
                    ct = f"{base_ct}{ct_suffix}"
                    instance_type = (
                        f"nebius-{gpu_lower}-{months}mo-{prepay_label}-{tier_slug}"
                    )
                    records.append(PriceRecord(
                        provider="nebius",
                        gpu_model=gpu_model,
                        gpu_count=1,
                        instance_type=instance_type,
                        region="global",
                        consumption_type=ct,
                        price_per_hour_usd=price,
                        price_per_gpu_hour_usd=price,
                        fetched_at=now,
                        source_url=SOURCE_URL,
                    ))

    logger.info(
        f"Nebius committed: {len(records)} records across "
        f"{len(NEBIUS_COMMITTED_PRICES)} GPU models"
    )
    return records
