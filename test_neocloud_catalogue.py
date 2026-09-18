"""Catalogue evidence regressions, using reduced official 18-Sep-2026 fixtures.

The fixtures preserve the actual CoreWeave rental-row semantics and capacity
comparison table. Tests are offline and never treat catalogue terms as stock.
"""
import copy
import io
import unittest
from unittest.mock import patch
from xml.etree import ElementTree

from fetchers import coreweave, coreweave_plans
from offer_catalogue import normalize_offers, build_catalogue_report, render_catalogue


NOW = "2026-09-18T16:00:00+00:00"
PLAN_TABLE = '''<table><thead><tr><th></th>
<th>Flex Reservations<br>Variable usage curves</th><th>Reservations<br>Steady workloads</th>
<th>Spot<br>Ad-hoc interruptible workloads</th><th>On-demand<br>Ad-hoc workloads</th></tr></thead><tbody>
<tr><td>Capacity</td><td>Fully guaranteed</td><td>Fully guaranteed</td><td>Best effort</td><td>Best effort</td></tr>
<tr><td>Interruptible</td><td>No</td><td>No</td><td>Yes (7 mins notice)</td><td>No</td></tr>
<tr><td>Pay-as-you-go</td><td>Usage only (fixed holding rate)</td><td>No</td><td>Yes</td><td>Yes</td></tr>
<tr><td>Commitment</td><td>Fixed-term</td><td>Fixed-term</td><td>None</td><td>None</td></tr>
<tr><td>Example workloads</td><td>Daily batch inference</td><td>Always-on API inference service</td>
<td>Data preprocessing</td><td>Fine-tuning experiments</td></tr></tbody></table>'''


def rental_row(sku="nvidia-gb300-nvl72", title="NVIDIA GB300 NVL72", count="4^1", spot="N/A"):
    # Current responsive pricing row has an empty OD numeric span and a desktop
    # Contact sales cell. Spot N/A is explicitly unavailable as a published tariff.
    return f'''<div class="table-row-v2"><div><h3 data-product="{sku}">{title}</h3>
    <div>Contact sales</div><h3 data-product="{sku}">{title}</h3>
    <span>On-Demand Price: / Hour</span><span>Spot Price: {spot} / Hour</span>
    <span>Inference Single GPU Price: $0.10 / Hour</span><div>Contact sales</div>
    <div>{count} GPU Count</div><div>144 vCPUs</div><div>960 System RAM</div></div></div>'''


def reference(**overrides):
    row = dict(provider="example", product_id="published-plan", gpu_model="H100",
               price_status="quote_required", source_url="https://example.com/pricing",
               observed_at=NOW, retrieved_at=NOW)
    row.update(overrides)
    return row


class CoreWeaveCatalogueEvidence(unittest.TestCase):
    def test_na_and_eu_quote_only_offers_keep_region_count_and_rental_type(self):
        block = rental_row()
        block += rental_row("nvidia-b300", "NVIDIA HGX B300", "8", "$35.84")
        block += rental_row("nvidia-rtx-pro-6000-blackwell-server-edition-standard-memory",
                            "NVIDIA RTX PRO 6000 Blackwell Server Edition (Standard Memory)", "8", "$9.56")
        raw = '<h5>REGION: NORTH AMERICA</h5>' + block + '<h5>REGION: EUROPE</h5>' + block
        offers = normalize_offers(coreweave._parse_catalogue(raw, NOW))
        self.assertEqual(len(offers), 6)
        self.assertEqual(len({row["offer_id"] for row in offers}), 6)
        self.assertEqual({row["region"] for row in offers}, {"NORTH AMERICA", "EUROPE"})
        self.assertEqual({row["purchase_type"] for row in offers}, {"on_demand"})
        self.assertEqual({row["gpu_count"] for row in offers if row["gpu_model"] == "GB300"}, {4})
        for row in offers:
            self.assertFalse(row["comparison_eligible"])
            self.assertEqual(row["availability"], "not_established")
            self.assertEqual(row["price_status"], "quote_required")
            self.assertNotIn("price_per_gpu_hour_usd", row)
        numeric = coreweave._parse_html(raw, NOW)
        self.assertEqual(len(numeric), 4)
        self.assertEqual({r.consumption_type for r in numeric}, {"spot"})
        self.assertNotIn("GB300", {r.gpu_model for r in numeric})

    def test_explicit_na_is_neither_a_quote_nor_a_zero_price_and_inference_is_separate(self):
        raw = rental_row().replace("On-Demand Price: / Hour", "On-Demand Price: N/A / Hour")
        self.assertEqual(coreweave._parse_catalogue(raw, NOW), [])
        self.assertEqual(coreweave._parse_html(raw, NOW), [])

    def test_unknown_quantity_and_region_stay_unknown_despite_nvl72_name(self):
        offers = normalize_offers(coreweave._parse_catalogue(rental_row(count="unspecified"), NOW))
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0]["region"], "unknown")
        self.assertIsNone(offers[0]["gpu_count"])
        self.assertEqual(offers[0]["gpu_count_relation"], "unknown")


