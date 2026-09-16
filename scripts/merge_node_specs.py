#!/usr/bin/env python3
"""
Build store/node_specs.json: the node configuration (GPUs per node, vCPU, RAM, local NVMe,
network) behind each provider's priced GPU SKU, so a $/GPU-hour comparison can say what
CPU and memory sit behind each price.

Inputs
  --research FILE   JSON with an "entries" list from the node-spec research workflow
                    (official provider docs, one source_url per entry, verifier flag)
  store/latest.json daily provider snapshot: vcpu / ram_gb where a provider API reports
                    them (AWS, Azure); used as a cross-check and as the only source for a
                    provider without a researched entry (confidence "snapshot")

Provider keys are normalised to the price monitor's history keys without the "cp_" prefix
and "-com" suffix (aws, gcp, azure, oracle, coreweave, lambda, together, crusoe, hyperstack,
nebius, vultr, digitalocean, denvr-dataworks, sesterce, scaleway, ionet, verda, 1legion,
baseten, runpod, sfcompute, vast).
"""
import argparse
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
    args = ap.parse_args()
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
            if have:
                x = have[0]
                mism = []
                if x.get("vcpu") not in (None, int(r["vcpu"])):
                    mism.append(f"vcpu doc {x['vcpu']} vs api {int(r['vcpu'])}")
                if x.get("ram_gb") is not None and abs(float(x["ram_gb"]) - float(r["ram_gb"])) > 0.02 * float(r["ram_gb"]):
                    mism.append(f"ram doc {x['ram_gb']} vs api {r['ram_gb']}")
                x["api_check"] = ("mismatch: " + "; ".join(mism)) if mism else f"matches the provider API snapshot {snap_as_of}"
                continue
            out[prov][tier].append({"instance_type": inst, "node_gpus": r.get("node_gpus") or r.get("gpu_count"), "vcpu": int(r["vcpu"]),
                                    "cpu_model": None, "ram_gb": float(r["ram_gb"]), "local_storage_tb": None,
                                    "network": (r.get("interconnect") or None), "form_factor": r.get("form_factor") or None,
                                    "source_url": r.get("source_url") or None, "as_of": snap_as_of, "confidence": "snapshot",
                                    "verified": True, "source": "provider API snapshot", "notes": "vCPU and RAM from the provider API as recorded by the daily monitor; storage and network bandwidth not captured"})
            n_snap += 1
    result = {"_generated": date.today().isoformat(),
              "_method": "One entry per priced SKU. Figures come from the provider's own documentation (research + independent verification of each figure against its source URL; 'verified' false = a figure could not be confirmed) or, where the provider API reports them, from the daily monitor snapshot. vCPU = threads (physical cores x2 where the page lists cores); RAM in GB as published (GiB accepted). Nothing is estimated from a typical HGX configuration.",
              "providers": {p: {t: sorted(v, key=lambda x: -(x.get("node_gpus") or 0)) for t, v in tiers.items()} for p, tiers in sorted(out.items())}}
    OUT.write_text(json.dumps(result, indent=1))
    print(f"wrote {OUT}: {sum(len(v) for tiers in out.values() for v in tiers.values())} entries ({n_res} researched, {n_snap} snapshot-only) for {len(out)} providers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
