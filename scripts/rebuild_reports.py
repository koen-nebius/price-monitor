#!/usr/bin/env python3
"""Regenerate pricing artifacts offline from an existing, dated accepted snapshot.

No fetchers, credentials, provider calls, raw-store writes or publication calls.
The retrieval manifest retains its dates and health. Generation time and hashes
are separate. A prior snapshot is usable only with its matching run manifest;
when absent, local git history is inspected without fetching from any remote.
"""
import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import diff as renderer
from config import ALERT_THRESHOLD_PCT, CONFLUENCE_PAGE_URL, provider_tier
from report_freshness import publication_records
from schema import PriceRecord


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _instant(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc)


def read_run(snapshot_bytes, manifest_bytes, label):
    """Validate that the accepted snapshot belongs to the stated retrieval run."""
    manifest = json.loads(manifest_bytes)
    rows = json.loads(snapshot_bytes)
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"{label}: accepted snapshot is empty or not a record list")
    day = date.fromisoformat(manifest["run_date"])
    completed = _instant(manifest["completed_at"])
    if completed.date() != day:
        raise ValueError(f"{label}: completion date does not match run_date")
    if manifest.get("record_count") != len(rows):
        raise ValueError(f"{label}: snapshot count does not match its accepted-record manifest")
    observations = [_instant(r["fetched_at"]) for r in rows if r.get("fetched_at")]
    if not observations or max(observations) > completed or max(observations).date() != day:
        raise ValueError(f"{label}: snapshot observation dates do not belong to the retrieval run")
    records = [PriceRecord.from_dict(row) for row in rows]
    provenance = {"source": label, "run_date": day.isoformat(),
                  "snapshot_sha256": _hash(snapshot_bytes), "manifest_sha256": _hash(manifest_bytes),
                  "observation_min": min(observations).isoformat(),
                  "observation_max": max(observations).isoformat()}
    return records, manifest, provenance


def _git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], check=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    return result.stdout


def previous_git_run(repo: Path, before: str):
    """Find a paired earlier run; never substitute cheapest-only history rows."""
    try:
        commits = _git(repo, "log", "-60", "--format=%H", "--", "store/run_manifest.json").decode().splitlines()
    except (subprocess.SubprocessError, OSError):
        return None
    for commit in commits:
        try:
            manifest_bytes = _git(repo, "show", f"{commit}:store/run_manifest.json")
            metadata = json.loads(manifest_bytes)
            if not metadata.get("run_date", "") < before:
                continue
            snapshot = _git(repo, "show", f"{commit}:store/last_snapshot.json")
            run = read_run(snapshot, manifest_bytes, f"git:{commit}:store/last_snapshot.json")
            return run
        except (subprocess.SubprocessError, OSError, ValueError, KeyError, TypeError):
            continue
    return None


@contextmanager
def _dated_renderer(day, history_path):
    # Legacy helpers use date.today for rolling windows and the reversion ledger.
    # Anchor those reads to the source report date, not the regeneration day.
    original_date, original_history = renderer.date, renderer.HISTORY_CSV
    class ReportDate(date):
        @classmethod
        def today(cls):
            return cls(day.year, day.month, day.day)
    renderer.date, renderer.HISTORY_CSV = ReportDate, history_path
    try:
        yield
    finally:
        renderer.date, renderer.HISTORY_CSV = original_date, original_history


def _key(value):
    return (value.provider, value.gpu_model, value.instance_type, value.region, value.consumption_type)


