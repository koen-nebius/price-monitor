"""
Snapshot store for the capacity monitor — mirrors the price monitor's store
(daily JSON snapshots + last_snapshot + a per-provider cache so a transient
fetch failure serves yesterday's signal marked stale instead of vanishing).
Everything lives under capacity/store/ so the two monitors never collide.
"""
import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from capacity.schema import AvailabilityRecord

logger = logging.getLogger(__name__)

STORE_DIR = Path(__file__).parent / "store"
STORE_DIR.mkdir(exist_ok=True)

PEER_CACHE_FILE = STORE_DIR / "peer_cache.json"
LAST_SNAPSHOT_FILE = STORE_DIR / "last_snapshot.json"
MANIFEST_FILE = STORE_DIR / "run_manifest.json"
HISTORY_FILE = STORE_DIR / "history.csv"

# A cached provider result older than this is dropped entirely — a capacity
# signal is time-critical; a >48h-old "available" is worse than "unknown".
CACHE_MAX_AGE_HOURS = 48

HISTORY_COLUMNS = [
    "date", "provider", "gpu_model", "region", "consumption_type",
    "state", "metric_type", "metric_value",
]


def save_snapshot(records: List[AvailabilityRecord], day: date = None) -> Path:
    day = day or datetime.now(timezone.utc).date()
    path = STORE_DIR / f"{day.isoformat()}.json"
    payload = [r.to_dict() for r in records]
    path.write_text(json.dumps(payload, indent=1))
    LAST_SNAPSHOT_FILE.write_text(json.dumps(payload, indent=1))
    logger.info(f"Saved {len(records)} availability records to {path.name}")
    return path


def load_snapshot(day: date) -> List[AvailabilityRecord]:
    path = STORE_DIR / f"{day.isoformat()}.json"
    if not path.exists():
        return []
    return [AvailabilityRecord.from_dict(d) for d in json.loads(path.read_text())]


def load_last_snapshot() -> List[AvailabilityRecord]:
    if not LAST_SNAPSHOT_FILE.exists():
        return []
    return [AvailabilityRecord.from_dict(d) for d in json.loads(LAST_SNAPSHOT_FILE.read_text())]


def list_snapshot_dates() -> List[date]:
    dates = []
    for p in STORE_DIR.glob("*.json"):
        try:
            dates.append(date.fromisoformat(p.stem))
        except ValueError:
            continue
    return sorted(dates)


def previous_snapshot_day(today: date = None) -> Optional[date]:
    today = today or datetime.now(timezone.utc).date()
    prior = [d for d in list_snapshot_dates() if d < today]
    return prior[-1] if prior else None


# ── Per-provider cache (transient-failure fallback) ─────────────────────────

def _load_cache() -> Dict[str, dict]:
    if not PEER_CACHE_FILE.exists():
        return {}
    try:
        return json.loads(PEER_CACHE_FILE.read_text())
    except json.JSONDecodeError:
        logger.warning("capacity peer_cache.json unreadable — starting fresh")
        return {}


def update_peer_cache(provider: str, records: List[AvailabilityRecord]) -> None:
    cache = _load_cache()
    cache[provider] = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "records": [r.to_dict() for r in records],
    }
    PEER_CACHE_FILE.write_text(json.dumps(cache, indent=1))


def get_cached_records(provider: str) -> (List[AvailabilityRecord], Optional[float]):
    """Return (records, age_hours). Empty when absent or older than CACHE_MAX_AGE_HOURS."""
    entry = _load_cache().get(provider)
    if not entry:
        return [], None
    fetched = datetime.fromisoformat(entry["fetched_at"])
    age_h = (datetime.now(timezone.utc) - fetched).total_seconds() / 3600
    if age_h > CACHE_MAX_AGE_HOURS:
        return [], age_h
    records = [AvailabilityRecord.from_dict(d) for d in entry["records"]]
    return records, age_h


# ── History (long-run trend of states/metrics) ──────────────────────────────

def append_history(records: List[AvailabilityRecord], day: date = None) -> None:
    """Replace today's rows in history.csv with the validated records."""
    import csv
    import io

    day = (day or datetime.now(timezone.utc).date()).isoformat()
    rows: List[List[str]] = []
    if HISTORY_FILE.exists():
        with HISTORY_FILE.open() as f:
            reader = csv.reader(f)
            header = next(reader, None)
            rows = [r for r in reader if r and r[0] != day]

    for r in records:
        rows.append([
            day, r.provider, r.gpu_model, r.region, r.consumption_type,
            r.state, r.metric_type,
            "" if r.metric_value is None else str(r.metric_value),
        ])

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(HISTORY_COLUMNS)
    writer.writerows(rows)
    HISTORY_FILE.write_text(buf.getvalue())
    logger.info(f"history.csv: wrote {len(records)} rows for {day}")


def save_run_manifest(manifest: dict) -> None:
    MANIFEST_FILE.write_text(json.dumps(manifest, indent=1))
    logger.info(f"Run manifest written: status={manifest.get('status')} "
                f"records={manifest.get('record_count')}")


def load_run_manifest() -> dict:
    if not MANIFEST_FILE.exists():
        return {}
    try:
        return json.loads(MANIFEST_FILE.read_text())
    except json.JSONDecodeError:
        return {}
