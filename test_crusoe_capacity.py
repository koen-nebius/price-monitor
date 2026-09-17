"""Offline Crusoe authentication, scope and exact-resource capacity tests."""
import base64
import hashlib
import hmac
import io
import json
import os
import unittest
import urllib.error
from unittest.mock import patch

import crusoe_api as api
from capacity.fetchers import crusoe

NOW = "2026-09-17T16:00:00+00:00"
ENV = {"CRUSOE_ACCESS_KEY_ID": "synthetic-access-key",
       "CRUSOE_SECRET_KEY": base64.urlsafe_b64encode(b"synthetic-test-secret").decode().rstrip("=")}


def item(sku="h100-80gb-sxm-ib.8x", location="us-east1-a", quantity=3, **extra):
    return {"type": sku, "location": location, "quantity": quantity,
            "num_slices": 8, "quota_type": "PROJECT_QUOTA_TYPE_H100", **extra}


class Response(io.BytesIO):
    status = 200


class CrusoeCapacityTests(unittest.TestCase):
    def setUp(self):
        # Exercise the unpaused client contract using synthetic credentials.
        pause = patch.object(api, "CAPACITY_ACCESS_PAUSED", False)
        pause.start()
        self.addCleanup(pause.stop)

    def test_exact_types_locations_and_raw_quantity_not_gpu_math(self):
        rows = crusoe.parse({"items": [item(), item("h100-80gb-sxm-ib.1x", quantity=24),
                                    item("h100-80gb-sxm-ib.8x", "eu-iceland1-a", 0)]}, NOW)
        self.assertEqual(len(rows), 3)
        self.assertEqual([(r.instance_type, r.region, r.metric_value) for r in rows], [
            ("h100-80gb-sxm-ib.8x", "us-east1-a", 3.0),
            ("h100-80gb-sxm-ib.1x", "us-east1-a", 24.0),
            ("h100-80gb-sxm-ib.8x", "eu-iceland1-a", 0.0)])
        self.assertEqual([r.state for r in rows], ["available", "available", "sold_out"])
        self.assertTrue(all(r.metric_type == "provider_quantity" and r.region != "global" for r in rows))
        self.assertIn("num_slices=8 per resource", rows[0].detail)
        self.assertIn("quota_type=PROJECT_QUOTA_TYPE_H100", rows[0].detail)
        self.assertIn("quota, reservation eligibility and multi-node stock not established", rows[0].detail)
        self.assertEqual((rows[0].source_url, rows[0].fetched_at, rows[0].parser_version),
                         (api.API_URL, NOW, "crusoe-capacity-2.1"))

    def test_supported_models_and_no_substring_confusion(self):
        skus = ["h100.1x", "h200.8x", "b200.8x", "b300.8x", "gb200.4x", "gb300.4x", "l40s.1x",
                "rtx-pro-6000-blackwell.1x", "gh200.1x", "h1000.1x", "a100.8x", "cpu.4x"]
        rows = crusoe.parse({"items": [item(sku) for sku in skus]})
        self.assertEqual([r.gpu_model for r in rows], ["H100", "H200", "B200", "B300", "GB200", "GB300", "L40S", "RTX6000"])
        self.assertEqual(crusoe.parse({"items": [{"location": "us-east1-a", "quantity": 1}]}), [])

    def test_missing_or_invalid_quantity_does_not_become_zero_or_available(self):
        for value in [None, True, -1, 1.5, "3", float("inf"), 2 ** 32, {}, []]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                crusoe.parse({"items": [item(quantity=value)]})
        record = item()
        del record["quantity"]
        with self.assertRaises(ValueError):
            crusoe.parse({"items": [record]})

    def test_invalid_identity_slices_and_quota_fail(self):
        for record in [item(location=""), item(location=None), item(sku="h100\nsecret"),
                       item(num_slices=-1), item(num_slices=True), item(quota_type={}),
                       item(quota_type="bad\nvalue"), None]:
            with self.subTest(record=record), self.assertRaises(ValueError):
                crusoe.parse({"items": [record]})

    def test_identical_duplicate_rows_are_one_observation(self):
        rows = crusoe.parse({"items": [item(), item(), item()]}, NOW)
        self.assertEqual(rows, crusoe.parse({"items": [item()]}, NOW))
        self.assertEqual((rows[0].state, rows[0].metric_value), ("available", 3.0))

    def test_conflicting_duplicates_are_unknown_and_order_independent(self):
        left = item(quantity=0, num_slices=1, quota_type="QUOTA_ONE")
        right = item(quantity=8, num_slices=8, quota_type="QUOTA_TWO")
        rows = crusoe.parse({"items": [left, right, right]}, NOW)
        reverse = crusoe.parse({"items": [right, left, left]}, NOW)
        self.assertEqual(rows, reverse)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].state, rows[0].metric_value), ("unknown", None))
        self.assertEqual((rows[0].instance_type, rows[0].region), (left["type"], left["location"]))
        for evidence in ["Ambiguous duplicate", "API quantity 0", "API quantity 8", "num_slices=1", "num_slices=8", "QUOTA_ONE", "QUOTA_TWO", "no aggregate quantity or stock verdict"]:
            self.assertIn(evidence, rows[0].detail)

    def test_same_quantity_with_conflicting_slice_or_quota_context_is_unknown(self):
        for changed in [item(num_slices=1), item(quota_type="DIFFERENT_QUOTA"), item(reservation_id="private-id")]:
            rows = crusoe.parse({"items": [item(), changed]})
            self.assertEqual((rows[0].state, rows[0].metric_value), ("unknown", None))
            self.assertNotIn("private-id", rows[0].detail)

    def test_reservation_context_never_becomes_open_availability(self):
        for field in ["reservation_id", "reservation", "reservation_specification", "reserved", "is_reserved", "requires_reservation"]:
            row = crusoe.parse({"items": [item(**{field: "synthetic-reservation"})]})[0]
            self.assertEqual(row.state, "unknown")
            self.assertNotIn("synthetic-reservation", row.detail)
        row = crusoe.parse({"items": [item(location="global")]})[0]
        self.assertNotEqual(row.region, "global")

    def test_schema_and_empty_response_do_not_infer_sellout(self):
        self.assertEqual(crusoe.parse({"items": []}), [])
        for payload in [None, {}, [], {"items": None}, {"items": [] , "next_page_token": "next"}]:
            with self.assertRaises(ValueError):
                crusoe.parse(payload)

    def test_docs_fallback_only_without_either_credential(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(crusoe, "_fetch_docs", return_value=["footprint"]) as docs, patch.object(crusoe, "fetch_capacities") as fetch:
            self.assertEqual(crusoe.fetch(), ["footprint"])
            docs.assert_called_once_with()
            fetch.assert_not_called()
        for env in [{"CRUSOE_ACCESS_KEY_ID": "only-access"}, {"CRUSOE_SECRET_KEY": "only-secret"}]:
            with patch.dict(os.environ, env, clear=True), patch.object(crusoe, "_fetch_docs") as docs, self.assertLogs(crusoe.logger, level="ERROR"):
                self.assertEqual(crusoe.fetch(), [])
                docs.assert_not_called()

    def test_api_failure_does_not_fallback_or_log_private_error(self):
        with patch.dict(os.environ, ENV, clear=True), patch.object(crusoe, "_fetch_docs") as docs, patch.object(crusoe, "fetch_capacities", side_effect=RuntimeError("private detail")):
            with self.assertLogs(crusoe.logger, level="ERROR") as logs:
                self.assertEqual(crusoe.fetch(), [])
            docs.assert_not_called()
            self.assertNotIn("private detail", " ".join(logs.output))

    def test_authenticated_success_uses_api_only(self):
        with patch.dict(os.environ, ENV, clear=True), patch.object(crusoe, "_fetch_docs") as docs, patch.object(crusoe, "fetch_capacities", return_value={"items": [item()]}):
            rows = crusoe.fetch()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].data_source, "official_api")
            docs.assert_not_called()

    def test_safe_http_status_survives_without_remote_message(self):
        with patch.dict(os.environ, ENV, clear=True), patch.object(crusoe, "fetch_capacities", side_effect=api.CrusoeAPIError("private error", http_status=403)):
            with self.assertLogs(crusoe.logger, level="ERROR") as logs:
                self.assertEqual(crusoe.fetch(), [])
            self.assertIn("HTTP 403", " ".join(logs.output))
            self.assertNotIn("private error", " ".join(logs.output))

    def test_parser_logs_static_reason_without_raw_values(self):
        with patch.dict(os.environ, ENV, clear=True), patch.object(crusoe, "fetch_capacities", return_value={"items": [item(quantity="private raw value")]}):
            with self.assertLogs(crusoe.logger, level="ERROR") as logs:
                self.assertEqual(crusoe.fetch(), [])
            self.assertIn("Crusoe capacity has invalid quantity", " ".join(logs.output))
            self.assertNotIn("private raw value", " ".join(logs.output))
        error = crusoe.CrusoeParseError("private unrecognized diagnostic")
        self.assertIsInstance(error, ValueError)
        self.assertEqual(str(error), "Crusoe capacity schema validation failed")

    def test_signature_empty_query_final_newline_and_get_only(self):
        expected = base64.urlsafe_b64encode(hmac.new(b"synthetic-test-secret", b"/v1/capacities\n\nGET\n2026-09-17T16:00:00+00:00\n", hashlib.sha256).digest()).decode().rstrip("=")
        headers = api._signed_headers(ENV[api.ACCESS_KEY_ENV], ENV[api.SECRET_KEY_ENV], NOW)
        self.assertEqual(headers["Authorization"], "Bearer 1.0:synthetic-access-key:" + expected)
        self.assertEqual(headers["X-Crusoe-Timestamp"], NOW)
        with patch.dict(os.environ, ENV, clear=True), patch.object(api.urllib.request, "build_opener") as build:
            build.return_value.open.return_value = Response(b'{"items": []}')
            self.assertEqual(api.fetch_capacities(), {"items": []})
            req = build.return_value.open.call_args.args[0]
            self.assertEqual((req.full_url, req.method, req.data), (api.API_URL, "GET", None))
            self.assertNotIn(".", req.get_header("X-crusoe-timestamp"))
            self.assertEqual(build.return_value.open.call_args.kwargs["timeout"], 30)
            self.assertIs(build.call_args.args[0], api._NoRedirect)

    def test_invalid_credentials_never_make_request(self):
        for env in [{}, {api.ACCESS_KEY_ENV: "partial"},
                    {api.ACCESS_KEY_ENV: "bad\nkey", api.SECRET_KEY_ENV: "YWJj"},
                    {api.ACCESS_KEY_ENV: "synthetic", api.SECRET_KEY_ENV: "invalid***"}]:
            with patch.dict(os.environ, env, clear=True), patch.object(api.urllib.request, "build_opener") as build:
                with self.assertRaises(api.CrusoeAPIError):
                    api.fetch_capacities()
                build.assert_not_called()

    def test_redirects_errors_and_remote_content_are_not_exposed(self):
        self.assertIsNone(api._NoRedirect().redirect_request(None, None, 302, "x", {}, "https://other.invalid"))
        errors = [urllib.error.HTTPError("https://secret.invalid", 403, "secret message", {}, None),
                  urllib.error.URLError("secret transport"), TimeoutError("secret timeout")]
        for error in errors:
            with patch.dict(os.environ, ENV, clear=True), patch.object(api.urllib.request, "build_opener") as build:
                build.return_value.open.side_effect = error
                with self.assertRaises(api.CrusoeAPIError) as raised:
                    api.fetch_capacities()
                self.assertNotIn("secret", str(raised.exception))

    def test_invalid_oversize_or_paginated_response_fails(self):
        for raw in [b"not JSON", b'{"items": {}}', b'{"items": [], "next_token": "x"}', b" " * (api._MAX_RESPONSE_BYTES + 1)]:
            with patch.dict(os.environ, ENV, clear=True), patch.object(api.urllib.request, "build_opener") as build:
                build.return_value.open.return_value = Response(raw)
                with self.assertRaises(api.CrusoeAPIError):
                    api.fetch_capacities()


if __name__ == "__main__":
    unittest.main()
