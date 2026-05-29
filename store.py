"""
Snapshot storage: save/load daily price records as JSON.

peer_cache.json
    Tracks the last successful scrape for each web-scraped provider.
    Committed to git so remote CCR runs (which can't scrape commercial sites)
    always have peer context. Updated automatically on successful local runs.
    A "_meta" key stores per-provider timestamps: {provider: {cached_at, record_count}}.

last_snapshot.json
    The most recent complete price snapshot, committed to git.
    Used as the "previous day" baseline for diff computation in environments
    (e.g. CCR) that start with a fresh clone and have no date-stamped history.
    Updated by the CCR routine after each successful run via git commit+push.

run_manifest.json
    Written at the end of each successful main.py run.
    Records run health: date, status, per-provider fetch status (live/cache/fallback),
    anomaly count, warnings. Read by the CCR agent to gate Slack/Confluence publishing.
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

# Providers whose data is cached in peer_cache.json so remote CCR runs can fall back
# to it when they can't fetch live data.
# - Web-scraped providers: blocked by Cloudflare in cloud environments (CCR / GitHub Actions IPs)
# - API providers that require secrets only available in GitHub Actions (e.g. gcp needs GCP_API_KEY):
#   GitHub Actions fetches and caches; CCR uses the cache.
WEB_SCRAPED_PROVIDERS = {"coreweave", "lambda", "crusoe", "nebius", "computeprices", "gcp", "hyperstack"}


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
    # Update _meta timestamp so we can compute cache age later
    meta = cache.get("_meta", {})
    if not isinstance(meta, dict):
        meta = {}
    meta[fetch_key] = {
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "record_count": len(records),
    }
    cache["_meta"] = meta
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(_PEER_CACHE_FILE, "w") as f:
            json.dump(cache, f, indent=2)
        logger.info(f"  peer_cache: updated '{fetch_key}' with {len(records)} records")
    except Exception as e:
        logger.warning(f"  peer_cache: write failed: {e}")


def get_cache_age_hours(fetch_key: str) -> Optional[float]:
    """Return the age in hours of the peer cache entry for fetch_key, or None if unknown."""
    try:
        cache = load_peer_cache()
        meta = cache.get("_meta", {})
        if not isinstance(meta, dict):
            return None
        cached_at_str = meta.get(fetch_key, {}).get("cached_at")
        if not cached_at_str:
            return None
        cached_at = datetime.fromisoformat(cached_at_str)
        if cached_at.tzinfo is None:
            cached_at = cached_at.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - cached_at).total_seconds() / 3600
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Run manifest — health contract between fetch job and publish job
# ---------------------------------------------------------------------------

RUN_MANIFEST_PATH = STORE_DIR / "run_manifest.json"


def save_run_manifest(manifest: dict):
    """
    Write run_manifest.json. Called at the end of each main.py run.
    The CCR publish agent reads this to gate Slack/Confluence publishing.
    """
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(RUN_MANIFEST_PATH, "w") as f:
            json.dump(manifest, f, indent=2)
        logger.info(f"Run manifest written: status={manifest.get('status')} "
                    f"records={manifest.get('record_count')}")
    except Exception as e:
        logger.warning(f"Could not write run_manifest.json: {e}")


def load_run_manifest() -> dict:
    """Load run_manifest.json. Returns {} if missing or unreadable."""
    if not RUN_MANIFEST_PATH.exists():
        return {}
    try:
        with open(RUN_MANIFEST_PATH) as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Could not read run_manifest.json: {e}")
        return {}
