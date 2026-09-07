#!/usr/bin/env python3
"""
Publish the daily pricing pages to Confluence straight from the GHA build.

Replaces step 3e of the 07:00 UTC "GPU Competitor Price Monitor" routine.
Why (2026-09-07): store/confluence_body.html is ~96 KB (~38K tokens). The
routine had to emit that whole body as ONE tool-call argument to the Atlassian
MCP, which exceeds Claude Code's 32,000 output-token cap, so every run since
the page crossed ~75 KB ended in "API Error: Claude's response exceeded the
32000 output token maximum" (run logs 08-30, 09-03, 09-05, 09-07; each burned
~30-45 min of retries first). Publishing from the build is the pattern the
capacity monitor has used since 2026-08-12 (capacity/notify_confluence.py).

Pages:
  store/confluence_body.html   -> config.CONFLUENCE_PAGE_ID       (1831469419)
  store/spot_auction_body.html -> config.CONFLUENCE_SPOT_PAGE_ID  (1970110707)

The bodies are HTML+ (Atlassian MCP dialect); the REST storage representation
flattens its status lozenges / panels to plain text, so confluence_storage
.to_storage() converts them to storage-format macros first.

Gates: the workflow runs this only after check_manifest.py passed, and this
script additionally requires store/run_manifest.json run_date == today (UTC)
unless PUBLISH_FORCE=1. Each page keeps its CURRENT title (never renamed).
Result goes to store/confluence_publish.json (per page: ok, version, bytes,
error) — committed with the artifacts so the routine can report it without
touching the pages. Soft-fails (exit 0) so the artifact commit still lands;
use --strict to exit 1 on any failure.

Env: CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN (Basic auth against the site URL).
     PUBLISH_FORCE=1 bypasses the run_date gate (manual re-publish).
Flags: --dry-run  convert + validate only, no network, no status file.
       --strict   exit 1 if any page failed.
"""
import base64
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import (  # noqa: E402
    CONFLUENCE_BASE_URL, CONFLUENCE_PAGE_ID, CONFLUENCE_SPOT_PAGE_ID,
)
from confluence_storage import to_storage, validate_xml  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("publish_confluence_pages")

STORE = ROOT / "store"
MANIFEST = STORE / "run_manifest.json"
STATUS_FILE = STORE / "confluence_publish.json"
PAGES = [
    (CONFLUENCE_PAGE_ID, STORE / "confluence_body.html"),
    (CONFLUENCE_SPOT_PAGE_ID, STORE / "spot_auction_body.html"),
]
API = f"{CONFLUENCE_BASE_URL}/rest/api/content"


def _req(method: str, url: str, auth: str, body: dict = None, timeout: int = 90) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


def _publish(page_id: str, storage_html: str, auth: str, run_date: str) -> dict:
    current = _req("GET", f"{API}/{page_id}?expand=version", auth)
    payload = {
        "type": "page",
        "title": current["title"],                       # keep the live title
        "version": {"number": current["version"]["number"] + 1,
                    "message": f"Daily refresh {run_date} (GHA publisher)"},
        "body": {"storage": {"value": storage_html, "representation": "storage"}},
    }
    last_err = None
    for attempt in (1, 2):
        try:
            out = _req("PUT", f"{API}/{page_id}", auth, payload)
            return {"ok": True, "version": out.get("version", {}).get("number"),
                    "title": out.get("title")}
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:600]
            last_err = f"HTTP {e.code}: {detail}"
            if e.code < 500:            # 4xx = our payload; retrying won't help
                break
        except Exception as e:          # timeouts, connection resets
            last_err = repr(e)
        if attempt == 1:
            time.sleep(10)
    return {"ok": False, "error": last_err}


def main(argv) -> int:
    dry, strict = "--dry-run" in argv, "--strict" in argv
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    run_date = ""
    if MANIFEST.exists():
        run_date = json.loads(MANIFEST.read_text()).get("run_date", "")
    if run_date != today and os.environ.get("PUBLISH_FORCE") != "1":
        log.warning(f"run_date={run_date!r} != today={today}; not publishing "
                    "(set PUBLISH_FORCE=1 to override)")
        return 0

    email, token = os.environ.get("CONFLUENCE_EMAIL", ""), os.environ.get("CONFLUENCE_API_TOKEN", "")
    if not dry and not (email and token):
        log.warning("CONFLUENCE_EMAIL / CONFLUENCE_API_TOKEN not set — skipping publish")
        return 0
    auth = base64.b64encode(f"{email}:{token}".encode()).decode() if not dry else ""

    results, failed = {}, False
    for page_id, path in PAGES:
        if not path.exists() or not path.read_text().strip():
            log.warning(f"{path.name} missing/empty — skipping page {page_id}")
            results[page_id] = {"file": path.name, "ok": False, "error": "artifact missing/empty"}
            continue
        raw = path.read_text()
        body = to_storage(raw)
        xml_err = validate_xml(body)
        info = {"file": path.name, "bytes": len(body.encode()),
                "lozenges": body.count('ac:name="status"'), "xml_warning": xml_err}
        if xml_err:
            log.warning(f"{path.name}: body is not well-formed XML ({xml_err}); publishing anyway, Confluence decides")
        if dry:
            log.info(f"DRY RUN page {page_id}: {info}")
            results[page_id] = {**info, "ok": None}
            continue
        r = _publish(page_id, body, auth, run_date)
        results[page_id] = {**info, **r}
        if r["ok"]:
            log.info(f"page {page_id} ({r['title']}) -> version {r['version']} ({info['bytes']} bytes)")
        else:
            failed = True
            log.error(f"page {page_id} FAILED: {r['error']}")

    if not dry:
        STATUS_FILE.write_text(json.dumps({
            "run_date": run_date,
            "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "publisher": "scripts/publish_confluence_pages.py (GitHub Actions)",
            "pages": results,
        }, indent=1) + "\n")
        log.info(f"status written to {STATUS_FILE.relative_to(ROOT)}")
    return 1 if (failed and strict) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
