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
import sys
from datetime import date, datetime, timezone
from pathlib import Path

STORE_DIR = Path(__file__).parent / "store"
MANIFEST_PATH = STORE_DIR / "run_manifest.json"
RESULT_PATH = STORE_DIR / "check_manifest_result.txt"

# Gate thresholds
MIN_RECORD_COUNT = 200   # floor — normal run produces 700+; single-provider runs can be low
MAX_STALE_HOURS = 25     # how old the run_date can be before we refuse to publish


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

    note_str = f" ({'; '.join(notes)})" if notes else ""
    msg = (
        f"✓ Manifest OK — {run_date}, {records} records, status={status}{note_str}\n"
        f"Safe to publish."
    )
    RESULT_PATH.write_text(msg)
    print(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
