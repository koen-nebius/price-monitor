"""Offline tests for exact Together inference scope and read-only discovery."""
import io
import json
import os
import unittest
import urllib.error
from unittest.mock import patch

import together_capacity_api as api
from capacity.fetchers import together
from capacity.schema import AvailabilityRecord

NOW = "2026-09-17T18:00:00+00:00"
ENV = {"TOGETHER_API_KEY": "synthetic-test-token"}


def region(name="us-east-1", value=5, relation="RELATION_GTE"):
    return {"name": name, "headroom": {"value": value, "relation": relation}}


def item(instance="ins_eight", count=8, gpu="H100", regions=None):
    return {"id": instance, "gpuType": gpu, "gpuCount": count,
            "regions": [region()] if regions is None else regions}


class Response(io.BytesIO):
    status = 200


class TogetherCapacityTests(unittest.TestCase):
    def test_preserves_each_shape_and_region_without_max_or_global_rows(self):
        payload = {"data": [
            item("ins_one", 1, regions=[region(value=20, relation="RELATION_EQ")]),
            item("ins_eight", 8, regions=[region(value=1, relation="RELATION_EQ"),
                                          region("eu-west-1", 0, "RELATION_EQ")]),
        ]}
        rows = together.parse(payload, NOW)
        self.assertEqual([(r.instance_type, r.gpu_count, r.region, r.metric_value, r.state)
                          for r in rows], [
            ("ins_eight", 8, "eu-west-1", 0.0, "sold_out"),
            ("ins_eight", 8, "us-east-1", 1.0, "limited"),
            ("ins_one", 1, "us-east-1", 20.0, "available"),
        ])
        for row in rows:
            self.assertEqual((row.metric_type, row.product_scope, row.data_source),
                             ("inference_replicas", "dedicated_inference", "official_api"))
            self.assertEqual((row.source_url, row.fetched_at, row.parser_version),
                             (api.API_URL, NOW, together.PARSER_VERSION))
            self.assertIn("not GPU-cluster capacity", row.detail)
            self.assertNotEqual(row.region, "global")

    def test_exact_small_positive_remains_limited(self):
        for quantity, state in [(0, "sold_out"), (1, "limited"), (2, "limited"), (3, "available")]:
            row = together.parse({"data": [item(regions=[region(value=quantity, relation="RELATION_EQ")])]})[0]
            self.assertEqual((row.state, row.metric_value, row.quantity_relation),
                             (state, float(quantity), "RELATION_EQ"))

    def test_lower_bound_is_not_exact_stock_or_gpu_math(self):
        for quantity in [1, 2, 5]:
            row = together.parse({"data": [item(regions=[region(value=quantity)])]})[0]
            self.assertEqual((row.state, row.metric_value, row.quantity_relation),
                             ("available", float(quantity), "RELATION_GTE"))
            self.assertIn("headroom ≥", row.detail)
            self.assertEqual(row.gpu_count, 8)

    def test_unknown_headroom_and_gte_zero_are_not_zero_stock(self):
        heads = [None, {}, {"value": 0}, {"value": 2},
                 {"value": 0, "relation": "RELATION_GTE"},
                 {"value": 0, "relation": "RELATION_UNKNOWN"},
                 {"relation": "RELATION_EQ"}]
        for head in heads:
            with self.subTest(head=head):
                row = together.parse({"data": [item(regions=[{"name": "us-east-1", "headroom": head}])]})[0]
                self.assertEqual((row.state, row.metric_value), ("unknown", None))
        row = together.parse({"data": [item(regions=[{"name": "us-east-1"}])]})[0]
        self.assertEqual((row.state, row.metric_value), ("unknown", None))

    def test_duplicate_headroom_deduplicates_and_conflicts_are_order_invariant(self):
        eq_zero = item(regions=[region(value=0, relation="RELATION_EQ")])
        gte_five = item()
        self.assertEqual(together.parse({"data": [gte_five, gte_five]}, NOW),
                         together.parse({"data": [gte_five]}, NOW))
        a = together.parse({"data": [eq_zero, gte_five, eq_zero]}, NOW)
        b = together.parse({"data": [gte_five, eq_zero, gte_five]}, NOW)
        self.assertEqual(a, b)
        self.assertEqual(len(a), 1)
        self.assertEqual((a[0].state, a[0].metric_value, a[0].quantity_relation),
                         ("unknown", None, ""))
        self.assertIn("RELATION_EQ:0; RELATION_GTE:5", a[0].detail)

    def test_missing_headroom_duplicates_are_order_invariant(self):
        rows = [item(regions=[{"name": "us-east-1"}]),
                item(regions=[{"name": "us-east-1", "headroom": {}}])]
        self.assertEqual(together.parse({"data": rows}), together.parse({"data": list(reversed(rows))}))

    def test_order_of_instances_and_regions_does_not_change_output(self):
        left = item("ins_one", 1, regions=[region(), region("eu-west-1")])
        right = item("ins_eight", 8)
        shuffled = dict(left, regions=list(reversed(left["regions"])))
        self.assertEqual(together.parse({"data": [left, right]}),
                         together.parse({"data": [right, shuffled]}))

    def test_gpu_matching_does_not_confuse_substrings(self):
        models = ["GB300", "GB200", "B300", "B200", "H200", "NVIDIA H100 80GB",
                  "L40S", "NVIDIA RTX PRO 6000 Blackwell", "GH200", "H1000", "RTX 6000 Ada", "A100"]
        rows = together.parse({"data": [item("ins_%02d" % i, gpu=model) for i, model in enumerate(models)]})
        self.assertEqual([r.gpu_model for r in rows],
                         ["GB300", "GB200", "B300", "B200", "H200", "H100", "L40S", "RTX6000"])

    def test_invalid_count_or_conflicting_instance_identity_fails(self):
        for count in [None, True, 0, -1, 1.5, "8", 2 ** 31, {}]:
            with self.subTest(count=count), self.assertRaises(together.TogetherParseError):
                together.parse({"data": [item(count=count)]})
        for changed in [item(count=1), item(gpu="H200")]:
            with self.assertRaisesRegex(together.TogetherParseError, "configuration conflicts"):
                together.parse({"data": [item(), changed]})

    def test_invalid_quantity_or_relation_fails_with_static_message(self):
        for value in [True, -1, 1.5, "secret-value", float("inf"), 2 ** 53, [], {}]:
            with self.subTest(value=value), self.assertRaisesRegex(together.TogetherParseError, "invalid headroom value"):
                together.parse({"data": [item(regions=[region(value=value)])]})
        for relation in [True, [], {}, "private\nvalue"]:
            with self.subTest(relation=relation), self.assertRaisesRegex(together.TogetherParseError, "invalid headroom relation"):
                together.parse({"data": [item(regions=[region(relation=relation)])]})

    def test_invalid_structure_or_region_does_not_infer_capacity(self):
        bad = [None, [], {}, {"data": None}, {"data": [None]},
               {"data": [], "next_page_token": "secret-page"},
               {"data": [dict(item(), id="bad\nsecret")]},
               {"data": [dict(item(), gpuType=None)]},
               {"data": [dict(item(), regions=None)]},
               {"data": [item(regions=[None])]},
               {"data": [item(regions=[region(name="global")])]},
               {"data": [item(regions=[region(name="")])]},
               {"data": [item(regions=[{"name": "us-east-1", "headroom": []}])]}]
        for payload in bad:
            with self.subTest(payload=payload), self.assertRaises(together.TogetherParseError):
                together.parse(payload)
        self.assertEqual(together.parse({"data": []}), [])
        self.assertEqual(together.parse({"data": [item(regions=[])]}), [])

    def test_new_fields_roundtrip_and_legacy_record_defaults(self):
        row = together.parse({"data": [item()]}, NOW)[0]
        self.assertEqual(AvailabilityRecord.from_dict(row.to_dict()), row)
        legacy = row.to_dict()
        for key in ["gpu_count", "product_scope", "quantity_relation"]:
            del legacy[key]
        old = AvailabilityRecord.from_dict(legacy)
        self.assertEqual((old.gpu_count, old.product_scope, old.quantity_relation), (None, "", ""))

    def test_no_key_skips_without_network(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(together, "fetch_instance_types") as fetch:
            with self.assertLogs(together.logger, level="WARNING"):
                self.assertEqual(together.fetch(), [])
            fetch.assert_not_called()

    def test_fetch_success_retains_scope_and_fetch_time(self):
        with patch.dict(os.environ, ENV, clear=True), patch.object(together, "fetch_instance_types", return_value={"data": [item()]}):
            row = together.fetch()[0]
            self.assertEqual(row.product_scope, "dedicated_inference")
            self.assertTrue(row.fetched_at)

    def test_fetch_errors_never_log_private_content(self):
        for error in [RuntimeError("private token and response"),
                      api.TogetherAPIError("private token and response", http_status=403)]:
            with patch.dict(os.environ, ENV, clear=True), patch.object(together, "fetch_instance_types", side_effect=error):
                with self.assertLogs(together.logger, level="ERROR") as logs:
                    self.assertEqual(together.fetch(), [])
                message = " ".join(logs.output)
                self.assertNotIn("private token", message)
                self.assertNotIn(ENV[api.API_KEY_ENV], message)
                if getattr(error, "http_status", None):
                    self.assertIn("HTTP 403", message)

    def test_parse_error_logs_safe_static_reason(self):
        with patch.dict(os.environ, ENV, clear=True), patch.object(together, "fetch_instance_types", return_value={"data": [item(count="private-value")]}):
            with self.assertLogs(together.logger, level="ERROR") as logs:
                self.assertEqual(together.fetch(), [])
            message = " ".join(logs.output)
            self.assertIn("invalid GPU count", message)
            self.assertNotIn("private-value", message)
        self.assertEqual(str(together.TogetherParseError("private-value")),
                         "Together inference schema validation failed")


class TogetherReadOnlyClientTests(unittest.TestCase):
    def test_only_two_fixed_get_endpoints_no_body_or_redirects(self):
        for fetch, url, payload in [(api.fetch_instance_types, api.API_URL, {"data": []}),
                                    (api.fetch_cluster_regions, api.CLUSTER_REGIONS_URL, {"regions": []})]:
            with patch.dict(os.environ, ENV, clear=True), patch.object(api.urllib.request, "build_opener") as build:
                build.return_value.open.return_value = Response(json.dumps(payload).encode())
                self.assertEqual(fetch(), payload)
                request = build.return_value.open.call_args.args[0]
                self.assertEqual((request.full_url, request.method, request.data), (url, "GET", None))
                self.assertEqual(build.return_value.open.call_args.kwargs["timeout"], 30)
                self.assertIs(build.call_args.args[0], api._NoRedirect)
                self.assertEqual(request.get_header("Authorization"), "Bearer " + ENV[api.API_KEY_ENV])
        self.assertIsNone(api._NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example/"))

    def test_arbitrary_url_is_rejected_before_auth_or_network(self):
        with patch.object(api.urllib.request, "build_opener") as build:
            with self.assertRaises(api.TogetherAPIError):
                api._get_json("https://other.example/")
            build.assert_not_called()

    def test_invalid_keys_never_reach_network(self):
        for key in ["", "abc\nsecret", "abc\tsecret", "unicode-\u2603"]:
            with patch.dict(os.environ, {api.API_KEY_ENV: key}, clear=True), patch.object(api.urllib.request, "build_opener") as build:
                with self.assertRaises(api.TogetherAPIError) as raised:
                    api.fetch_instance_types()
                build.assert_not_called()
                self.assertNotIn("secret", str(raised.exception))

    def test_http_status_kept_without_remote_body_or_key(self):
        for status in [301, 401, 403, 500]:
            error = urllib.error.HTTPError(api.API_URL, status, "private-token", {}, io.BytesIO(b"private-body"))
            with patch.dict(os.environ, ENV, clear=True), patch.object(api.urllib.request, "build_opener") as build:
                build.return_value.open.side_effect = error
                with self.assertRaises(api.TogetherAPIError) as raised:
                    api.fetch_instance_types()
                self.assertEqual(raised.exception.http_status, status)
                self.assertNotIn("private", str(raised.exception))
                self.assertTrue(raised.exception.__suppress_context__)

    def test_transport_and_json_failures_are_sanitized(self):
        for value in [urllib.error.URLError("private-host-secret"), b"private invalid json"]:
            with patch.dict(os.environ, ENV, clear=True), patch.object(api.urllib.request, "build_opener") as build:
                if isinstance(value, Exception):
                    build.return_value.open.side_effect = value
                else:
                    build.return_value.open.return_value = Response(value)
                with self.assertRaises(api.TogetherAPIError) as raised:
                    api.fetch_instance_types()
                self.assertNotIn("private", str(raised.exception))

    def test_response_structure_pagination_and_size_are_bounded(self):
        for payload in [[], {}, {"data": None}, {"data": [], "next": "private-token"}]:
            with patch.dict(os.environ, ENV, clear=True), patch.object(api.urllib.request, "build_opener") as build:
                build.return_value.open.return_value = Response(json.dumps(payload).encode())
                with self.assertRaises(api.TogetherAPIError):
                    api.fetch_instance_types()
        with patch.dict(os.environ, ENV, clear=True), patch.object(api.urllib.request, "build_opener") as build, patch.object(api, "_MAX_RESPONSE_BYTES", 4):
            build.return_value.open.return_value = Response(b"12345")
            with self.assertRaisesRegex(api.TogetherAPIError, "size limit"):
                api.fetch_instance_types()

    def test_cluster_region_catalog_is_not_parsed_as_capacity(self):
        payload = {"regions": [{"name": "us-east-1", "supported_instance_types": ["8xH100"], "driver_versions": []}]}
        with patch.dict(os.environ, ENV, clear=True), patch.object(api.urllib.request, "build_opener") as build:
            build.return_value.open.return_value = Response(json.dumps(payload).encode())
            result = api.fetch_cluster_regions()
        self.assertEqual(result, payload)
        with self.assertRaises(together.TogetherParseError):
            together.parse(result)


if __name__ == "__main__":
    unittest.main()
