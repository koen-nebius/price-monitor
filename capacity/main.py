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
import os
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
FETCH_HEALTH = {}


def _legacy_missing_credentials(provider):
    """Infer pending only for old collectors that do not emit fetch health."""
    from capacity.config import PENDING_ACTIVATION
    required = {
        "aws_capacity_blocks": ("AWS_ACCESS_KEY_ID",),
        "hyperstack": ("HYPERSTACK_API_KEY",),
        "verda": ("VERDA_CLIENT_ID", "VERDA_CLIENT_SECRET"),
        "lambda": ("LAMBDA_API_KEY",), "together": ("TOGETHER_API_KEY",),
    }.get(provider)
    return bool(provider in PENDING_ACTIVATION and required
                and any(not os.environ.get(key, "").strip() for key in required))


def _fetch_provider(provider: str):
    mod = importlib.import_module(f"capacity.fetchers.{FETCHER_MODULES.get(provider, provider)}")
    try:
        return mod.fetch()
    finally:
        FETCH_HEALTH[provider] = dict(getattr(mod, "LAST_FETCH_HEALTH", {}))


def run(providers=None, test=False):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
    started = datetime.now(timezone.utc)
    selected = providers or PROVIDERS

    all_records = []
    provider_status = {}
    failed, stale, paused = [], [], []

    for provider in selected:
        if provider == "crusoe":
            from crusoe_api import CAPACITY_ACCESS_PAUSED, CAPACITY_PAUSE_REASON
            if CAPACITY_ACCESS_PAUSED:
                paused.append(provider)
                provider_status[provider] = {
                    "status": "paused", "reason": CAPACITY_PAUSE_REASON, "record_count": 0,
                }
                logger.info("Crusoe: authenticated capacity access paused; no fetch or cache fallback")
                continue
        FETCH_HEALTH.pop(provider, None)
        fetch_error = None
        try:
            records = _fetch_provider(provider)
        except Exception as e:
            fetch_error = type(e).__name__
            logger.error("%s: fetch raised %s", provider, fetch_error)
            records = []

        health = FETCH_HEALTH.get(provider, {})
        if fetch_error:
            health = {**health, "status": "failed", "reason": "fetcher raised an exception",
                      "error_code": fetch_error}
        elif not health and not records and _legacy_missing_credentials(provider):
            health = {"status": "pending", "reason": "required API credentials are not configured",
                      "error_code": "missing_credentials"}
        if health.get("status") in {"failed", "error"}:
            # Failed health cannot become a successful cache refresh merely
            # because a collector accidentally returned some rows with it.
            records = []
        if health.get("status") in {"partial", "empty", "pending"}:
            # A complete empty catalogue does not establish zero GPU inventory.
            # A partial read must not replace a complete cache or look healthy.
            provider_status[provider] = {**health, "record_count": len(records)}
            if health["status"] == "partial":
                failed.append(provider)
            all_records.extend(records)
            continue
        if provider == "scaleway":
            from capacity.insights import is_scaleway_instance
            records = [r for r in records if is_scaleway_instance(r)]
        if records:
            store.update_peer_cache(provider, records)
            provider_status[provider] = {**health, "status": "live", "record_count": len(records)}
            logger.info(f"{provider}: {len(records)} records (live)")
        else:
            cached, age_h = store.get_cached_records(provider)
            if provider == "scaleway":
                cached = [r for r in cached if is_scaleway_instance(r)]
            if provider == "lambda":
                from capacity.insights import is_lambda_instance
                # Old rows combined shapes and cannot substitute for an exact
                # instance observation. Keep original timestamps on valid cache.
                cached = [r for r in cached if is_lambda_instance(r)]
            if provider == "together":
                # Legacy rows maximized across shapes and synthesized cluster
                # stock. They are not safe fallback observations for inference.
                cached = [r for r in cached
                          if r.product_scope == "dedicated_inference"
                          and r.metric_type == "inference_replicas"
                          and r.data_source == "official_api"
                          and r.region != "global" and r.instance_type]
            if provider == "crusoe":
                from crusoe_api import credentials_configured
                if credentials_configured():
                    # An API outage may reuse a labelled stale API observation,
                    # never promote the old documentation footprint as a
                    # substitute for the newly configured authenticated source.
                    cached = [r for r in cached if r.data_source == "official_api"
                              and r.metric_type == "provider_quantity"]
            if cached:
                records = cached
                stale.append(provider)
                provider_status[provider] = {
                    **health,
                    "status": "cached", "record_count": len(records),
                    "cache_age_hours": round(age_h, 1),
                }
                logger.warning(f"{provider}: 0 live records — serving {len(records)} "
                               f"cached ({age_h:.1f}h old)")
            else:
                failed.append(provider)
                provider_status[provider] = {**health, "status": "failed", "record_count": 0}
                logger.error(f"{provider}: 0 records and no usable cache")

        all_records.extend(records)

    today = started.date()
    # Diff vs the COMMITTED last_snapshot.json — in GHA the checkout has no
    # daily snapshot files (gitignored), so the previous-day file may not exist.
    old_records = store.load_last_snapshot()
    # Access suspension is not a capacity disappearance. Other sources can
    # retain explicitly labelled aggregator observations, never the paused
    # provider's old authoritative data as today's available/zero stock.
    def usable_during_pause(record):
        return record.provider not in paused or record.data_source == "aggregator"
    all_records = [r for r in all_records if usable_during_pause(r)]
    old_records = [r for r in old_records if usable_during_pause(r)]

    diff = compute_diff(all_records, old_records)

    if not test:
        store.save_snapshot(all_records, today)
        store.append_history(all_records, today)

    live_count = sum(1 for s in provider_status.values() if s["status"] == "live")
    # A partial collector still supplied bounded evidence. Keep that distinct
    # from a run where every attempted active collector failed completely.
    has_partial = any(s["status"] == "partial" for s in provider_status.values())
    real_failed = [p for p in failed if provider_status[p]["status"] == "failed"]
    incomplete = [p for p, s in provider_status.items()
                  if s["status"] in {"partial", "empty", "cached", "pending"}]
    manifest = {
        "run_date": today.isoformat(),
        "started_at": started.isoformat(),
        "completed_at": datetime.now(timezone.utc).isoformat(),
        # Explicit pending credentials are distinct from failed authenticated
        # checks. A static activation list must never hide a runtime failure.
        "status": ("partial" if incomplete else "success") if not real_failed else ("partial" if live_count or has_partial else "failed"),
        "record_count": len(all_records),
        "diff_count": len(diff),
        "provider_count": len(selected),
        "live_provider_count": live_count,
        "failed_providers": failed,
        "stale_providers": stale,
        "paused_providers": paused,
        "provider_status": provider_status,
        "post_thread": True,
    }

    write_artifacts(all_records, diff, manifest, old_records)
    if not test:
        store.save_run_manifest(manifest)

    logger.info(f"Capacity run complete: {len(all_records)} records, "
                f"{len(diff)} changes, {live_count}/{len(selected)} live")
    return manifest


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--provider", action="append", help="run only this provider (repeatable)")
    p.add_argument("--test", action="store_true", help="no snapshot/history/manifest writes")
    args = p.parse_args()
    result = run(providers=args.provider, test=args.test)
    sys.exit(0 if result["status"] != "failed" else 1)
