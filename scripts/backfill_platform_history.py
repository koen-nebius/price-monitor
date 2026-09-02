"""
One-off backfill of Modal + Baseten price history from Wayback Machine
snapshots (2026-09-02, Koen: "Can you backfill Modal and Baseten data?").

Sources — primary pages only, no aggregator inference:
  - Modal:   web.archive.org monthly captures of https://modal.com/pricing
             (29 captures back to 2024-01). Parsed with the live fetcher's
             regex, falling back to a tag-stripped text parse for older page
             markups. Per-second rate × 3600.
  - Baseten: captures of docs.baseten.co/performance/instances (pre-2025-04)
             and docs.baseten.co/deployment/resources (after the 308 rename).
             Parsed with the live fetcher's table parser. Per-minute × 60 ÷
             GPU count.

Why NOT the ComputePrices aggregator for backfill: its Modal feed misreads
per-MINUTE rates as $/hr (H100 "$0.07" ≈ $0.0658/min × wrong unit), its scrape
cadence is unknown, and it exposes no history endpoint — backing out prices
from a misread relay would stack two inferences. Wayback carries the primary
page, so every backfilled number is a price the provider actually published
on that date.

Rows are merged into store/history.csv keyed (snapshot_date, provider,
gpu_model, consumption_type) — existing keys are never overwritten — with
data_source="web_scrape_backfill" so backfilled points stay distinguishable
from live daily scrapes. File is rewritten sorted by date.

Usage:  python3 scripts/backfill_platform_history.py [--dry-run]
"""
import csv
import html as _html
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fetchers import modal as modal_f          # noqa: E402
from fetchers import baseten as baseten_f      # noqa: E402

HISTORY = Path(__file__).resolve().parent.parent / "store" / "history.csv"
CDX = ("http://web.archive.org/cdx/search/cdx?url={url}&output=json"
       "&from=20240101&filter=statuscode:200&fl=timestamp")
RAW = "http://web.archive.org/web/{ts}id_/{url}"
UA = {"User-Agent": "price-monitor-backfill/1.0 (contact: internal)"}

# Older Modal page markups: tag-strip to spaces, then name/price adjacency.
_MODAL_TEXT = re.compile(
    r"Nvidia\s+([A-Za-z0-9 ,.]+?)\s+\$\s*([0-9]*\.?[0-9]+)\s*/\s*sec")


def _get(url: str, tries: int = 2) -> str:
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception:
            if i + 1 == tries:
                raise
            time.sleep(3)


def _cdx_monthly(url: str, per_month: int = 4):
    """All captures grouped by month, capped. Monthly 'collapse' alone is not
    enough: many captures are JS-shell (client-rendered eras, ~13KB, no price
    content), so we try up to `per_month` captures until one parses."""
    data = json.loads(_get(CDX.format(url=url)))
    months = {}
    for (ts,) in (data[1:] if data else []):
        months.setdefault(ts[:6], []).append(ts)
    return {m: ts_list[:per_month] for m, ts_list in sorted(months.items())}


# Baseten marketing page: pricePerHour JSON appears escaped (App-Router RSC
# era: \"pricePerHour\":6.5) or plain (pages-router __NEXT_DATA__ era).
_BT_MKT = re.compile(
    r'\\?"name\\?":\\?"([^"\\]+?)\\?",[^{}]*?\\?"pricePerHour\\?":([0-9]*\.?[0-9]+)')


def _parse_modal(page: str):
    """model → per_gpu_hr, using live regex first, text fallback second."""
    out = {}
    for m in modal_f._ROW.finditer(page):
        name = _html.unescape(m.group(1)).strip().rstrip(",")
        model = modal_f.GPU_NAME_MAP.get(name)
        if model and model not in out:
            per = float(m.group(2)) * 3600.0
            if 0.10 <= per <= 30:
                out[model] = (round(per, 4), name)
    if out:
        return out
    text = _html.unescape(re.sub(r"<[^>]+>", " ", page))
    for name, price in _MODAL_TEXT.findall(text):
        model = modal_f.GPU_NAME_MAP.get(name.strip().rstrip(","))
        if model and model not in out:
            per = float(price) * 3600.0
            if 0.10 <= per <= 30:
                out[model] = (round(per, 4), name.strip())
    return out


def _parse_baseten_marketing(page: str):
    """model → (per_gpu_hr, 1, name) from the marketing page's embedded JSON
    (works across both Next.js eras). Base configs are single-GPU."""
    out = {}
    for name, hourly in _BT_MKT.findall(page):
        model = baseten_f._family(name)
        if not model:
            continue
        per = float(hourly)
        if 0.10 <= per <= 30 and model not in out:
            out[model] = (round(per, 4), 1, name)
    return out


