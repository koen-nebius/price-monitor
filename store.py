"""
Snapshot storage: save/load daily price records as JSON.

peer_cache.json
    Tracks the last successful scrape for each web-scraped provider.
    Committed to git so remote CCR runs (which can't scrape commercial sites)
    always have peer context. Updated automatically on successful local runs.
"""
import json
import logging
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from schema import PriceRecord

logger = logging.getLogger(__name__)

STORE_DIR = Path(__file__).parent / "store"

# Providers whose data comes from web scraping (blocked in cloud/CCR environments).
# Their records are cached in peer_cache.json so remote runs can fall back to them.
WEB_SCRAPED_PROVIDERS = {"coreweave", "lambda", "crusoe", "nebius", "computeprices"}


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


# ---------------------------------------------------------------------------
# Peer cache — last successful web-scraped data, committed to git
# ---------------------------------------------------------------------------

_PEER_CACHE_FILE = STORE_DIR / "peer_cache.json"


def load_peer_cache() -> Dict[str, List[dict]]:
    """Load the peer cache. Returns {fetch_key: [record_dicts]}."""
    if not _PEER_CACHE_FILE.exists():
        return {}
    try:
        with open(_PEER_CACHE_FILE) as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Could not read peer_cache.json: {e}")
        return {}


def get_cached_records(fetch_key: str) -> List[PriceRecord]:
    """Return cached records for a provider fetch key, or [] if none."""
    cache = load_peer_cache()
    data = cache.get(fetch_key, [])
    if not data:
        return []
    try:
        records = [PriceRecord.from_dict(d) for d in data]
        logger.info(f"  peer_cache: loaded {len(records)} records for '{fetch_key}'")
        return records
    except Exception as e:
        logger.warning(f"  peer_cache: failed to deserialise '{fetch_key}': {e}")
        return []


def update_peer_cache(fetch_key: str, records: List[PriceRecord]):
    """Persist fresh records for a fetch key into peer_cache.json."""
    cache = load_peer_cache()
    cache[fetch_key] = [r.to_dict() for r in records]
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(_PEER_CACHE_FILE, "w") as f:
            json.dump(cache, f, indent=2)
        logger.info(f"  peer_cache: updated '{fetch_key}' with {len(records)} records")
    except Exception as e:
        logger.warning(f"  peer_cache: write failed: {e}")
