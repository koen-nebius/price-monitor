"""Coverage distinguishes supplier/source identity, missingness and dated evidence."""
from dataclasses import replace
import unittest
from coverage_report import build_price_coverage, build_capacity_coverage, render_coverage
from schema import PriceRecord
from capacity.schema import AvailabilityRecord
from comparability import enrich_comparability
from confluence_storage import to_storage, validate_xml

AS_OF = "2026-09-18T12:00:00Z"


def offer(**kwargs):
    values = dict(provider="coreweave", gpu_model="H100", gpu_count=8, instance_type="h100-8",
                  region="europe", consumption_type="on_demand", price_per_hour_usd=48,
                  price_per_gpu_hour_usd=6, fetched_at=AS_OF, data_source="web_scrape")
    values.update(kwargs)
    return PriceRecord(**values)


class CoverageTests(unittest.TestCase):
    def test_duplicate_feeds_are_one_competitor_and_stale_quote_is_visible(self):
        rows = [offer(), offer(provider="cp_coreweave", source_feed="computeprices",
                    source_observed_at="2026-09-13T12:00:00Z", data_source="aggregator"),
                offer(provider="nebius")]
        before = [r.to_dict() for r in rows]
        report = build_price_coverage(rows, AS_OF)
        self.assertEqual(report["tracked_competitors"], ["coreweave"])
        self.assertEqual(report["observed_cells"], 1)
        self.assertEqual(report["cells"][0]["statuses"], {"fresh_price": 1, "reference_only": 1})
        self.assertEqual(before, [r.to_dict() for r in rows])

    def test_no_record_is_neither_sold_out_nor_a_fabricated_region(self):
        report = build_price_coverage([], AS_OF, {"lambda": {"status": "failed", "reason": "access denied"}})
        self.assertEqual(report["cells"], [])
        self.assertEqual(len(report["unobserved"]), 8)
        self.assertNotIn("region", report["unobserved"][0])
        self.assertIn("access denied", render_coverage(report))

    def test_unknown_source_date_never_uses_new_retrieval_time(self):
        report = build_price_coverage([offer(source_feed="computeprices")], AS_OF)
        cell = report["cells"][0]
        self.assertEqual(cell["statuses"], {"reference_only": 1})
        self.assertEqual(cell["latest_observed_at"], "")
        self.assertIsNone(cell["latest_age_hours"])

    def test_unavailable_and_unqualified_prices_do_not_become_fresh(self):
        rows = [offer(available=False), offer(region="us", price_basis="account_catalog"),
                offer(region="unknown", comparison_eligible=False, correction_reason="host variant unknown")]
        report = build_price_coverage(rows, AS_OF)
        self.assertTrue(all("fresh_price" not in c["statuses"] for c in report["cells"]))
        self.assertIn("host variant unknown", render_coverage(report))

    def test_capacity_retains_evidence_type_and_partial_health(self):
        row = AvailabilityRecord("coreweave", "B300", "us", "on_demand", "available",
                                 "listed_offering", fetched_at=AS_OF)
        report = build_capacity_coverage([row], AS_OF, {"coreweave": {"status": "partial", "reason": "page 2 failed"}})
        self.assertEqual(report["cells"][0]["statuses"], {"source_problem": 1})
        self.assertEqual(report["cells"][0]["evidence_types"], ["footprint: listed_offering"])
        html = render_coverage(report)
        self.assertIn("page 2 failed", html)
        validate_xml(to_storage(html))

    def test_no_implicit_eight_gpu_node_or_fabric_for_repaired_collectors(self):
        row = offer(provider="hyperstack", gpu_count=1, node_gpus=1, form_factor="SXM",
                    interconnect="unknown", parser_version="direct-offers-1")
        missing = replace(row, form_factor="unknown")
        enrich_comparability([row, missing])
        self.assertEqual(row.node_gpus, 1)
        self.assertEqual(missing.form_factor, "unknown")
        self.assertEqual(missing.interconnect, "unknown")

    def test_capacity_health_follows_collector_not_underlying_supplier(self):
        row = AvailabilityRecord("voltage_park", "H100", "global", "on_demand", "available",
                                 "regions_with_capacity", fetched_at=AS_OF, data_source="aggregator")
        docs = AvailabilityRecord("gcp", "H100", "us", "on_demand", "available",
                                  "listed_offering", fetched_at=AS_OF, data_source="web_scrape")
        health = {"voltage_park": {"status": "failed"}, "shadeform": {"status": "live"},
                  "gcp_zones": {"status": "cached"}}
        cells = {c["provider"]: c for c in build_capacity_coverage([row, docs], AS_OF, health)["cells"]}
        self.assertEqual(cells["voltage_park"]["statuses"], {"observed_signal": 1})
        self.assertEqual(cells["voltage_park"]["feeds"], ["shadeform"])
        self.assertEqual(cells["voltage_park"]["evidence_types"], ["aggregator: regions_with_capacity"])
        self.assertEqual(cells["gcp"]["statuses"], {"source_problem": 1})

    def test_reservation_collector_does_not_become_a_second_competitor(self):
        rows = [offer(provider="vast_reserved", consumption_type="reserved_short"), offer(provider="cp_vast")]
        report = build_price_coverage(rows, AS_OF, {"vast_reserved": {"status": "live"}})
        self.assertEqual(report["tracked_competitors"], ["vast"])


if __name__ == "__main__":
    unittest.main()
