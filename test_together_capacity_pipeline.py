"""Together product scope survives cache, history and change detection."""
import csv
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest.mock import patch

from capacity import main, store
from capacity.diff import compute_diff
from capacity.schema import AvailabilityRecord


def inference(**changes):
    return replace(AvailabilityRecord(
        provider="together", gpu_model="H100", region="us-central-8",
        consumption_type="on_demand", state="limited", metric_type="inference_replicas",
        metric_value=2, instance_type="h100-8", data_source="official_api",
        fetched_at="2026-09-17T12:00:00+00:00", product_scope="dedicated_inference",
        gpu_count=8, quantity_relation="RELATION_EQ"), **changes)


class TogetherPersistenceTests(unittest.TestCase):
    def test_inference_context_survives_json_and_legacy_csv_migration(self):
        row = inference()
        self.assertEqual(AvailabilityRecord.from_dict(row.to_dict()), row)
        with tempfile.TemporaryDirectory() as directory:
            history = Path(directory) / "history.csv"
            history.write_text("date,provider,gpu_model,region,consumption_type,state,metric_type,metric_value\n"
                               "2026-09-16,together,H100,global,on_demand,available,regions_with_capacity,3\n")
            with patch.object(store, "HISTORY_FILE", history):
                store.append_history([row], date(2026, 9, 17))
            with history.open() as handle:
                rows = list(csv.DictReader(handle))
        self.assertEqual(rows[0]["product_scope"], "")
        self.assertEqual(rows[1]["product_scope"], "dedicated_inference")
        self.assertEqual(rows[1]["gpu_count"], "8")
        self.assertEqual(rows[1]["quantity_relation"], "RELATION_EQ")

    def run_failed_fetch(self, cached):
        with patch.object(main, "_fetch_provider", return_value=[]), \
                patch.object(store, "get_cached_records", return_value=(cached, 17)), \
                patch.object(store, "load_last_snapshot", return_value=[]), \
                patch.object(main, "write_artifacts") as artifacts, \
                patch("capacity.config.PENDING_ACTIVATION", set()), self.assertLogs(level="INFO"):
            manifest = main.run(providers=["together"], test=True)
        return manifest, artifacts.call_args.args[0]

    def test_api_failure_rejects_legacy_aggregated_cache(self):
        old = inference(product_scope="", metric_type="regions_with_capacity", region="global")
        manifest, rows = self.run_failed_fetch([old])
        self.assertEqual(manifest["provider_status"]["together"]["status"], "failed")
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(rows, [])

    def test_api_failure_only_uses_exact_inference_cache_and_original_timestamp(self):
        good = inference()
        old = inference(product_scope="", metric_type="stock_level")
        manifest, rows = self.run_failed_fetch([old, good])
        self.assertEqual(rows, [good])
        self.assertEqual(manifest["provider_status"]["together"]["cache_age_hours"], 17)
        self.assertEqual(manifest["stale_providers"], ["together"])

    def test_legacy_rows_do_not_create_removal_or_market_stock_changes(self):
        old = inference(product_scope="", metric_type="regions_with_capacity", region="global")
        changes = compute_diff([inference()], [old])
        self.assertEqual([c.change_type for c in changes], ["added"])
        self.assertNotEqual(changes[0].region, "global")

    def test_only_exact_comparable_replica_quantities_get_percentage_changes(self):
        old, new = inference(metric_value=10, state="available"), inference(metric_value=20, state="available")
        self.assertEqual(compute_diff([new], [old])[0].change_type, "metric_move")
        lower = replace(old, quantity_relation="RELATION_GTE")
        self.assertEqual(compute_diff([new], [lower]), [])
        self.assertEqual(compute_diff([replace(new, quantity_relation="RELATION_GTE")], [lower]), [])
        self.assertEqual(compute_diff([replace(new, gpu_count=1)], [old]), [])


if __name__ == "__main__":
    unittest.main()
