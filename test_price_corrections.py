"""Offline regression tests for the false Sep17 pricing alerts and stale AWS feed."""
import copy
import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from schema import PriceRecord
from price_corrections import (
    AZURE_CORRECTION, TOGETHER_CORRECTION, AWS_STATIC_CORRECTION,
    correct_record, correct_snapshot, correct_history_rows, known_restatement,
)
from scripts.rebuild_corrected_history import write_derived_history


def azure(**changes):
    values = dict(provider="azure", gpu_model="RTX6000", gpu_count=1,
                  instance_type="Standard_NC36lds_xl_RTXPRO6000BSE_v6", region="eastus",
                  consumption_type="on_demand", price_per_hour_usd=1.243,
                  price_per_gpu_hour_usd=1.243, fetched_at="2026-09-16T07:00:00Z",
                  data_source="official_api")
    values.update(changes)
    return PriceRecord(**values)


def together(gpu="H200", **changes):
    prices = {"H100": 1.99, "H200": 2.99, "B200": 4.09}
    values = dict(provider="together", gpu_model=gpu, gpu_count=8,
                  instance_type=f"together-hgx-{gpu.lower()}", region="global",
                  consumption_type="on_demand", price_per_hour_usd=prices[gpu] * 8,
                  price_per_gpu_hour_usd=prices[gpu], fetched_at="2026-09-16T07:00:00Z",
                  data_source="web_scrape")
    values.update(changes)
    return PriceRecord(**values)


