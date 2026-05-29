"""
Historical price CSV builder.

Reads all date-stamped snapshots from store/YYYY-MM-DD.json and compiles
them into store/history.csv — one row per (date, provider, gpu_model,
consumption_type), keeping only the cheapest price observed that day.

This makes it easy to track pricing trends over time and build charts.

Usage:
    python history.py              # rebuild history.csv from all snapshots
    python history.py --append     # only add today's snapshot (faster)

Called automatically by main.py after each successful run.
"""
import argparse
import csv
import logging
from datetime import date
from pathlib import Path
from typing import List, Dict, Tuple

from store import STORE_DIR, list_snapshot_dates, load_snapshot
from schema import PriceRecord

logger = logging.getLogger(__name__)

HISTORY_CSV = STORE_DIR / "history.csv"

COLUMNS = [
    "snapshot_date",
    "provider",
    "gpu_model",
    "consumption_type",
    "region",           # cheapest region for this (provider, gpu, ct) on that day
    "instance_type",    # cheapest instance_type for this combo
    "gpu_count",
    "price_per_gpu_hour_usd",
    "price_per_hour_usd",
    "data_source",
]

# Consumption types to include — exclude noisy sub-variants (50pct/30pct upfront)
# so trend lines stay clean. Full data is always in the daily JSON snapshots.
INCLUDE_CONSUMPTION_TYPES = {
    "on_demand",
    "spot",
    "preemptible",
    "reserved_1yr",
    "reserved_3yr",
    "committed_9mo",
    "committed_1yr",
    "committed_18mo",
    "committed_2yr",
    "committed_3yr",
}


def _cheapest_per_combo(
    records: List[PriceRecord],
) -> Dict[Tuple[str, str, str], PriceRecord]:
    """
    Return the cheapest record per (provider, gpu_model, consumption_type).
    Ties broken by lowest price_per_gpu_hour_usd.
    """
    best: Dict[Tuple[str, str, str], PriceRecord] = {}
    for r in records:
        if r.consumption_type not in INCLUDE_CONSUMPTION_TYPES:
            continue
        key = (r.provider, r.gpu_model, r.consumption_type)
        if key not in best or r.price_per_gpu_hour_usd < best[key].price_per_gpu_hour_usd:
            best[key] = r
    return best


def _rows_for_date(day: date) -> List[dict]:
    records = load_snapshot(day)
    if not records:
        return []
    best = _cheapest_per_combo(records)
    rows = []
    for r in sorted(best.values(), key=lambda x: (x.provider, x.gpu_model, x.consumption_type)):
        rows.append({
            "snapshot_date":          day.isoformat(),
            "provider":               r.provider,
            "gpu_model":              r.gpu_model,
            "consumption_type":       r.consumption_type,
            "region":                 r.region,
            "instance_type":          r.instance_type,
            "gpu_count":              r.gpu_count,
            "price_per_gpu_hour_usd": round(r.price_per_gpu_hour_usd, 4),
            "price_per_hour_usd":     round(r.price_per_hour_usd, 4),
            "data_source":            getattr(r, "data_source", ""),
        })
    return rows


def rebuild() -> Path:
    """
    Rebuild history.csv from scratch using all available date-stamped snapshots.
    Returns the path to the written file.
    """
    days = list_snapshot_dates()
    if not days:
        logger.warning("No date-stamped snapshots found in store/ — history.csv not written")
        return HISTORY_CSV

    all_rows = []
    for day in days:
        rows = _rows_for_date(day)
        all_rows.extend(rows)
        logger.info(f"  history: {day.isoformat()} → {len(rows)} rows")

    STORE_DIR.mkdir(parents=True, exist_ok=True)
    with open(HISTORY_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(all_rows)

    logger.info(f"history.csv: wrote {len(all_rows)} rows across {len(days)} snapshots → {HISTORY_CSV}")
    return HISTORY_CSV


def append_today() -> Path:
    """
    Write today's snapshot rows to history.csv, replacing any existing rows
    for today (or rebuild if history.csv doesn't exist).
    Reads from the snapshot file. Use append_records() if you have already-validated
    records in memory (preferred — avoids re-reading the raw snapshot).
    """
    if not HISTORY_CSV.exists():
        return rebuild()

    today = date.today()
    today_str = today.isoformat()

    rows = _rows_for_date(today)
    if not rows:
        logger.warning(f"history.csv: no snapshot for {today_str} found — nothing to write")
        return HISTORY_CSV

    # Drop any existing rows for today, then rewrite with fresh data
    with open(HISTORY_CSV, newline="") as f:
        reader = csv.DictReader(f)
        kept_rows = [row for row in reader if row["snapshot_date"] != today_str]

    with open(HISTORY_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(kept_rows)
        writer.writerows(rows)

    logger.info(f"history.csv: wrote {len(rows)} rows for {today_str}")
    return HISTORY_CSV


def append_records(records: List[PriceRecord], day: date = None) -> Path:
    """
    Write a caller-supplied list of records for `day` (default: today) into
    history.csv, replacing any existing rows for that date.

    Idempotent: running twice with the same input produces the same output.
    Replaces rather than skips when the date already exists — avoids the
    partial-data trap where a prior incomplete run (e.g. only manual/committed
    rows) would block a later full run from writing its on_demand data.

    Preferred over append_today() when records have already been validated
    in memory — ensures history.csv only contains accepted data.
    Rebuilds from scratch if history.csv doesn't exist yet.
    """
    if not HISTORY_CSV.exists():
        return rebuild()

    day = day or date.today()
    day_str = day.isoformat()

    best = _cheapest_per_combo(records)
    new_rows = []
    for r in sorted(best.values(), key=lambda x: (x.provider, x.gpu_model, x.consumption_type)):
        new_rows.append({
            "snapshot_date":          day_str,
            "provider":               r.provider,
            "gpu_model":              r.gpu_model,
            "consumption_type":       r.consumption_type,
            "region":                 r.region,
            "instance_type":          r.instance_type,
            "gpu_count":              r.gpu_count,
            "price_per_gpu_hour_usd": round(r.price_per_gpu_hour_usd, 4),
            "price_per_hour_usd":     round(r.price_per_hour_usd, 4),
            "data_source":            getattr(r, "data_source", ""),
        })

    if not new_rows:
        logger.warning(f"history.csv: no valid records for {day_str} — nothing to write")
        return HISTORY_CSV

    # Read existing rows, dropping any that belong to this date (we're replacing them)
    with open(HISTORY_CSV, newline="") as f:
        reader = csv.DictReader(f)
        all_existing = list(reader)
    kept_rows = [row for row in all_existing if row["snapshot_date"] != day_str]
    action = "replaced" if len(kept_rows) < len(all_existing) else "appended"

    with open(HISTORY_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(kept_rows)
        writer.writerows(new_rows)

    logger.info(f"history.csv: {action} {len(new_rows)} rows for {day_str} (from validated records)")
    return HISTORY_CSV


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s")
    parser = argparse.ArgumentParser(description="Build historical GPU price CSV")
    parser.add_argument(
        "--append", action="store_true",
        help="Append today's snapshot only (faster). Default: full rebuild."
    )
    args = parser.parse_args()

    path = append_today() if args.append else rebuild()
    print(f"Written: {path}")
