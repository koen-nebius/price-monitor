"""Offline regressions from Lambda's public tables retrieved 18 September 2026.

Reduced fixtures preserve explicit tab quantities, per-GPU column semantics,
the six 1CC prices and the two unpriced longer-term offerings. No test reads a
credential or contacts a provider.
"""
import io
import unittest
from unittest.mock import patch

from fetchers import lambda_labs as fetcher
from price_corrections import correct_snapshot
from report_freshness import publication_records


NOW = "2026-09-18T12:00:00+00:00"
TIERS = [
    ("HGX B200", "16", "9.86"), ("HGX B200", "64", "9.36"),
    ("HGX B200", "256+", "8.87"), ("H100", "16", "6.16"),
    ("H100", "64", "5.85"), ("H100", "256", "5.54"),
]


def cluster_html(tiers=TIERS, quotes=True):
    rows = [(gpu, "2 weeks – 1 year", count, "$" + price)
            for gpu, count, price in tiers]
    if quotes:
        rows += [(gpu, "1 year+", "16+", "—") for gpu in ["HGX B200", "H100"]]
    return "<table>" + "".join(
        f"<tr><th>NVIDIA {gpu}</th><td>{term}</td><td>{count}</td>"
        f"<td>{price}</td><td>Talk to our team</td></tr>"
        for gpu, term, count, price in rows) + "</table>"


def od_row(plan="NVIDIA H100 SXM", cpu=208, ram="1800 GiB", price="3.99"):
    return (f'<tr data-plan="{plan}"><th>{plan}</th><td>80 GB</td>'
            f'<td>{cpu}</td><td>{ram}</td><td>22 TiB SSD</td><td>${price}</td></tr>')


def od_html(count=8, row=None):
    return (f'<button role="tab" aria-controls="gpu-tab">{count}x</button>'
            f'<div role="tabpanel" id="gpu-tab"><div><table>{row or od_row()}</table></div></div>')


CSV_HEADER = "InstanceType,AcceleratorName,AcceleratorCount,vCPUs,MemoryGiB,Price,Region,SpotPrice\n"
CSV_ROWS = (
    "gpu_8x_h100_sxm5,H100,8,208,1800,31.92,us-west-3,\n"
    "gpu_8x_h100_sxm5,H100,8,208,1800,31.92,us-east-1,\n"
    "gpu_1x_h100_pcie,H100,1,26,225,3.29,us-west-3,2.00\n"
)


class LambdaClusterOffers(unittest.TestCase):
    def setUp(self):
        fetcher.LAST_FETCH_HEALTH.clear()
        fetcher.LAST_CATALOGUE_OFFERS.clear()

    def test_all_six_tiers_with_exact_quantity_relation_and_duration(self):
        rows, quotes = fetcher._parse_one_click_clusters(cluster_html(), NOW)
        self.assertEqual(len(rows), 6)
        actual = {(r.gpu_model, r.gpu_count, r.gpu_count_relation): r.price_per_gpu_hour_usd
                  for r in rows}
        self.assertEqual(actual, {
            ("B200", 16, "exact"): 9.86, ("B200", 64, "exact"): 9.36,
            ("B200", 256, "minimum"): 8.87, ("H100", 16, "exact"): 6.16,
            ("H100", 64, "exact"): 5.85, ("H100", 256, "exact"): 5.54,
        })
        for row in rows:
            self.assertEqual((row.term_min_days, row.term_max_days, row.term_label),
                             (14, 365, "2 weeks – 1 year"))
            self.assertEqual((row.region, row.node_gpus), ("unknown", 0))
            self.assertIsNone(row.commitment_months)
            self.assertIsNone(row.available)
            self.assertAlmostEqual(row.price_per_hour_usd,
                                   row.price_per_gpu_hour_usd * row.gpu_count)
        self.assertEqual(len({row.offer_id for row in rows}), 6)
        self.assertIn("minimum_quantity", next(r for r in rows if r.gpu_count_relation == "minimum").price_basis)
        self.assertEqual(len(quotes), 2)

    def test_quote_only_rows_are_catalogue_evidence_without_numeric_price(self):
        rows, quotes = fetcher._parse_one_click_clusters(cluster_html(tiers=[]), NOW)
        self.assertEqual(rows, [])
        self.assertEqual({q["gpu_model"] for q in quotes}, {"B200", "H100"})
        for quote in quotes:
            self.assertEqual((quote["gpu_count"], quote["gpu_count_relation"]), (16, "minimum"))
            self.assertEqual(quote["price_status"], "quote_required")
            self.assertEqual(quote["region"], "unknown")
            self.assertEqual(quote["source_url"], fetcher.ONE_CLICK_URL)
            self.assertEqual((quote["observed_at"], quote["retrieved_at"]), (NOW, NOW))
            self.assertEqual(quote["commercial_terms"]["term_min_days"], 365)
            self.assertIsNone(quote["commercial_terms"]["term_max_days"])
            self.assertFalse(any(key.startswith("price_per_") for key in quote))

    def test_exact_duplicates_dedup_and_offer_identity_survives_repricing(self):
        rows, quotes = fetcher._parse_one_click_clusters(cluster_html() * 2, NOW)
        self.assertEqual((len(rows), len(quotes)), (6, 2))
        changed, _ = fetcher._parse_one_click_clusters(cluster_html().replace("$9.86", "$9.90"), "2026-09-19")
        self.assertEqual(rows[0].offer_id, changed[0].offer_id)
        self.assertNotEqual(rows[0].price_per_gpu_hour_usd, changed[0].price_per_gpu_hour_usd)

    def test_correct_tavily_function_handles_shell_and_exposes_catalogue(self):
        with patch.object(fetcher.urllib.request, "urlopen", return_value=io.BytesIO(b"<html>shell</html>")), \
                patch("fetchers._tavily.fetch_text", return_value=cluster_html()) as rendered:
            rows = fetcher._fetch_one_click_clusters(NOW)
        rendered.assert_called_once_with(fetcher.ONE_CLICK_URL)
        self.assertEqual((len(rows), len(fetcher.LAST_CATALOGUE_OFFERS)), (6, 2))
        self.assertEqual(fetcher.LAST_FETCH_HEALTH["components"]["one_click_clusters"]["status"], "live")

    def test_failed_direct_retrieval_still_uses_rendered_fallback(self):
        with patch.object(fetcher.urllib.request, "urlopen", side_effect=OSError("no network")), \
                patch("fetchers._tavily.fetch_text", return_value=cluster_html()):
            self.assertEqual(len(fetcher._fetch_one_click_clusters(NOW)), 6)
        self.assertEqual(fetcher.LAST_FETCH_HEALTH["components"]["one_click_clusters"]["status"], "live")

    def test_partial_ladder_and_quote_only_health_are_not_reported_live(self):
        rows, quotes = fetcher._parse_one_click_clusters(cluster_html(TIERS[:1]), NOW)
        fetcher._cluster_health(rows, quotes)
        health = fetcher.LAST_FETCH_HEALTH["components"]["one_click_clusters"]
        self.assertEqual(health["status"], "partial")
        self.assertEqual(len(health["missing_priced_tiers"]), 5)
        fetcher._cluster_health([], quotes)
        self.assertEqual(fetcher.LAST_FETCH_HEALTH["components"]["one_click_clusters"]["status"], "partial")


