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
from intel_schema import validate_row   # noqa: E402

PAGE_ID = "2054817562"
BASE = "https://nebius.atlassian.net/wiki"
INTEL_CSV = REPO / "store" / "intel.csv"
COLUMNS = ["message_ts", "message_date", "gpu_model", "price_per_gpu_hour_usd",
           "term_months", "prepay_pct", "provider_type", "provider_name", "notes"]


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
    a <pre>, or a <code> element — accept all three, then keep only lines that
    look like intel rows (>= 8 commas; the header line is skipped by design).
    """
    chunks = re.findall(r"<!\[CDATA\[(.*?)\]\]>", storage, re.S)
    chunks += re.findall(r"<pre[^>]*>(.*?)</pre>", storage, re.S)
    chunks += re.findall(r"<code[^>]*>(.*?)</code>", storage, re.S)
    lines = []
    for chunk in chunks:
        text = html.unescape(re.sub(r"<[^>]+>", "\n", chunk))
        for ln in text.splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("message_ts,"):
                continue
            if ln.count(",") >= 8:
                lines.append(ln)
    return lines


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
        rows = []
        reader = csv.reader(io.StringIO("\n".join(lines)))
        for parts in reader:
            if len(parts) < 9:
                continue
            rows.append(dict(zip(COLUMNS, [p.strip() for p in parts[:9]])))
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
                out.append(r)
                appended += 1
            existing_ts.add(ts)   # in-batch dedupe too

        if out:
            with open(INTEL_CSV, "a", newline="") as f:
                w = csv.DictWriter(f, fieldnames=COLUMNS)
                for r in out:
                    w.writerow(r)
        print(f"intel-inbox: merged {appended} new row(s), "
              f"{dup} already-known, {dropped} invalid (of {len(rows)} on page)")
    except Exception as e:
        print(f"intel-inbox: WARNING — merge failed, skipping: {e}")
        sys.exit(0)


if __name__ == "__main__":
    main()
