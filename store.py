"""
Snapshot storage: save/load daily price records as JSON.
"""
import json
import logging
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import List, Optional

from schema import PriceRecord

logger = logging.getLogger(__name__)

STORE_DIR = Path(__file__).parent / "store"


def save_snapshot(records: List[PriceRecord], day: date = None) -> Path:
    day = day or date.today()
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    path = STORE_DIR / f"{day.isoformat()}.json"
    data = [r.to_dict() for r in records]
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    # Always update latest.json
    latest = STORE_DIR / "latest.json"
    with open(latest, "w") as f:
        json.dump(data, f, indent=2)
    logger.info(f"Saved {len(records)} records to {path}")
    return path


def load_snapshot(day: date) -> List[PriceRecord]:
    path = STORE_DIR / f"{day.isoformat()}.json"
    if not path.exists():
        return []
    with open(path) as f:
        data = json.load(f)
    return [PriceRecord.from_dict(d) for d in data]


def load_latest() -> List[PriceRecord]:
    path = STORE_DIR / "latest.json"
    if not path.exists():
        return []
    with open(path) as f:
        data = json.load(f)
    return [PriceRecord.from_dict(d) for d in data]


def list_snapshot_dates() -> List[date]:
    if not STORE_DIR.exists():
        return []
    dates = []
    for p in sorted(STORE_DIR.glob("????-??-??.json")):
        try:
            dates.append(date.fromisoformat(p.stem))
        except ValueError:
            pass
    return dates


def previous_snapshot_day() -> Optional[date]:
    days = list_snapshot_dates()
    today = date.today()
    # Return the most recent day before today
    past = [d for d in days if d < today]
    return past[-1] if past else None
