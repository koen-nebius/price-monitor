#!/usr/bin/env python3
"""
Merge field-intel rows from the Confluence "Field Intel Inbox" page into
store/intel.csv.

Why this exists (migration 2026-07-10): the Claude posting routine lost repo
write access (scheduled sessions are read-only on Anthropic's side) and a
Slack app is not approvable, so the routine now PARKS extracted intel rows on
a Confluence page (ID 2054817562) instead of committing intel.csv. This script
runs in the 01:23 UTC GitHub Actions scrape — the one place with a working
commit credential — pulls the inbox, validates rows, and merges new ones.

Behavior (matches the tested Plan-2 design):
- Dedupe is MESSAGE-level, keyed by message_ts: if a ts already exists in
  intel.csv, ALL rows for that ts are skipped (multi-quote messages merge
  atomically); otherwise all its valid rows append together.
- Row validation via intel_schema.validate_row; invalid rows are dropped and
  logged, never written.
- SOFT-FAIL: any error (page unreachable, bad credentials, parse failure)
  logs a warning and exits 0 so an inbox hiccup never blocks the scrape.
- Missing CONFLUENCE_EMAIL / CONFLUENCE_API_TOKEN env -> informative skip,
  exit 0 (lets the workflow ship before the secrets are provisioned).
- Idempotent: re-running against the same inbox is a no-op.
"""
import base64
import csv
import html
import io
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from quote_evidence import payment_known  # noqa: E402
from intel_schema import validate_row, INTEL_COLUMNS   # noqa: E402

PAGE_ID = "2054817562"
BASE = "https://nebius.atlassian.net/wiki"
INTEL_CSV = REPO / "store" / "intel.csv"
COLUMNS = INTEL_COLUMNS
# prepay_known (2026-09-15): 1 when the quote states its prepayment (any non-zero value or an
# explicit zero in the notes), 0 when 0 % is only the extractor's default. See intel_quality.py.


def fetch_inbox_storage() -> str:
    email = os.environ.get("CONFLUENCE_EMAIL")
    token = os.environ.get("CONFLUENCE_API_TOKEN")
    if not email or not token:
        print("intel-inbox: CONFLUENCE_EMAIL/CONFLUENCE_API_TOKEN not set — skipping merge")
        sys.exit(0)
    auth = base64.b64encode(f"{email}:{token}".encode()).decode()
    url = f"{BASE}/rest/api/content/{PAGE_ID}?expand=body.storage"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Basic {auth}", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=45) as resp:
        data = json.load(resp)
    return data["body"]["storage"]["value"]


def extract_csv_lines(storage: str) -> list:
    """
    Pull candidate CSV lines out of the page's storage XHTML. The inbox keeps
    rows in a code block; depending on the editor that is a CDATA code macro,
    a <pre>, or a <code> element. Retain headers and quoted multiline fields
    together whenever the block contains a candidate nine-column CSV row.
    """
    raw_chunks = re.findall(r"<!\[CDATA\[(.*?)\]\]>", storage, re.S)
    markup_chunks = re.findall(r"<pre[^>]*>(.*?)</pre>", storage, re.S)
    markup_chunks += re.findall(r"<code[^>]*>(.*?)</code>", storage, re.S)
    chunks = raw_chunks + [html.unescape(re.sub(r"<[^>]+>", "", chunk)) for chunk in markup_chunks]
    lines = []
    seen = set()
    for chunk in chunks:
        # Keep quoted multiline notes intact, including continuation lines with
        # no commas. CDATA is literal content, not markup to strip from notes.
        text = chunk.strip()
        if text in seen or not any(ln.count(",") >= 8 for ln in text.splitlines()):
            continue
        seen.add(text)
        lines.extend(text.splitlines())
    return lines


def parse_rows(lines):
    """Legacy nine-column rows and explicit extended headers share one validator."""
    columns = COLUMNS
    rows = []
    for parts in csv.reader(io.StringIO("\n".join(lines))):
        if parts and parts[0].strip() == "message_ts":
            columns = [p.strip() for p in parts]
            continue
        if len(parts) < 9 or len(parts) > len(columns):
            continue
        rows.append(dict(zip(columns, [p.strip() for p in parts])))
    return rows


def append_preserving_schema(path, additions):
    """Upgrade the CSV header atomically; never append wider rows under an old header."""
    import os
    import tempfile
    path = Path(path)
    existing, columns = [], list(COLUMNS)
    if path.exists():
        with path.open(newline="") as stream:
            reader = csv.DictReader(stream)
            columns = list(dict.fromkeys(list(reader.fieldnames or []) + COLUMNS))
            existing = list(reader)
    columns = list(dict.fromkeys(columns + [key for row in additions for key in row]))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader(); writer.writerows(existing + additions)
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)


def main():
    try:
        storage = fetch_inbox_storage()
    except SystemExit:
        raise
    except Exception as e:
        print(f"intel-inbox: WARNING — inbox fetch failed, skipping merge: {e}")
        sys.exit(0)

    try:
        lines = extract_csv_lines(storage)
        rows = parse_rows(lines)
        if not rows:
            print("intel-inbox: no candidate rows on the inbox page — nothing to merge")
            return

        existing_ts = set()
        if INTEL_CSV.exists():
            with open(INTEL_CSV, newline="") as f:
                for r in csv.DictReader(f):
                    existing_ts.add(r.get("message_ts", ""))

        # message-level grouping so multi-quote messages merge atomically
        by_ts, order = {}, []
        for r in rows:
            ts = r["message_ts"]
            if ts not in by_ts:
                by_ts[ts] = []
                order.append(ts)
            by_ts[ts].append(r)

        appended, dropped, dup = 0, 0, 0
        out = []
        for ts in order:
            if not ts or ts in existing_ts:
                dup += len(by_ts[ts])
                continue
            for r in by_ts[ts]:
                problems = validate_row(r)
                if problems:
                    dropped += 1
                    print(f"intel-inbox: dropping invalid row ts={ts}: {problems}")
                    continue
                r["prepay_known"] = "1" if payment_known(r) else "0"
                out.append(r)
                appended += 1
            existing_ts.add(ts)   # in-batch dedupe too

        if out:
            append_preserving_schema(INTEL_CSV, out)
        print(f"intel-inbox: merged {appended} new row(s), "
              f"{dup} already-known, {dropped} invalid (of {len(rows)} on page)")
    except Exception as e:
        print(f"intel-inbox: WARNING — merge failed, skipping: {e}")
        sys.exit(0)


if __name__ == "__main__":
    main()
