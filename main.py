"""
GPU Competitor Price Monitor — daily orchestrator.

Run:
    python main.py               # full run
    python main.py --test        # fetch only, skip Slack/Confluence
    python main.py --provider aws  # single provider

Outputs:
    store/YYYY-MM-DD.json        daily snapshot
    store/diff_YYYY-MM-DD.json   change log vs previous day
    store/slack_message.txt      pre-formatted Slack digest
    store/confluence_body.html   pre-formatted Confluence page body

When run as a scheduled Claude Code agent, the agent reads the output
files and uses MCP tools to post to Slack and update Confluence.
"""
import argparse
import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from typing import List

from store import (save_snapshot, load_snapshot, previous_snapshot_day, STORE_DIR,
                   WEB_SCRAPED_PROVIDERS, get_cached_records, update_peer_cache,
                   load_last_snapshot, save_last_snapshot)
from diff import compute_diff, format_slack_message, format_confluence_table
from history import append_today as append_history
from config import PROVIDERS
from schema import PriceRecord

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger("main")

from config import CONFLUENCE_PAGE_URL


def validate_prices(new_records: List[PriceRecord], old_records: List[PriceRecord]) -> List[str]:
    """
    Sanity-check new_records against old_records.
    Returns a list of anomaly strings for any suspicious prices.
    Flags:
      - Day-over-day change > ±40%
      - price_per_gpu_hour_usd < 0.20 or > 200.0
    """
    # Build lookup: (provider, gpu_model, consumption_type) -> old price
    old_lookup = {}
    for r in old_records:
        key = (r.provider, r.gpu_model, r.consumption_type)
        if key not in old_lookup or r.price_per_gpu_hour_usd < old_lookup[key]:
            old_lookup[key] = r.price_per_gpu_hour_usd

    anomalies = []
    for r in new_records:
        p = r.price_per_gpu_hour_usd

        # Absolute range check
        if p < 0.20:
            anomalies.append(
                f"{r.provider} {r.gpu_model} {r.consumption_type} {r.region}: "
                f"${p:.2f}/GPU-hr ⚠ implausibly low price"
            )
            continue
        if p > 200.0:
            anomalies.append(
                f"{r.provider} {r.gpu_model} {r.consumption_type} {r.region}: "
                f"${p:.2f}/GPU-hr ⚠ implausibly high price"
            )
            continue

        # Day-over-day change check
        key = (r.provider, r.gpu_model, r.consumption_type)
        old_price = old_lookup.get(key)
        if old_price is not None and old_price > 0:
            change_pct = (p - old_price) / old_price * 100
            if abs(change_pct) > 40.0:
                anomalies.append(
                    f"{r.provider} {r.gpu_model} {r.consumption_type} {r.region}: "
                    f"${old_price:.2f} → ${p:.2f} ({change_pct:+.1f}%) ⚠ price anomaly"
                )

    return anomalies


