"""
Daily snapshot of Aiven's public price matrix (managed-DB partnership tracking).

Aiven's entire plan x cloud x region price list is a public no-auth JSON API —
the data source behind aiven.io/pricing. We snapshot a focused slice daily so
that by the time Nebius SKUs exist there is a price history to compare against
(discovered 2026-08-15 during the Steven Kuhn / Aiven pricing prep; the future
Nebius substrate will appear as new cloud entries in this same API).

Standalone by design: managed-DB services are a different product domain, so
rows go to store/aiven_prices.csv (date-keyed upsert), NOT into the GPU
PriceRecord flow. Runs as its own soft-fail step in scrape.yml.
"""
import csv
import json
import sys
import urllib.request
from datetime import date
from pathlib import Path

API = "https://api.aiven.io/v1/service_types"
OUT = Path(__file__).resolve().parent.parent / "store" / "aiven_prices.csv"

SERVICES = {"valkey", "pg", "mysql", "kafka"}
# Plans that anchor our benchmark tables + the small end for dev-tier context.
PLANS = {"hobbyist", "startup-2", "startup-4", "startup-8", "startup-16",
         "business-8", "business-16", "premium-8", "premium-16"}
# Clouds we compare on today; any future nebius-* substrate is auto-captured.
CLOUD_PREFIXES = ("aws-us-east-1", "google-us-east4", "azure-eastus",
                  "do-nyc", "aws-eu-west-1", "google-europe-west1",
                  "azure-westeurope", "nebius")

FIELDS = ["date", "service", "plan", "cloud", "price_usd_hour",
          "node_count", "node_cpu", "node_ram_mb", "disk_mb"]


def main() -> int:
    try:
        req = urllib.request.Request(API, headers={"User-Agent": "Mozilla/5.0 (price-monitor/1.0)"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        print(f"aiven snapshot skipped (fetch failed: {e})")
        return 0   # soft-fail: never break the pipeline

    today = date.today().isoformat()
    rows = []
    service_types = data.get("service_types", data)
    for svc, sdef in service_types.items():
        if svc not in SERVICES:
            continue
        for plan in sdef.get("service_plans", []):
            name = plan.get("service_plan", "")
            if name not in PLANS:
                continue
            # regions is a dict keyed by cloud name; per-node specs (cpu/ram/disk)
            # live INSIDE each region entry, not on the plan.
            for cloud, region in (plan.get("regions") or {}).items():
                if not cloud.startswith(CLOUD_PREFIXES):
                    continue
                rows.append({
                    "date": today, "service": svc, "plan": name, "cloud": cloud,
                    "price_usd_hour": region.get("price_usd", ""),
                    "node_count": plan.get("node_count", ""),
                    "node_cpu": region.get("node_cpu_count", ""),
                    "node_ram_mb": region.get("node_memory_mb", ""),
                    "disk_mb": region.get("disk_space_mb", ""),
                })

    if not rows:
        print("aiven snapshot: 0 rows matched — API shape may have changed")
        return 0

    old = []
    if OUT.exists():
        with open(OUT, newline="") as f:
            old = [r for r in csv.DictReader(f) if r["date"] != today]
    with open(OUT, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(old + sorted(rows, key=lambda r: (r["service"], r["plan"], r["cloud"])))
    nebius_rows = [r for r in rows if r["cloud"].startswith("nebius")]
    if nebius_rows:
        print(f"aiven snapshot: NEBIUS SUBSTRATE DETECTED in Aiven's matrix "
              f"({len(nebius_rows)} rows) — the partnership column is live!")
    print(f"aiven snapshot: {len(rows)} rows for {today} "
          f"({len(old)} historical rows kept)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
