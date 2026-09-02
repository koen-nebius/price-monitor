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
                   get_cache_age_hours, save_run_manifest,
                   apply_cache_staleness_guard,
                   PEER_CACHE_SOFT_STALE_HOURS, PEER_CACHE_HARD_STALE_HOURS)
from fetchers.computeprices import FETCH_KEY as COMPUTEPRICES_KEY
from diff import (compute_diff, format_slack_message, format_slack_summary,
                  format_confluence_table, format_spot_auction_page)
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

    # ── Nebius committed prices staleness check (INTERNAL ONLY) ──────────────
    # Past NEBIUS_COMMITTED_STALE_DAYS the committed section is omitted from the
    # exec-facing output by diff.py (see _committed_freshness). Here we only log it
    # and record it in run_manifest warnings — it is NEVER surfaced in the Slack/
    # Confluence broadcast, so a stale internal sheet can't leak a debug line to execs.
    try:
        from config import NEBIUS_COMMITTED_PRICES_VERIFIED_DATE, NEBIUS_COMMITTED_STALE_DAYS
        verified = datetime.strptime(NEBIUS_COMMITTED_PRICES_VERIFIED_DATE, "%Y-%m-%d").date()
        days_old = (date.today() - verified).days
        if days_old > NEBIUS_COMMITTED_STALE_DAYS:
            msg = (
                f"Nebius committed prices last verified {days_old} days ago "
                f"(> {NEBIUS_COMMITTED_STALE_DAYS}d) — committed section omitted from exec output; "
                f"re-verify against the AE pricing sheet and bump NEBIUS_COMMITTED_PRICES_VERIFIED_DATE."
            )
            logger.warning(msg)
            warnings.append(msg)
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

                # ── Staleness guard for the ComputePrices aggregator source ──────
                # ComputePrices intermittently 429s/500s, triggering this cache
                # fallback. Week-old peer prices must never be served as current to
                # leadership. SOFT-stale (>48h): flag the records (data_source ->
                # aggregator_stale + is_stale) so diff.py drops them from the headline.
                # HARD-stale (>7d) or no timestamp: drop them from the assembled set.
                if provider == COMPUTEPRICES_KEY:
                    records, verdict, n_flagged, n_dropped = apply_cache_staleness_guard(
                        records, cache_age
                    )
                    provider_status[provider]["record_count"] = len(records)
                    if verdict == "drop":
                        msg = (
                            f"{provider}: cached peer data is too old "
                            f"({age_str}, hard threshold {PEER_CACHE_HARD_STALE_HOURS:.0f}h) "
                            f"— dropped {n_dropped} stale aggregator records (not shown as current)"
                        )
                        logger.warning(msg)
                        warnings.append(msg)
                        provider_status[provider]["status"] = "missing"
                        provider_status[provider]["stale_verdict"] = "drop"
                    elif verdict == "stale":
                        msg = (
                            f"{provider}: cached peer data is stale "
                            f"({age_str}, soft threshold {PEER_CACHE_SOFT_STALE_HOURS:.0f}h) "
                            f"— flagged {n_flagged} aggregator records as stale "
                            f"(excluded from headline by diff.py)"
                        )
                        logger.warning(msg)
                        warnings.append(msg)
                        provider_status[provider]["stale_verdict"] = "stale"
                    else:
                        provider_status[provider]["stale_verdict"] = "ok"
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

    # ── Drop aggregator twins of providers we fetch directly ─────────────────
    # ComputePrices SKIP_PROVIDERS prevents these on a LIVE fetch, but a ComputePrices
    # outage triggers a cache fallback that can resurrect the stale cp_* twin, double-
    # counting against the direct fetcher (e.g. cp_together-ai alongside direct together).
    # Drop them unconditionally at assembly so direct always wins.
    SUPERSEDED_AGGREGATORS = {"cp_oracle", "cp_together-ai", "cp_hyperstack",
                              "cp_verda"}   # direct verda.py fetcher since 2026-08-11
    _before = len(all_records)
    all_records = [r for r in all_records if r.provider not in SUPERSEDED_AGGREGATORS]
    if len(all_records) < _before:
        logger.info(f"Dropped {_before - len(all_records)} superseded aggregator records "
                    f"(direct fetchers exist): {sorted(SUPERSEDED_AGGREGATORS)}")

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

    # ── Independent price cross-check (Phase 1.9) ────────────────────────────
    # Validate our directly-fetched on-demand prices against ComputePrices as an
    # independent second source. >5% disagreement → flag in manifest + downgrade the
    # affected records' confidence to "low". Replaces ad-hoc verification agents with
    # a standing daily check. Graceful: any failure just skips.
    try:
        from fetchers.computeprices import fetch_crosscheck
        xcheck = fetch_crosscheck()
        if xcheck:
            # Plausibility floor per GPU — below this, ComputePrices is the suspect
            # source (it carries occasional absurd values and committed-as-on-demand
            # mislabels), so we flag CP rather than undermining our own number.
            _CP_FLOOR = {"H100": 0.8, "H200": 1.0, "B200": 2.0, "B300": 2.5,
                         "GB200": 2.0, "GB300": 3.0, "L40S": 0.25}
            # cheapest direct on-demand price + its source per (provider, gpu).
            # Skip Nebius: it's our own product (we have verified internal prices);
            # an aggregator's third-hand Nebius data isn't a valid check on us.
            ours: Dict[tuple, tuple] = {}
            for r in accepted_records:
                if r.provider == "nebius":
                    continue
                if r.consumption_type == "on_demand" and r.data_source in ("official_api", "web_scrape"):
                    k = (r.provider, r.gpu_model)
                    if k not in ours or r.price_per_gpu_hour_usd < ours[k][0]:
                        ours[k] = (r.price_per_gpu_hour_usd, r.data_source)
            n_flagged = 0
            for k, (our_px, src) in ours.items():
                xp = xcheck.get(k)
                if not xp or our_px <= 0:
                    continue
                gap = abs(our_px - xp) / our_px * 100
                is_scrape = (src == "web_scrape")
                cp_implausible = xp < _CP_FLOOR.get(k[1], 0)
                if cp_implausible:
                    # CP value is below a sane floor — treat CP as the bad source.
                    if gap > 15:
                        n_flagged += 1
                        msg = (f"{k[0]} {k[1]} on-demand: ComputePrices ${xp:.2f} is implausibly "
                               f"low vs ours ${our_px:.2f} — ignoring CP (likely error/mislabel)")
                        logger.warning(f"Cross-check: {msg}")
                        warnings.append(f"cross-check: {msg}")
                    continue
                # Direction matters for scrapes (2026-07-14, Hyperstack incident):
                # we deliberately keep the provider's CHEAPEST variant, while CP's
                # fresh coverage may only include a pricier variant (e.g. SXM $3.20
                # when we correctly emit plain $2.50). Ours ABOVE CP's floor is the
                # real mis-parse signature (picking the pricey variant — the June
                # regression); ours moderately BELOW it is normal coverage
                # asymmetry. Only a huge low-side gap (>40%, e.g. parsing a CPU row
                # as a GPU) is treated as a suspect parse.
                # Our provider-API prices are authoritative → only flag a LARGE gap,
                # never downgrade because a flaky aggregator disagrees.
                if is_scrape:
                    if our_px > xp:
                        flag, downgrade = gap > 5, True
                        note = "our scrape may be mis-parsed — verify"
                    elif gap > 40:
                        flag, downgrade = True, True
                        note = ("ours far below CP's freshest coverage — verify we "
                                "didn't parse a non-GPU/spot row")
                    else:
                        flag, downgrade = False, False
                        if gap > 5:
                            logger.info(
                                f"Cross-check: {k[0]} {k[1]} ours ${our_px:.2f} below "
                                f"CP ${xp:.2f} ({gap:.0f}%) — cheapest-variant coverage "
                                f"asymmetry, not flagged")
                else:
                    flag, downgrade = gap > 15, False
                    note = "ComputePrices likely off (provider API is authoritative)"
                if flag:
                    n_flagged += 1
                    msg = (f"{k[0]} {k[1]} on-demand: ours ${our_px:.2f} ({'scrape' if is_scrape else 'api'}) "
                           f"vs ComputePrices ${xp:.2f} ({gap:.0f}% gap) — {note}")
                    logger.warning(f"Cross-check disagreement: {msg}")
                    warnings.append(f"cross-check: {msg}")
                    if downgrade:
                        for r in accepted_records:
                            if (r.provider == k[0] and r.gpu_model == k[1]
                                    and r.consumption_type == "on_demand"):
                                r.confidence = "low"
            logger.info(f"Cross-check: {len(ours)} direct on-demand prices vs ComputePrices — "
                        f"{n_flagged} flagged")
    except Exception as e:
        logger.debug(f"cross-check skipped: {e}")

    # ── Write canonical outputs (using validated records) ────────────────────
    save_snapshot(all_records, today)              # raw snapshot — includes everything
    save_last_snapshot(accepted_records)           # baseline for next diff — accepted only
    append_history_records(accepted_records, today)  # trend CSV — accepted only
    # Persist today's computed position gaps so the Monday anchor can report
    # week-over-week movement consistent with what was actually published.
    from diff import record_position_history
    record_position_history(accepted_records)

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

    # ── Post-worthiness: gate the full-tables thread ─────────────────────────
    # Always post the short summary daily (channel presence + run-health). The
    # full thread (all tables) is the WEEKLY anchor: post it on Mondays, and
    # otherwise only when the market actually moved enough to be worth opening.
    # Two sources of daily noise are explicitly EXCLUDED from the gate, because
    # they fire nearly every day and made the thread post daily (repetitive):
    #   - spot/preemptible prices jitter a few percent daily as normal market
    #     behaviour, so interruptible moves never trigger the thread;
    #   - "added" diffs are mostly fetch-coverage churn (a provider or zone
    #     cycling between live and cached), not genuine new competitor SKUs,
    #     so they no longer trigger it either.
    # What remains: a hyperscaler repricing on-demand/committed, or 3+ tracked
    # providers moving the same day (a coordinated shift). A single neocloud SKU
    # tick is reported in the daily summary line instead of re-posting all tables.
    from config import ALERT_THRESHOLD_PCT, provider_tier
    from diff import INTERRUPTIBLE_CTS
    _tracked = ("hyperscaler", "raw_gpu_cloud", "enterprise_gpu_cloud")
    list_moves = [
        d for d in diffs
        if d.change_type == "price_change"
        and abs(d.delta_pct or 0) >= ALERT_THRESHOLD_PCT
        and provider_tier(d.provider) in _tracked
        and d.consumption_type not in INTERRUPTIBLE_CTS
    ]
    significant_moves = list_moves  # recorded in the manifest for visibility
    hyperscaler_move = any(provider_tier(d.provider) == "hyperscaler" for d in list_moves)
    coordinated_move = len(list_moves) >= 3
    is_weekly = today.weekday() == 0  # Monday weekly anchor
    post_thread = is_weekly or hyperscaler_move or coordinated_move

    slack_summary = format_slack_summary(
        diffs, run_date, CONFLUENCE_PAGE_URL,
        records=accepted_records,
        provider_status=provider_status,
        post_thread=post_thread,
        weekly=is_weekly,
    )
    slack_thread = format_slack_message(
        diffs, run_date, CONFLUENCE_PAGE_URL,
        records=accepted_records,
        provider_status=provider_status,
    )

    # Data-quality anomalies are INTERNAL ONLY — never prepended to the exec/sales
    # broadcast. Implausible-range records (e.g. serverless platforms like Modal whose
    # per-second rate reads as $0.07/GPU-hr) are already excluded from every displayed
    # table (formatters use accepted_records), so the warning is a maintainer signal,
    # not something execs should see. It stays in the logs and run_manifest warnings.
    implausible = [a for a in anomalies if "implausibly" in a]
    if implausible:
        logger.warning(f"Implausible-price records excluded from output: {implausible}")

    # ── Storage benchmark: change lines + sibling page + drift warnings ─────
    # (2026-08-15) STORAGE_PRICES is a curated verified table; when a value is
    # deliberately updated, the change surfaces once in the daily digest.
    try:
        from config import STORAGE_PRICES
        prev_path = STORE_DIR / "storage_prices_prev.json"
        flat = {f"{sec}:{r['provider']}:{r['name']}": (r["price"], r.get("currency", "USD"))
                for sec, rows in STORAGE_PRICES.items() for r in rows}
        prev = json.loads(prev_path.read_text()) if prev_path.exists() else {}
        storage_moves = []
        for k, (px, cur) in flat.items():
            old = prev.get(k)
            if old and abs(old[0] - px) > 1e-9:
                sym = "€" if cur == "EUR" else "$"
                _sec, prov, name = k.split(":", 2)
                storage_moves.append(f"{prov.title()} {name} {sym}{old[0]:g}→{sym}{px:g}/GiB-mo")
        prev_path.write_text(json.dumps({k: list(v) for k, v in flat.items()}, indent=0))
        if storage_moves:
            line = "*Storage:* " + " · ".join(storage_moves[:3])
            # Insert above the standing footer so the line reads as part of the
            # day's changes (slack_summary is posted verbatim by the routine).
            anchor = "\nFull benchmark (live, updated daily):"
            if anchor in slack_summary:
                slack_summary = slack_summary.replace(anchor, f"\n{line}" + anchor, 1)
            else:
                slack_summary += f"\n\n{line}"
            logger.info(f"Storage benchmark changes: {storage_moves}")
        from storage_page import format_storage_page
        with open(STORE_DIR / "storage_body.html", "w") as f:
            f.write(format_storage_page(run_date))
        drift_file = STORE_DIR / "storage_drift.txt"
        if drift_file.exists():
            for _l in drift_file.read_text().splitlines():
                warnings.append(_l)   # internal-only, like other data-quality warnings
    except Exception as e:
        logger.warning(f"storage benchmark step failed (non-blocking): {e}")

    slack_path = STORE_DIR / "slack_message.txt"
    with open(slack_path, "w") as f:
        f.write(slack_summary)
    thread_path = STORE_DIR / "slack_thread.txt"
    with open(thread_path, "w") as f:
        f.write(slack_thread)

    confluence_body = format_confluence_table(accepted_records, run_date,
                                              provider_status=provider_status,
                                              diffs=diffs)
    # Separate competitor spot/auction page for the PVM Auctions project (own pipeline output)
    spot_auction_body = format_spot_auction_page(accepted_records, run_date)
    with open(STORE_DIR / "spot_auction_body.html", "w") as f:
        f.write(spot_auction_body)
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

    # ── Per-source freshness summary ──────────────────────────────────────────
    # Compact {source: {status, age_hours}} derived from provider_status, so
    # staleness is visible to the CCR publish agent and to anyone reading the
    # manifest. status: "live" (fetched this run) | "cached" (served from
    # peer_cache.json) | "stale" (cached AND past the soft threshold) | "dropped"
    # (cached but too old to serve) | "missing" | "error". age_hours is the cache
    # age for cached/stale/dropped sources, else null.
    provider_freshness: Dict[str, dict] = {}
    for p, s in provider_status.items():
        raw_status = s.get("status")
        age = s.get("cache_age_hours")
        if raw_status == "live":
            fr_status = "live"
        elif raw_status == "fallback":
            fr_status = "live"  # SkyPilot catalog is a live alternate source, not a cache
        elif raw_status == "cache":
            verdict = s.get("stale_verdict")
            fr_status = "stale" if verdict == "stale" else "cached"
        elif raw_status == "missing":
            # ComputePrices records dropped by the hard staleness guard report "dropped";
            # a genuinely empty source with no cache reports "missing".
            fr_status = "dropped" if s.get("stale_verdict") == "drop" else "missing"
        else:
            fr_status = raw_status or "unknown"
        provider_freshness[p] = {
            "status":    fr_status,
            "age_hours": round(age, 1) if isinstance(age, (int, float)) else None,
        }

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
        "provider_freshness": provider_freshness,
        "warnings":          warnings,
        # Phase 3.6: posting hints for the CCR routine.
        "post_thread":       post_thread,   # post full tables thread? (change or weekly)
        "is_weekly":         is_weekly,     # Monday weekly digest
        "significant_moves": len(significant_moves),
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


