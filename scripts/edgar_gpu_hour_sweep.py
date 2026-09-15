"""
Monthly EDGAR full-text sweep for GPU rental rates disclosed in SEC filings.

Finds filings whose text contains an explicit per-GPU-hour price (8-K/6-K exhibits,
10-Q, 10-K/20-F, S-1, 424B prospectuses), extracts the matching sentences and writes
candidates to store/edgar_candidates.csv for MANUAL curation into
store/public_contracts.csv (the forward curve's public-contracts leg). Curation is
deliberate: TCV-implied rates need the GPU count, term and what the TCV bundles.

Verified yield (2026-09-15 discovery): ~10-15 documents/year carry an explicit
$/GPU-hour, all multi-year committed tier from small/mid-cap neoclouds (HIVE/BUZZ
HPC, Boost Run, Digi Power X, Nidar); the large disclosures (CoreWeave, Nebius,
IREN) give TCV/term/MW but no GPU counts.

EDGAR access rules (https://www.sec.gov/os/accessing-edgar-data): declared
User-Agent with company + contact, <= 10 requests/second, no crawling. We issue
one search per phrase plus one GET per new document, sleeping 0.2 s between calls
and backing off on HTTP 503.

Usage:  python3 scripts/edgar_gpu_hour_sweep.py [--days 45] [--max-docs 40]
"""
import argparse
import csv
import html as _html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

STORE = Path(__file__).resolve().parent.parent / "store"
OUT = STORE / "edgar_candidates.csv"
UA = {"User-Agent": "Nebius B.V. price-monitor koen@nebius.com", "Accept-Encoding": "gzip, deflate"}
PHRASES = ['"per GPU hour"', '"per GPU-hour"', '"per GPU per hour"', '"GPU-hour"']
FORMS = "8-K,6-K,10-K,10-Q,20-F,S-1,S-1/A,424B3,424B4,F-4"
SEARCH = ("https://efts.sec.gov/LATEST/search-index?q={q}&dateRange=custom"
          "&startdt={start}&enddt={end}&forms={forms}")
RATE_RE = re.compile(r"(?:\$|USD\s?)\s?\d{1,2}(?:\.\d{1,3})?\s*(?:per|/)\s*GPU(?:[\s-]*hour|\s*per\s*hour|/hr)", re.I)
CTX_RE = re.compile(r"[^.]{0,220}(?:per GPU[\s-]*hour|GPU-hour|per GPU per hour)[^.]{0,220}\.", re.I)


def _get(url: str, tries: int = 3) -> bytes:
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    import gzip
                    data = gzip.decompress(data)
                return data
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and i + 1 < tries:
                time.sleep(2 + 3 * i)
                continue
            raise
        finally:
            time.sleep(0.2)
    return b""


def search(phrase: str, start: date, end: date) -> list:
    url = SEARCH.format(q=urllib.parse.quote(phrase), start=start.isoformat(), end=end.isoformat(),
                        forms=urllib.parse.quote(FORMS))
    data = json.loads(_get(url).decode("utf-8", "replace"))
    hits = []
    for h in data.get("hits", {}).get("hits", []):
        src = h.get("_source", {})
        acc, _, fname = h.get("_id", "").partition(":")
        ciks = src.get("ciks") or []
        if not (acc and fname and ciks):
            continue
        cik = str(int(ciks[0]))
        hits.append({
            "filer": (src.get("display_names") or [""])[0],
            "form": src.get("form_type") or src.get("file_type", ""),
            "filed": src.get("file_date", ""),
            "url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{fname}",
            "phrase": phrase,
        })
    return hits


def extract(url: str) -> tuple:
    raw = _get(url).decode("utf-8", "replace")
    text = _html.unescape(re.sub(r"<[^>]+>", " ", raw))
    text = re.sub(r"\s+", " ", text)
    sentences = [m.group(0).strip() for m in CTX_RE.finditer(text)]
    rates = sorted(set(m.group(0) for m in RATE_RE.finditer(text)))
    return sentences[:6], rates[:10]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=45)
    ap.add_argument("--max-docs", type=int, default=40)
    a = ap.parse_args()
    end, start = date.today(), date.today() - timedelta(days=a.days)

    seen_urls = set()
    if OUT.exists():
        with open(OUT, newline="") as f:
            seen_urls = {r["url"] for r in csv.DictReader(f)}

    found = {}
    for ph in PHRASES:
        try:
            for h in search(ph, start, end):
                found.setdefault(h["url"], h)
        except Exception as e:
            print(f"search {ph}: {e}", file=sys.stderr)
    new = [h for u, h in found.items() if u not in seen_urls][: a.max_docs]
    print(f"{len(found)} documents matched, {len(new)} new (window {start}..{end})")

    rows = []
    for h in new:
        try:
            sentences, rates = extract(h["url"])
        except Exception as e:
            sentences, rates = [f"fetch failed: {e}"], []
        rows.append({**h, "explicit_rates": " | ".join(rates), "context": " || ".join(sentences)[:1500],
                     "curated": ""})
        print(f"- {h['filed']} {h['form']:7} {h['filer'][:40]:40} rates={rates[:4]}")

    write_header = not OUT.exists()
    with open(OUT, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["filed", "form", "filer", "url", "phrase", "explicit_rates", "context", "curated"])
        if write_header:
            w.writeheader()
        w.writerows(rows)
    print(f"appended {len(rows)} rows -> {OUT}")


if __name__ == "__main__":
    main()
