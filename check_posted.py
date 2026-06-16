"""
Idempotency guard for the Slack/Confluence post (Phase 3.6).

Prevents a double-post when the CCR routine is run more than once for the same day
(the Jun-15 incident: a manual re-trigger posted the digest twice). The routine calls
this BEFORE posting; if today's run was already posted, it skips.

Exit codes:
  0 — not yet posted for this run_date; safe to post
  1 — already posted for this run_date; SKIP posting (still safe to do field intel / push)

Keys on run_manifest.run_date so re-running the same day is caught.
Call mark_posted.py after a successful post to record it.
"""
import json
import sys
from pathlib import Path

STORE = Path(__file__).parent / "store"
MANIFEST = STORE / "run_manifest.json"
POST_LOG = STORE / "post_log.json"


def main() -> int:
    try:
        run_date = json.loads(MANIFEST.read_text()).get("run_date", "")
    except Exception:
        print("no run_manifest — cannot determine run_date; allowing post")
        return 0
    if not run_date:
        print("run_manifest has no run_date; allowing post")
        return 0
    if POST_LOG.exists():
        try:
            last = json.loads(POST_LOG.read_text()).get("last_posted_run_date", "")
        except Exception:
            last = ""
        if last == run_date:
            print(f"ALREADY POSTED for run_date {run_date} — skip posting")
            return 1
    print(f"not yet posted for run_date {run_date} — safe to post")
    return 0


if __name__ == "__main__":
    sys.exit(main())
