"""Offline MCP transport checks: read-only requests, bounded parsing, no secrets."""
import io
import json
import os
import unittest
import urllib.error
from unittest.mock import patch

import massedcompute_api as api

PAYLOAD = {"gpu_inventory": {"synthetic-sku": {"capacity_available": 0}}}
SYNTHETIC_KEY = "synthetic-offline-only-key"


def envelope(result, request_id=2):
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


class Response(io.BytesIO):
    def __init__(self, message=None, *, raw=None, status=200, headers=None):
        if raw is None:
            raw = json.dumps(message).encode() if message is not None else b""
        super().__init__(raw)
        self.status = status
        self.headers = {"Content-Type": "application/json", **(headers or {})}


class Opener:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


class MassedAPITests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {api.SECRET_NAME: SYNTHETIC_KEY}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def run_client(self, final_response):
        opener = Opener([
            Response(envelope({"protocolVersion": "2025-03-26"}, 1),
                     headers={"Mcp-Session-Id": "synthetic-session"}),
            Response(status=202), final_response,
        ])
        with patch.object(api.urllib.request, "build_opener", return_value=opener) as build:
            result = api.fetch_inventory()
        self.assertIs(build.call_args.args[0], api._NoRedirect)
        return result, opener

    def test_fixed_endpoint_and_inventory_only_tool_call(self):
        result, opener = self.run_client(Response(envelope({"structuredContent": PAYLOAD})))
        self.assertEqual(result, PAYLOAD)
        bodies = [json.loads(r.data) for r in opener.requests]
        self.assertEqual([b["method"] for b in bodies],
                         ["initialize", "notifications/initialized", "tools/call"])
        self.assertEqual(bodies[-1]["params"], {"name": "gpu_inventory_list", "arguments": {}})
        for request in opener.requests:
            self.assertEqual(request.full_url, api.API_URL)
            self.assertEqual(request.get_header("Authorization"), "Bearer " + SYNTHETIC_KEY)
            self.assertNotIn(SYNTHETIC_KEY, request.data.decode())
        self.assertEqual(opener.requests[-1].get_header("Mcp-session-id"), "synthetic-session")
        self.assertIsNone(api._NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere.invalid"))

    def test_missing_key_does_not_open_connection(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(api.urllib.request, "build_opener") as build:
                with self.assertRaises(api.MassedAPIError):
                    api.fetch_inventory()
                build.assert_not_called()

    def test_sse_skips_other_ids_and_parses_inventory_text(self):
        unrelated = json.dumps(envelope({}, 99))
        matched = json.dumps(envelope({"content": [{"type": "text", "text": json.dumps(PAYLOAD)}]}))
        raw = ("event: message\ndata: " + unrelated + "\n\n"
               "data: " + matched + "\n\n").encode()
        result, _ = self.run_client(Response(raw=raw, headers={"Content-Type": "text/event-stream"}))
        self.assertEqual(result, PAYLOAD)

    def test_wrong_ids_bad_types_and_rpc_errors_are_sanitized(self):
        messages = [
            [], envelope(None), envelope([]), envelope({}, 7), envelope({}, True),
            {"id": 2, "result": {}},
            {"jsonrpc": "2.0", "id": 2, "error": {"message": SYNTHETIC_KEY}},
            envelope({"content": [SYNTHETIC_KEY]}), envelope({"content": {}}),
            envelope({"isError": True, "content": [{"text": SYNTHETIC_KEY}]}),
            envelope({"structuredContent": {"gpu_inventory": []}}),
            envelope({"structuredContent": {"gpu_inventory": {}}}),
        ]
        for message in messages:
            with self.subTest(message_type=type(message).__name__):
                with self.assertRaises(api.MassedAPIError) as caught:
                    self.run_client(Response(message))
                self.assertNotIn(SYNTHETIC_KEY, str(caught.exception))

    def test_http_and_transport_errors_do_not_expose_urls_or_remote_messages(self):
        errors = [
            urllib.error.HTTPError("https://elsewhere.invalid/" + SYNTHETIC_KEY, 302,
                                   SYNTHETIC_KEY, {}, None),
            urllib.error.HTTPError(api.API_URL, 401, SYNTHETIC_KEY, {}, None),
            urllib.error.URLError(SYNTHETIC_KEY), OSError(SYNTHETIC_KEY),
        ]
        for error in errors:
            with self.assertRaises(api.MassedAPIError) as caught:
                self.run_client(error)
            self.assertNotIn(SYNTHETIC_KEY, str(caught.exception))

    def test_json_and_sse_responses_have_size_limits(self):
        for headers in ({}, {"Content-Type": "text/event-stream"}):
            with patch.object(api, "MAX_RESPONSE_BYTES", 128):
                with self.assertRaises(api.MassedAPIError):
                    self.run_client(Response(raw=b":" + b"x" * 129, headers=headers))

    def test_sse_timeout_and_invalid_messages_are_sanitized(self):
        with patch.object(api.time, "monotonic", side_effect=[0, 100]):
            with self.assertRaisesRegex(api.MassedAPIError, "time limit"):
                self.run_client(Response(raw=b": keepalive\n", headers={"Content-Type": "text/event-stream"}))
        for raw in (b"data: []\n\n", b"data: not-json\n\n", b": keepalive\n\n"):
            with self.assertRaises(api.MassedAPIError):
                self.run_client(Response(raw=raw, headers={"Content-Type": "text/event-stream"}))

    def test_request_requires_response_not_notification_ack(self):
        with self.assertRaisesRegex(api.MassedAPIError, "no response"):
            self.run_client(Response(status=202))


if __name__ == "__main__":
    unittest.main()
