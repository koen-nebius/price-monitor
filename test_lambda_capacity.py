"""Offline Lambda exact-SKU launchability and credential-safe GET tests."""
import io
import json
import os
import unittest
import urllib.error
from unittest.mock import patch

import lambda_capacity_api as api
from capacity.fetchers import lambda_labs as fetcher
from capacity.schema import AvailabilityRecord

NOW = "2026-09-17T19:00:00+00:00"
KEY = "synthetic-lambda-test-token"
SKU = "gpu_8x_h100_sxm5"
_DEFAULT = object()


def item(sku=SKU, count=8, regions=_DEFAULT):
    return {"instance_type": {"name": sku, "specs": {"gpus": count}},
            "regions_with_capacity_available": ([{"name": "us-east-1"}]
                                                if regions is _DEFAULT else regions)}


def payload(entry=None, sku=SKU):
    return {"data": {sku: item() if entry is None else entry}}


class Response(io.BytesIO):
    status = 200


class LambdaCapacityTests(unittest.TestCase):
    def test_exact_shapes_are_preserved_instead_of_combined(self):
        one, eight = "gpu_1x_h100_pcie", "gpu_8x_h100_sxm5"
        rows = fetcher.parse({"data": {eight: item(eight, 8, []), one: item(one, 1)}}, NOW)
        self.assertEqual([(r.instance_type, r.gpu_count, r.region, r.state, r.metric_value)
                          for r in rows], [
            (one, 1, "global", "available", 1.0),
            (one, 1, "us-east-1", "available", 1.0),
            (eight, 8, "global", "sold_out", 0.0),
        ])
        self.assertEqual([r.metric_type for r in rows],
                         ["launchable_regions", "instance_launchability", "launchable_regions"])
        for row in rows:
            self.assertEqual((row.product_scope, row.data_source, row.fetched_at, row.parser_version),
                             ("on_demand_instance", "official_api", NOW, "lambda-instance-capacity-2.0"))
            self.assertEqual(row.source_url, api.API_URL)
            self.assertIn("multi-node availability not established", row.detail)

    def test_region_count_is_not_gpu_or_instance_count(self):
        rows = fetcher.parse(payload(item(regions=[{"name": "us-east-1"}, {"name": "us-west-1"}])))
        self.assertEqual(rows[0].metric_value, 2)
        self.assertEqual([r.metric_value for r in rows[1:]], [1.0, 1.0])
        self.assertTrue(all(r.gpu_count == 8 for r in rows))

    def test_only_valid_explicit_empty_list_is_sold_out(self):
        empty = fetcher.parse(payload(item(regions=[])))
        self.assertEqual(len(empty), 1)
        self.assertEqual((empty[0].state, empty[0].metric_value), ("sold_out", 0.0))
        missing = item()
        del missing["regions_with_capacity_available"]
        for entry in [missing] + [item(regions=r) for r in [None, "", {}, False, 0, [None], [{}], [{"name": ""}], [{"name": "global"}]]]:
            with self.subTest(entry=entry):
                rows = fetcher.parse(payload(entry))
                self.assertEqual(len(rows), 1)
                self.assertEqual((rows[0].state, rows[0].metric_value), ("unknown", None))

    def test_partial_malformed_list_has_unknown_total_but_retains_explicit_positive(self):
        rows = fetcher.parse(payload(item(regions=[None, {"name": "us-east-1"}, {"name": "secret\ninvalid"}])))
        self.assertEqual([(r.region, r.state, r.metric_value) for r in rows],
                         [("global", "unknown", None), ("us-east-1", "available", 1.0)])
        self.assertNotIn("secret", " ".join(r.detail for r in rows))

    def test_region_and_sku_order_and_duplicates_are_deterministic(self):
        a = item(regions=[{"name": "us-west-1"}, {"name": "us-east-1"}, {"name": "us-west-1"}])
        b = item("gpu_1x_h100_pcie", 1, [])
        first = fetcher.parse({"data": {SKU: a, "gpu_1x_h100_pcie": b}}, NOW)
        a["regions_with_capacity_available"].reverse()
        second = fetcher.parse({"data": {"gpu_1x_h100_pcie": b, SKU: a}}, NOW)
        self.assertEqual(first, second)
        self.assertEqual(len([r for r in first if r.instance_type == SKU and r.region != "global"]), 2)

    def test_token_boundaries_skip_gh200_legacy_rtx_and_h1000(self):
        supported = ["gb300", "gb200", "b300", "b200", "h200", "h100", "l40s", "rtx_pro_6000", "rtxpro6000"]
        excluded = ["gh200", "h1000", "rtx6000", "rtx6000_ada", "a100"]
        entries = {f"gpu_1x_{gpu}": item(f"gpu_1x_{gpu}", 1, []) for gpu in supported + excluded}
        rows = fetcher.parse({"data": entries})
        self.assertEqual(len(rows), len(supported))
        self.assertEqual({r.gpu_model for r in rows}, {"GB300", "GB200", "B300", "B200", "H200", "H100", "L40S", "RTX6000"})

    def test_invalid_counts_or_sku_count_mismatch_fail_statically(self):
        for count in [None, 0, -1, True, 1.5, "8", 2 ** 31, {}, 1]:
            with self.subTest(count=count), self.assertRaises(fetcher.LambdaParseError):
                fetcher.parse(payload(item(count=count)))
        unknown_count_sku = "gpu_h100_sxm5"
        with self.assertRaisesRegex(fetcher.LambdaParseError, "count differs"):
            fetcher.parse(payload(item(unknown_count_sku), unknown_count_sku))

    def test_critical_schema_and_identity_fail_safely(self):
        invalid_entries = [None, [], {}, {"instance_type": None},
                           {"instance_type": {"name": "secret-mismatch", "specs": {"gpus": 8}}},
                           {"instance_type": {"name": SKU, "specs": None}}]
        for entry in invalid_entries:
            with self.subTest(entry=entry), self.assertRaises(fetcher.LambdaParseError):
                fetcher.parse({"data": {SKU: entry}})
        for data in [None, [], {}, {"data": []}, {"data": {}, "next": "secret-token"}, {"data": {"secret\nvalue": {}}}]:
            with self.subTest(data=data), self.assertRaises(fetcher.LambdaParseError):
                fetcher.parse(data)
        self.assertEqual(fetcher.parse({"data": {}}), [])

    def test_schema_roundtrip_and_empty_quantity_relation(self):
        row = fetcher.parse(payload(), NOW)[0]
        self.assertEqual(AvailabilityRecord.from_dict(row.to_dict()), row)
        self.assertEqual(row.quantity_relation, "")

    def test_absent_key_skips_without_network(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(fetcher, "fetch_instance_types") as fetch:
            with self.assertLogs(fetcher.logger, level="WARNING"):
                self.assertEqual(fetcher.fetch(), [])
            fetch.assert_not_called()

    def test_success_and_errors_keep_safe_fetch_boundary(self):
        with patch.dict(os.environ, {api.API_KEY_ENV: KEY}, clear=True), patch.object(fetcher, "fetch_instance_types", return_value=payload()):
            self.assertTrue(fetcher.fetch()[0].fetched_at)
        errors = [RuntimeError("private key or body"), api.LambdaAPIError("private key or body", 403)]
        for error in errors:
            with patch.dict(os.environ, {api.API_KEY_ENV: KEY}, clear=True), patch.object(fetcher, "fetch_instance_types", side_effect=error):
                with self.assertLogs(fetcher.logger, level="ERROR") as logs:
                    self.assertEqual(fetcher.fetch(), [])
                message = " ".join(logs.output)
                self.assertNotIn("private", message)
                self.assertNotIn(KEY, message)
                if isinstance(error, api.LambdaAPIError):
                    self.assertIn("HTTP 403", message)

    def test_static_parser_diagnostic_is_logged_without_bad_value(self):
        with patch.dict(os.environ, {api.API_KEY_ENV: KEY}, clear=True), patch.object(fetcher, "fetch_instance_types", return_value=payload(item(count="private-body"))):
            with self.assertLogs(fetcher.logger, level="ERROR") as logs:
                self.assertEqual(fetcher.fetch(), [])
            self.assertIn("invalid GPU count", " ".join(logs.output))
            self.assertNotIn("private-body", " ".join(logs.output))
        self.assertEqual(str(fetcher.LambdaParseError("private-body")), "Lambda instance schema validation failed")


class LambdaSafeClientTests(unittest.TestCase):
    def test_fixed_current_host_bearer_get_without_body_or_redirects(self):
        with patch.dict(os.environ, {api.API_KEY_ENV: KEY}, clear=True), patch.object(api.urllib.request, "build_opener") as build:
            build.return_value.open.return_value = Response(b'{"data": {}}')
            self.assertEqual(api.fetch_instance_types(), {"data": {}})
            request = build.return_value.open.call_args.args[0]
            self.assertEqual((request.full_url, request.method, request.data),
                             ("https://cloud.lambda.ai/api/v1/instance-types", "GET", None))
            self.assertEqual(request.get_header("Authorization"), "Bearer " + KEY)
            self.assertEqual(build.return_value.open.call_args.kwargs["timeout"], 30)
            self.assertIs(build.call_args.args[0], api._NoRedirect)
        self.assertIsNone(api._NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example/"))

    def test_missing_or_invalid_key_never_calls_network(self):
        for key in ["", "bad\nprivate", "bad\tprivate", "nonascii-\u2603"]:
            with patch.dict(os.environ, {api.API_KEY_ENV: key}, clear=True), patch.object(api.urllib.request, "build_opener") as build:
                with self.assertRaises(api.LambdaAPIError) as raised:
                    api.fetch_instance_types()
                self.assertNotIn("private", str(raised.exception))
                build.assert_not_called()

    def test_http_status_survives_without_headers_body_or_secret(self):
        for status in [301, 401, 403, 500]:
            error = urllib.error.HTTPError(api.API_URL, status, "private-key", {}, io.BytesIO(b"private-body"))
            with patch.dict(os.environ, {api.API_KEY_ENV: KEY}, clear=True), patch.object(api.urllib.request, "build_opener") as build:
                build.return_value.open.side_effect = error
                with self.assertRaises(api.LambdaAPIError) as raised:
                    api.fetch_instance_types()
                self.assertEqual(raised.exception.http_status, status)
                self.assertNotIn("private", str(raised.exception))
                self.assertTrue(raised.exception.__suppress_context__)

    def test_transport_invalid_json_and_schema_fail_with_static_messages(self):
        for value in [urllib.error.URLError("private-body"), b"private-body", b"[]", b'{"data": []}', b'{"data": {}, "next_token": "private-token"}']:
            with patch.dict(os.environ, {api.API_KEY_ENV: KEY}, clear=True), patch.object(api.urllib.request, "build_opener") as build:
                if isinstance(value, Exception):
                    build.return_value.open.side_effect = value
                else:
                    build.return_value.open.return_value = Response(value)
                with self.assertRaises(api.LambdaAPIError) as raised:
                    api.fetch_instance_types()
                self.assertNotIn("private", str(raised.exception))

    def test_response_size_is_bounded(self):
        with patch.dict(os.environ, {api.API_KEY_ENV: KEY}, clear=True), patch.object(api.urllib.request, "build_opener") as build, patch.object(api, "_MAX_RESPONSE_BYTES", 4):
            build.return_value.open.return_value = Response(b"12345")
            with self.assertRaisesRegex(api.LambdaAPIError, "size limit"):
                api.fetch_instance_types()


if __name__ == "__main__":
    unittest.main()
