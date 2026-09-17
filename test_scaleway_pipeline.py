"""Scaleway cache migration, persistence and diff boundaries without network."""
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
from test_scaleway_scope import legacy, observation


class ScalewayPipelineTests(unittest.TestCase):
    def run_fetch(self, fresh, cached):
        with patch.object(main, "_fetch_provider", return_value=fresh), \
                patch.object(store, "get_cached_records", return_value=(cached, 17)) as read, \
                patch.object(store, "update_peer_cache") as write, \
                patch.object(store, "load_last_snapshot", return_value=cached), \
                patch.object(main, "write_artifacts") as artifacts, self.assertLogs(level="INFO"):
            manifest = main.run(providers=["scaleway"], test=True)
        return manifest, artifacts.call_args.args[0], artifacts.call_args.args[1], read, write

    def test_failed_fetch_filters_legacy_and_unknown_shape_cache(self):
        bad = [legacy(), legacy("aggregator"), observation(gpu_count=None), observation(product_scope="")]
        manifest, rows, changes, _, write = self.run_fetch([], bad)
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(rows, [])
        self.assertEqual(changes, [])
        write.assert_not_called()

    def test_cache_keeps_only_exact_records_and_original_time(self):
        good = observation()
        manifest, rows, _, _, write = self.run_fetch([], [good, legacy()])
        self.assertEqual(rows, [good])
        self.assertEqual(rows[0].fetched_at, good.fetched_at)
        self.assertEqual(manifest["provider_status"]["scaleway"]["cache_age_hours"], 17)
        self.assertEqual(manifest["stale_providers"], ["scaleway"])
        write.assert_not_called()

    def test_live_unknown_is_not_replaced_by_old_available_or_treated_as_zero(self):
        unknown = observation(state="unknown")
        manifest, rows, changes, read, write = self.run_fetch([unknown], [observation(state="available")])
        self.assertEqual(rows, [unknown])
        self.assertIsNone(rows[0].metric_value)
        self.assertEqual(changes, [])
        self.assertEqual(manifest["provider_status"]["scaleway"]["status"], "live")
        read.assert_not_called()
        write.assert_called_once_with("scaleway", [unknown])

    def test_legacy_migration_and_missing_sku_or_zone_never_report_stock_removal(self):
        good = observation()
        self.assertEqual(compute_diff([], [good]), [])
        self.assertEqual(compute_diff([], [legacy()]), [])
        changes = compute_diff([good], [legacy(state="sold_out")])
        self.assertEqual([c.change_type for c in changes], ["added"])
        other = observation(zone="fr-par-3", state="sold_out")
        changes = compute_diff([good], [good, other])
        self.assertEqual(changes, [])

    def test_changed_count_or_metric_semantics_are_not_stock_transitions(self):
        old = observation(state="available")
        self.assertEqual(compute_diff([replace(old, gpu_count=2, state="sold_out")], [old]), [])
        self.assertEqual(compute_diff([replace(old, metric_value=5)], [replace(old, metric_value=1)]), [])

    def test_json_csv_preserve_exact_scope_count_time_and_raw_enum(self):
        row = observation()
        self.assertEqual(AvailabilityRecord.from_dict(json.loads(json.dumps(row.to_dict()))), row)
        with tempfile.TemporaryDirectory() as directory:
            history = Path(directory) / "history.csv"
            with patch.object(store, "HISTORY_FILE", history):
                store.append_history([row], date(2026, 9, 17))
            with history.open() as handle:
                saved = list(csv.DictReader(handle))[0]
        for field in ("instance_type", "gpu_count", "region", "product_scope", "metric_type", "fetched_at", "detail"):
            self.assertEqual(saved[field], str(getattr(row, field)))


if __name__ == "__main__":
    unittest.main()
