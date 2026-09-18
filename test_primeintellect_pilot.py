"""Documented-fixture pilot checks; no provider network or production writes."""
import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from fetchers import primeintellect as pi
from scripts import primeintellect_pilot as cli

FIXTURE = Path(__file__).parent / "tests/fixtures/primeintellect_documented_single_node.json"
FETCHED = "2026-09-18T12:00:00+00:00"


def item(**changes):
    value = json.loads(FIXTURE.read_text())["items"][0]
    value.update(changes)
    return value


def payload(items, total=None):
    return json.dumps({"items": items, "totalCount": len(items) if total is None else total}).encode()


class PrimeIntellectPilotTests(unittest.TestCase):
    def test_documented_configuration_preserves_provenance_and_unknown_price_basis(self):
        original = item()
        unchanged = copy.deepcopy(original)
        row = pi.audit_offer(original, "single_node", FETCHED)
        self.assertEqual(original, unchanged)
        self.assertEqual((row["provider"], row["source_feed"]), ("runpod", "primeintellect"))
        self.assertEqual((row["cloud_id"], row["data_center"], row["gpu_count"]),
                         ("NVIDIA H100 PCIe", "US-KS-2", 1))
        self.assertEqual(row["resource_specs"]["disk"]["defaultCount"], 80)
        self.assertEqual(row["prices_raw"]["onDemand"], 2.69)
        self.assertIsNone(row["price_per_gpu_hour_usd"])
        self.assertIsNone(row["consumption_type"])
        self.assertFalse(row["comparison_eligible"])
        self.assertIn("disk_default_cost_is_additional", row["quarantine_reasons"])
        self.assertEqual(row["fetched_at"], FETCHED)
        self.assertIsNone(row["source_observed_at"])

    def test_identity_preserves_configuration_and_separates_single_and_multinode(self):
        variants = [item(), item(dataCenter="US-KS-3"), item(region="eu_west"),
                    item(gpuCount=8), item(socket="SXM5"), item(security="community_cloud"),
                    item(prepaidTime=720), item(memory={"defaultCount": 512}),
                    item(interconnect=400, interconnectType="InfiniBand")]
        ids = [pi.audit_offer(value, "single_node")["offer_id"] for value in variants]
        self.assertEqual(len(set(ids)), len(variants))
        multi = pi.audit_offer(item(), "multi_node")
        self.assertNotEqual(multi["offer_id"], ids[0])
        self.assertEqual(multi["product_scope"], "multinode_catalog")
        self.assertIsNone(multi["node_count"])
        self.assertIn("multinode_topology_and_node_count_unverified", multi["quarantine_reasons"])

    def test_price_stock_and_retrieval_changes_do_not_create_new_identity(self):
        before = pi.audit_offer(item(), "single_node")
        after = pi.audit_offer(item(prices={"currency": "USD", "onDemand": 3.0},
                                    stockStatus="Unavailable"), "single_node", FETCHED)
        self.assertEqual(before["offer_id"], after["offer_id"])
        self.assertIs(after["available"], False)

    def test_prepaid_hours_are_not_a_contract_month_and_spot_capability_is_not_price_type(self):
        row = pi.audit_offer(item(prepaidTime=720, isSpot=True), "single_node")
        self.assertEqual(row["prepaid_hours"], 720)
        self.assertIsNone(row["commitment_months"])
        self.assertIs(row["spot_supported"], True)
        self.assertIsNone(row["consumption_type"])

    def test_community_price_keeps_security_tier_and_currency(self):
        row = pi.audit_offer(item(security="community_cloud", prices={
            "currency": "EUR", "onDemand": 9, "communityPrice": 2, "isVariable": True}), "single_node")
        self.assertEqual(row["price_field_for_security"], "communityPrice")
        self.assertIn("non_usd_or_missing_currency", row["quarantine_reasons"])
        self.assertIn("variable_price", row["quarantine_reasons"])
        self.assertIsNone(row["price_per_gpu_hour_usd"])

    def test_stock_levels_are_labels_not_gpu_inventory_quantities(self):
        for label in ["Low", "Medium", "High", None, "FutureEnum"]:
            row = pi.audit_offer(item(stockStatus=label, gpuCount=8), "single_node")
            self.assertIsNone(row["available"])
            self.assertIsNone(row["stock_quantity"])
        self.assertIs(pi.audit_offer(item(stockStatus="Available"), "single_node")["available"], True)

    def test_unknown_gpu_and_malformed_fields_remain_quarantined(self):
        for gpu in ["GH200_96GB", "H200_96GB", "RTX6000Ada_48GB", {"bad": "type"}]:
            row = pi.audit_offer(item(gpuType=gpu), "single_node")
            self.assertIsNone(row["gpu_model"])
            self.assertIn("unmapped_gpu_type", row["quarantine_reasons"])
        row = pi.audit_offer(item(gpuCount=True, security={}, stockStatus={},
                                  prepaidTime=-1, disk="wrong"), "single_node")
        for code in ["invalid_gpu_count", "unknown_security_tier", "unknown_stock_status",
                     "invalid_prepaid_hours", "invalid_disk_spec"]:
            self.assertIn(code, row["quarantine_reasons"])
        self.assertEqual(pi.audit_offer(None, "single_node")["quarantine_reasons"], ["item_not_object"])

    def test_missing_credential_makes_no_request(self):
        request = Mock()
        result = pi.collect("", request_page=request)
        request.assert_not_called()
        self.assertEqual((result["status"], result["blocked_stage"]), ("blocked", "authentication"))
        self.assertFalse(result["live_attempted"])

    def test_complete_pagination_and_raw_callback_keep_no_headers_or_token(self):
        requests, pages = [], []
        def read(url, token):
            requests.append((url, token))
            page = int(parse_qs(urlsplit(url).query)["page"][0])
            return payload([item(cloudId="shape-" + str(page))], 2)
        def persist(endpoint, page, raw, metadata):
            pages.append((endpoint, page, raw, metadata))
        result = pi.collect("secret-not-output", ("single_node",), page_size=1,
                            request_page=read, on_page=persist)
        self.assertEqual(result["status"], "collected_for_review")
        self.assertEqual(len(result["offers"]), 2)
        self.assertEqual([p[1] for p in pages], [1, 2])
        self.assertTrue(result["endpoints"]["single_node"]["complete"])
        self.assertNotIn("secret-not-output", json.dumps(result))
        self.assertEqual(len(requests), 2)

    def test_pagination_rejects_repetition_drift_truncation_and_limit(self):
        scenarios = [
            ([payload([item()], 2), payload([item()], 2)], 5, "repeated_configuration_during_pagination"),
            ([payload([item()], 2), payload([item(cloudId="new")], 3)], 5, "total_count_changed_during_pagination"),
            ([payload([], 1)], 5, "incomplete_or_oversized_page"),
            ([payload([item()], 2)], 1, "page_limit_reached"),
        ]
        for responses, limit, expected in scenarios:
            with self.subTest(expected=expected):
                result = pi.collect("test", ("single_node",), page_size=1, max_pages=limit,
                                    request_page=Mock(side_effect=responses))
                self.assertEqual(result["status"], "incomplete")
                self.assertEqual(result["endpoints"]["single_node"]["error"], expected)
                self.assertFalse(result["production_eligible"])

    def test_transport_errors_never_render_arbitrary_error_messages(self):
        for error, expected in [(pi.PilotError("http_401"), "http_401"),
                                (ValueError("Authorization: Bearer secret"), "transport_failed"),
                                (pi.PilotError("Authorization: Bearer secret"), "transport_failed")]:
            result = pi.collect("secret", ("single_node",), request_page=Mock(side_effect=error))
            self.assertEqual(result["endpoints"]["single_node"]["error"], expected)
            self.assertNotIn("secret", json.dumps(result))
        with self.assertRaises(pi.PilotError):
            pi.read_page("https://other.example/steal", "secret")

    def test_invalid_json_numbers_and_envelopes_are_rejected(self):
        for raw in [b'{"items":[],"totalCount":NaN}', b'{"items":[],"totalCount":true}',
                    b'{"items":{},"totalCount":0}']:
            result = pi.collect("test", ("single_node",), request_page=Mock(return_value=raw))
            self.assertEqual(result["status"], "incomplete")
        empty = pi.collect("test", ("single_node",), request_page=Mock(return_value=payload([])))
        self.assertTrue(empty["endpoints"]["single_node"]["complete"])

    def test_offline_cli_preserves_exact_raw_bytes_and_does_not_fake_fetch_time(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "pilot"
            with patch.object(pi, "read_page", side_effect=AssertionError("network not permitted")), redirect_stdout(io.StringIO()):
                code = cli.main(["--input-json", str(FIXTURE), "--endpoint", "single_node", "--output-dir", str(out)])
            self.assertEqual(code, 0)
            self.assertEqual((out / "raw_single_node_page_0001.json").read_bytes(), FIXTURE.read_bytes())
            audit = json.loads((out / "audit.json").read_text())
            rows = json.loads((out / "offers_for_review.json").read_text())
            self.assertFalse(audit["live_attempted"])
            self.assertEqual(audit["price_record_count"], 0)
            self.assertIsNone(rows[0]["fetched_at"])
            self.assertIsNone(rows[0]["source_observed_at"])
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.main(["--input-json", str(FIXTURE), "--endpoint", "single_node", "--output-dir", str(out)])

    def test_cli_blocks_missing_access_without_default_store_output(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "pilot"
            with patch.dict(cli.os.environ, {}, clear=True), redirect_stdout(io.StringIO()):
                code = cli.main(["--live", "--output-dir", str(out)])
            self.assertEqual(code, 2)
            audit = json.loads((out / "audit.json").read_text())
            self.assertEqual(audit["blocked_stage"], "authentication")
            self.assertEqual(list(out.glob("raw_*")), [])
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main(["--live", "--output-dir", str(cli.ROOT / "store" / "pilot")])


if __name__ == "__main__":
    unittest.main()
