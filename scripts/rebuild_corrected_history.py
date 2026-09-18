#!/usr/bin/env python3
"""Write an explicitly separate, audited analytical view of existing history.

Does not refresh providers or overwrite raw history/snapshots. Excluded rows stay
in the derived file with comparison_eligible=False for inspection. Analytical
consumers should use price_corrections.correct_history_rows to omit those rows.
"""
import argparse
import csv
import hashlib
import json
from collections import Counter
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from price_corrections import AUDIT_COLUMNS, correct_history_rows


def write_derived_history(source: Path, destination: Path) -> dict:
    source, destination = source.resolve(), destination.resolve()
    if source == destination or destination == (ROOT / "store/history.csv").resolve():
        raise ValueError("The corrected view must be separate from raw history.csv")
    with source.open(newline="") as stream:
        reader = csv.DictReader(stream)
        columns = list(reader.fieldnames or [])
        rows = list(reader)
    corrected = correct_history_rows(rows, include_excluded=True)
    for column in AUDIT_COLUMNS:
        if column not in columns:
            columns.append(column)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(corrected)
    return {
        "source": str(source), "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "output": str(destination), "rows": len(corrected),
        "corrections": dict(Counter(r["correction_id"] for r in corrected if r.get("correction_id"))),
        "excluded_rows": sum(str(r.get("comparison_eligible")).lower() == "false" for r in corrected),
        "raw_observations_unchanged": True,
        "limitation": "Cheapest-only history cannot reselect historical minima from all SKUs; Together's affected OD observations are excluded, not reconstructed",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", type=Path, default=ROOT / "store/history.csv")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(write_derived_history(args.history, args.output), indent=2))


if __name__ == "__main__":
    main()
