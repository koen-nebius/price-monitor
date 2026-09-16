"""
Supply-tightness line for the daily digest, read from the capacity monitor's
committed outputs (capacity/store/, refreshed by its own 02:23 UTC workflow, so
the price digest at 01:23 UTC sees the previous run — the line says which).

Availability moves before list prices do: Lambda's B200 has had zero regions with
capacity for weeks, Scaleway's B300 flag reads "shortage", Hyperstack's stock API
alternates 0 and 10+. Rendering the sold-out ratio per GPU next to the price
position turns the capacity monitor into a price-move leading indicator, and the
week-over-week delta says whether the market is tightening or loosening.

Rules: only LIVE stock metrics count (regions_with_capacity, stock_status_label,
stock_level, offer_depth_gpus, binary); footprint metrics (listed_offering) and
the SF Compute clearing price are not availability. One provider, one vote per
GPU: sold_out only if ALL its live rows for that GPU are sold_out; Nebius (own
capacity) is excluded. Providers with no live metric for a GPU are not counted.
"""
import csv
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

CAP_DIR = Path(__file__).resolve().parent / "capacity" / "store"
LIVE_METRICS = {"regions_with_capacity", "stock_status_label", "stock_level", "offer_depth_gpus", "binary"}
GPUS = ["H100", "H200", "B200", "B300", "GB200", "GB300"]
NAMES = {"lambda": "Lambda", "coreweave": "CoreWeave", "crusoe": "Crusoe", "hyperstack": "Hyperstack",
         "verda": "Verda", "scaleway": "Scaleway", "voltage_park": "Voltage Park", "gmi": "GMI",
         "together": "Together", "runpod": "RunPod", "vast": "Vast", "sfcompute": "SF Compute",
         "aws": "AWS", "gcp": "GCP", "azure": "Azure", "oracle": "Oracle"}


def _provider_states(rows: List[dict]) -> Dict[str, Dict[str, str]]:
    """gpu -> provider -> sold_out | limited | available (one vote per provider)."""
    votes: Dict[str, Dict[str, set]] = {}
    for r in rows:
        if r.get("provider") == "nebius" or r.get("metric_type") not in LIVE_METRICS:
            continue
        gpu, state = r.get("gpu_model"), (r.get("state") or "").lower()
        if gpu not in GPUS or state not in ("sold_out", "limited", "available"):
            continue
        votes.setdefault(gpu, {}).setdefault(r["provider"], set()).add(state)
    out: Dict[str, Dict[str, str]] = {}
    for gpu, provs in votes.items():
        for p, states in provs.items():
            if states == {"sold_out"}:
                s = "sold_out"
            elif "available" in states:
                s = "available"
            else:
                s = "limited"
            out.setdefault(gpu, {})[p] = s
    return out


def _counts(states: Dict[str, Dict[str, str]]) -> Dict[str, Tuple[int, int, List[str]]]:
    """gpu -> (sold_out_count, live_provider_count, sold_out_provider_names)."""
    res = {}
    for gpu, provs in states.items():
        so = sorted(p for p, s in provs.items() if s == "sold_out")
        res[gpu] = (len(so), len(provs), [NAMES.get(p, p) for p in so])
    return res


def _history_rows_for(day: date) -> List[dict]:
    path = CAP_DIR / "history.csv"
    if not path.exists():
        return []
    rows = []
    with open(path, newline="") as f:
        for r in csv.reader(f):
            if len(r) < 8 or r[0] != day.isoformat():
                continue
            rows.append({"provider": r[1], "gpu_model": r[2], "region": r[3], "consumption_type": r[4],
                         "state": r[5], "metric_type": r[6], "metric_value": r[7]})
    return rows


def supply_line(today: Optional[date] = None) -> str:
    """One Slack line, or "" when the capacity snapshot is missing/stale (> 3 days)."""
    snap_path, man_path = CAP_DIR / "last_snapshot.json", CAP_DIR / "run_manifest.json"
    if not snap_path.exists():
        return ""
    try:
        rows = json.loads(snap_path.read_text())
        run_date = json.loads(man_path.read_text()).get("run_date") if man_path.exists() else None
    except Exception:
        return ""
    rd = datetime.strptime(run_date, "%Y-%m-%d").date() if run_date else None
    today = today or date.today()
    if rd and (today - rd).days > 3:
        return ""
    cur = _counts(_provider_states(rows))
    prev = _counts(_provider_states(_history_rows_for((rd or today) - timedelta(days=7)))) if rd else {}
    parts = []
    for gpu in GPUS:
        if gpu not in cur or cur[gpu][1] < 2:
            continue
        so, n, names = cur[gpu]
        seg = f"{gpu} {so}/{n} sold out"
        if so and so <= 4:
            seg += f" ({', '.join(names)})"
        if gpu in prev and prev[gpu][1]:
            d = so - prev[gpu][0]
            if d:
                seg += f" [{d:+d} WoW]"
        parts.append(seg)
    if not parts:
        return ""
    tag = f" (capacity run {run_date})" if run_date else ""
    return "*Supply:* " + " · ".join(parts) + tag


def supply_alerts(today: Optional[date] = None) -> List[str]:
    """Internal warnings when supply tightens sharply: a GPU's sold-out provider count rises
    by >= 2 week-over-week, or at least half of >= 4 live-stock providers are sold out."""
    snap_path, man_path = CAP_DIR / "last_snapshot.json", CAP_DIR / "run_manifest.json"
    if not snap_path.exists():
        return []
    try:
        rows = json.loads(snap_path.read_text())
        run_date = json.loads(man_path.read_text()).get("run_date") if man_path.exists() else None
    except Exception:
        return []
    rd = datetime.strptime(run_date, "%Y-%m-%d").date() if run_date else (today or date.today())
    cur = _counts(_provider_states(rows))
    prev = _counts(_provider_states(_history_rows_for(rd - timedelta(days=7))))
    out = []
    for gpu, (so, n, names) in cur.items():
        d = so - prev[gpu][0] if gpu in prev else 0
        if d >= 2:
            out.append(f"supply: {gpu} sold out at {so}/{n} live-stock providers, +{d} WoW ({', '.join(names)}) — tightening")
        elif n >= 4 and so * 2 >= n:
            out.append(f"supply: {gpu} sold out at {so}/{n} live-stock providers ({', '.join(names)})")
    return out


if __name__ == "__main__":
    print(supply_line() or "(no supply line)")
