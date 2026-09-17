"""Offline tests for Vultr public bare-metal catalogue normalization."""
import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from fetchers import vultr
from schema import PriceRecord

NOW = "2026-09-17T15:00:00+00:00"


def b200():
    # Observed official /v2/plans-metal fields, 17 September 2026. Catalogue
    # prices remain observations when deployment is disabled / regions empty.
    return {
        "id": "vbm-256c-3072gb-8-b200-gpu", "gpu_type": "NVIDIA_B200",
        "gpu_count": 8, "cpu_count": 128, "cpu_threads": 256,
        "ram": 3145728, "disk": 3576, "disk_count": 8,
        "invoice_type": "hourly", "hourly_cost": 68,
        "hourly_cost_preemptible": 25.6, "monthly_cost": 45696,
        "deploy_ondemand": False, "deploy_preemptible": True, "locations": [],
    }


def page(plans, total=None, next_cursor=""):
    return json.dumps({
        "plans_metal": plans,
        "meta": {"total": len(plans) if total is None else total,
                 "links": {"next": next_cursor, "prev": ""}},
    }).encode()


class VultrPriceTests(unittest.TestCase):
    def test_live_b200_full_node_division_and_hardware_units(self):
        ondemand, preemptible = vultr.normalize_plan(b200(), NOW)
        self.assertEqual((ondemand.gpu_count, ondemand.node_gpus, ondemand.gpu_model), (8, 8, "B200"))
        self.assertEqual((ondemand.price_per_hour_usd, ondemand.price_per_gpu_hour_usd), (68, 8.5))
        self.assertEqual((preemptible.price_per_hour_usd, preemptible.price_per_gpu_hour_usd), (25.6, 3.2))
        self.assertEqual((ondemand.vcpu, ondemand.ram_gb, ondemand.storage_gb), (256, 3072, 28608))
        self.assertEqual(ondemand.source_type, "api")
        self.assertEqual(ondemand.source_url, "https://api.vultr.com/v2/plans-metal")
        self.assertEqual(ondemand.fetched_at, NOW)
        self.assertEqual(ondemand.parser_version, "vultr-metal-price-1.0")

    def test_disabled_mode_is_retained_but_not_a_normal_public_price(self):
        ondemand, preemptible = vultr.normalize_plan(b200())
        self.assertEqual((ondemand.consumption_type, ondemand.price_basis),
                         ("on_demand", "public_catalog_ondemand_disabled"))
        self.assertEqual((preemptible.consumption_type, preemptible.price_basis),
                         ("preemptible", "public_catalog_no_locations"))
        self.assertEqual(ondemand.region, "unspecified")
        plan = b200()
        plan["deploy_preemptible"] = False
        plan["locations"] = ["ewr"]
        self.assertEqual(vultr.normalize_plan(plan)[1].price_basis, "public_catalog_preemptible_disabled")

    def test_flag_must_be_explicit_boolean_and_price_cannot_supply_it(self):
        for enabled in [None, 1, 0, "true", "false", {}, []]:
            with self.subTest(enabled=enabled):
                plan = b200()
                plan["deploy_ondemand"] = enabled
                plan["locations"] = ["ewr"]
                self.assertEqual(vultr.normalize_plan(plan)[0].price_basis, "public_catalog_deployment_unknown")
        plan = b200()
        del plan["deploy_ondemand"]
        self.assertEqual(vultr.normalize_plan(plan)[0].price_basis, "public_catalog_deployment_unknown")

    def test_listed_regions_are_exact_catalogue_scope_not_live_stock(self):
        plan = b200()
        plan["locations"] = ["ewr", "ams", "ewr"]
        plan["deploy_ondemand"] = True
        rows = vultr.normalize_plan(plan)
        self.assertEqual([(r.consumption_type, r.region) for r in rows],
                         [("on_demand", "ams"), ("on_demand", "ewr"),
                          ("preemptible", "ams"), ("preemptible", "ewr")])
        self.assertTrue(all(r.price_basis == "public_catalog" for r in rows))
        self.assertTrue(all(r.interconnect == "unknown" for r in rows))
        for locations in [None, "ewr", [None], [{}], [""], ["US East"]]:
            plan["locations"] = locations
            self.assertEqual(vultr.normalize_plan(plan), [])

    def test_gpu_count_is_explicit_positive_integer_and_matches_sku(self):
        for count in [None, 0, -1, True, "8", 8.5, 4, float("nan"), float("inf")]:
            with self.subTest(count=count):
                plan = b200()
                plan["gpu_count"] = count
                self.assertEqual(vultr.normalize_plan(plan), [])

    def test_prices_must_be_finite_positive_usd_hourly_numbers(self):
        for price in [None, 0, -1, True, "68", float("nan"), float("inf"), [], {}]:
            with self.subTest(price=price):
                plan = b200()
                plan["hourly_cost"] = price
                rows = vultr.normalize_plan(plan)
                self.assertEqual([r.consumption_type for r in rows], ["preemptible"])
        for unit in [None, "monthly", "daily", "Hourly", "", 1]:
            plan = b200()
            plan["invoice_type"] = unit
            self.assertEqual(vultr.normalize_plan(plan), [])
        for currency in ["EUR", "", None, {}]:
            plan = b200()
            plan["currency"] = currency
            self.assertEqual(vultr.normalize_plan(plan), [])

    def test_unknown_gpus_and_other_plans_do_not_become_tracked_gpu_offers(self):
        for gpu in [None, "NVIDIA_GH200", "NVIDIA_A100", "AMD_MI300X", "NVIDIA_H1000", "H100", {}]:
            plan = b200()
            plan["gpu_type"] = gpu
            self.assertEqual(vultr.normalize_plan(plan), [])
        for sku in [None, "vcg-a100", "", {}]:
            plan = b200()
            plan["id"] = sku
            self.assertEqual(vultr.normalize_plan(plan), [])
        for malformed in [None, [], {}, 42]:
            self.assertEqual(vultr.normalize_plan(malformed), [])
        plan = b200()
        plan["gpu_type"] = "NVIDIA_H100"
        self.assertEqual(vultr.normalize_plan(plan), [])

    def test_h100_and_l40s_are_not_guessed_from_sku_or_vram(self):
        h100 = b200()
        h100.update(id="vbm-112c-2048gb-8-h100-gpu", gpu_type="NVIDIA_H100",
                    hourly_cost=23.92, hourly_cost_preemptible=18.4)
        row = vultr.normalize_plan(h100)[0]
        self.assertEqual((row.gpu_model, row.form_factor), ("H100", "unknown"))
        self.assertAlmostEqual(row.price_per_gpu_hour_usd, 2.99)
        l40s = b200()
        l40s.update(id="vbm-64c-2048gb-8-l40-gpu", gpu_type="NVIDIA_L40S",
                    hourly_cost=13.368, hourly_cost_preemptible=11.92)
        row = vultr.normalize_plan(l40s)[0]
        self.assertEqual((row.gpu_model, row.form_factor), ("L40S", "PCIe"))
        self.assertAlmostEqual(row.price_per_gpu_hour_usd, 1.671)

    def test_missing_optional_specs_remain_unknown_and_schema_roundtrips(self):
        plan = b200()
        for field in ["cpu_threads", "ram", "disk_count"]:
            del plan[field]
        row = vultr.normalize_plan(plan)[0]
        self.assertEqual((row.vcpu, row.ram_gb, row.storage_gb), (None, None, None))
        self.assertEqual(PriceRecord.from_dict(row.to_dict()), row)

    def test_invalid_top_level_and_duplicate_offers_fail(self):
        for payload in [None, {}, [], {"plans_metal": {}}, {"plans_metal": None}]:
            with self.assertRaises(ValueError):
                vultr.parse(payload)
        with self.assertRaises(ValueError):
            vultr.parse({"plans_metal": [b200(), b200()]})

    def test_pagination_consumes_opaque_cursor_and_keeps_all_plans(self):
        cpu = {"id": "vbm-4c-32gb"}
        cursor = "opaque/+cursor="
        with patch.object(vultr, "http_get", side_effect=[page([cpu], 2, cursor), page([b200()], 2)]) as http:
            rows = vultr.fetch()
        self.assertEqual(len(rows), 2)
        self.assertEqual(http.call_count, 2)
        first, second = [call.args[0] for call in http.call_args_list]
        self.assertEqual(first, vultr.API_URL + "?per_page=500")
        self.assertEqual(urlsplit(second).netloc, "api.vultr.com")
        self.assertEqual(parse_qs(urlsplit(second).query), {"per_page": ["500"], "cursor": [cursor]})
        self.assertTrue(all(not call.kwargs.get("data") for call in http.call_args_list))

    def test_partial_pagination_changed_catalogue_and_cycles_fail_whole_fetch(self):
        cpu = {"id": "vbm-4c-32gb"}
        cases = [
            [page([b200()], 2)],
            [page([cpu], 2, "next"), page([b200()], 3)],
            [page([cpu], 2, "next"), page([b200()], 2, "next")],
            [page([cpu], 2, "next"), page([cpu], 2)],
            [page([], 2, "next")],
            [b'{"plans_metal": [], "meta": {"total": 0, "links": {}}}'],
            [b'{"plans_metal": [null], "meta": {"total": 1, "links": {"next": ""}}}'],
            [b'{"plans_metal": [], "meta": {"total": true, "links": {"next": ""}}}'],
        ]
        for pages in cases:
            with self.subTest(pages=pages), patch.object(vultr, "http_get", side_effect=pages):
                with self.assertLogs(vultr.logger, level="ERROR"):
                    self.assertEqual(vultr.fetch(), [])

    def test_fetch_failure_has_no_stale_fallback_or_arbitrary_error_content(self):
        with patch.object(vultr, "http_get", side_effect=RuntimeError("private detail")):
            with self.assertLogs(vultr.logger, level="ERROR") as log:
                self.assertEqual(vultr.fetch(), [])
        self.assertNotIn("private detail", " ".join(log.output))


if __name__ == "__main__":
    unittest.main()
