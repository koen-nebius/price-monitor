#!/usr/bin/env python3
"""
Publish the internal GPU forward curve to Confluence (page + chart attachment).

Runs in the daily GHA build after forward_curve.py. Idempotent:
  1. finds the page by title in the space (creates it under the same parent as the
     daily overview page when missing),
  2. uploads/updates the attachments forward_curve.png, forward_curve.svg and
     forward_view.html from store/forward_curve/,
  3. renders the body WITH the <ac:image> tag (forward_curve.render_confluence_body
     with_images=True), converts HTML+ -> storage (confluence_storage.to_storage)
     and PUTs it as version+1.

Soft-fails (exit 0) so the artifact commit still lands; --strict exits 1 on error.
Writes store/forward_curve/publish.json with the outcome.

Env: CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN. Optional FORWARD_PAGE_TITLE /
     FORWARD_PAGE_SPACE override config defaults.
Flags: --dry-run  render + validate only, no network.
"""
import base64
import json
import logging
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import forward_curve  # noqa: E402
from config import (  # noqa: E402
    CONFLUENCE_BASE_URL, CONFLUENCE_FORWARD_PAGE_ID, CONFLUENCE_FORWARD_PAGE_TITLE,
    CONFLUENCE_FORWARD_VIEW_PAGE_ID, CONFLUENCE_FORWARD_VIEW_PAGE_TITLE, CONFLUENCE_PAGE_ID,
    CONFLUENCE_SPACE_KEY, FORGE_HTML_MACRO_KEY,
)
from confluence_storage import to_storage, validate_xml  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("publish_forward_curve")

API = f"{CONFLUENCE_BASE_URL}/rest/api/content"
OUT_DIR = forward_curve.OUT_DIR
STATUS_FILE = OUT_DIR / "publish.json"
TITLE = os.environ.get("FORWARD_PAGE_TITLE", CONFLUENCE_FORWARD_PAGE_TITLE)
SPACE = os.environ.get("FORWARD_PAGE_SPACE", CONFLUENCE_SPACE_KEY)
ATTACHMENTS = ["forward_curve.png", "forward_curve.svg", "forward_view.html"]


def _req(method, url, auth, body=None, headers=None, raw=None, timeout=90):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    h = {"Authorization": f"Basic {auth}", "Accept": "application/json"}
    if raw is None:
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


def _multipart(filename: str, content: bytes, comment: str):
    boundary = f"----fc{uuid.uuid4().hex}"
    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    parts = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
        f"Content-Type: {ctype}\r\n\r\n".encode() + content + b"\r\n",
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"comment\"\r\n\r\n{comment}\r\n".encode(),
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"minorEdit\"\r\n\r\ntrue\r\n".encode(),
        f"--{boundary}--\r\n".encode(),
    ]
    return b"".join(parts), {"Content-Type": f"multipart/form-data; boundary={boundary}",
                             "X-Atlassian-Token": "nocheck"}


def _find_page(auth):
    if CONFLUENCE_FORWARD_PAGE_ID:
        try:
            return _req("GET", f"{API}/{CONFLUENCE_FORWARD_PAGE_ID}?expand=version", auth)
        except urllib.error.HTTPError as e:
            log.warning(f"page id {CONFLUENCE_FORWARD_PAGE_ID} lookup failed (HTTP {e.code}); falling back to title")
    q = urllib.parse.urlencode({"title": TITLE, "spaceKey": SPACE, "expand": "version"})
    res = _req("GET", f"{API}?{q}", auth).get("results", [])
    return res[0] if res else None


def _create_page(auth, storage_html):
    # nest under the daily overview page so the three pricing pages sit together
    payload = {"type": "page", "title": TITLE, "space": {"key": SPACE},
               "ancestors": [{"id": CONFLUENCE_PAGE_ID}],
               "body": {"storage": {"value": storage_html, "representation": "storage"}}}
    return _req("POST", API, auth, payload)


