"""Publish capacity evidence and verify the stored Confluence structure and text.

The source manifest must be dated today in UTC. --force (or PUBLISH_FORCE=1)
explicitly permits an older source date without changing it. --strict returns
nonzero on every validation, credential, network or read-back failure. A receipt
records delivery evidence, never credentials. --dry-run validates without writes.
"""
import argparse
import base64
import hashlib
import json
import logging
import os
import re
import sys
import xml.etree.ElementTree as ET
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from capacity.config import CONFLUENCE_BASE_URL
from confluence_storage import to_storage, validate_xml

logger = logging.getLogger("capacity.notify_confluence")
STORE = Path(__file__).parent / "store"
META_FILE = STORE / "confluence_page.json"
BODY_FILE = STORE / "confluence_body.html"
MANIFEST_FILE = STORE / "run_manifest.json"
RECEIPT_FILE = STORE / "confluence_publish.json"


def _request(method, url, auth, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": auth, "Content-Type": "application/json", "Accept": "application/json",
    })
    with urllib.request.urlopen(request, timeout=90) as response:
        return json.loads(response.read())


def canonical_storage(body):
    """Compare XML structure/text, ignoring only server-generated macro metadata.

    Confluence adds macro UUIDs and schema-version attributes after PUT. Those
    do not change content. All other attributes, table/macro structure, links,
    child order and non-whitespace text remain part of verification.
    """
    root = ET.fromstring('<root xmlns:ac="http://atlassian.com/content" '
                         'xmlns:ri="http://atlassian.com/resource/identifier">' + body + '</root>')
    ignored = {"{http://atlassian.com/content}macro-id", "{http://atlassian.com/content}schema-version"}
    clean_text = lambda text: re.sub(r"\s+", " ", text or "").strip()
    def content(node):
        return [node.tag, sorted((k, v) for k, v in node.attrib.items() if k not in ignored),
                clean_text(node.text), [content(child) for child in node], clean_text(node.tail)]
    return json.dumps(content(root), ensure_ascii=False, separators=(",", ":"))


def _save_receipt(receipt):
    # Atomic replacement prevents a partial receipt being mistaken for success.
    temporary = RECEIPT_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    temporary.replace(RECEIPT_FILE)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    force = args.force or os.environ.get("PUBLISH_FORCE") == "1"
    receipt = {"run_date": "", "attempted_at": now.isoformat(timespec="seconds"),
               "publisher": "capacity/notify_confluence.py", "forced": force,
               "ok": False, "write_attempted": False, "stage": "validation", "pages": {}}
    info = {"file": BODY_FILE.name, "ok": False}
    error = None
    try:
        if not MANIFEST_FILE.exists():
            raise ValueError("capacity run manifest missing")
        manifest = json.loads(MANIFEST_FILE.read_text())
        run_date = manifest.get("run_date", "")
        receipt["run_date"] = run_date
        if run_date != now.date().isoformat() and not force:
            raise ValueError("capacity run_date is not today in UTC; explicit --force required")
        if not META_FILE.exists():
            raise ValueError("capacity page metadata missing")
        page_id = str(json.loads(META_FILE.read_text()).get("page_id") or "")
        if not re.fullmatch(r"\d+", page_id):
            raise ValueError("capacity page_id missing or invalid")
        receipt["pages"][page_id] = info
        if not BODY_FILE.exists() or not BODY_FILE.read_text().strip():
            raise ValueError("capacity page body missing or empty")
        body = to_storage(BODY_FILE.read_text())
        xml_error = validate_xml(body)
        if xml_error:
            # The XML error position is useful; do not log source snippets.
            raise ValueError("capacity body is not valid Confluence storage XML")
        info.update({"bytes": len(body.encode("utf-8")),
                     "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest()})
        if args.dry_run:
            logger.info("Capacity publication validation passed; no network or receipt write")
            return 0
        email = os.environ.get("CONFLUENCE_EMAIL", "").strip()
        token = os.environ.get("CONFLUENCE_API_TOKEN", "").strip()
        if not email or not token:
            raise ValueError("Confluence credentials missing")
        auth = "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()
        api = f"{CONFLUENCE_BASE_URL}/rest/api/content/{page_id}"
        receipt["stage"] = "read_current"
        current = _request("GET", api + "?expand=version", auth)
        title = current.get("title")
        version = current.get("version", {}).get("number")
        if not isinstance(title, str) or not title.strip() or type(version) is not int:
            raise ValueError("live page title or version missing")
        expected_version = version + 1
        payload = {"version": {"number": expected_version,
                               "message": f"Capacity observations {run_date}"},
                   "title": title, "type": "page",
                   "body": {"storage": {"value": body, "representation": "storage"}}}
        info.update({"title": title, "expected_version": expected_version})
        receipt.update({"stage": "write", "write_attempted": True})
        result = _request("PUT", api, auth, payload)
        info["returned_version"] = result.get("version", {}).get("number")
        receipt["stage"] = "read_back"
        confirmed = _request("GET", api + "?expand=version,body.storage", auth)
        actual_body = confirmed.get("body", {}).get("storage", {}).get("value")
        actual_version = confirmed.get("version", {}).get("number")
        info["version"] = actual_version
        if isinstance(actual_body, str):
            info["readback_body_sha256"] = hashlib.sha256(actual_body.encode("utf-8")).hexdigest()
        if (result.get("version", {}).get("number") != expected_version
                or actual_version != expected_version or confirmed.get("title") != title
                or not isinstance(actual_body, str)
                or canonical_storage(actual_body) != canonical_storage(body)):
            raise ValueError("Confluence read-back did not match title, version and canonical storage content")
        info.update({"verification": "canonical_storage_xml", "raw_body_equal": actual_body == body,
                     "canonical_body_sha256": hashlib.sha256(canonical_storage(body).encode()).hexdigest()})
        verified_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        info.update({"ok": True, "verified_at": verified_at})
        receipt.update({"ok": True, "stage": "verified", "published_at": verified_at})
        logger.info("Capacity page %s verified at version %s", page_id, actual_version)
    except ValueError as exc:
        # Known validation messages contain no source data or credentials.
        error = str(exc) if type(exc) is ValueError else "Malformed JSON in publication input or response"
    except urllib.error.HTTPError as exc:
        error = f"Confluence HTTP {exc.code}; delivery not verified"
    except Exception as exc:
        # Network errors can echo request URLs/headers; retain the type only.
        error = f"{type(exc).__name__} during {receipt['stage']}; delivery not verified"
    if error:
        receipt["error"] = error
        info["error"] = error
        logger.error("Capacity publication failed: %s", error)
    if not args.dry_run:
        try:
            _save_receipt(receipt)
        except Exception as exc:
            logger.error("Capacity publication receipt could not be saved (%s)", type(exc).__name__)
            return 1 if args.strict else 0
    return 1 if args.strict and not receipt["ok"] else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(main())
