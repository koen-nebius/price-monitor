"""Read-only end-to-end inventory check; no publishing or persisted raw response."""
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from massedcompute_api import fetch_inventory
from fetchers.massedcompute import parse as parse_prices
from capacity.fetchers.massedcompute import parse as parse_capacity


def main():
    try:
        payload = fetch_inventory()
        now = datetime.now(timezone.utc).isoformat()
        prices = parse_prices(payload, now)
        capacity = parse_capacity(payload, now)
        if not prices or not capacity:
            raise ValueError("No supported inventory")
        summary = {
            "fetched_at": now,
            "inventory_configurations": len(payload["gpu_inventory"]),
            "price_records": len(prices),
            "capacity_records": len(capacity),
            "price_basis": "account_catalog",
            "gpu_models": sorted({r.gpu_model for r in prices}),
            "capacity_states": dict(Counter(r.state for r in capacity)),
            "writes_to_provider": 0,
        }
        rendered = json.dumps(summary, indent=2)
        print(rendered)
        summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_file:
            with open(summary_file, "a") as handle:
                handle.write("Massed inventory check passed. No resources created or changed.\n\n```json\n"
                             + rendered + "\n```\n")
        return 0
    except Exception as exc:
        # Provider responses and exception text may include sensitive values.
        print(f"Massed inventory check failed ({type(exc).__name__}); check secret and API access.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