def run(providers=None, test=False):
    providers = providers or PROVIDERS
    all_records = []
    errors = []

    # Item 2: Check Nebius committed prices staleness
    nebius_date_warning = None
    try:
        from config import NEBIUS_COMMITTED_PRICES_VERIFIED_DATE
        verified = datetime.strptime(NEBIUS_COMMITTED_PRICES_VERIFIED_DATE, "%Y-%m-%d").date()
        days_old = (date.today() - verified).days
        if days_old > 30:
            msg = (
                f"Nebius committed prices in config.py were last verified {days_old} days ago "
                f"— verify against current pricing sheet."
            )
            logger.warning(msg)
            nebius_date_warning = f"_⚠ Nebius committed prices last verified {days_old} days ago — check config.py_"
    except Exception as e:
        logger.debug(f"Could not check NEBIUS_COMMITTED_PRICES_VERIFIED_DATE: {e}")

    for provider in providers:
        try:
            records = _fetch_provider(provider)
        except Exception as e:
            logger.error(f"{provider} fetch failed: {e}", exc_info=True)
            errors.append(provider)
            records = []

        if records:
            # Successful live fetch — update peer cache for web-scraped providers
            if provider in WEB_SCRAPED_PROVIDERS:
                update_peer_cache(provider, records)
            logger.info(f"{provider}: {len(records)} records (live)")
        elif provider in WEB_SCRAPED_PROVIDERS:
            # Web scrape returned nothing (likely blocked in cloud environment).
            # Fall back to last known-good data from peer_cache.json.
            records = get_cached_records(provider)
            if records:
                logger.warning(
                    f"{provider}: live fetch returned 0 records — "
                    f"falling back to {len(records)} cached records"
                )
            else:
                logger.warning(
                    f"{provider}: live fetch returned 0 records and no cache available. "
                    f"Run main.py locally once to populate peer_cache.json."
                )
        else:
            logger.info(f"{provider}: {len(records)} records")

        all_records.extend(records)

    today = date.today()
    save_snapshot(all_records, today)
    logger.info(f"Saved {len(all_records)} total records for {today}")

    # Diff — compare vs previous day snapshot, falling back to last_snapshot.json
    # (last_snapshot.json is committed to git so CCR fresh-clone runs have a baseline)
    prev_day = previous_snapshot_day()
    old_records = []
    diffs = []
    if prev_day:
        old_records = load_snapshot(prev_day)
        diffs = compute_diff(old_records, all_records)
        diff_path = STORE_DIR / f"diff_{today.isoformat()}.json"
        with open(diff_path, "w") as f:
            json.dump([d.to_dict() for d in diffs], f, indent=2)
        logger.info(f"Diff vs {prev_day}: {len(diffs)} changes")
    else:
        # No date-stamped history — try last_snapshot.json (committed baseline)
        old_records = load_last_snapshot()
        if old_records:
            diffs = compute_diff(old_records, all_records)
            diff_path = STORE_DIR / f"diff_{today.isoformat()}.json"
            with open(diff_path, "w") as f:
                json.dump([d.to_dict() for d in diffs], f, indent=2)
            logger.info(f"Diff vs last_snapshot.json: {len(diffs)} changes")
        else:
            logger.info("No previous snapshot — skipping diff (first run)")

    # Update last_snapshot.json so the next run has a baseline.
    # In CCR environments, the prompt instructs the agent to git commit+push this file.
    save_last_snapshot(all_records)

    # Append today's data to the historical price CSV
    append_history()

    # Item 1: Validate prices for anomalies
    anomalies = validate_prices(all_records, old_records)
    for anomaly in anomalies:
        logger.warning(f"Price anomaly: {anomaly}")

    # Write Slack message
    run_date = today.strftime("%B %d, %Y")
    slack_msg = format_slack_message(diffs, run_date, CONFLUENCE_PAGE_URL, records=all_records)

    # Prepend anomaly block if any anomalies detected
    slack_prefix_parts = []
    if anomalies:
        anomaly_lines = "\n".join(f"• {a}" for a in anomalies)
        slack_prefix_parts.append(f"⚠ *Data anomaly detected*\n{anomaly_lines}")
    if nebius_date_warning:
        slack_prefix_parts.append(nebius_date_warning)
    if slack_prefix_parts:
        slack_msg = "\n\n".join(slack_prefix_parts) + "\n\n" + slack_msg

    slack_path = STORE_DIR / "slack_message.txt"
    with open(slack_path, "w") as f:
        f.write(slack_msg)

    # Write Confluence body
    confluence_body = format_confluence_table(all_records, run_date)
    conf_path = STORE_DIR / "confluence_body.html"
    with open(conf_path, "w") as f:
        f.write(confluence_body)

    logger.info(f"Output files written to {STORE_DIR}")

    if errors:
        logger.warning(f"Providers with errors: {errors}")

    return {
        "records": len(all_records),
        "diffs": len(diffs),
        "errors": errors,
        "slack_message": slack_msg,
        "confluence_body": confluence_body,
    }


def _fetch_provider(provider: str):
    if provider == "aws":
        from fetchers.aws import fetch
    elif provider == "gcp":
        from fetchers.gcp import fetch
    elif provider == "azure":
        from fetchers.azure import fetch
    elif provider == "coreweave":
        from fetchers.coreweave import fetch
    elif provider == "lambda":
        from fetchers.lambda_labs import fetch
    elif provider == "crusoe":
        from fetchers.crusoe import fetch
    elif provider == "nebius":
        from fetchers.nebius import fetch
    elif provider == "nebius_committed":
        from fetchers.nebius_committed import fetch
    elif provider == "computeprices":
        from fetchers.computeprices import fetch
    elif provider == "oracle":
        from fetchers.oracle import fetch
    elif provider == "hyperstack":
        from fetchers.hyperstack import fetch
    else:
        raise ValueError(f"Unknown provider: {provider}")
    return fetch()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="GPU Competitor Price Monitor")
    parser.add_argument("--test", action="store_true", help="Fetch only, skip Slack/Confluence posts")
    parser.add_argument("--provider", nargs="+", help="Limit to specific provider(s)")
    args = parser.parse_args()

    result = run(providers=args.provider, test=args.test)
    print(f"\n=== Run complete ===")
    print(f"Records: {result['records']}")
    print(f"Changes: {result['diffs']}")
    if result["errors"]:
        print(f"Errors: {result['errors']}")
    print(f"\n--- Slack message ---")
    print(result["slack_message"])