class LambdaCatalogueReferences(unittest.TestCase):
    def test_catalogue_retains_shape_region_and_purchase_type_with_real_provenance(self):
        rows = fetcher._parse_skypilot_catalog(CSV_HEADER + CSV_ROWS, NOW)
        self.assertEqual(len(rows), 4)
        self.assertEqual(len({row.offer_id for row in rows}), 4)
        for row in rows:
            self.assertEqual(row.source_url, fetcher.SKYPILOT_CSV_URL)
            self.assertEqual(row.source_feed, "skypilot")
            self.assertEqual(row.source_observed_at, "")
            self.assertEqual(row.fetched_at, NOW)
            self.assertFalse(row.comparison_eligible)
            self.assertIn("no upstream observation timestamp", row.correction_reason)
            self.assertAlmostEqual(row.price_per_hour_usd, row.gpu_count * row.price_per_gpu_hour_usd)
        copied = correct_snapshot(rows, include_excluded=True)
        self.assertEqual([r.to_dict() for r in copied], [r.to_dict() for r in rows])
        eligible, reasons = publication_records(rows, NOW)
        self.assertEqual(eligible, [])
        self.assertTrue(reasons)

    def test_same_gpu_family_does_not_suppress_missing_shapes_regions_or_spot(self):
        catalogue = fetcher._parse_skypilot_catalog(CSV_HEADER + CSV_ROWS, NOW)
        direct = fetcher._parse_api_data({"data": {"gpu_8x_h100_sxm5": {
            "price_cents_per_hour": 3192,
            "regions_with_capacity_available": [{"name": "us-west-3"}],
        }}}, NOW)
        with patch.object(fetcher, "_fetch_skypilot_catalog", return_value=catalogue):
            supplemented = fetcher._supplement_with_skypilot(direct, NOW)
        self.assertEqual(len(supplemented), 4)
        self.assertIs(supplemented[0], direct[0])
        self.assertEqual(sum(r.source_feed == "skypilot" for r in supplemented), 3)
        self.assertEqual({r.region for r in supplemented}, {"us-west-3", "us-east-1"})
        self.assertEqual({r.gpu_count for r in supplemented}, {1, 8})
        self.assertIn("spot", {r.consumption_type for r in supplemented})

    def test_unknown_region_invalid_quantities_and_independent_spot(self):
        content = CSV_HEADER + "gpu_8x_gb200,GB200,8,208,1800,,,20\n"
        rows = fetcher._parse_skypilot_catalog(content, NOW)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].gpu_model, rows[0].region, rows[0].consumption_type),
                         ("GB200", "unknown", "spot"))
        for bad in ["", "0", "-1", "1.5", "NaN", "Infinity", "true"]:
            self.assertEqual(fetcher._parse_skypilot_catalog(
                CSV_HEADER + f"gpu_8x_h100_sxm5,H100,{bad},208,1800,31.92,us-west-3,\n", NOW), [])

    def test_identical_duplicate_dedup_but_conflicting_rate_keeps_evidence(self):
        first = "gpu_8x_h100_sxm5,H100,8,208,1800,31.92,us-west-3,\n"
        rows = fetcher._parse_skypilot_catalog(CSV_HEADER + first + first + first.replace("31.92", "32.00"), NOW)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].offer_id, rows[1].offer_id)
        self.assertNotEqual(rows[0].price_per_hour_usd, rows[1].price_per_hour_usd)


