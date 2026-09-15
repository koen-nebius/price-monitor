"""
Wayback backfill of competitor LIST-price history for every direct fetcher that
parses a public HTML pricing page (2026-09-15; generalizes the Modal/Baseten
backfill in backfill_platform_history.py).

For each provider, monthly Wayback captures of its pricing page(s) are pushed
through the fetcher's OWN parser (the same code production runs), so every
recovered number is a price the provider actually published on that date, parsed
the same way we parse it today. Output: history.csv rows keyed
(snapshot_date, provider, gpu_model, consumption_type) — cheapest per key per
capture — tagged data_source="web_scrape_backfill"; existing keys are never
overwritten; the file is rewritten sorted.

Why: gives each competitor's repricing cadence and decay slope (a third
forward-curve input next to SemiAnalysis and field intel). Gaps are honest:
many captures are JS shells with no price content; up to 4 captures per month
are tried.

Usage:  python3 scripts/backfill_wayback_history.py [--dry-run] [--providers coreweave,lambda]  (nebius excluded, see PROVIDERS) [--from 2024-01]
"""
import argparse
import csv
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fetchers import coreweave, crusoe, hyperstack, lambda_labs, together  # noqa: E402

HISTORY = Path(__file__).resolve().parent.parent / "store" / "history.csv"
CDX = ("http://web.archive.org/cdx/search/cdx?url={url}&output=json"
       "&from={frm}&filter=statuscode:200&fl=timestamp")
RAW = "http://web.archive.org/web/{ts}id_/{url}"
UA = {"User-Agent": "price-monitor-backfill/1.0 (contact: internal)"}

# provider key -> (parser(html, now) -> [PriceRecord], [page URLs across eras])
PROVIDERS = {
    "coreweave": (coreweave._parse_html, ["https://www.coreweave.com/pricing", "https://www.coreweave.com/gpu-cloud-pricing"]),
    "crusoe":    (crusoe._parse_html,    ["https://crusoe.ai/cloud/pricing/", "https://www.crusoe.ai/cloud/pricing"]),
    "hyperstack": (hyperstack._parse_pricing, ["https://www.hyperstack.cloud/gpu-pricing"]),
    "lambda":    (lambda_labs._parse_html, ["https://lambda.ai/instances", "https://lambda.ai/pricing", "https://lambdalabs.com/service/gpu-cloud"]),
    # "nebius": excluded 2026-09-15 — on older page markups the parser reads the
    # preemptible column as on-demand (H200 $2.30, GB200 $5.50 in captures) and our
    # own list history is authoritative in cdm/billing/sku_prices_hist anyway.
    "together":  (together._parse,       ["https://www.together.ai/pricing"]),
}


def _get(url: str, tries: int = 2, timeout: int = 60) -> str:
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception:
            if i + 1 == tries:
                raise
            time.sleep(3)


def _cdx_monthly(url: str, frm: str, per_month: int = 4) -> dict:
    try:
        data = json.loads(_get(CDX.format(url=url.replace("https://", ""), frm=frm)))
    except Exception as e:
        print(f"  cdx {url}: {e}")
        return {}
    months = {}
    for (ts,) in (data[1:] if data else []):
        months.setdefault(ts[:6], []).append(ts)
    return {m: v[:per_month] for m, v in sorted(months.items())}


def backfill_provider(key: str, frm: str) -> list:
    parser, urls = PROVIDERS[key]
    rows, seen_months = [], set()
    for url in urls:
        for month, ts_list in _cdx_monthly(url, frm).items():
            if month in seen_months:
                continue
            for ts in ts_list:
                date = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"
                try:
                    page = _get(RAW.format(ts=ts, url=url))
                except Exception:
                    time.sleep(1.2)
                    continue
                time.sleep(1.2)
                try:
                    recs = parser(page, f"{date}T00:00:00+00:00")
                except Exception as e:
                    print(f"  {key} {date}: parser error {e}")
                    recs = []
                if not recs:
                    continue
                best = {}
                for r in recs:
                    if not (0.10 <= r.price_per_gpu_hour_usd <= 100):
                        continue
                    k = (r.gpu_model, r.consumption_type)
                    if k not in best or r.price_per_gpu_hour_usd < best[k].price_per_gpu_hour_usd:
                        best[k] = r
                for (gpu, ct), r in best.items():
                    rows.append({"snapshot_date": date, "provider": key, "gpu_model": gpu, "consumption_type": ct,
                                 "region": r.region, "instance_type": r.instance_type, "gpu_count": r.gpu_count,
                                 "price_per_gpu_hour_usd": round(r.price_per_gpu_hour_usd, 4),
                                 "price_per_hour_usd": round(r.price_per_hour_usd, 4),
                                 "data_source": "web_scrape_backfill"})
                print(f"  {key} {date} [{url.split('/')[2]}]: " + ", ".join(
                    f"{g} {ct} ${r.price_per_gpu_hour_usd:.2f}" for (g, ct), r in sorted(best.items())))
                seen_months.add(month)
                break
            else:
                print(f"  {key} {month}: no capture parsed ({len(ts_list)} tried) [{url.split('/')[2]}]")
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--providers", default=",".join(PROVIDERS))
    ap.add_argument("--from", dest="frm", default="2024-01")
    a = ap.parse_args()
    frm = a.frm.replace("-", "")[:6] + "01"
    new_rows = []
    for key in [p.strip() for p in a.providers.split(",") if p.strip()]:
        print(f"== {key} ==")
        new_rows += backfill_provider(key, frm)

    with open(HISTORY, newline="") as f:
        reader = csv.DictReader(f); cols = reader.fieldnames; rows = list(reader)
    existing = {(r["snapshot_date"], r["provider"], r["gpu_model"], r["consumption_type"]) for r in rows}
    added = 0
    for nr in new_rows:
        k = (nr["snapshot_date"], nr["provider"], nr["gpu_model"], nr["consumption_type"])
        if k in existing:
            continue
        existing.add(k); rows.append({c: str(nr.get(c, "")) for c in cols}); added += 1
    rows.sort(key=lambda r: (r["snapshot_date"], r["provider"], r["gpu_model"], r["consumption_type"]))
    print(f"\n{added} backfill rows to add ({len(new_rows) - added} already present)")
    if a.dry_run:
        print("dry-run: history.csv untouched"); return
    with open(HISTORY, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(rows)
    print(f"history.csv rewritten: {len(rows)} rows")


if __name__ == "__main__":
    main()
