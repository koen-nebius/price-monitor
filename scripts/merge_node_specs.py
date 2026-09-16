#!/usr/bin/env python3
"""
Build store/node_specs.json: the node configuration (GPUs per node, vCPU, RAM, local NVMe,
network) behind each provider's priced GPU SKU, so a $/GPU-hour comparison can say what
CPU and memory sit behind each price.

Inputs
  --research FILE   JSON with an "entries" list from the node-spec research workflow
                    (official provider docs, one source_url per entry, verifier flag)
  store/latest.json daily provider snapshot: vcpu / ram_gb / node_gpus where a fetcher
                    records them (provider APIs and pages); cross-check of the researched
                    figures and the only source for a SKU without one (confidence "snapshot")
  --catalog         also read the dstack gpuhunt catalogs (fetchers/gpuhunt.py fetch_specs):
                    cpu, memory and disk per instance for aws, azure, gcp, lambda, nebius,
                    oracle, runpod, verda (confidence "catalog")
Precedence: researched documentation entry > API snapshot > gpuhunt catalog. Machine sources
fill empty fields of a documented SKU (recorded in `filled_from`) and report disagreements
(`api_check`, `catalog_check`); they add a SKU only when documentation has none.
Every field change against the previous store/node_specs.json is appended to
store/node_spec_changes.csv (date, provider, gpu, sku, field, old, new, source), so a price
change can be read next to a configuration change. Runs daily in the GitHub Action.

Provider keys are normalised to the price monitor's history keys without the "cp_" prefix
and "-com" suffix (aws, gcp, azure, oracle, coreweave, lambda, together, crusoe, hyperstack,
nebius, vultr, digitalocean, denvr-dataworks, sesterce, scaleway, ionet, verda, 1legion,
baseten, runpod, sfcompute, vast).
"""
import argparse
import csv
import json
from collections import defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "store" / "node_specs.json"
SNAPSHOT = ROOT / "store" / "latest.json"
TIERS = {"H100", "H200", "B200", "B300", "GB200", "GB300", "VR"}
FIELDS = ("instance_type", "node_gpus", "vcpu", "cpu_model", "ram_gb", "local_storage_tb", "network",
          "form_factor", "source_url", "as_of", "confidence", "verified", "notes")


CHANGES = ROOT / "store" / "node_spec_changes.csv"
CHECK_FIELDS = ("vcpu", "ram_gb", "node_gpus", "local_storage_tb")


def _fill_and_check(x: dict, vals: dict, check_key: str, label: str) -> None:
    """Fill empty fields of a documented SKU from a machine source and record disagreements."""
    filled, mism = [], []
    for k, v in vals.items():
        if v in (None, "", 0):
            continue
        try:
            v = int(float(v)) if k in ("vcpu", "node_gpus") else float(v)
        except (TypeError, ValueError):
            continue
        cur = x.get(k)
        if cur in (None, ""):
            x[k] = v
            filled.append(k)
        elif k in ("vcpu", "node_gpus") and int(float(cur)) != v:
            mism.append(f"{k} doc {cur} vs {v}")
        elif k in ("ram_gb", "local_storage_tb") and abs(float(cur) - v) > 0.11 * max(float(cur), v):   # GiB->GB is +7.4%, TiB->TB +10%: unit conventions, not disagreements
            mism.append(f"{k} doc {cur} vs {v}")
    if filled:
        x["filled_from"] = ((x.get("filled_from") or "") + f"; {', '.join(filled)} from {label}").strip("; ")
    x[check_key] = ("mismatch: " + "; ".join(mism)) if mism else f"matches {label}"


def _changes(previous: dict, out: dict) -> list:
    """Field-level differences between the previous node_specs.json and the merged result."""
    prev = {}
    for prov, tiers in (previous.get("providers") or {}).items():
        for tier, entries in tiers.items():
            for e in entries:
                prev[(prov, tier, (e.get("instance_type") or "").lower())] = e
    rows, today = [], date.today().isoformat()
    for prov, tiers in out.items():
        for tier, entries in tiers.items():
            for e in entries:
                old = prev.get((prov, tier, (e.get("instance_type") or "").lower()))
                if not old:
                    continue
                for k in CHECK_FIELDS:
                    a, b = old.get(k), e.get(k)
                    if a in (None, "") or b in (None, "") or a == b:
                        continue
                    try:
                        if abs(float(a) - float(b)) <= 1e-9:
                            continue
                    except (TypeError, ValueError):
                        pass
                    rows.append({"date": today, "provider": prov, "gpu": tier, "instance_type": e.get("instance_type"), "field": k,
                                 "old": a, "new": b, "source": e.get("filled_from") or e.get("source") or ""})
    return rows