class CoreWeavePlanEvidence(unittest.TestCase):
    def test_actual_comparison_table_preserves_four_plan_terms_without_invented_tariffs(self):
        offers = normalize_offers(coreweave_plans.parse(PLAN_TABLE, NOW))
        self.assertEqual(len(offers), 4)
        plans = {row["purchase_type"]: row for row in offers}
        self.assertEqual(plans["flex_reservation"]["commercial_terms"], {
            "capacity": "Fully guaranteed", "interruptible": "No",
            "pay_as_you_go": "Usage only (fixed holding rate)", "commitment": "Fixed-term"})
        self.assertEqual(plans["spot"]["commercial_terms"]["interruptible"], "Yes (7 mins notice)")
        for row in offers:
            self.assertEqual(row["price_status"], "plan_terms")
            self.assertEqual((row["gpu_model"], row["region"]), ("", "unknown"))
            self.assertIsNone(row["gpu_count"])
            self.assertNotIn("commitment_months", row)
            self.assertNotIn("price_per_gpu_hour_usd", row)
            self.assertEqual((row["observed_at"], row["retrieved_at"]), (NOW, NOW))

    def test_missing_or_incomplete_table_fails_closed(self):
        for raw in ["<html>Flex Reservations</html>", "",
                    PLAN_TABLE.replace("<td>Commitment</td>", "<td>Something else</td>")]:
            with self.assertRaises(ValueError):
                coreweave_plans.parse(raw, NOW)

    def test_fetch_failure_clears_previous_plan_evidence_and_records_failure(self):
        with patch.object(coreweave_plans.urllib.request, "urlopen", return_value=io.BytesIO(PLAN_TABLE.encode())):
            self.assertEqual(coreweave_plans.fetch(), [])
        self.assertEqual(len(coreweave_plans.LAST_CATALOGUE_OFFERS), 4)
        self.assertEqual(coreweave_plans.LAST_FETCH_HEALTH["status"], "catalogue_only")
        with patch.object(coreweave_plans.urllib.request, "urlopen", return_value=io.BytesIO(b"<html>shell</html>")):
            self.assertEqual(coreweave_plans.fetch(), [])
        self.assertEqual(coreweave_plans.LAST_CATALOGUE_OFFERS, [])
        self.assertEqual(coreweave_plans.LAST_FETCH_HEALTH["status"], "failed")


class CatalogueContractAndPresentation(unittest.TestCase):
    def test_identity_ignores_fetch_time_and_price_but_distinguishes_region_and_terms(self):
        raw = reference(price_status="published_unscoped", price_per_gpu_hour_usd=2.00)
        later = {**raw, "observed_at": "2026-09-19", "retrieved_at": "2026-09-19", "price_per_gpu_hour_usd": 2.50}
        rows = normalize_offers([raw, raw, later, {**raw, "region": "europe"},
                                 {**raw, "commercial_terms": {"commitment": "1 year"}}])
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["offer_id"], rows[1]["offer_id"])
        self.assertEqual(len({row["offer_id"] for row in rows}), 3)

    def test_fresh_retrieval_does_not_refresh_old_or_missing_observation(self):
        offers = [reference(product_id="old", observed_at="2026-09-01", retrieved_at=NOW),
                  reference(product_id="unknown", observed_at="", retrieved_at=NOW), reference(product_id="current")]
        original = copy.deepcopy(offers)
        rows = build_catalogue_report(offers, NOW)["offers"]
        self.assertEqual([r["freshness"] for r in rows], ["dated", "unknown", "recent"])
        self.assertGreater(rows[0]["age_hours"], 48)
        self.assertIsNone(rows[1]["age_hours"])
        self.assertEqual(offers, original)

    def test_invalid_prices_counts_sources_and_contradictory_status_are_rejected(self):
        invalid = [
            {"source_url": "https:missing-host"}, {"source_url": "javascript:alert(1)"},
            {"gpu_count": 0}, {"gpu_count": 8, "gpu_count_relation": "unknown"},
            {"gpu_count": None, "gpu_count_relation": "exact"}, {"gpu_count": True},
            {"price_per_gpu_hour_usd": 0}, {"price_per_gpu_hour_usd": 2},
            {"price_status": "plan_terms", "price_per_gpu_hour_usd": 2},
            {"price_status": "plan_terms", "price_per_hour_usd": 0},
            {"price_status": "published_unscoped", "price_per_gpu_hour_usd": float("nan")},
            {"observed_at": "not-a-date"},
        ]
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                normalize_offers([reference(**changes)])

    def test_html_is_xml_valid_and_escapes_evidence_without_misrepresenting_minimums(self):
        raw = reference(product_id='GPU <unsafe>&"', description="Terms <script>alert(1)</script>",
                        source_url="https://example.com/pricing?a=1&b=2", gpu_count=16,
                        gpu_count_relation="minimum", commercial_terms={"term_label": "1 year+"})
        report = build_catalogue_report([raw], NOW)
        html = render_catalogue(report)
        root = ElementTree.fromstring("<root>" + html + "</root>")
        self.assertEqual(len(root.findall(".//table")), 1)
        self.assertEqual(len(root.findall(".//script")), 0)
        self.assertIn("16+ GPUs", html)
        self.assertIn("Quote required", html)
        self.assertIn("Source", html)
        self.assertNotIn("$0", html)


if __name__ == "__main__":
    unittest.main()
