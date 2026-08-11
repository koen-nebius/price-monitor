"""
Cross-table consistency guard (Phase 1.5).

The same (provider, GPU, consumption_type) must not render as different values in
different sections of the report without a region label. The classic failure:
AWS H100 on-demand showing $6.88 in the executive table but $8.60 in the
capacity-block "vs OD" cell, because the two sections picked different regions.

The rule we enforce: every section that shows a single headline value for a
(provider, gpu, on_demand) pair must use the CHEAPEST-region value (the same
reference the executive table uses). Sections that intentionally show a specific
region must label it.

Run: python3 test_consistency.py   (exit 0 = pass, 1 = fail)
Also importable as check_cross_table_consistency(records) for use as a run gate.
"""
import sys
from typing import List

from schema import PriceRecord
from store import load_last_snapshot


def _cheapest_od(records: List[PriceRecord], provider: str) -> dict:
    out: dict = {}
    for r in records:
        if r.provider == provider and r.consumption_type == "on_demand":
            if r.gpu_model not in out or r.price_per_gpu_hour_usd < out[r.gpu_model]:
                out[r.gpu_model] = r.price_per_gpu_hour_usd
    return out


def check_cross_table_consistency(records: List[PriceRecord]) -> List[str]:
    """
    Return a list of human-readable inconsistencies (empty = consistent).

    Re-derives the values the executive table and the AWS capacity-block section
    use for AWS/Nebius on-demand and asserts they agree (both must be the cheapest
    region). This catches a regression where one section silently switches to a
    different region's price for the same logical cell.
    """
    problems: List[str] = []

    from diff import _best_comparable, GPU_ORDER

    aws_cheapest = _cheapest_od(records, "aws")

    for gpu in GPU_ORDER:
        # Executive table's "cheapest hyperscaler" for AWS-led GPUs and the
        # capacity-block "vs OD" baseline must reference the same AWS OD value.
        exec_hyp = _best_comparable(records, gpu, "on_demand", tiers=["hyperscaler"])
        cb_baseline = aws_cheapest.get(gpu)
        if exec_hyp and exec_hyp.provider == "aws" and cb_baseline is not None:
            if abs(exec_hyp.price_per_gpu_hour_usd - cb_baseline) > 0.005:
                problems.append(
                    f"{gpu} AWS on-demand: exec table ${exec_hyp.price_per_gpu_hour_usd:.2f} "
                    f"!= capacity-block baseline ${cb_baseline:.2f} (region mismatch, no label)"
                )

    return problems


def check_field_only_gpus(records: List[PriceRecord]) -> List[str]:
    """
    Regression guard for field-intel-only GPUs (VR, 2026-08-11 incident):
    1. No intel.csv row whose notes name Vera Rubin may be filed under another
       gpu_model — misfiled VR quotes polluted GB300 AND GB200 field stats and
       were compared against Nebius GB300 committed pricing on the live page.
    2. Every FIELD_ONLY_GPUS model with intel rows in the 90d window must render
       in the field-intel sections (they used to iterate GPU_ORDER only, which
       silently dropped VR)...
    3. ...and its rendered block must NOT price-compare against Nebius (no
       Nebius list/committed tier exists for field-only GPUs by definition).
    """
    import csv as _csv
    import re as _re
    import diff as _diff

    problems: List[str] = []

    with open("store/intel.csv", newline="") as f:
        for r in _csv.DictReader(f):
            notes = r.get("notes", "")
            if (("Vera Rubin" in notes or "VR200" in notes)
                    and r.get("gpu_model") != "VR"):
                problems.append(f"intel misfile: Vera Rubin quote filed as "
                                f"{r.get('gpu_model')} ({r.get('message_date')}: {notes[:50]})")

    _diff.enrich_comparability(records)
    intel_gpus = {r.get("gpu_model") for r in _diff._load_intel(days=90)}
    for g in _diff.FIELD_ONLY_GPUS:
        if g not in intel_gpus:
            continue
        for name, out in (
                ("field-intel section", _diff._build_field_intel_callout(records)),
                ("field committed section", _diff._build_field_committed_section(records)),
        ):
            if g not in out:
                problems.append(f"{g}: has intel rows but is missing from the {name}")
                continue
            zone = out[out.find(g):out.find(g) + 800]
            if _re.search(r"vs Nebius \$|Nebius \$[\d.]", zone):
                problems.append(f"{g}: {name} price-compares a field-only GPU "
                                f"against a Nebius price")
    return problems


def main() -> int:
    records = load_last_snapshot()
    if not records:
        print("no snapshot to check — skipping (not a failure)")
        return 0
    problems = check_cross_table_consistency(records)
    problems += check_field_only_gpus(records)
    if problems:
        print("CROSS-TABLE CONSISTENCY FAILURES:")
        for p in problems:
            print(f"  ✗ {p}")
        return 1
    print(f"cross-table consistency OK ({len(records)} records checked, "
          f"incl. field-only GPU guards)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