def norm(p: str) -> str:
    p = (p or "").strip().lower()
    if p.startswith("cp_"):
        p = p[3:]
    if p.endswith("-com"):
        p = p[:-4]
    return {"vast_reserved": "vast", "lambda_labs": "lambda", "onelegion": "1legion", "io.net": "ionet", "datacrunch": "verda",
            "denvr": "denvr-dataworks", "denvrdata": "denvr-dataworks", "sf compute": "sfcompute", "sf-compute": "sfcompute"}.get(p, p)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--research", type=Path, default=None)
    ap.add_argument("--catalog", action="store_true", help="also merge the dstack gpuhunt catalogs")
    args = ap.parse_args()
    previous = json.loads(OUT.read_text()) if OUT.exists() else None
    out = defaultdict(lambda: defaultdict(list))
    n_res = 0
    if args.research and args.research.exists():
        data = json.loads(args.research.read_text())
        for e in data.get("entries", []):
            tier = str(e.get("gpu_model", "")).upper()
            if tier not in TIERS:
                continue
            row = {k: e.get(k) for k in FIELDS}
            row["verified"] = bool(e.get("verified"))
            row["source"] = "provider documentation"
            if e.get("verifier_note"):
                row["notes"] = (row.get("notes") or "") + (" | verifier: " + e["verifier_note"] if e["verifier_note"] else "")
            out[norm(e.get("provider"))][tier].append(row)
            n_res += 1
    # snapshot cross-check / fallback
    n_snap = 0
    if SNAPSHOT.exists():
        rows = json.loads(SNAPSHOT.read_text())
        rows = rows if isinstance(rows, list) else []
        seen = set()
        for r in rows:
            tier = str(r.get("gpu_model", "")).upper()
            if tier not in TIERS or not (r.get("vcpu") and r.get("ram_gb")):
                continue
            key = (norm(r.get("provider")), tier, r.get("instance_type"))
            if key in seen:
                continue
            seen.add(key)
            prov, _, inst = key
            have = [x for x in out[prov][tier] if (x.get("instance_type") or "").lower() == str(inst).lower()]
            snap_as_of = str(r.get("fetched_at", ""))[:10]
            if not have:   # same node size counts as the same SKU when the documented label differs from the API name
                gc = r.get("node_gpus") or r.get("gpu_count")
                have = [x for x in out[prov][tier] if gc and x.get("node_gpus") == int(float(gc))]
            if have:
                x = have[0]
                _fill_and_check(x, {"vcpu": int(r["vcpu"]), "ram_gb": float(r["ram_gb"]), "node_gpus": r.get("node_gpus") or r.get("gpu_count")},
                                "api_check", f"provider snapshot {snap_as_of}")
                continue
            out[prov][tier].append({"instance_type": inst, "node_gpus": r.get("node_gpus") or r.get("gpu_count"), "vcpu": int(r["vcpu"]),
                                    "cpu_model": None, "ram_gb": float(r["ram_gb"]), "local_storage_tb": None,
                                    "network": (r.get("interconnect") or None), "form_factor": r.get("form_factor") or None,
                                    "source_url": r.get("source_url") or None, "as_of": snap_as_of, "confidence": "snapshot",
                                    "verified": True, "source": "provider API snapshot", "notes": "vCPU and RAM from the provider API as recorded by the daily monitor; storage and network bandwidth not captured"})
            n_snap += 1
    n_cat = 0
    if args.catalog:
        import sys
        sys.path.insert(0, str(ROOT))
        try:
            from fetchers.gpuhunt import fetch_specs
            cat = fetch_specs()
        except Exception as e:  # network or import: the merge still runs on the other sources
            print(f"gpuhunt catalogs unavailable: {e}")
            cat = []
        for c in cat:
            tier = str(c.get("gpu_model", "")).upper()
            if tier not in TIERS:
                continue
            prov = norm(c.get("provider"))
            inst = (c.get("instance_type") or "").lower()
            # same node size first; the catalog's instance name only disambiguates between SKUs of that size
            same_size = [x for x in out[prov][tier] if x.get("node_gpus") == c.get("node_gpus")]
            have = [x for x in same_size if inst and (x.get("instance_type") or "").lower().replace(" ", "").find(inst.replace(" ", "")) >= 0] or same_size
            label = f"dstack gpuhunt catalog {c.get('catalog_version') or ''}".strip()
            if have:
                _fill_and_check(have[0], {"vcpu": c.get("vcpu"), "ram_gb": c.get("ram_gb"), "local_storage_tb": c.get("local_storage_tb"),
                                          "node_gpus": c.get("node_gpus")}, "catalog_check", label)
                continue
            out[prov][tier].append({"instance_type": c.get("instance_type"), "node_gpus": c.get("node_gpus"), "vcpu": c.get("vcpu"), "cpu_model": None,
                                    "ram_gb": c.get("ram_gb"), "local_storage_tb": c.get("local_storage_tb"), "network": None, "form_factor": None,
                                    "source_url": "https://github.com/dstackai/gpuhunt", "as_of": date.today().isoformat(), "confidence": "catalog",
                                    "verified": True, "source": label,
                                    "notes": f"CPU threads, memory GB and disk from the dstack gpuhunt catalog ({c.get('location', '')}); network not carried"})
            n_cat += 1
    changes = _changes(previous, out) if previous else []
    if changes:
        new_file = not CHANGES.exists()
        with open(CHANGES, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["date", "provider", "gpu", "instance_type", "field", "old", "new", "source"])
            if new_file:
                w.writeheader()
            w.writerows(changes)
    result = {"_generated": date.today().isoformat(),
              "_method": "One entry per priced SKU. Figures come from the provider's own documentation (research + independent verification of each figure against its source URL; 'verified' false = a figure could not be confirmed) or, where the provider API reports them, from the daily monitor snapshot. vCPU = threads (physical cores x2 where the page lists cores); RAM in GB as published (GiB accepted). Nothing is estimated from a typical HGX configuration.",
              "providers": {p: {t: sorted(v, key=lambda x: -(x.get("node_gpus") or 0)) for t, v in tiers.items()} for p, tiers in sorted(out.items())}}
    OUT.write_text(json.dumps(result, indent=1))
    print(f"wrote {OUT}: {sum(len(v) for tiers in out.values() for v in tiers.values())} entries ({n_res} researched, {n_snap} snapshot-only, {n_cat} catalog-only) "
          f"for {len(out)} providers; {len(changes)} field changes logged" + (f" to {CHANGES.name}" if changes else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
