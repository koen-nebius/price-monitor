"""
CCR publish gate: validates run_manifest.json before the agent posts to Slack/Confluence.

Exit codes:
  0 — manifest is valid, run is fresh, safe to publish
  1 — stale, failed, or missing manifest — do NOT publish; post alert instead

Usage (in CCR agent prompt):
    python3 check_manifest.py
    # If exit code != 0, read check_manifest_result.txt and post its contents
    # as a Slack alert instead of the normal digest.

Writes store/check_manifest_result.txt with a human-readable summary either way,
so the CCR agent always has context for what to say.
"""
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

STORE_DIR = Path(__file__).parent / "store"
MANIFEST_PATH = STORE_DIR / "run_manifest.json"
RESULT_PATH = STORE_DIR / "check_manifest_result.txt"

# Gate thresholds
MIN_RECORD_COUNT = 200   # floor — normal run produces 700+; single-provider runs can be low
MAX_STALE_HOURS = 25     # how old the run_date can be before we refuse to publish

# Per-provider completeness gate (2026-07-14, external-review fix: a run that
# lost 62% of its AWS records still passed as "success / safe to publish").
# Known transition window: when a parser change legitimately GROWS a provider's
# record count (e.g. the 2026-07-14 AWS reserved fix adds ~partial-upfront CTs),
# the block floor lags below the new normal until the 7-run median catches up
# (~4 passing runs). Catastrophic drops still block throughout; only the
# 50-75%-of-new-basis zone is temporarily under-protected. Accepted trade-off.
# Baseline = median of the last BASELINE_KEEP gate-PASSING record counts per
# provider, stored in store/provider_baseline.json and updated ONLY by this
# script immediately before a passing exit — a degraded run can never lower
# the bar for the next one. Providers whose baseline is tiny (< MIN_BASELINE)
# are exempt from the ratio test: their day-to-day variation (sfcompute=1,
# vast_reserved 2-4, crusoe=3...) makes any % threshold pure noise, and their
# real failure modes (empty fetch / exception) already surface as
# stale_providers / status=partial.
BASELINE_PATH = STORE_DIR / "provider_baseline.json"
BASELINE_KEEP = 7        # rolling window of passing runs per provider
MIN_BASELINE = 20        # ratio test only for providers this big
BLOCK_DROP_RATIO = 0.5   # < 50% of baseline -> do not publish
WARN_DROP_RATIO = 0.75   # < 75% of baseline -> publish but annotate