def rebuild_reports(store_dir=ROOT / "store", *, previous_snapshot=None,
                    previous_manifest=None, discover_git=True, generated_at=None):
    store_dir = Path(store_dir).resolve()
    snapshot_path, manifest_path = store_dir / "last_snapshot.json", store_dir / "run_manifest.json"
    snapshot_bytes, manifest_bytes = snapshot_path.read_bytes(), manifest_path.read_bytes()
    records, original, source = read_run(snapshot_bytes, manifest_bytes, "store/last_snapshot.json")
    day = date.fromisoformat(original["run_date"])
    run_date = day.strftime("%B %d, %Y")
    if bool(previous_snapshot) != bool(previous_manifest):
        raise ValueError("An explicit previous snapshot requires its matching previous manifest")
    previous = None
    if previous_snapshot:
        previous = read_run(Path(previous_snapshot).read_bytes(), Path(previous_manifest).read_bytes(),
                            str(Path(previous_snapshot).resolve()))
        if previous[1]["run_date"] >= original["run_date"]:
            raise ValueError("Previous snapshot must precede the source report date")
    elif discover_git:
        previous = previous_git_run(store_dir.parent, original["run_date"])

    eligible, exclusions = publication_records(records, run_date)
    eligible_keys = {_key(r) for r in eligible}
    old_records = previous[0] if previous else []
    old_eligible = publication_records(old_records, previous[1]["run_date"])[0] if previous else []
    old_keys = {_key(r) for r in old_eligible}
    with _dated_renderer(day, store_dir / "history.csv"):
        raw_diffs = renderer.compute_diff(old_records, records) if previous else []
        # Keep corrections as corrections, including Together's excluded old OD
        # baseline. Market alerts require both observations to be eligible.
        diffs = [d for d in raw_diffs if d.change_type == "restatement"
                 or (d.change_type == "removed" and _key(d) in old_keys)
                 or (d.change_type == "added" and _key(d) in eligible_keys)
                 or (_key(d) in old_keys and _key(d) in eligible_keys)]
        list_moves = [d for d in diffs if d.change_type == "price_change"
                      and not d.provider.startswith(("cp_", "sf_"))
                      and abs(d.delta_pct or 0) >= ALERT_THRESHOLD_PCT
                      and provider_tier(d.provider) in {"hyperscaler", "raw_gpu_cloud", "enterprise_gpu_cloud"}
                      and d.consumption_type not in renderer.INTERRUPTIBLE_CTS]
        weekly = day.weekday() == 0
        post_thread = (weekly or any(provider_tier(d.provider) == "hyperscaler" for d in list_moves)
                       or len({d.provider for d in list_moves}) >= 3
                       or any(d.change_type == "restatement" for d in diffs))
        providers = original.get("provider_status", {})
        outputs = {
            "slack_message.txt": renderer.format_slack_summary(
                diffs, run_date, CONFLUENCE_PAGE_URL, records=records,
                provider_status=providers, post_thread=post_thread, weekly=weekly),
            "slack_thread.txt": renderer.format_slack_message(
                diffs, run_date, CONFLUENCE_PAGE_URL, records=records, provider_status=providers),
            "confluence_body.html": renderer.format_confluence_table(records, run_date, providers, diffs),
            "spot_auction_body.html": renderer.format_spot_auction_page(records, run_date),
            f"report_diff_{day.isoformat()}.json": json.dumps([d.to_dict() for d in diffs], indent=2),
        }
    if previous is None:
        note = "No coherent earlier accepted snapshot is available; price-change comparison was not regenerated."
        for name in ("slack_message.txt", "slack_thread.txt"):
            lines = [line for line in outputs[name].splitlines() if not line.startswith(
                ("No observed competitor price changes", "No eligible price changes"))]
            lines.insert(1, note)
            outputs[name] = "\n".join(lines)
        outputs["confluence_body.html"] = "<p>" + note + "</p>" + outputs["confluence_body.html"]

    # Prepare all bytes and the updated manifest before writing any artifact.
    generated = generated_at or datetime.now(timezone.utc).isoformat()
    artifact_bytes = {name: (text.rstrip() + "\n").encode() for name, text in outputs.items()}
    manifest = dict(original)
    manifest.setdefault("retrieval_diff_count", original.get("diff_count"))
    manifest.update({"diff_count": len(diffs), "post_thread": post_thread, "is_weekly": weekly,
                     "significant_moves": len(list_moves), "comparison_exclusions": exclusions,
                     "comparison_record_count": len(eligible), "artifacts_generated_at": generated})
    manifest["generated_outputs"] = {**original.get("generated_outputs", {}),
                                     "slack_message": True, "slack_thread": True,
                                     "confluence_body": True, "spot_auction_body": True}
    manifest["artifact_generation"] = {
        "mode": "offline_regeneration", "generated_at": generated,
        "source": source, "comparison_baseline": previous[2] if previous else None,
        "comparison_available": previous is not None,
        "source_exclusions": exclusions,
        "suppressed_ineligible_diff_records": len(raw_diffs) - len(diffs),
        "diff_types": dict(Counter(d.change_type for d in diffs)),
        "artifacts": {name: {"sha256": _hash(data), "bytes": len(data)} for name, data in artifact_bytes.items()},
        "raw_observations_unchanged": True,
    }
    # Any prior delivery keys/receipts remain untouched. Regeneration creates no
    # delivery status and cannot imply that existing destinations were updated.
    for name, data in artifact_bytes.items():
        (store_dir / name).write_bytes(data)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    if snapshot_path.read_bytes() != snapshot_bytes:
        raise RuntimeError("Raw accepted snapshot changed during report regeneration")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store-dir", type=Path, default=ROOT / "store")
    parser.add_argument("--previous-snapshot", type=Path)
    parser.add_argument("--previous-manifest", type=Path)
    parser.add_argument("--no-git-baseline", action="store_true")
    args = parser.parse_args()
    manifest = rebuild_reports(args.store_dir, previous_snapshot=args.previous_snapshot,
                               previous_manifest=args.previous_manifest, discover_git=not args.no_git_baseline)
    print(json.dumps({"run_date": manifest["run_date"], "retrieval_status": manifest["status"],
                      "artifacts_generated_at": manifest["artifacts_generated_at"],
                      "post_thread": manifest["post_thread"],
                      "artifact_generation": manifest["artifact_generation"]}, indent=2))


if __name__ == "__main__":
    main()
