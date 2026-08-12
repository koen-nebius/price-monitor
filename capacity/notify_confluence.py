"""
Update the capacity Confluence page directly from the GHA run (Basic auth,
same CONFLUENCE_EMAIL/CONFLUENCE_API_TOKEN secrets the bootstrap uses).

The pricing monitor posts Confluence via the 07:00 routine's Atlassian MCP;
for capacity the GHA updates the page itself right after the build — one
fewer moving part, and the 2026-08-12 shakedown showed the routine's
Atlassian connector can be unavailable in a session while Basic-auth REST
from GHA works. The 07:10 routine remains Slack-only.

Soft-fails (exit 0) when creds or the page meta are absent.
"""
import base64
import json
import logging
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from capacity.config import CONFLUENCE_BASE_URL, CONFLUENCE_PAGE_TITLE

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("capacity.notify_confluence")

STORE = Path(__file__).parent / "store"
META_FILE = STORE / "confluence_page.json"
BODY_FILE = STORE / "confluence_body.html"


def main() -> None:
    email = os.environ.get("CONFLUENCE_EMAIL", "")
    token = os.environ.get("CONFLUENCE_API_TOKEN", "")
    if not email or not token:
        logger.warning("Confluence creds not set — skipping page update")
        return
    if not META_FILE.exists() or not BODY_FILE.exists():
        logger.warning("Page meta or body missing — skipping page update")
        return

    page_id = json.loads(META_FILE.read_text()).get("page_id")
    if not page_id:
        logger.warning("No page_id in confluence_page.json — skipping")
        return

    auth = "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()
    api = f"{CONFLUENCE_BASE_URL}/rest/api/content/{page_id}"

    req = urllib.request.Request(api, headers={"Authorization": auth, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        current = json.loads(resp.read())

    payload = json.dumps({
        "version": {"number": current["version"]["number"] + 1},
        "title": CONFLUENCE_PAGE_TITLE,
        "type": "page",
        "body": {"storage": {"value": BODY_FILE.read_text(), "representation": "storage"}},
    }).encode()
    req = urllib.request.Request(api, data=payload, method="PUT", headers={
        "Authorization": auth, "Content-Type": "application/json", "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())
    logger.info(f"Confluence page {page_id} updated to version {result['version']['number']}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.error(f"Confluence update failed (non-fatal): {e}")
