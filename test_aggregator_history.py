"""Offer provenance and cluster-reference coverage survive daily summaries."""
import csv
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest.mock import patch

import forward_curve as fc
import history
from schema import PriceRecord


DAY = date(2026, 9, 18)


def offer(provider="coreweave", price=3.0, **changes):
    row = PriceRecord(
        provider, "H100", 8, "hgx-h100-8", "us-east", "on_demand", price * 8, price,
        fetched_at="2026-09-18T00:00:00+00:00", source_url="https://example.test/offer",
        data_source="aggregator", source_feed="computeprices",
        source_observed_at="2026-09-18T00:00:00+00:00", offer_id="hgx-8-east",
        commitment_months=0, available=True, gpu_variant="H100 SXM 80GB",
        offer_variant="Dedicated", form_factor="SXM", interconnect="InfiniBand",
        parser_version="aggregator-offers-1", price_basis="public_catalog")
    return replace(row, **changes)


def write_history(path, rows):
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=history.COLUMNS)
        writer.writeheader()
        writer.writerows(history._history_row(row, DAY) for row in rows)


class OfferHistory(unittest.TestCase):
    def test_append_keeps_selected_offer_metadata_and_old_header_rows(self):
        expensive = offer(price=4.0)
        chosen = offer(price=3.0, source_feed="shadeform", offer_id="shade-8-east",
                       commitment_months=1, available=None, offer_variant="Secure")
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "history.csv"
            target.write_text("snapshot_date,provider,gpu_model,consumption_type,price_per_gpu_hour_usd\n"
                              "2026-09-17,lambda,H100,on_demand,4.0\n")
            with patch.object(history, "HISTORY_CSV", target):
                history.append_records([expensive, chosen], DAY)
                first = target.read_bytes()
                history.append_records([expensive, chosen], DAY)
                self.assertEqual(target.read_bytes(), first)
            rows = history.load_comparison_history(target)
        old, selected = rows
        self.assertEqual(old["provider"], "lambda")
        self.assertEqual(old["source_feed"], "")
        self.assertEqual(selected["price_per_gpu_hour_usd"], "3.0")
        for field in ("source_feed", "source_observed_at", "offer_id", "parser_version",
                      "fetched_at", "source_url", "price_basis", "gpu_variant", "offer_variant"):
            self.assertEqual(selected[field], getattr(chosen, field))
        self.assertEqual(selected["commitment_months"], "1")
        self.assertEqual(selected["available"], "")

    def test_rebuild_uses_same_provenance_and_excludes_nonpublic_candidates(self):
        selected = offer()
        excluded = [offer(price=0.5, available=False),
                    offer(price=0.6, source_observed_at=""),
                    offer(price=0.7, price_basis="account_catalog"),
                    offer(price=0.8, price_basis="public_catalog_ondemand_disabled"),
                    offer(price=0.9, source_observed_at="2026-08-01T00:00:00+00:00"),
                    offer(price=1.0, source_observed_at="", parser_version="2.1")]
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "history.csv"
            with patch.object(history, "HISTORY_CSV", target), \
                    patch.object(history, "STORE_DIR", Path(folder)), \
                    patch.object(history, "list_snapshot_dates", return_value=[DAY]), \
                    patch.object(history, "load_snapshot", return_value=excluded + [selected]):
                history.rebuild()
            rows = history.load_comparison_history(target)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["price_per_gpu_hour_usd"], "3.0")
        self.assertEqual(rows[0]["available"], "True")
        self.assertEqual(rows[0]["offer_id"], selected.offer_id)

    def test_append_rechecks_source_time_and_clears_previous_same_day_minimum(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "history.csv"
            write_history(target, [offer(price=0.5)])
            stale = offer(price=0.5, source_observed_at="2026-08-01T00:00:00+00:00")
            with patch.object(history, "HISTORY_CSV", target):
                history.append_records([stale, offer(source_observed_at="")], DAY)
            self.assertEqual(history.load_comparison_history(target), [])

    def test_rebuild_freshness_uses_snapshot_day_and_preserves_raw_price_basis(self):
        historical = date(2026, 8, 3)
        row = offer(fetched_at="2026-08-03T01:00:00+00:00",
                    source_observed_at="2026-08-03T00:00:00+00:00")
        azure = PriceRecord("azure", "RTX", 1, "Standard_NC36ds_xl_RTXPRO6000BSE_v6",
                            "eastus", "on_demand", 1.465, 1.465,
                            fetched_at="2026-08-03T00:00:00+00:00", data_source="official_api")
        with patch.object(history, "load_snapshot", return_value=[row, azure]):
            rows = history._rows_for_date(historical)
        self.assertEqual(len(rows), 2)
        raw = next(r for r in rows if r["provider"] == "azure")
        self.assertEqual(raw["gpu_count"], 1)
        self.assertEqual(raw["price_per_gpu_hour_usd"], 1.465)
        self.assertEqual({r["snapshot_date"] for r in rows}, {"2026-08-03"})

    def test_legacy_rows_without_new_metadata_remain_supported(self):
        legacy = offer(source_feed="", source_observed_at="", parser_version="2.1", available=None)
        self.assertEqual(list(history._cheapest_per_combo([legacy]).values()), [legacy])


class FullOfferForwardReference(unittest.TestCase):
    def reference(self, folder, records, *, history_rows=()):
        path = Path(folder) / "history.csv"
        write_history(path, history_rows)
        (Path(folder) / "last_snapshot.json").write_text(json.dumps([r.to_dict() for r in records]))
        return fc.load_on_demand(path, realised=Path(folder) / "absent.csv", as_of=DAY)["H100"]

    def test_cluster_offer_survives_cheaper_entry_and_provider_is_counted_once(self):
        entry = offer(price=1.0, gpu_count=1, node_gpus=1, offer_id="entry", instance_type="h100-1")
        cluster = offer(price=3.0)
        second_feed = offer(price=2.5, source_feed="shadeform", offer_id="shade-hgx")
        lambda_row = offer(provider="lambda", price=4.0)
        with tempfile.TemporaryDirectory() as folder:
            result = self.reference(folder, [entry, cluster, second_feed, lambda_row], history_rows=[entry, lambda_row])
        self.assertEqual(result["peer_od_n"], 2)
        self.assertEqual(result["peer_od_median"], 3.25)
        self.assertEqual(result["peer_od_min"], 2.5)
        selected = next(row for row in result["peer_list"] if row["provider"] == "coreweave")
        self.assertEqual(selected["source_feed"], "shadeform")
        self.assertEqual(selected["offer_id"], "shade-hgx")
        self.assertEqual(selected["source_observed_at"], second_feed.source_observed_at)

    def test_upstream_staleness_unknown_dates_and_unavailable_do_not_return_via_history(self):
        candidates = [offer(price=0.5, source_observed_at="2026-09-10T00:00:00+00:00"),
                      offer(price=0.6, source_observed_at=""),
                      offer(price=0.7, available=False),
                      offer(price=0.8, price_basis="account_catalog")]
        with tempfile.TemporaryDirectory() as folder:
            result = self.reference(folder, candidates, history_rows=[offer(price=0.5)])
        self.assertNotIn("peer_od_median", result)

    def test_aggregator_slice_on_cluster_host_stays_evidence_not_cluster_benchmark(self):
        per_gpu = offer(price=3.0, gpu_count=1, node_gpus=8)
        unknown = offer(provider="lambda", price=1.0, form_factor="unknown")
        pcie = offer(provider="lambda", price=1.5, form_factor="PCIe")
        with tempfile.TemporaryDirectory() as folder:
            result = self.reference(folder, [per_gpu, unknown, pcie])
        self.assertNotIn("peer_od_median", result)
        self.assertEqual(list(history._cheapest_per_combo([per_gpu]).values()), [per_gpu])

    def test_verified_direct_per_gpu_node_convention_remains_supported(self):
        direct = offer(provider="crusoe", price=3.0, gpu_count=1, node_gpus=8,
                       source_feed="", data_source="official_api", source_type="api", parser_version="2.1")
        with tempfile.TemporaryDirectory() as folder:
            result = self.reference(folder, [direct])
        self.assertEqual(result["peer_od_n"], 1)
        self.assertEqual(result["peer_od_median"], 3.0)

    def test_explicit_snapshot_supported_without_history_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "accepted.json"
            path.write_text(json.dumps([offer().to_dict()]))
            result = fc.load_on_demand(Path(folder) / "absent.csv", snapshot=path,
                                      realised=Path(folder) / "none.csv", as_of=DAY)["H100"]
        self.assertEqual(result["peer_od_median"], 3.0)

    def test_custom_history_ignores_raw_latest_and_checkout_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "history.csv"
            write_history(path, [offer(price=4.0)])
            (Path(folder) / "latest.json").write_text(json.dumps([offer(price=0.1).to_dict()]))
            with patch.object(fc, "LATEST_SNAPSHOT_JSON", Path(folder) / "latest.json"):
                result = fc.load_on_demand(path, realised=Path(folder) / "none.csv", as_of=DAY)["H100"]
        self.assertEqual(result["peer_od_median"], 4.0)

    def test_new_aggregator_history_fallback_rechecks_upstream_date_and_qualification(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "history.csv"
            write_history(path, [offer(price=0.5, source_observed_at="2026-08-01T00:00:00+00:00"),
                                 offer(price=0.6, source_observed_at=""),
                                 offer(price=0.7, available=False),
                                 offer(price=0.8, price_basis="account_catalog"),
                                 offer(price=0.9, source_observed_at="", parser_version="2.1"),
                                 offer(provider="lambda", price=4.0)])
            result = fc.load_on_demand(path, realised=Path(folder) / "none.csv", as_of=DAY)["H100"]
        self.assertEqual(result["peer_od_n"], 1)
        self.assertEqual(result["peer_od_median"], 4.0)
        self.assertEqual(result["peer_list"][0]["provider"], "lambda")

    def test_history_fallback_preserves_latest_payg_cohort_and_as_of(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "history.csv"
            with path.open("w", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=history.COLUMNS)
                writer.writeheader()
                writer.writerows([
                    history._history_row(offer(price=4.0), date(2026, 9, 17)),
                    history._history_row(offer(price=2.0, consumption_type="reserved_1yr"), DAY),
                    history._history_row(offer(price=1.0), date(2026, 9, 19)),
                ])
            result = fc.load_on_demand(path, realised=Path(folder) / "none.csv", as_of=DAY)["H100"]
        self.assertEqual(result["peer_od_median"], 4.0)
        self.assertEqual(result["list_snapshot"], "2026-09-17")


if __name__ == "__main__":
    unittest.main()
