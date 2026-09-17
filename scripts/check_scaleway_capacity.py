"""Verify public exact-SKU availability without provisioning or publishing."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from capacity import insights
from capacity.fetchers import scaleway


def main():
    records = scaleway.fetch()
    if not records:
        print("Scaleway returned no usable exact-SKU availability observations.", file=sys.stderr)
        return 1
    if any(r.product_scope != "gpu_instance" or r.metric_type != "instance_stock_status"
           or not r.instance_type or r.region == "global"
           or not isinstance(r.gpu_count, int) or r.gpu_count < 1 for r in records):
        print("Scaleway returned an unscoped availability observation.", file=sys.stderr)
        return 1
    models = sorted({r.gpu_model for r in records})
    if any(insights.live_reads(records, model) for model in models):
        print("Exact instance stock incorrectly entered cluster availability.", file=sys.stderr)
        return 1
    print(json.dumps({
        "source": "public Scaleway API; no authentication",
        "observations": len(records),
        "zones_with_gpu_offers": sorted({r.region for r in records}),
        "gpu_models": models,
        "scope": "exact SKU and zone; neither quota nor multi-node cluster availability",
        "eight_gpu_offers": [
            {"sku": r.instance_type, "zone": r.region, "state": r.state,
             "evidence": r.detail, "observed_at": r.fetched_at}
            for r in records if r.gpu_count == 8
        ],
        "writes_to_provider": 0,
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
