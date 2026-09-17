"""Read-only check of the daily dispatcher and Vultr's public GPU catalogue."""
import json
import math
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from main import _fetch_provider


def main():
    records = _fetch_provider("vultr")
    if not records:
        print("Vultr check failed: no supported catalogue records", file=sys.stderr)
        return 1
    for record in records:
        if (record.data_source != "official_api" or record.gpu_count <= 0
                or not record.price_basis.startswith("public_catalog")
                or not math.isclose(record.price_per_hour_usd / record.gpu_count,
                                    record.price_per_gpu_hour_usd)):
            print("Vultr check failed: invalid provenance, unit or qualification", file=sys.stderr)
            return 1
    print(json.dumps({
        "fetched_at": records[0].fetched_at,
        "catalogue_records": len(records),
        "gpu_models": sorted({r.gpu_model for r in records}),
        "price_bases": dict(Counter(r.price_basis for r in records)),
        "note": "Catalogue metadata does not establish live stock or multi-node availability.",
        "writes_to_provider": 0,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
