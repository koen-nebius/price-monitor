"""Capacity history retains shape identity and migrates old rows honestly."""
import csv
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch
from capacity import store
from capacity.schema import AvailabilityRecord


class CapacityHistoryTests(unittest.TestCase):
    def test_migrates_legacy_and_preserves_exact_rows_on_rerun(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "history.csv"
            target.write_text("date,provider,gpu_model,region,consumption_type,state,metric_type,metric_value\n"
                              "2026-09-16,crusoe,H100,global,on_demand,available,listed_offering,3.0\n")
            records = [AvailabilityRecord(
                "crusoe", "H100", "us-east1-a", "on_demand", "available",
                "provider_quantity", 8, instance_type=sku, data_source="official_api",
                fetched_at="2026-09-17T10:00:00+00:00",
                detail="quantity units not aggregated",
            ) for sku in ("h100.1x", "h100.8x")]
            with patch.object(store, "HISTORY_FILE", target):
                store.append_history(records, date(2026, 9, 17))
                store.append_history(records, date(2026, 9, 17))
            with target.open() as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[0]["metric_value"], "3.0")
            self.assertEqual(rows[0]["instance_type"], "")
            self.assertEqual(rows[0]["data_source"], "")
            self.assertEqual({r["instance_type"] for r in rows[1:]}, {"h100.1x", "h100.8x"})
            self.assertTrue(all(r["data_source"] == "official_api" for r in rows[1:]))
            self.assertTrue(all(r["detail"] == "quantity units not aggregated" for r in rows[1:]))


if __name__ == "__main__":
    unittest.main()