class LambdaOnDemandAndHealth(unittest.TestCase):
    def setUp(self):
        fetcher.LAST_FETCH_HEALTH.clear()
        fetcher.LAST_CATALOGUE_OFFERS.clear()

    def test_public_table_uses_explicit_quantity_not_cpu_ratio_or_region_guess(self):
        # Deliberately choose 64 CPUs: the previous vCPU/26 guess produced 2 GPUs.
        rows = fetcher._parse_html(od_html(count=8, row=od_row(cpu=64)), NOW)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row.gpu_count, row.vcpu, row.region, row.node_gpus), (8, 64, "unknown", 0))
        self.assertEqual(row.storage_gb, 22 * 1024)
        self.assertEqual(row.price_per_hour_usd, 3.99 * 8)
        self.assertEqual(fetcher._parse_html("<table>" + od_row() + "</table>", NOW), [])

    def test_each_linked_tab_and_pcie_variant_are_separate(self):
        first = od_html(count=8)
        second = od_html(count=1, row=od_row("NVIDIA H100 PCIe", 26, "225 GiB", "3.29"))
        rows = fetcher._parse_html(first + second.replace("gpu-tab", "single-tab"), NOW)
        self.assertEqual(len(rows), 2)
        self.assertEqual({(r.gpu_count, r.form_factor, r.price_per_gpu_hour_usd) for r in rows},
                         {(8, "SXM", 3.99), (1, "PCIe", 3.29)})

    def test_api_unknown_region_is_retained_without_implying_global_deployability(self):
        rows = fetcher._parse_api_data({"data": {"gpu_8x_b200_sxm6": {
            "instance_type": {"name": "gpu_8x_b200_sxm6", "price_cents_per_hour": 5352,
                              "specs": {"gpus": 8, "vcpus": 208, "memory_gib": 2900}},
            "regions_with_capacity_available": [{}],
        }}}, NOW)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].gpu_count, rows[0].region, rows[0].price_per_gpu_hour_usd), (8, "unknown", 6.69))
        self.assertEqual(rows[0].source_url, fetcher.API_URL)
        self.assertEqual(rows[0].source_observed_at, NOW)

    def test_addon_failure_preserves_od_but_health_is_partial_and_previous_catalogue_cleared(self):
        direct = fetcher._parse_html(od_html(), NOW)
        fetcher.LAST_CATALOGUE_OFFERS.append({"stale": True})
        with patch.dict("os.environ", {}, clear=True), \
                patch.object(fetcher, "_scrape_pricing_page", return_value=direct), \
                patch.object(fetcher, "_fetch_skypilot_catalog", return_value=[]), \
                patch.object(fetcher, "_fetch_one_click_clusters", side_effect=RuntimeError("parser failure")):
            rows = fetcher.fetch()
        self.assertEqual(rows, direct)
        self.assertEqual(fetcher.LAST_CATALOGUE_OFFERS, [])
        self.assertEqual(fetcher.LAST_FETCH_HEALTH["status"], "partial")
        self.assertEqual(fetcher.LAST_FETCH_HEALTH["components"]["on_demand"]["status"], "live")
        self.assertEqual(fetcher.LAST_FETCH_HEALTH["components"]["one_click_clusters"]["status"], "failed")

    def test_only_undated_catalogue_rows_are_fallback_not_live(self):
        fallback = fetcher._parse_skypilot_catalog(CSV_HEADER + CSV_ROWS, NOW)
        with patch.dict("os.environ", {}, clear=True), \
                patch.object(fetcher, "_scrape_pricing_page", return_value=[]), \
                patch.object(fetcher, "_fetch_skypilot_catalog", return_value=fallback), \
                patch.object(fetcher, "_fetch_one_click_clusters", side_effect=RuntimeError("failure")):
            self.assertEqual(fetcher.fetch(), fallback)
        self.assertEqual(fetcher.LAST_FETCH_HEALTH["status"], "fallback")
        self.assertEqual(fetcher.LAST_FETCH_HEALTH["components"]["on_demand"]["status"], "fallback")


if __name__ == "__main__":
    unittest.main()
