"""
Validate (and optionally clean) store/intel.csv against the field-intel schema.

The CCR agent appends rows to intel.csv from #price-intelligence. This enforces the
same contract a forced tool-call would: real rows must validate or they're dropped,
so malformed quotes never reach the decision-trigger "competitor field deal" column
or the battlecards. Seed rows (message_ts starts with 'seed_') are left untouched.

Usage:
    python3 validate_intel.py          # report; exit 1 if any invalid real rows
    python3 validate_intel.py --fix     # drop invalid real rows (backs up to .bak), exit 0
"""
import csv
import sys
from pathlib import Path

from intel_schema import validate_row

INTEL_CSV = Path(__file__).parent / "store" / "intel.csv"


def main() -> int:
    fix = "--fix" in sys.argv
    if not INTEL_CSV.exists():
        print("no intel.csv — nothing to validate")
        return 0

    with open(INTEL_CSV, newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    kept, dropped = [], []
    for row in rows:
        if (row.get("message_ts", "") or "").startswith("seed_"):
            kept.append(row)             # seed data: leave untouched
            continue
        problems = validate_row(row)
        if problems:
            dropped.append((row, problems))
        else:
            kept.append(row)

    if not dropped:
        print(f"intel.csv: {len(kept)} rows, all valid")
        return 0

    print(f"intel.csv: {len(dropped)} invalid row(s):")
    for row, problems in dropped:
        print(f"  ✗ {row.get('message_date')} {row.get('provider_name')} "
              f"{row.get('gpu_model')} ${row.get('price_per_gpu_hour_usd')}: {'; '.join(problems)}")

    if fix:
        INTEL_CSV.with_suffix(".csv.bak").write_text(INTEL_CSV.read_text())
        with open(INTEL_CSV, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(kept)
        print(f"--fix: dropped {len(dropped)} invalid row(s); kept {len(kept)} (backup: intel.csv.bak)")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