# Provider fetch-key -> fetcher module. Every entry in config.PROVIDERS MUST
# have a row here — enforced by test_consistency.check_provider_dispatch, so a
# provider registered in config without a dispatcher entry fails the publish
# gate instead of erroring silently at 01:23 UTC (2026-08-12 incident: verda +
# aws_capacity_blocks were added to PROVIDERS but not to the old if/elif chain
# and ran as status=error on their first production cycle).
PROVIDER_MODULES = {
    "aws": "aws",
    "gcp": "gcp",
    "azure": "azure",
    "coreweave": "coreweave",
    "lambda": "lambda_labs",
    "crusoe": "crusoe",
    "nebius": "nebius",
    "nebius_committed": "nebius_committed",
    "computeprices": "computeprices",
    "oracle": "oracle",
    "hyperstack": "hyperstack",
    "runpod": "runpod",
    "sfcompute": "sfcompute",
    "together": "together",
    "vast_reserved": "vast_reserved",
    "sfcompute_fills": "sfcompute_fills",
    "verda": "verda",
    "aws_capacity_blocks": "aws_capacity_blocks",
    "modal": "modal",
    "baseten": "baseten",
}


def _fetch_provider(provider: str):
    module = PROVIDER_MODULES.get(provider)
    if module is None:
        raise ValueError(f"Unknown provider: {provider} — add it to PROVIDER_MODULES")
    import importlib
    return importlib.import_module(f"fetchers.{module}").fetch()


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
