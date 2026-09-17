"""Read-only production-dispatch check; no resources, store or external posts."""
import json
import logging
import os
import sys
from collections import Counter
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from capacity.main import _fetch_provider
from crusoe_api import fetch_capacities


def main():
    logging.basicConfig(level=logging.INFO)
    try:
        if not all(os.environ.get(key, "").strip() for key in
                   ("CRUSOE_ACCESS_KEY_ID", "CRUSOE_SECRET_KEY")):
            raise ValueError("Both Crusoe credentials are required")
        payload = fetch_capacities()
        # Schema-only diagnostics, never raw values or response content.
        structure = {
            "api_items": len(payload["items"]),
            "field_types": {field: dict(Counter(type(item.get(field)).__name__
                                                for item in payload["items"]
                                                if isinstance(item, dict)))
                            for field in ("type", "location", "quantity", "num_slices", "quota_type")},
        }
        print(json.dumps(structure, indent=2))
        # Exercise the real production dispatcher/parser with this live response
        # without issuing a duplicate authenticated request.
        with patch("capacity.fetchers.crusoe.fetch_capacities", return_value=payload):
            records = _fetch_provider("crusoe")
        if not records or any(r.data_source != "official_api" or
                              r.metric_type != "provider_quantity" for r in records):
            raise ValueError("No authenticated capacity records")
        summary = {
            "capacity_records": len(records),
            "gpu_models": sorted({r.gpu_model for r in records}),
            "locations": sorted({r.region for r in records}),
            "capacity_states": dict(Counter(r.state for r in records)),
            "quantity_basis": "provider-reported per configuration; not summed across shapes",
            "quota_and_cluster_availability": "not established",
            "writes_to_provider": 0,
        }
        rendered = json.dumps(summary, indent=2)
        print(rendered)
        summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_file:
            with open(summary_file, "a") as handle:
                handle.write("Crusoe capacity check passed. No resources created or changed.\n\n```json\n"
                             + rendered + "\n```\n")
        return 0
    except Exception as exc:
        # Exception text, headers and bodies can contain sensitive values.
        print(f"Crusoe capacity check failed ({type(exc).__name__}); check secrets and API access.",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