def main() -> int:
    today = date.today().isoformat()
    now = datetime.now(timezone.utc)

    # ── 1. Manifest must exist ───────────────────────────────────────────────
    if not MANIFEST_PATH.exists():
        msg = (
            f"⚠ *Price monitor: no run manifest found*\n"
            f"Expected `store/run_manifest.json` — the 06:00 GitHub Actions run may not have completed.\n"
            f"_Not publishing today's digest. Check GitHub Actions logs._"
        )
        RESULT_PATH.write_text(msg)
        print(msg)
        return 1

    try:
        manifest = json.loads(MANIFEST_PATH.read_text())
    except Exception as e:
        msg = f"⚠ *Price monitor: run manifest unreadable*\n`{e}`\n_Not publishing._"
        RESULT_PATH.write_text(msg)
        print(msg)
        return 1

    run_date = manifest.get("run_date", "")
    status   = manifest.get("status", "unknown")
    records  = manifest.get("record_count", 0)
    errors   = manifest.get("failed_providers", [])
    stale    = manifest.get("stale_providers", [])
    warnings = manifest.get("warnings", [])
    completed_at_str = manifest.get("completed_at", "")

    issues = []

    # ── 2. Run must be from today ────────────────────────────────────────────
    if run_date != today:
        issues.append(f"run_date is {run_date!r}, expected {today!r} — data may be stale")

    # ── 3. Check completed_at age ────────────────────────────────────────────
    if completed_at_str:
        try:
            completed_at = datetime.fromisoformat(completed_at_str)
            if completed_at.tzinfo is None:
                completed_at = completed_at.replace(tzinfo=timezone.utc)
            age_hours = (now - completed_at).total_seconds() / 3600
            if age_hours > MAX_STALE_HOURS:
                issues.append(f"run completed {age_hours:.0f}h ago — may be yesterday's data")
        except Exception:
            pass

    # ── 4. Status must not be "failed" ───────────────────────────────────────
    if status == "failed":
        issues.append(f"run status is 'failed' — majority of providers errored")

    # ── 5. Minimum record count ──────────────────────────────────────────────
    if records < MIN_RECORD_COUNT:
        issues.append(f"only {records} records (minimum is {MIN_RECORD_COUNT}) — fetch may have been incomplete")

    # ── 6. Per-provider completeness vs baseline ─────────────────────────────
    # Catches partial fetches that still return "some" data (regional API
    # failures, silent truncation) — status='live' only means non-empty.
    pstatus = manifest.get("provider_status", {})
    baseline: dict = {}
    if BASELINE_PATH.exists():
        try:
            baseline = json.loads(BASELINE_PATH.read_text())
        except Exception:
            baseline = {}
    soft_notes = []
    warn_providers = set()   # below WARN ratio — published but NEVER folded into
                             # the baseline, or a sustained partial fetch would
                             # ratchet the bar down 25% per window and a -60%
                             # loss would become the new normal within days.
    for prov, counts in sorted(baseline.items()):
        if not counts:
            continue
        base = sorted(counts)[len(counts) // 2]   # median of passing runs
        info = pstatus.get(prov)
        if info is None:
            issues.append(f"{prov}: present in the last {len(counts)} passing runs "
                          f"but absent from this manifest — provider silently vanished "
                          f"(decommissioned? delete its entry in provider_baseline.json)")
            continue
        if base < MIN_BASELINE or info.get("status") != "live":
            # tiny providers: ratio is noise; non-live already surfaces as stale
            continue
        count = info.get("record_count", 0)
        if count < base * BLOCK_DROP_RATIO:
            issues.append(f"{prov}: {count} records vs baseline {base} "
                          f"({(count - base) / base * 100:+.0f}%) — partial fetch, not publishing")
        elif count < base * WARN_DROP_RATIO:
            warn_providers.add(prov)
            soft_notes.append(f"{prov} {count} vs baseline {base} "
                              f"({(count - base) / base * 100:+.0f}%)")

    # ── Build result message ─────────────────────────────────────────────────
    if issues:
        issue_lines = "\n".join(f"• {i}" for i in issues)
        notes = []
        if errors:
            notes.append(f"Failed providers: {', '.join(errors)}")
        if stale:
            notes.append(f"Stale/cached providers: {', '.join(stale)}")
        note_str = ("\n" + "\n".join(f"  {n}" for n in notes)) if notes else ""
        msg = (
            f"⚠ *Price monitor: publish gate failed — not posting today's digest*\n"
            f"{issue_lines}{note_str}\n"
            f"_Check GitHub Actions run for {run_date}._"
        )
        RESULT_PATH.write_text(msg)
        print(f"GATE FAILED: {'; '.join(issues)}")
        return 1

    # ── All checks passed ────────────────────────────────────────────────────
    # Build an informational summary (used by CCR agent for context, not posted)
    notes = []
    if errors:
        notes.append(f"failed providers: {', '.join(errors)}")
    if stale:
        # Format with cache ages if available
        stale_parts = []
        pstatus = manifest.get("provider_status", {})
        for p in stale:
            info = pstatus.get(p, {})
            age = info.get("cache_age_hours")
            src = info.get("fallback_source")
            if info.get("status") == "fallback" and src:
                stale_parts.append(f"{p} (via {src})")
            elif age is not None:
                stale_parts.append(f"{p} (cached {age:.0f}h)")
            else:
                stale_parts.append(p)
        notes.append(f"stale: {', '.join(stale_parts)}")
    if warnings:
        notes.append(f"{len(warnings)} warning(s)")
    if soft_notes:
        notes.append(f"below baseline (publishing anyway): {', '.join(soft_notes)}")

    note_str = f" ({'; '.join(notes)})" if notes else ""
    msg = (
        f"✓ Manifest OK — {run_date}, {records} records, status={status}{note_str}\n"
        f"Safe to publish."
    )
    RESULT_PATH.write_text(msg)
    print(msg)

    # Blessed run: fold its per-provider counts into the rolling baseline.
    # Only reached on gate pass, and warn-zone providers are excluded, so a
    # degraded fetch — abrupt OR sustained — never lowers the bar it will be
    # judged against tomorrow. (Upward shifts DO fold in immediately, so a
    # legitimately grown provider raises its own bar within the window.)
    # CI-only: the 07:00 posting routine re-runs this gate in a write-free
    # checkout, where a second fold would just leave the file dirty (the CI
    # run already folded and committed today's counts).
    if not os.environ.get("GITHUB_ACTIONS"):
        return 0
    try:
        for prov, info in pstatus.items():
            if prov in warn_providers:
                continue
            if info.get("status") == "live" and info.get("record_count", 0) > 0:
                counts = baseline.setdefault(prov, [])
                counts.append(info["record_count"])
                del counts[:-BASELINE_KEEP]
        BASELINE_PATH.write_text(json.dumps(baseline, indent=2, sort_keys=True))
    except Exception as e:
        print(f"(baseline update skipped: {e})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
