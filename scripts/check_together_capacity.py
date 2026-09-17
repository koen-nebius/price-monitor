"""Read-only scope check; never creates clusters, runs inference, or publishes."""
import json
import logging
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from capacity.main import _fetch_provider
from capacity import insights
from together_capacity_api import fetch_cluster_regions


def cluster_probe():
    try:
        payload = fetch_cluster_regions()
        regions = payload.get("regions") if isinstance(payload, dict) else None
        if not isinstance(regions, list):
            raise ValueError("Missing regions array")
        # Only aggregate documented response shape, never account data or raw body.
        return {
            "status": "reachable",
            "region_count": len(regions),
            "supported_instance_type_entries": sum(
                len(r["supported_instance_types"]) for r in regions
                if isinstance(r, dict) and isinstance(r.get("supported_instance_types"), list)),
            "live_gpu_cluster_availability": "not established; endpoint lists supported configurations",
        }
    except Exception as exc:
        status = getattr(exc, "http_status", None)
        return {"status": "unavailable",
                "reason": f"HTTP {status}" if type(status) is int else type(exc).__name__,
                "live_gpu_cluster_availability": "not established"}


def main():
    logging.basicConfig(level=logging.INFO)
    summary = {"cluster_regions_probe": cluster_probe(), "writes_to_provider": 0}
    exit_code = 0
    try:
        if not os.environ.get("TOGETHER_API_KEY", "").strip():
            raise ValueError("Missing credential")
        records = _fetch_provider("together")
        if not records or any(r.product_scope != "dedicated_inference"
                              or r.metric_type != "inference_replicas"
                              or r.region == "global" for r in records):
            raise ValueError("No correctly scoped inference records")
        models = sorted({r.gpu_model for r in records})
        if any(insights.live_reads(records, gpu) for gpu in models):
            raise ValueError("Inference leaked into GPU cluster availability")
        summary["inference_check"] = {
            "status": "passed", "records": len(records), "gpu_models": models,
            "replica_headroom_states": dict(Counter(r.state for r in records)),
            "gpu_counts_per_replica": sorted({r.gpu_count for r in records if r.gpu_count is not None}),
            "cluster_stock_contribution": 0,
        }
    except Exception as exc:
        summary["inference_check"] = {"status": "failed", "reason": type(exc).__name__}
        exit_code = 1
    rendered = json.dumps(summary, indent=2)
    print(rendered)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as handle:
            handle.write("Together scope check. No resources created or changed.\n\n```json\n"
                         + rendered + "\n```\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
