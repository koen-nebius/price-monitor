"""Read-only Lambda production-dispatch check. No launches or publishing."""
import json
import logging
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from capacity.main import _fetch_provider
from capacity import insights


def validate(records):
    if not records or any(not insights.is_lambda_instance(row) for row in records):
        raise ValueError("No correctly scoped exact-instance records")
    summaries = [row for row in records if row.region == "global"]
    if not summaries or len({row.instance_type for row in summaries}) != len(summaries):
        raise ValueError("Missing or duplicate exact-SKU summaries")
    by_sku = {row.instance_type: row for row in summaries}
    for row in records:
        summary = by_sku.get(row.instance_type)
        if summary is None or (summary.gpu_model, summary.gpu_count) != (row.gpu_model, row.gpu_count):
            raise ValueError("Conflicting exact-SKU identity")
        if row.region != "global" and (row.state != "available" or row.metric_value != 1):
            raise ValueError("Invalid regional launchability")
    for row in summaries:
        regions = {r.region for r in records if r.instance_type == row.instance_type and r.region != "global"}
        if row.state == "unknown":
            if row.metric_value is not None:
                raise ValueError("Unknown summary has a numeric count")
        elif row.state not in {"available", "sold_out"} or row.metric_value != len(regions) \
                or (row.state == "available") != bool(regions):
            raise ValueError("Exact-SKU regions do not reconcile")
    if all(row.state == "unknown" for row in summaries):
        raise ValueError("All availability fields are unknown")
    models = sorted({row.gpu_model for row in records})
    if any(insights.live_reads(records, gpu) or insights.tightness(records, gpu) for gpu in models):
        raise ValueError("Instance launchability leaked into cluster availability")
    if insights.gtm_claims(records, []) != {"ammo": [], "expired": []}:
        raise ValueError("Instance launchability leaked into market claims")
    if any(value.get("cheapest_bookable", {}).get("provider") == "Lambda"
           for value in insights.price_join(records).values() if value.get("cheapest_bookable")):
        raise ValueError("Unmatched price marked bookable")
    return {"status": "passed", "exact_skus": len(summaries), "records": len(records),
            "gpu_models": models, "gpu_counts_per_instance": sorted({row.gpu_count for row in records}),
            "exact_sku_states": dict(Counter(row.state for row in summaries)),
            "region_records": len(records) - len(summaries), "cluster_stock_contribution": 0}


def main():
    logging.basicConfig(level=logging.INFO)
    summary = {"writes_to_provider": 0}
    exit_code = 0
    try:
        if not os.environ.get("LAMBDA_API_KEY", "").strip():
            raise ValueError("Missing credential")
        summary["instance_check"] = validate(_fetch_provider("lambda"))
    except Exception as exc:
        summary["instance_check"] = {"status": "failed", "reason": type(exc).__name__}
        exit_code = 1
    rendered = json.dumps(summary, indent=2)
    print(rendered)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as handle:
            handle.write("Lambda exact-instance check. No resources created or changed.\n\n```json\n"
                         + rendered + "\n```\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