def _upsert_attachment(auth, page_id, name, content, as_of):
    q = urllib.parse.urlencode({"filename": name})
    existing = _req("GET", f"{API}/{page_id}/child/attachment?{q}", auth).get("results", [])
    raw, headers = _multipart(name, content, f"forward curve {as_of}")
    if existing:
        url = f"{API}/{page_id}/child/attachment/{existing[0]['id']}/data"
    else:
        url = f"{API}/{page_id}/child/attachment"
    return _req("POST", url, auth, raw=raw, headers=headers)


def view_adf(result: dict, marks_page_id: str) -> dict:
    """ADF document for the interactive child page: intro paragraph + the Forge HTML macro
    (Just Add+) carrying the whole self-contained view as its body. Node shape copied from
    Nebius page 2078277860 (a pasted Claude artifact) on 2026-09-15."""
    fragment = forward_curve.render_view_fragment(result)
    link = f"{CONFLUENCE_BASE_URL}/spaces/{CONFLUENCE_SPACE_KEY}/pages/{marks_page_id}"
    return {"type": "doc", "version": 1, "content": [
        {"type": "paragraph", "content": [
            {"type": "text", "text": f"Interactive view of the internal GPU forward curve, as of {result.get('as_of')} "
                                     "(refreshed daily by the price-monitor build). Internal only: never quote marks to customers. "
                                     "Tables, method and provenance: ", "marks": [{"type": "em"}]},
            {"type": "text", "text": "GPU Forward Curve — Internal Marks", "marks": [{"type": "em"}, {"type": "link", "attrs": {"href": link}}]},
            {"type": "text", "text": ". If the panel below stays blank, your browser blocked the embedded script; the same page is attached there as forward_view.html.", "marks": [{"type": "em"}]},
        ]},
        {"type": "extension", "attrs": {
            "layout": "full-width", "extensionType": "com.atlassian.ecosystem", "extensionKey": FORGE_HTML_MACRO_KEY,
            "text": "HTML",
            "parameters": {"layout": "extension", "guestParams": {
                "sourceType": "MacroBody", "darkmode": "auto", "attachmentPageId": "", "syntax": "HTML",
                "attachmentId": "", "__bodyContent": fragment}}}},
    ]}


def _find_view_page(auth, parent_id):
    if CONFLUENCE_FORWARD_VIEW_PAGE_ID:
        try:
            return _req("GET", f"{API}/{CONFLUENCE_FORWARD_VIEW_PAGE_ID}?expand=version", auth)
        except urllib.error.HTTPError as e:
            log.warning(f"view page id lookup failed (HTTP {e.code}); falling back to title")
    q = urllib.parse.urlencode({"title": CONFLUENCE_FORWARD_VIEW_PAGE_TITLE, "spaceKey": SPACE, "expand": "version"})
    res = _req("GET", f"{API}?{q}", auth).get("results", [])
    return res[0] if res else None


def publish_view(auth, result, marks_page_id):
    """Create/update the interactive child page (atlas_doc_format body)."""
    adf = json.dumps(view_adf(result, marks_page_id))
    body = {"atlas_doc_format": {"value": adf, "representation": "atlas_doc_format"}}
    page = _find_view_page(auth, marks_page_id)
    if page:
        payload = {"type": "page", "title": page["title"],
                   "version": {"number": page["version"]["number"] + 1,
                               "message": f"Forward curve interactive refresh {result.get('as_of')} (GHA publisher)"},
                   "body": body}
        out = _req("PUT", f"{API}/{page['id']}", auth, payload)
    else:
        payload = {"type": "page", "title": CONFLUENCE_FORWARD_VIEW_PAGE_TITLE, "space": {"key": SPACE},
                   "ancestors": [{"id": marks_page_id}], "body": body}
        out = _req("POST", API, auth, payload)
        log.info(f"created interactive page {out.get('id')} — set CONFLUENCE_FORWARD_VIEW_PAGE_ID in config.py")
    return {"ok": True, "page_id": out.get("id"), "version": out.get("version", {}).get("number"),
            "bytes": len(adf.encode()), "url": f"{CONFLUENCE_BASE_URL}{out.get('_links', {}).get('webui', '')}"}


