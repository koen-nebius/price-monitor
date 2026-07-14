"""
One-time migration (2026-07-15): restate pre-fix AWS reserved rows in
store/history.csv to upfront-amortized effective rates.

Why this is exact, not fabrication: the pre-fix parser read only the recurring
hourly component of the SAME partial-upfront offer, and every AWS reserved
series in history is perfectly FLAT over its whole 45-day window (verified
2026-07-14) — AWS never repriced those offers. So the true effective series
was also flat, at exactly the value the fixed parser (schema PARSER_VERSION
2.1) measures today. Restating removes the fake ~2x "step" the trend chart
would otherwise show at the changeover date.

Method: for each (gpu, ct, REGION, instance_type) the post-fix snapshot
provides the corrected effective price; pre-fix rows are rewritten to it.
Region is part of the key — RI pricing is NOT globally uniform (ap-northeast-1
runs ~25% above us-east-1); rows whose exact region is absent from the
post-fix snapshot are left untouched and reported.

Run ONCE after the first post-fix pipeline run, then commit history.csv:
    python3 scripts/restate_aws_reserved_history.py        # dry-run report
    python3 scripts/restate_aws_reserved_history.py --apply
Aborts without writing if any pre-fix series is not flat, or if the snapshot
still carries pre-fix values (no parser-2.1 AWS reserved records yet).
"""
import csv
import json
import shutil
import sys
from collections import defaultdict
from pathlib import Path

STORE = Path(__file__).resolve().parent.parent / "store"
HISTORY = STORE / "history.csv"
SNAPSHOT = STORE / "last_snapshot.json"
CTS = {"reserved_1yr", "reserved_3yr"}


def main() -> int:
    apply = "--apply" in sys.argv

    snap = json.loads(SNAPSHOT.read_text())
    corrected = {}
    for r in snap:
        if (r.get("provider") == "aws" and r.get("consumption_type") in CTS
                and r.get("parser_version") == "2.1"):
            corrected[(r["gpu_model"], r["consumption_type"], r["region"],
                       r["instance_type"])] = float(r["price_per_gpu_hour_usd"])
    if not corrected:
        print("ABORT: last_snapshot.json has no parser-2.1 AWS reserved records yet — "
              "run this only after the first post-fix pipeline run.")
        return 1

    rows = list(csv.DictReader(HISTORY.open()))
    fields = rows[0].keys()

    def _matches(a: float, b: float) -> bool:
        # history.csv rounds prices (~4 decimals) while the snapshot carries
        # full precision — compare with relative tolerance, not equality.
        return b > 0 and abs(a - b) / b < 1e-3

    # Flatness guard: every pre-fix series must be single-valued.
    seen = defaultdict(set)
    for r in rows:
        if r["provider"] == "aws" and r["consumption_type"] in CTS:
            key = (r["gpu_model"], r["consumption_type"], r["region"],
                   r["instance_type"])
            if key in corrected and not _matches(
                    float(r["price_per_gpu_hour_usd"]), corrected[key]):
                seen[key].add(r["price_per_gpu_hour_usd"])
    bad = {k: v for k, v in seen.items() if len(v) > 1}
    if bad:
        print(f"ABORT: non-flat pre-fix series, restatement would not be exact: {bad}")
        return 1

    changed = 0
    for r in rows:
        if r["provider"] != "aws" or r["consumption_type"] not in CTS:
            continue
        key = (r["gpu_model"], r["consumption_type"], r["region"],
               r["instance_type"])
        new = corrected.get(key)
        if new is None:
            print(f"  SKIP (region not in post-fix snapshot): {r['snapshot_date']} {key}")
            continue
        old = float(r["price_per_gpu_hour_usd"])
        if _matches(old, new):
            continue
        gpus = float(r.get("gpu_count") or 1)
        print(f"  {r['snapshot_date']} {key[0]} {key[1]} {key[2]} "
              f"{key[3]}: {old} -> {new:.4f}")
        r["price_per_gpu_hour_usd"] = f"{new:g}"
        r["price_per_hour_usd"] = f"{new * gpus:g}"
        changed += 1

    print(f"{'RESTATED' if apply else 'DRY-RUN, would restate'} {changed} rows "
          f"across {len(corrected)} corrected series.")
    if apply and changed:
        shutil.copy(HISTORY, HISTORY.with_suffix(".csv.pre-restatement.bak"))
        with HISTORY.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
        print(f"Written. Backup at {HISTORY.with_suffix('.csv.pre-restatement.bak').name}; "
              f"commit history.csv to publish.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