class PriceCorrectionTests(unittest.TestCase):
    def test_azure_normalization_rebases_old_observation_without_mutation(self):
        old = azure()
        before = old.to_dict()
        result = correct_record(old)
        self.assertEqual(old.to_dict(), before)
        self.assertEqual(result.correction_id, AZURE_CORRECTION)
        self.assertEqual(result.record.gpu_count, .25)
        self.assertEqual(result.record.price_per_hour_usd, 1.243)
        self.assertEqual(result.record.price_per_gpu_hour_usd, 4.972)
        self.assertEqual(result.record.original_price_per_gpu_hour_usd, 1.243)
        self.assertEqual(result.record.original_gpu_count, 1)
        self.assertEqual(correct_record(result.record).record, result.record)
        new = azure(gpu_count=.25, price_per_gpu_hour_usd=4.972,
                    fetched_at="2026-09-17T07:00:00Z")
        self.assertTrue(known_restatement(old, new))

    def test_real_underlying_vm_price_move_survives_denominator_correction(self):
        old = azure()
        new = azure(gpu_count=.25, price_per_hour_usd=1.5, price_per_gpu_hour_usd=6,
                    fetched_at="2026-09-17T07:00:00Z")
        self.assertIsNone(known_restatement(old, new))
        self.assertAlmostEqual(new.price_per_gpu_hour_usd / correct_record(old).record.price_per_gpu_hour_usd - 1,
                               1.5 / 1.243 - 1)

    def test_diff_reports_only_ten_percent_vm_move_during_fraction_correction(self):
        import diff
        old = azure()
        new = azure(gpu_count=.25, price_per_hour_usd=1.243 * 1.1,
                    price_per_gpu_hour_usd=4.972 * 1.1, fetched_at="2026-09-17T07:00:00Z")
        with patch.object(diff, "_recent_price_levels", return_value={}):
            changes = diff.compute_diff([old], [new])
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].change_type, "price_change")
        self.assertAlmostEqual(changes[0].delta_pct, 10)
        self.assertAlmostEqual(changes[0].old_price, 4.972)
        self.assertAlmostEqual(changes[0].new_price, 4.972 * 1.1)

    def test_diff_labels_pure_fraction_fix_restatement(self):
        import diff
        old = azure()
        new = azure(gpu_count=.25, price_per_gpu_hour_usd=4.972, fetched_at="2026-09-17T07:00:00Z")
        with patch.object(diff, "_recent_price_levels", return_value={}):
            changes = diff.compute_diff([old], [new])
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].change_type, "restatement")
        self.assertEqual(diff._group_significant_moves(changes), [])

    def test_diff_cannot_infer_move_from_unknown_historical_together_od(self):
        import diff
        old = together()
        # Any new OD price is incomparable to the incorrectly labelled old
        # preemptible rate, not just the exact 5.99 observed at the correction.
        new = together(price_per_gpu_hour_usd=6.25, price_per_hour_usd=50,
                       fetched_at="2026-09-17T07:00:00Z")
        with patch.object(diff, "_recent_price_levels", return_value={}):
            changes = diff.compute_diff([old], [new])
        self.assertEqual(changes[0].change_type, "restatement")
        self.assertEqual(diff._group_significant_moves(changes), [])

    def test_publication_correction_uses_source_observation_date_for_cached_records(self):
        from report_freshness import publication_records
        from datetime import datetime, timezone
        row = together(fetched_at="2026-09-16T23:00:00Z")
        eligible, notes = publication_records([row], datetime(2026, 9, 18, 1, tzinfo=timezone.utc))
        self.assertEqual(eligible, [])
        self.assertTrue(any("preemptible column" in note for note in notes))

    def test_aggregator_only_updates_never_lead_primary_slack(self):
        import diff
        from schema import DiffEntry
        row = together(provider="cp_hyperstack", price_per_gpu_hour_usd=8,
                       price_per_hour_usd=64, fetched_at="2026-09-18T07:00:00Z",
                       data_source="aggregator")
        change = DiffEntry(provider=row.provider, gpu_model=row.gpu_model,
                           instance_type=row.instance_type, region=row.region,
                           consumption_type=row.consumption_type, change_type="price_change",
                           old_price=2, new_price=8, delta_pct=300)
        text = diff.format_slack_summary([change], "September 18, 2026", "https://example.com", records=[row])
        self.assertEqual(diff._group_significant_moves([change]), [])
        self.assertNotIn("300.0%", text)
        self.assertNotIn("$2.00 → $8.00", text)

    def test_exact_sku_and_recorded_hourly_price_required(self):
        for changes in (
            {"instance_type": "Standard_NC36_RTXPRO6000BSE_v6"},
            {"gpu_model": "H100"}, {"provider": "cp_azure"},
            {"gpu_count": .25}, {"price_per_hour_usd": 8},
            {"fetched_at": "2026-09-18T07:00:00Z"},
            {"fetched_at": ""}, {"data_source": "aggregator"},
        ):
            with self.subTest(changes=changes):
                self.assertFalse(correct_record(azure(**changes)).correction_id)

    def test_historical_together_product_mislabel_is_excluded_not_doubled(self):
        for gpu, corrected_price in [("H100", 3.99), ("H200", 5.99), ("B200", 8.19)]:
            old = together(gpu)
            result = correct_record(old)
            self.assertFalse(result.comparison_eligible)
            self.assertEqual(result.record.price_per_gpu_hour_usd, old.price_per_gpu_hour_usd)
            self.assertEqual(result.correction_id, TOGETHER_CORRECTION)
            self.assertEqual(correct_snapshot([old]), [])
            self.assertEqual(len(correct_snapshot([old], include_excluded=True)), 1)
            new = together(gpu, fetched_at="2026-09-17T00:00:00Z",
                           price_per_gpu_hour_usd=corrected_price,
                           price_per_hour_usd=corrected_price * 8)
            self.assertTrue(known_restatement(old, new))
            self.assertIsNone(known_restatement(old, together(gpu, fetched_at="2026-09-18T00:00:00Z",
                                                            price_per_gpu_hour_usd=corrected_price + .5)))

    def test_together_legitimate_history_and_other_products_are_unchanged(self):
        for changes in (
            {"fetched_at": "2026-09-02T00:00:00Z"},
            {"fetched_at": "2026-09-18T00:00:00Z"},
            {"consumption_type": "preemptible"},
            {"instance_type": "together-dedicated-inference-h200"},
            {"price_per_gpu_hour_usd": 5.99, "price_per_hour_usd": 47.92},
            {"price_per_gpu_hour_usd": 2.8, "price_per_hour_usd": 22.4},
        ):
            self.assertTrue(correct_record(together(**changes)).comparison_eligible)

    def test_csv_rows_retain_original_values_and_date_without_inventing_snapshot_data(self):
        row = {k: str(v) for k, v in azure().to_dict().items()
               if k in {"provider", "gpu_model", "gpu_count", "instance_type", "region", "consumption_type",
                        "price_per_hour_usd", "price_per_gpu_hour_usd", "data_source"}}
        row["snapshot_date"] = "2026-09-16"
        before = copy.deepcopy(row)
        corrected = correct_history_rows([row])[0]
        self.assertEqual(row, before)
        self.assertEqual(corrected["snapshot_date"], "2026-09-16")
        self.assertEqual(corrected["price_per_gpu_hour_usd"], 4.972)
        self.assertEqual(corrected["original_price_per_gpu_hour_usd"], 1.243)
        self.assertEqual(correct_history_rows([corrected]), [corrected])

    def test_legacy_static_aws_records_are_filtered_but_live_scraped_rates_survive(self):
        old = PriceRecord(provider="aws", gpu_model="B300", gpu_count=8,
                          instance_type="p6-b300.48xlarge", region="us-east-1", consumption_type="capacity_block",
                          price_per_hour_usd=93.6, price_per_gpu_hour_usd=11.7,
                          fetched_at="2026-09-18T00:00:00Z", data_source="official_api")
        result = correct_record(old)
        self.assertFalse(result.comparison_eligible)
        self.assertEqual(result.correction_id, AWS_STATIC_CORRECTION)
        live = PriceRecord.from_dict({**old.to_dict(), "instance_type": "capacity-block-p6-b300.48xlarge",
                                      "consumption_type": "reserved_short", "data_source": "web_scrape"})
        self.assertEqual(correct_snapshot([old, live]), [live])

    def test_aws_fetch_no_longer_appends_static_capacity_prices(self):
        from fetchers import aws
        with patch.object(aws, "_fetch_region", return_value=[]), patch.object(aws, "_fetch_spot", return_value=[]):
            self.assertEqual(aws.fetch(["us-east-1"]), [])

    def test_live_capacity_block_scrape_keeps_actual_fetch_time_and_provenance(self):
        from fetchers import aws_capacity_blocks as cb
        table = json.dumps(json.dumps([{"region": "US East", "gpu": "8 x B300", "rate": "$112.32 ($14.04 USD)"}]))[1:-1]
        groups = json.dumps(json.dumps([{"label": "p6-b300.48xlarge"}]))[1:-1]
        html = f'"itemTableData":"{table}","dark":"","id":"x","itemHeading":"","itemTableRowGroups":"{groups}","itemRegionProperty"'
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return html.encode()
        with patch.object(cb.urllib.request, "urlopen", return_value=Response()):
            rows = cb.fetch()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].data_source, "web_scrape")
        self.assertEqual(rows[0].source_type, "provider_page")
        self.assertEqual(rows[0].price_per_gpu_hour_usd, 14.04)
        self.assertEqual(rows[0].price_basis, "published_capacity_block_rate")
        self.assertTrue(rows[0].fetched_at)
        self.assertEqual(rows[0].region, "US East")

    def test_derived_export_cannot_overwrite_raw_history_and_retains_exclusions(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder) / "raw.csv", Path(folder) / "corrected.csv"
            data = {**together().to_dict(), "snapshot_date": "2026-09-16"}
            with source.open("w", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(data)); writer.writeheader(); writer.writerow(data)
            before = source.read_bytes()
            with self.assertRaises(ValueError): write_derived_history(source, source)
            result = write_derived_history(source, target)
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(result["excluded_rows"], 1)
            with target.open(newline="") as stream: rows = list(csv.DictReader(stream))
            self.assertEqual(rows[0]["comparison_eligible"], "False")
            self.assertEqual(correct_history_rows(rows), [])

    def test_history_reader_filters_analytical_view_and_preserves_source_bytes(self):
        from history import load_comparison_history
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "raw.csv"
            rows = [{**record.to_dict(), "snapshot_date": "2026-09-16"}
                    for record in [azure(), together()]]
            with source.open("w", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
                writer.writeheader(); writer.writerows(rows)
            original = source.read_bytes()
            got = load_comparison_history(source)
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0]["price_per_gpu_hour_usd"], 4.972)
            self.assertEqual(len(load_comparison_history(source, include_excluded=True)), 2)
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(load_comparison_history(Path(folder) / "absent.csv"), [])


if __name__ == "__main__":
    unittest.main()