def main(argv) -> int:
    dry, strict = "--dry-run" in argv, "--strict" in argv
    latest = OUT_DIR / "latest.json"
    if not latest.exists():
        log.warning("store/forward_curve/latest.json missing — run forward_curve.py first")
        return 1 if strict else 0
    result = json.loads(latest.read_text())
    as_of = result.get("as_of", "")
    body = to_storage(forward_curve.render_confluence_body(result, with_images=True))
    xml_err = validate_xml(body)
    info = {"as_of": as_of, "bytes": len(body.encode()), "lozenges": body.count('ac:name="status"'),
            "xml_warning": xml_err, "published_at": datetime.now(timezone.utc).isoformat()}
    if xml_err:
        log.warning(f"body not well-formed XML ({xml_err}); publishing anyway")
    if dry:
        adf_bytes = len(json.dumps(view_adf(result, CONFLUENCE_FORWARD_PAGE_ID or "0")).encode())
        log.info(f"DRY RUN: {info} | interactive page ADF {adf_bytes // 1024} KB")
        return 0

    email, token = os.environ.get("CONFLUENCE_EMAIL", ""), os.environ.get("CONFLUENCE_API_TOKEN", "")
    if not (email and token):
        log.warning("CONFLUENCE_EMAIL / CONFLUENCE_API_TOKEN not set — skipping publish")
        return 1 if strict else 0
    auth = base64.b64encode(f"{email}:{token}".encode()).decode()

    outcome = dict(info, ok=False)
    try:
        page = _find_page(auth)
        if not page:
            created = _create_page(auth, body)          # body without images first is fine; re-PUT below
            page = {"id": created["id"], "version": created["version"], "title": created["title"]}
            log.info(f"created page {page['id']} '{TITLE}'")
        page_id = page["id"]
        att = {}
        for name in ATTACHMENTS:
            p = OUT_DIR / name
            if not p.exists():
                att[name] = "missing"
                continue
            try:
                _upsert_attachment(auth, page_id, name, p.read_bytes(), as_of)
                att[name] = "ok"
            except urllib.error.HTTPError as e:
                att[name] = f"HTTP {e.code}: {e.read().decode(errors='replace')[:200]}"
        current = _req("GET", f"{API}/{page_id}?expand=version", auth)
        payload = {"type": "page", "title": current["title"],
                   "version": {"number": current["version"]["number"] + 1,
                               "message": f"Forward curve refresh {as_of} (GHA publisher)"},
                   "body": {"storage": {"value": body, "representation": "storage"}}}
        last_err = None
        for attempt in (1, 2):
            try:
                out = _req("PUT", f"{API}/{page_id}", auth, payload)
                outcome.update(ok=True, page_id=page_id, version=out.get("version", {}).get("number"),
                               url=f"{CONFLUENCE_BASE_URL}{out.get('_links', {}).get('webui', '')}",
                               attachments=att)
                break
            except urllib.error.HTTPError as e:
                last_err = f"HTTP {e.code}: {e.read().decode(errors='replace')[:600]}"
                if e.code < 500:
                    break
            except Exception as e:
                last_err = repr(e)
            if attempt == 1:
                time.sleep(10)
        if not outcome["ok"]:
            outcome.update(error=last_err, page_id=page_id, attachments=att)
        try:
            outcome["interactive"] = publish_view(auth, result, page_id)
        except urllib.error.HTTPError as e:
            outcome["interactive"] = {"ok": False, "error": f"HTTP {e.code}: {e.read().decode(errors='replace')[:600]}"}
        except Exception as e:
            outcome["interactive"] = {"ok": False, "error": repr(e)}
    except urllib.error.HTTPError as e:
        outcome["error"] = f"HTTP {e.code}: {e.read().decode(errors='replace')[:600]}"
    except Exception as e:
        outcome["error"] = repr(e)

    STATUS_FILE.write_text(json.dumps(outcome, indent=1))
    (log.info if outcome["ok"] else log.error)(f"publish outcome: {outcome}")
    return 0 if (outcome["ok"] or not strict) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
