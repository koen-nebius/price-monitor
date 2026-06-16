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
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

# Load .env so local runs get the same API keys GHA injects from secrets.
# Existing env vars win — .env only fills gaps.
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

from store import (save_snapshot, load_snapshot, previous_snapshot_day, STORE_DIR,
                   WEB_SCRAPED_PROVIDERS, get_cached_records, update_peer_cache,
                   load_last_snapshot, save_last_snapshot,
                   get_cache_age_hours, save_run_manifest)
from diff import (compute_diff, format_slack_message, format_slack_summary,
                  format_confluence_table)
from history import append_records as append_history_records
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

    Validation is performed at the (provider, gpu_model, consumption_type) level —
    comparing the CHEAPEST new price against the CHEAPEST old price for each combo.
    This matches the granularity the dashboard uses for positioning, avoids
    false positives from multi-size instance families, and keeps the signal clean.

    Flags:
      - Best new price < $0.20 or > $200 per GPU-hr (implausible range)
      - Best new price changed > ±40% vs best old price (day-over-day spike)
        UNLESS the total instance cost (price_per_hour_usd) is stable — which
        means only the GPU count per instance changed, not the actual price.
        e.g. Together.ai H200: 1×$7.89 → 2×$3.95 = same $7.89/hr total, not a drop.
    """
    # Build best-price lookup per (provider, gpu_model, consumption_type)
    # Also track the total hourly cost for the best record, to detect instance-size changes
    def best_per_combo(records):
        lookup = {}       # key → price_per_gpu_hour_usd
        total_lookup = {} # key → price_per_hour_usd of the best record
        for r in records:
            key = (r.provider, r.gpu_model, r.consumption_type)
            if key not in lookup or r.price_per_gpu_hour_usd < lookup[key]:
                lookup[key] = r.price_per_gpu_hour_usd
                total_lookup[key] = r.price_per_hour_usd
        return lookup, total_lookup

    old_lookup, old_total = best_per_combo(old_records)
    new_lookup, new_total = best_per_combo(new_records)

    anomalies = []
    for (provider, gpu, ct), p in sorted(new_lookup.items()):
        # Absolute range check
        if p < 0.20:
            anomalies.append(
                f"{provider} {gpu} {ct}: ${p:.2f}/GPU-hr ⚠ implausibly low price"
            )
            continue
        if p > 200.0:
            anomalies.append(
                f"{provider} {gpu} {ct}: ${p:.2f}/GPU-hr ⚠ implausibly high price"
            )
            continue

        # Day-over-day change check (best vs best)
        key = (provider, gpu, ct)
        old_price = old_lookup.get(key)
        if old_price is not None and old_price > 0:
            change_pct = (p - old_price) / old_price * 100
            if abs(change_pct) > 40.0:
                # Check if the total hourly cost is stable — if so, only the instance
                # size changed (e.g. 1×$7.89 → 2×$3.95), not the actual price.
                old_hr = old_total.get(key, 0)
                new_hr = new_total.get(key, 0)
                if old_hr > 0 and new_hr > 0:
                    total_change_pct = abs((new_hr - old_hr) / old_hr * 100)
                    if total_change_pct < 5.0:
                        # Total cost unchanged — instance size artifact, not a real move
                        continue
                anomalies.append(
                    f"{provider} {gpu} {ct}: "
                    f"${old_price:.2f} → ${p:.2f} ({change_pct:+.1f}%) ⚠ price anomaly"
                )

    return anomalies


def run(providers=None, test=False):
    providers = providers or PROVIDERS
    all_records: List[PriceRecord] = []
    errors: List[str] = []
    warnings: List[str] = []
    started_at = datetime.now(timezone.utc).isoformat()

    # Per-provider fetch status — used in manifest and Slack footer
    # Schema: {provider: {status, record_count, cache_age_hours}}
    # status: "live" | "cache" | "fallback" | "error" | "missing"
    provider_status: Dict[str, dict] = {}

    # ── Nebius committed prices staleness check ──────────────────────────────
    # Warn in logs after 60 days. Only surface in Slack on the first crossing
    # and then weekly (every 7 days), to avoid nagging every single day.
    nebius_date_warning = None
    try:
        from config import NEBIUS_COMMITTED_PRICES_VERIFIED_DATE
        verified = datetime.strptime(NEBIUS_COMMITTED_PRICES_VERIFIED_DATE, "%Y-%m-%d").date()
        days_old = (date.today() - verified).days
        if days_old > 60:
            msg = (
                f"Nebius committed prices in config.py were last verified {days_old} days ago "
                f"— verify against current pricing sheet."
            )
            logger.warning(msg)
            warnings.append(msg)
            # Surface in Slack on day 61, then every 7 days after that
            if (days_old - 61) % 7 == 0:
                nebius_date_warning = f"_⚠ Nebius committed prices last verified {days_old} days ago — check config.py_"
    except Exception as e:
        logger.debug(f"Could not check NEBIUS_COMMITTED_PRICES_VERIFIED_DATE: {e}")

    # ── Fetch all providers ──────────────────────────────────────────────────
    for provider in providers:
        try:
            records = _fetch_provider(provider)
        except Exception as e:
            logger.error(f"{provider} fetch failed: {e}", exc_info=True)
            errors.append(provider)
            provider_status[provider] = {"status": "error", "record_count": 0}
            records = []

        if records:
            # Live fetch succeeded — update peer cache so a future empty/blocked
            # run (e.g. GHA runner IPs blocked by Azure/Oracle/RunPod) can fall back
            update_peer_cache(provider, records)
            logger.info(f"{provider}: {len(records)} records (live)")
            provider_status[provider] = {"status": "live", "record_count": len(records)}
        else:
            # Fetch returned nothing — fall back to peer_cache.json.
            # Applies to ALL providers, not just web scrapes: API providers also
            # return 0 when GHA runner IPs are blocked, and 0 records with status
            # "live" silently drops the provider from the snapshot.
            cache_age = get_cache_age_hours(provider)
            records = get_cached_records(provider)
            if records:
                age_str = f"{cache_age:.0f}h" if cache_age is not None else "unknown age"
                logger.warning(
                    f"{provider}: live fetch returned 0 records — "
                    f"falling back to {len(records)} cached records ({age_str})"
                )
                provider_status[provider] = {
                    "status": "cache",
                    "record_count": len(records),
                    "cache_age_hours": round(cache_age, 1) if cache_age is not None else None,
                }
            elif provider_status.get(provider, {}).get("status") != "error":
                logger.warning(
                    f"{provider}: live fetch returned 0 records and no cache available. "
                    f"Run main.py locally once to populate peer_cache.json."
                )
                provider_status[provider] = {"status": "missing", "record_count": 0}

        # Detect SkyPilot fallback for lambda (all records have data_source="aggregator")
        if provider == "lambda" and records and all(
            getattr(r, "data_source", "") == "aggregator" for r in records
        ):
            provider_status[provider] = {
                "status": "fallback",
                "record_count": len(records),
                "fallback_source": "skypilot_catalog",
            }

        all_records.extend(records)

    today = date.today()
    logger.info(f"Fetched {len(all_records)} total records for {today}")

    # ── Tag comparability (Phase 1.3/2.6) ────────────────────────────────────
    # Stamp form_factor/interconnect so cluster-class (8×SXM HGX) SKUs can be
    # compared like-for-like and single-GPU NVL/PCIe entry SKUs cannot masquerade
    # as cluster prices. Done before snapshot save so the tags persist.
    from comparability import enrich_comparability
    enrich_comparability(all_records)

    # ── Load previous snapshot for diff and validation ───────────────────────
    prev_day = previous_snapshot_day()
    old_records: List[PriceRecord] = []
    if prev_day:
        old_records = load_snapshot(prev_day)
    else:
        old_records = load_last_snapshot()

    # ── Validate BEFORE writing canonical outputs ────────────────────────────
    # Anomalies are flagged here; suspicious records are noted in the manifest
    # but still saved to the raw snapshot (for auditability). history.csv
    # receives only the non-anomalous accepted records.
    anomalies = validate_prices(all_records, old_records)
    for anomaly in anomalies:
        logger.warning(f"Price anomaly: {anomaly}")
    if anomalies:
        warnings.extend(anomalies)

    # Build accepted set: exclude records flagged by absolute range check
    # (>±40% day-over-day is flagged but kept — real price changes can be large)
    anomalous_keys = set()
    for anomaly in anomalies:
        # Absolute range violations (implausibly low/high) are excluded from history
        if "implausibly" in anomaly:
            parts = anomaly.split()
            if len(parts) >= 3:
                anomalous_keys.add((parts[0], parts[1], parts[2]))
    accepted_records = [
        r for r in all_records
        if (r.provider, r.gpu_model, r.consumption_type) not in anomalous_keys
    ]
    quarantined_count = len(all_records) - len(accepted_records)
    if quarantined_count:
        logger.warning(f"Quarantined {quarantined_count} records with implausible prices from history.csv")

    # ── Cross-table consistency guard (Phase 1.5) ────────────────────────────
    # Fail loudly (in the manifest) if the same (provider, gpu, on_demand) would
    # render divergent values across sections without a region label.
    try:
        from test_consistency import check_cross_table_consistency
        consistency_problems = check_cross_table_consistency(accepted_records)
        for p in consistency_problems:
            logger.warning(f"Cross-table inconsistency: {p}")
        warnings.extend(consistency_problems)
    except Exception as e:
        logger.debug(f"consistency check skipped: {e}")

    # ── Write canonical outputs (using validated records) ────────────────────
    save_snapshot(all_records, today)              # raw snapshot — includes everything
    save_last_snapshot(accepted_records)           # baseline for next diff — accepted only
    append_history_records(accepted_records, today)  # trend CSV — accepted only

    # ── Compute diff ─────────────────────────────────────────────────────────
    diffs = []
    if old_records:
        source = str(prev_day) if prev_day else "last_snapshot.json"
        diffs = compute_diff(old_records, accepted_records)
        diff_path = STORE_DIR / f"diff_{today.isoformat()}.json"
        with open(diff_path, "w") as f:
            json.dump([d.to_dict() for d in diffs], f, indent=2)
        logger.info(f"Diff vs {source}: {len(diffs)} changes")
    else:
        logger.info("No previous snapshot — skipping diff (first run)")

    # ── Format outputs ────────────────────────────────────────────────────────
    # slack_message.txt  → short headline summary, posted to the channel
    # slack_thread.txt   → full tables, posted as a thread reply to the summary
    run_date = today.strftime("%B %d, %Y")
    slack_summary = format_slack_summary(
        diffs, run_date, CONFLUENCE_PAGE_URL,
        records=accepted_records,
        provider_status=provider_status,
    )
    slack_thread = format_slack_message(
        diffs, run_date, CONFLUENCE_PAGE_URL,
        records=accepted_records,
        provider_status=provider_status,
    )

    # Prepend anomaly and staleness warnings to the channel summary
    # Only surface truly implausible prices (absolute range violations) in the Slack
    # anomaly header. Day-over-day ±40% swings are real market moves that already
    # appear in the "Price moves" section — flagging them as anomalies is misleading.
    slack_prefix_parts = []
    implausible = [a for a in anomalies if "implausibly" in a]
    if implausible:
        anomaly_lines = "\n".join(f"• {a}" for a in implausible)
        slack_prefix_parts.append(f"⚠ *Data anomaly detected*\n{anomaly_lines}")
    if nebius_date_warning:
        slack_prefix_parts.append(nebius_date_warning)
    if slack_prefix_parts:
        slack_summary = "\n\n".join(slack_prefix_parts) + "\n\n" + slack_summary

    slack_path = STORE_DIR / "slack_message.txt"
    with open(slack_path, "w") as f:
        f.write(slack_summary)
    thread_path = STORE_DIR / "slack_thread.txt"
    with open(thread_path, "w") as f:
        f.write(slack_thread)

    confluence_body = format_confluence_table(accepted_records, run_date,
                                              provider_status=provider_status)
    conf_path = STORE_DIR / "confluence_body.html"
    with open(conf_path, "w") as f:
        f.write(confluence_body)

    logger.info(f"Output files written to {STORE_DIR}")
    if errors:
        logger.warning(f"Providers with errors: {errors}")

    # ── Write run manifest ────────────────────────────────────────────────────
    completed_at = datetime.now(timezone.utc).isoformat()
    stale_providers = [p for p, s in provider_status.items() if s["status"] in ("cache", "fallback", "missing")]
    run_status = "failed" if len(errors) >= len(providers) // 2 else \
                 "partial" if (errors or stale_providers) else "success"
    manifest = {
        "run_date":          today.isoformat(),
        "started_at":        started_at,
        "completed_at":      completed_at,
        "status":            run_status,
        "record_count":      len(accepted_records),
        "raw_record_count":  len(all_records),
        "anomaly_count":     len(anomalies),
        "quarantined_count": quarantined_count,
        "diff_count":        len(diffs),
        "failed_providers":  errors,
        "stale_providers":   stale_providers,
        "provider_status":   provider_status,
        "warnings":          warnings,
        "generated_outputs": {
            "slack_message":    True,
            "confluence_body":  True,
        },
    }
    save_run_manifest(manifest)

    return {
        "records": len(accepted_records),
        "diffs": len(diffs),
        "errors": errors,
        "slack_message": slack_summary,
        "confluence_body": confluence_body,
        "manifest": manifest,
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
    elif provider == "runpod":
        from fetchers.runpod import fetch
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
