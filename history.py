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
    Append today's snapshot rows to history.csv (or rebuild if it doesn't exist).
    Avoids re-reading all historical snapshots on every daily run.
    """
    if not HISTORY_CSV.exists():
        return rebuild()

    today = date.today()

    # Check if today is already in the file (idempotent)
    with open(HISTORY_CSV) as f:
        reader = csv.DictReader(f)
        existing_dates = {row["snapshot_date"] for row in reader}

    if today.isoformat() in existing_dates:
        logger.info(f"history.csv: {today.isoformat()} already present — skipping append")
        return HISTORY_CSV

    rows = _rows_for_date(today)
    if not rows:
        logger.warning(f"history.csv: no snapshot for {today.isoformat()} found — nothing to append")
        return HISTORY_CSV

    with open(HISTORY_CSV, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writerows(rows)

    logger.info(f"history.csv: appended {len(rows)} rows for {today.isoformat()}")
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
