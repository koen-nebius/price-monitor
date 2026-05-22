"""
Snapshot storage: save/load daily price records as JSON.

peer_cache.json
    Tracks the last successful scrape for each web-scraped provider.
    Committed to git so remote CCR runs (which can't scrape commercial sites)
    always have peer context. Updated automatically on successful local runs.

last_snapshot.json
    The most recent complete price snapshot, committed to git.
    Used as the "previous day" baseline for diff computation in environments
    (e.g. CCR) that start with a fresh clone and have no date-stamped history.
    Updated by the CCR routine after each successful run via git commit+push.
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
# Last snapshot — full price snapshot committed to git for CCR diff baseline
# ---------------------------------------------------------------------------

LAST_SNAPSHOT_PATH = STORE_DIR / "last_snapshot.json"


def load_last_snapshot() -> List[PriceRecord]:
    """
    Load the last committed snapshot. Used as the diff baseline when no
    date-stamped snapshot file exists (e.g. fresh CCR clone).
    Returns [] if the file doesn't exist or can't be read.
    """
    if not LAST_SNAPSHOT_PATH.exists():
        return []
    try:
        with open(LAST_SNAPSHOT_PATH) as f:
            data = json.load(f)
        records = [PriceRecord.from_dict(d) for d in data]
        logger.info(f"Loaded {len(records)} records from last_snapshot.json as diff baseline")
        return records
    except Exception as e:
        logger.warning(f"Could not read last_snapshot.json: {e}")
        return []


def save_last_snapshot(records: List[PriceRecord]):
    """
    Persist the current run's records as last_snapshot.json.
    Called at the end of each successful run so the next run has a baseline.
    In CCR environments, the caller is responsible for git add+commit+push.
    """
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(LAST_SNAPSHOT_PATH, "w") as f:
            json.dump([r.to_dict() for r in records], f, indent=2)
        logger.info(f"Saved {len(records)} records to last_snapshot.json")
    except Exception as e:
        logger.warning(f"Could not write last_snapshot.json: {e}")


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
