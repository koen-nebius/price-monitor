#!/usr/bin/env python3
"""
Refresh the quality columns of store/intel.csv and write the duplicates report.

  python3 scripts/intel_quality_refresh.py            # rewrites intel.csv in place (adds/updates prepay_known)
  python3 scripts/intel_quality_refresh.py --check    # report only, no write

Adds the column `prepay_known` (1 = prepayment stated in the quote, 0 = the 0 % is a
parser default and must be treated as unknown) using intel_quality.prepay_known, and
writes store/intel_duplicates.csv with one line per flagged row and an `action` column:
  removed  confirmed repeat of an offer already on file (same provider, or a seed row that
           repeats a retrieved row); consumers drop it
  review   looks like another offer (same Slack message with a different provider, or a
           seed row matching another provider's row); consumers KEEP it, a human decides
Rows are never deleted here: consumers (forward_curve.py, and diff.py through its own key)
decide what to count. Run after any bulk import (the audit merge of 2026-09-15 was one).
"""
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from intel_quality import classify, prepay_known  # noqa: E402

INTEL = ROOT / "store" / "intel.csv"
REPORT = ROOT / "store" / "intel_duplicates.csv"
REPORT_COLS = ["action", "message_ts", "message_date", "gpu_model", "price_per_gpu_hour_usd", "term_months",
               "provider_name", "matches_message_ts", "matches_provider", "reason"]


def main(argv) -> int:
    check = "--check" in argv
    with open(INTEL, newline="") as f:
        reader = csv.DictReader(f)
        cols = list(reader.fieldnames)
        rows = list(reader)
    if "prepay_known" not in cols:
        cols.append("prepay_known")
    changed = 0
    for r in rows:
        v = "1" if prepay_known(r) else "0"
        if r.get("prepay_known") != v:
            changed += 1
        r["prepay_known"] = v
    kept, removed, review = classify(rows)
    known = sum(1 for r in rows if r["prepay_known"] == "1")
    print(f"{len(rows)} rows · prepay known {known} / unknown {len(rows) - known} · removed {len(removed)} confirmed repeats · "
          f"{len(review)} kept for review · distinct offers {len(kept)}"
          + (f" · prepay_known updated on {changed} rows" if changed else ""))
    if check:
        for action, items in (("removed", removed), ("review", review)):
            for d in items:
                r = d["row"]
                other = d.get("duplicate_of") or d.get("similar_to")
                print(f"  {action:7} {r['message_date']} {r['gpu_model']:5} ${float(r['price_per_gpu_hour_usd']):5.2f} {r['term_months']:>3}m {r['provider_name'][:18]:18} [{str(r['message_ts'])[:16]}] -> {str(other)[:16]} ({d['reason']})")
        return 0
    with open(INTEL, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    with open(REPORT, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(REPORT_COLS)
        for action, items in (("removed", removed), ("review", review)):
            for d in items:
                r = d["row"]
                other_ts = d.get("duplicate_of") or d.get("similar_to")
                other_prov = d.get("duplicate_provider") or d.get("similar_provider") or ""
                w.writerow([action, r["message_ts"], r["message_date"], r["gpu_model"], r["price_per_gpu_hour_usd"], r["term_months"],
                            r["provider_name"], other_ts, other_prov, d["reason"]])
    print(f"wrote {INTEL.name} (+prepay_known) and {REPORT.name} ({len(removed)} removed, {len(review)} review)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
