"""Exact Lambda VM observations survive persistence and fetch failures safely."""
import csv
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest.mock import patch

from capacity import main, store
from capacity.diff import compute_diff
from capacity.schema import AvailabilityRecord
from scripts.check_lambda_capacity import validate


def observation(**changes):
    return replace(AvailabilityRecord(
        provider="lambda", gpu_model="H100", region="global", consumption_type="on_demand",
        state="available", metric_type="launchable_regions", metric_value=1,
        instance_type="gpu_8x_h100_sxm5", data_source="official_api",
        fetched_at="2026-09-17T08:00:00+00:00", product_scope="on_demand_instance",
        gpu_count=8, parser_version="lambda-instance-capacity-2.0"), **changes)


def regional(**changes):
    return observation(region="us-west-1", metric_type="instance_launchability", **changes)


class LambdaPipelineTests(unittest.TestCase):
    def test_json_and_csv_preserve_exact_shape_scope_and_observation_time(self):
        row = observation()
        self.assertEqual(AvailabilityRecord.from_dict(json.loads(json.dumps(row.to_dict()))), row)
        with tempfile.TemporaryDirectory() as directory:
            history = Path(directory) / "history.csv"
            history.write_text("date,provider,gpu_model,region,consumption_type,state,metric_type,metric_value\n"
                               "2026-09-16,lambda,H100,global,on_demand,available,regions_with_capacity,3\n")
            with patch.object(store, "HISTORY_FILE", history):
                store.append_history([row], date(2026, 9, 17))
            with history.open() as handle:
                rows = list(csv.DictReader(handle))
        self.assertEqual(rows[0]["product_scope"], "")
        for field in ("product_scope", "gpu_count", "instance_type", "fetched_at", "parser_version"):
            self.assertEqual(rows[1][field], str(getattr(row, field)))

    def run_fetch(self, fresh, cached):
        with patch.object(main, "_fetch_provider", return_value=fresh), \
                patch.object(store, "get_cached_records", return_value=(cached, 17)) as cache_read, \
                patch.object(store, "update_peer_cache") as cache_write, \
                patch.object(store, "load_last_snapshot", return_value=cached), \
                patch.object(main, "write_artifacts") as artifacts, \
                patch("capacity.config.PENDING_ACTIVATION", set()), self.assertLogs(level="INFO"):
            manifest = main.run(providers=["lambda"], test=True)
        return manifest, artifacts.call_args.args[0], cache_read, cache_write

    def test_failed_fetch_rejects_aggregated_and_unverified_cache(self):
        bad = [observation(product_scope=""), observation(gpu_count=None),
               observation(metric_type="regions_with_capacity"), observation(data_source="aggregator")]
        manifest, rows, _, cache_write = self.run_fetch([], bad)
        self.assertEqual(rows, [])
        self.assertEqual(manifest["status"], "failed")
        cache_write.assert_not_called()

    def test_failed_fetch_retains_valid_cache_and_original_timestamp(self):
        good = observation()
        manifest, rows, _, cache_write = self.run_fetch([], [good, observation(product_scope="")])
        self.assertEqual(rows, [good])
        self.assertEqual(rows[0].fetched_at, good.fetched_at)
        self.assertEqual(manifest["provider_status"]["lambda"]["cache_age_hours"], 17)
        self.assertEqual(manifest["stale_providers"], ["lambda"])
        cache_write.assert_not_called()

    def test_fresh_unknown_replaces_old_available_without_cache_fallback(self):
        unknown = observation(state="unknown", metric_value=None)
        manifest, rows, cache_read, cache_write = self.run_fetch([unknown], [observation(), regional()])
        self.assertEqual(rows, [unknown])
        self.assertEqual(manifest["provider_status"]["lambda"]["status"], "live")
        cache_read.assert_not_called()
        cache_write.assert_called_once_with("lambda", [unknown])

    def test_missing_or_unknown_sku_does_not_report_stock_disappearance(self):
        old = [observation(), regional()]
        self.assertEqual(compute_diff([], old), [])
        self.assertEqual(compute_diff([observation(state="unknown", metric_value=None)], old), [])

    def test_legacy_migration_never_reports_global_recovery_or_removal(self):
        legacy = observation(instance_type="", gpu_count=None, product_scope="",
                             metric_type="regions_with_capacity", state="sold_out", metric_value=0)
        changes = compute_diff([observation(), regional()], [legacy])
        self.assertEqual({change.change_type for change in changes}, {"added"})
        self.assertTrue(all(change.instance_type == observation().instance_type for change in changes))

    def test_explicit_empty_list_changes_only_exact_sku(self):
        empty = observation(state="sold_out", metric_value=0)
        changes = compute_diff([empty], [observation(), regional()])
        self.assertEqual({change.change_type for change in changes}, {"state_change", "removed"})
        transition = next(change for change in changes if change.change_type == "state_change")
        self.assertEqual((transition.old_state, transition.new_state), ("available", "sold_out"))
        self.assertEqual(transition.instance_type, empty.instance_type)

    def test_counts_and_metric_changes_are_not_same_product_or_stock_volume(self):
        self.assertEqual(compute_diff([observation(gpu_count=1)], [observation(), regional()]), [])
        self.assertEqual(compute_diff([observation(metric_value=4)], [observation(metric_value=1)]), [])
        self.assertEqual(compute_diff([regional(metric_value=8)], [regional()]), [])

    def test_smoke_reconciles_exact_skus_and_rejects_unknown_or_mismatched_evidence(self):
        summary = validate([observation(), regional()])
        self.assertEqual((summary["exact_skus"], summary["region_records"]), (1, 1))
        self.assertEqual(summary["cluster_stock_contribution"], 0)
        for rows in ([], [observation(state="unknown", metric_value=None)],
                     [observation(), regional(gpu_count=1)],
                     [observation(), observation()], [regional()],
                     [observation(metric_value=2), regional()]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                validate(rows)


if __name__ == "__main__":
    unittest.main()
