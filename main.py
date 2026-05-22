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
from datetime import date
from pathlib import Path

from store import (save_snapshot, load_snapshot, previous_snapshot_day, STORE_DIR,
                   WEB_SCRAPED_PROVIDERS, get_cached_records, update_peer_cache,
                   load_last_snapshot, save_last_snapshot)
from diff import compute_diff, format_slack_message, format_confluence_table
from config import PROVIDERS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger("main")

from config import CONFLUENCE_PAGE_URL


def run(providers=None, test=False):
    providers = providers or PROVIDERS
    all_records = []
    errors = []

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

    # Write Slack message
    run_date = today.strftime("%B %d, %Y")
    slack_msg = format_slack_message(diffs, run_date, CONFLUENCE_PAGE_URL, records=all_records)
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
