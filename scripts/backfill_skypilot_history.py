"""
Backfill competitor on-demand/spot LIST-price history from the SkyPilot catalog's
git history (github.com/skypilot-org/skypilot-catalog; 2026-09-16 source sweep).

SkyPilot refreshes catalogs/v*/<cloud>/vms.csv from each cloud's own API (Lambda
since 2023-01, RunPod since 2024-01) and commits only when something changed, so
the commit history is a dated record of list-price changes, at exact dates where
Wayback only gave monthly samples. Per commit date we keep, per GPU, the cheapest
per-GPU on-demand price (Price / AcceleratorCount) and spot price where present,
i.e. the same "cheapest variant" rule the live fetchers use.

Rows enter store/history.csv keyed (snapshot_date, provider, gpu_model,
consumption_type) with data_source="catalog_backfill_skypilot"; existing keys are
never overwritten; the file is rewritten sorted.

Usage:  python3 scripts/backfill_skypilot_history.py --repo <clone> [--dry-run]
        [--clouds lambda,runpod] [--sample-days 7]   (clone: git clone --filter=blob:none --no-checkout)
"""
import argparse
import csv
import io
import subprocess
import sys
from collections import Counter
from datetime import date
from pathlib import Path

HISTORY = Path(__file__).resolve().parent.parent / "store" / "history.csv"
CLOUD_KEY = {"lambda": "lambda", "runpod": "runpod", "verda": "verda"}
# SkyPilot AcceleratorName -> our gpu_model. Lambda's "RTX6000" is the Quadro RTX 6000
# (not RTX PRO 6000) and is deliberately unmapped; GH200 is not tracked.
GPU_MAP = {"H100": "H100", "H100-SXM": "H100", "H100-80GB": "H100", "H100-80GB-SXM": "H100", "H100-NVL": "H100",
           "H200": "H200", "H200-SXM": "H200", "H200-141GB": "H200",
           "B200": "B200", "B200-CC": "B200", "B300": "B300",
           "L40S": "L40S", "RTX-PRO-6000": "RTX6000", "RTX-PRO-6000-CC": "RTX6000", "RTXPRO6000": "RTX6000"}
SAMPLE_DEFAULT = {"lambda": 1, "runpod": 7, "verda": 7}   # days between kept dates


def git(repo, *args) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def commits_for(repo, cloud):
    """[(date, sha, path)] for every commit touching any version of the cloud's vms.csv, oldest first."""
    out = []
    for v in range(1, 9):
        path = f"catalogs/v{v}/{cloud}/vms.csv"
        log = git(repo, "log", "--format=%cs %H", "--", path).strip()
        if not log:
            continue
        for line in log.splitlines():
            d, sha = line.split()
            out.append((d, sha, path))
    out.sort()
    return out


def parse_csv(text: str, provider_key: str, seen_names: Counter):
    """Per gpu -> {ct: (per_gpu_price, count, instance, region)} cheapest variant."""
    best = {}
    for r in csv.DictReader(io.StringIO(text)):
        name = (r.get("AcceleratorName") or "").strip()
        if not name:
            continue
        seen_names[name] += 1
        gpu = GPU_MAP.get(name)
        if not gpu:
            continue
        try:
            count = float(r.get("AcceleratorCount") or 0)
        except ValueError:
            continue
        if count <= 0:
            continue
        for ct, col in (("on_demand", "Price"), ("spot", "SpotPrice")):
            try:
                total = float(r.get(col) or 0)
            except ValueError:
                continue
            if total <= 0:
                continue
            per = total / count
            if not (0.10 <= per <= 100):
                continue
            k = (gpu, ct)
            if k not in best or per < best[k][0]:
                best[k] = (round(per, 4), int(count), r.get("InstanceType", ""), r.get("Region", ""))
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--clouds", default="lambda,runpod")
    ap.add_argument("--sample-days", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    repo = Path(a.repo)
    new_rows, names = [], Counter()
    for cloud in [c.strip() for c in a.clouds.split(",") if c.strip()]:
        key = CLOUD_KEY.get(cloud, cloud)
        step = a.sample_days or SAMPLE_DEFAULT.get(cloud, 7)
        by_date = {}
        for d, sha, path in commits_for(repo, cloud):
            by_date[d] = (sha, path)          # last commit of the day wins
        dates = sorted(by_date)
        kept, last = [], None
        for d in dates:                        # keep at most one date per `step` days, always the last
            dd = date.fromisoformat(d)
            if last is None or (dd - last).days >= step or d == dates[-1]:
                kept.append(d); last = dd
        print(f"== {cloud}: {len(dates)} change dates {dates[0]}..{dates[-1]}, sampling {len(kept)} (every {step}d)")
        prev_summary = None
        for d in kept:
            sha, path = by_date[d]
            try:
                text = git(repo, "show", f"{sha}:{path}")
            except subprocess.CalledProcessError as e:
                print(f"  {d}: git show failed ({e.stderr.strip()[:80]})"); continue
            best = parse_csv(text, key, names)
            summary = ", ".join(f"{g} {ct} ${v[0]:.2f}" for (g, ct), v in sorted(best.items()))
            if summary != prev_summary:
                print(f"  {d} [{path.split('/')[1]}]: {summary or 'no tracked GPUs'}")
            prev_summary = summary
            for (gpu, ct), (per, count, inst, region) in best.items():
                # RunPod's catalog "SpotPrice" mirrors the on-demand price from 2026-05 on
                # (secureSpotPrice == Price); a spot row equal to on-demand carries no information.
                if ct == "spot" and (gpu, "on_demand") in best and abs(best[(gpu, "on_demand")][0] - per) < 0.005:
                    continue
                new_rows.append({"snapshot_date": d, "provider": key, "gpu_model": gpu, "consumption_type": ct,
                                 "region": region or "global", "instance_type": inst, "gpu_count": count,
                                 "price_per_gpu_hour_usd": per, "price_per_hour_usd": round(per * count, 4),
                                 "data_source": "catalog_backfill_skypilot"})
    unmapped = {n: c for n, c in names.items() if n not in GPU_MAP}
    print(f"\naccelerator names met: {dict(names)}\nunmapped (ignored): {unmapped}")

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
    print(f"\n{added} rows to add ({len(new_rows) - added} keys already present)")
    if a.dry_run:
        print("dry-run: history.csv untouched"); return
    with open(HISTORY, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(rows)
    print(f"history.csv rewritten: {len(rows)} rows")


if __name__ == "__main__":
    main()