def main():
    dry = "--dry-run" in sys.argv
    new_rows = []

    def _try_month(ts_list: list, url: str, parse) -> tuple:
        """First capture in the month that parses → (date, parsed)."""
        for ts in ts_list:
            date = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"
            try:
                page = _get(RAW.format(ts=ts, url=url))
            except Exception as e:
                print(f"  {date}: fetch failed ({e})")
                time.sleep(1.2)
                continue
            time.sleep(1.2)
            parsed = parse(page)
            if parsed:
                return date, parsed
        return f"{ts_list[0][:4]}-{ts_list[0][4:6]}-{ts_list[0][6:8]}", {}

    # ── Modal ────────────────────────────────────────────────────────────
    for month, ts_list in _cdx_monthly("modal.com/pricing").items():
        date, parsed = _try_month(ts_list, "https://modal.com/pricing",
                                  _parse_modal)
        print(f"modal {date}: " + (", ".join(
            f"{m} ${v[0]:.2f}" for m, v in sorted(parsed.items()))
            if parsed else f"no capture parsed ({len(ts_list)} tried)"))
        for model, (per, name) in parsed.items():
            new_rows.append({
                "snapshot_date": date, "provider": "modal",
                "gpu_model": model, "consumption_type": "on_demand",
                "region": "us (serverless)",
                "instance_type": f"serverless {name}", "gpu_count": 1,
                "price_per_gpu_hour_usd": per, "price_per_hour_usd": per,
                "data_source": "web_scrape_backfill",
            })

    # ── Baseten: docs table (full SKUs) + marketing page (base configs) ──
    seen_months = set()
    bt_sources = [
        ("https://docs.baseten.co/performance/instances",
         baseten_f._parse_docs_table),
        ("https://docs.baseten.co/deployment/resources",
         baseten_f._parse_docs_table),
        ("https://www.baseten.co/pricing/", _parse_baseten_marketing),
    ]
    for url, parse in bt_sources:
        for month, ts_list in _cdx_monthly(url.replace("https://", "")).items():
            if month in seen_months:
                continue
            date, parsed = _try_month(ts_list, url, parse)
            print(f"baseten {date} [{url.split('/')[2]}]: " + (", ".join(
                f"{m} ${v[0]:.2f}(x{v[1]})" for m, v in sorted(parsed.items()))
                if parsed else f"no capture parsed ({len(ts_list)} tried)"))
            if not parsed:
                continue
            seen_months.add(month)
            for model, (per, count, sku) in parsed.items():
                new_rows.append({
                    "snapshot_date": date, "provider": "baseten",
                    "gpu_model": model, "consumption_type": "on_demand",
                    "region": "us (managed)", "instance_type": sku,
                    "gpu_count": count,
                    "price_per_gpu_hour_usd": per,
                    "price_per_hour_usd": round(per * count, 4),
                    "data_source": "web_scrape_backfill",
                })

    # ── Source-conflict pass (disclosed, deterministic) ─────────────────
    # Baseten's DOCS table lagged the 2025-H1 H100 cut: the marketing page
    # showed H100 $6.50 from 2025-06-20 while docs still showed the old $9.98
    # through 2025-11-07 (stale docs, same class as Modal's stale FAQ rates).
    # Keeping both would write a fake $6.50<->$9.98 oscillation into history —
    # the Scaleway-flip-flop artifact class. The marketing page is the
    # provider's canonical public price surface, so stale-level docs rows on
    # or after the first marketing observation are dropped. Pre-cut $9.98
    # rows (2024-05 .. 2025-04) are real history and stay.
    _FIRST_MKT_H100 = "2025-06-20"
    before = len(new_rows)
    new_rows = [r for r in new_rows if not (
        r["provider"] == "baseten" and r["gpu_model"] == "H100"
        and r["snapshot_date"] >= _FIRST_MKT_H100
        and r["price_per_gpu_hour_usd"] > 9)]
    if len(new_rows) != before:
        print(f"dropped {before - len(new_rows)} stale Baseten docs H100 rows "
              f"(conflict with marketing page from {_FIRST_MKT_H100})")

    # ── Merge into history.csv (never overwrite an existing key) ────────
    with open(HISTORY, newline="") as f:
        reader = csv.DictReader(f)
        cols = reader.fieldnames
        rows = list(reader)
    existing = {(r["snapshot_date"], r["provider"], r["gpu_model"],
                 r["consumption_type"]) for r in rows}
    added = 0
    for nr in new_rows:
        key = (nr["snapshot_date"], nr["provider"], nr["gpu_model"],
               nr["consumption_type"])
        if key in existing:
            continue
        existing.add(key)
        rows.append({c: str(nr.get(c, "")) for c in cols})
        added += 1
    rows.sort(key=lambda r: (r["snapshot_date"], r["provider"],
                             r["gpu_model"], r["consumption_type"]))
    print(f"\n{added} backfill rows to add "
          f"({len(new_rows) - added} already present/skipped)")
    if dry:
        print("dry-run: history.csv untouched")
        return
    with open(HISTORY, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    print(f"history.csv rewritten: {len(rows)} rows")


if __name__ == "__main__":
    main()
