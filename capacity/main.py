"""
Daily GPU capacity monitor pipeline.

Run from the repo root:  python3 -m capacity.main [--provider lambda] [--test]

Mirrors the price monitor's flow: fetch every provider (cache-fallback on
transient failure), snapshot, diff vs the previous day, write the run
manifest and the ready-to-post artifacts. The 07:00 UTC posting routine
posts the artifacts verbatim — this pipeline decides all content.
"""
import argparse
import importlib
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow `python3 capacity/main.py` as well as `python3 -m capacity.main`
sys.path.insert(0, str(Path(__file__).parent.parent))

from capacity import store
from capacity.config import PROVIDERS
from capacity.diff import compute_diff
from capacity.render import write_artifacts

logger = logging.getLogger("capacity.main")

# Providers below this share of their trailing record baseline are treated as
# failed (fetch returned a suspiciously small subset — e.g. a half-parsed page).
MIN_BASELINE_SHARE = 0.5


# provider key → module under capacity/fetchers/ (where they differ)
FETCHER_MODULES = {
    "lambda": "lambda_labs",   # "lambda" is a Python keyword
}


def _fetch_provider(provider: str):
    mod = importlib.import_module(f"capacity.fetchers.{FETCHER_MODULES.get(provider, provider)}")
    return mod.fetch()


def run(providers=None, test=False):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
    started = datetime.now(timezone.utc)
    selected = providers or PROVIDERS

    all_records = []
    provider_status = {}
    failed, stale = [], []

    for provider in selected:
        try:
            records = _fetch_provider(provider)
        except Exception as e:
            logger.error(f"{provider}: fetch raised {e}")
            records = []

        if records:
            store.update_peer_cache(provider, records)
            provider_status[provider] = {"status": "live", "record_count": len(records)}
            logger.info(f"{provider}: {len(records)} records (live)")
        else:
            cached, age_h = store.get_cached_records(provider)
            if cached:
                records = cached
                stale.append(provider)
                provider_status[provider] = {
                    "status": "cached", "record_count": len(records),
                    "cache_age_hours": round(age_h, 1),
                }
                logger.warning(f"{provider}: 0 live records — serving {len(records)} "
                               f"cached ({age_h:.1f}h old)")
            else:
                failed.append(provider)
                provider_status[provider] = {"status": "failed", "record_count": 0}
                logger.error(f"{provider}: 0 records and no usable cache")

        all_records.extend(records)

    today = started.date()
    # Diff vs the COMMITTED last_snapshot.json — in GHA the checkout has no
    # daily snapshot files (gitignored), so the previous-day file may not exist.
    old_records = store.load_last_snapshot()

    diff = compute_diff(all_records, old_records)

    if not test:
        store.save_snapshot(all_records, today)
        store.append_history(all_records, today)

    live_count = sum(1 for s in provider_status.values() if s["status"] == "live")
    manifest = {
        "run_date": today.isoformat(),
        "started_at": started.isoformat(),
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "status": "success" if not failed else ("partial" if live_count else "failed"),
        "record_count": len(all_records),
        "diff_count": len(diff),
        "provider_count": len(selected),
        "live_provider_count": live_count,
        "failed_providers": failed,
        "stale_providers": stale,
        "provider_status": provider_status,
        "post_thread": True,
        "method_rows": _method_rows(),
    }

    write_artifacts(all_records, diff, manifest)
    if not test:
        store.save_run_manifest(manifest)

    logger.info(f"Capacity run complete: {len(all_records)} records, "
                f"{len(diff)} changes, {live_count}/{len(selected)} live")
    return manifest


def _method_rows():
    """Provider → signal → semantics, rendered into the Confluence method table."""
    return [
        ("Lambda", "instance-types API: regions_with_capacity_available",
         "Live per-region launchability of each instance type; empty list = sold out fleet-wide."),
        ("Hyperstack", "public GPU-pricing page stock badges / stock API",
         "Provider-reported per-model stock status in their EU/NA regions."),
        ("Verda", "instance-availability API (per location)",
         "Live bookability per instance type per DC (Finland/Iceland)."),
        ("Scaleway", "public availability API (per zone)",
         "Provider-reported enum per GPU SKU: available / scarce / shortage."),
        ("RunPod", "GraphQL stockStatus per GPU type",
         "Marketplace-wide stock label (High/Medium/Low/None) for Secure Cloud."),
        ("Vast.ai", "public search API offer depth",
         "Count of rentable GPUs listed right now — market depth, not DC inventory."),
        ("SF Compute", "market index price",
         "H100 exchange clearing price; price level & moves proxy scarcity."),
        ("AWS", "Capacity Blocks offerings: earliest start date",
         "Days until the earliest reservable GPU block per region — lead time = scarcity."),
        ("Nebius", "public console/API platform availability",
         "Outside-in view of which GPU platforms our own regions offer publicly."),
        ("GCP", "accelerator-zones listing",
         "Which zones OFFER each GPU family (static offering, not live stock)."),
    ]


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--provider", action="append", help="run only this provider (repeatable)")
    p.add_argument("--test", action="store_true", help="no snapshot/history/manifest writes")
    args = p.parse_args()
    result = run(providers=args.provider, test=args.test)
    sys.exit(0 if result["status"] != "failed" else 1)
