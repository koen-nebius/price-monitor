"""
One-time (idempotent) Confluence page bootstrap for the capacity monitor.

Runs in the GHA workflow before the pipeline. If capacity/store/
confluence_page.json already records the page, exits immediately. Otherwise
looks the page up by title in the PR space (survives a lost meta file) and
creates it as a child of the pricing overview page if truly absent, then
writes the meta file (committed by the workflow) so the posting routine and
the Slack-message renderer know the page id/url.

Auth: CONFLUENCE_EMAIL + CONFLUENCE_API_TOKEN (same repo secrets the intel
inbox merge uses). Soft-fails with exit 0 when creds are absent so the data
pipeline still runs — the renderer just omits the Confluence link until then.
"""
import base64
import json
import logging
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from capacity.config import (
    CONFLUENCE_BASE_URL, CONFLUENCE_PAGE_TITLE, CONFLUENCE_PARENT_PAGE_ID,
    CONFLUENCE_SPACE_KEY,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("capacity.bootstrap")

META_FILE = Path(__file__).parent / "store" / "confluence_page.json"

INITIAL_BODY = (
    "<p><em>GPU competitor capacity monitor — first data build pending. "
    "This page is refreshed daily by the capacity monitor (companion to the "
    "GPU Competitor Pricing overview).</em></p>"
)


def _auth() -> str:
    email = os.environ.get("CONFLUENCE_EMAIL", "")
    token = os.environ.get("CONFLUENCE_API_TOKEN", "")
    if not email or not token:
        return ""
    return "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()


def _req(url: str, auth: str, payload: dict = None, method: str = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": auth,
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _write_meta(page_id: str) -> None:
    url = (f"{CONFLUENCE_BASE_URL}/spaces/{CONFLUENCE_SPACE_KEY}/pages/{page_id}/"
           + urllib.parse.quote(CONFLUENCE_PAGE_TITLE.replace(" ", "+"), safe="+"))
    META_FILE.parent.mkdir(exist_ok=True)
    META_FILE.write_text(json.dumps({
        "page_id": page_id,
        "title": CONFLUENCE_PAGE_TITLE,
        "url": url,
    }, indent=1))
    logger.info(f"Confluence page meta written: id={page_id} url={url}")


def main() -> None:
    if META_FILE.exists():
        try:
            meta = json.loads(META_FILE.read_text())
            if meta.get("page_id"):
                logger.info(f"Page already bootstrapped (id={meta['page_id']}) — nothing to do")
                return
        except json.JSONDecodeError:
            pass

    auth = _auth()
    if not auth:
        logger.warning("CONFLUENCE_EMAIL/CONFLUENCE_API_TOKEN not set — skipping bootstrap")
        return

    # Look up by title first — page may exist from a previous run whose commit failed
    q = urllib.parse.urlencode({
        "title": CONFLUENCE_PAGE_TITLE,
        "spaceKey": CONFLUENCE_SPACE_KEY,
        "limit": "1",
    })
    found = _req(f"{CONFLUENCE_BASE_URL}/rest/api/content?{q}", auth)
    results = found.get("results", [])
    if results:
        _write_meta(results[0]["id"])
        return

    created = _req(f"{CONFLUENCE_BASE_URL}/rest/api/content", auth, payload={
        "type": "page",
        "title": CONFLUENCE_PAGE_TITLE,
        "space": {"key": CONFLUENCE_SPACE_KEY},
        "ancestors": [{"id": CONFLUENCE_PARENT_PAGE_ID}],
        "body": {"storage": {"value": INITIAL_BODY, "representation": "storage"}},
    })
    logger.info(f"Confluence page created: id={created['id']}")
    _write_meta(created["id"])


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Soft-fail: the data pipeline must run even when Confluence is down
        logger.error(f"Confluence bootstrap failed (non-fatal): {e}")
