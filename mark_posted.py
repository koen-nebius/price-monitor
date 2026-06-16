"""
Record a successful Slack post (Phase 3.6 idempotency). Call AFTER posting.
Writes store/post_log.json with the run_date just posted, so a same-day re-run is
caught by check_posted.py. Commit post_log.json so the marker persists across CCR runs.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

STORE = Path(__file__).parent / "store"
MANIFEST = STORE / "run_manifest.json"
POST_LOG = STORE / "post_log.json"


def main() -> int:
    try:
        run_date = json.loads(MANIFEST.read_text()).get("run_date", "")
    except Exception:
        print("no run_manifest — nothing to mark")
        return 0
    POST_LOG.write_text(json.dumps({
        "last_posted_run_date": run_date,
        "posted_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2))
    print(f"marked posted for run_date {run_date}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
