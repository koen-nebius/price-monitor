"""A suspension hold makes zero requests and does not imply zero capacity."""
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import crusoe_api
from capacity import main, render, store
from capacity.fetchers import crusoe
from capacity.schema import AvailabilityRecord
from scripts import check_crusoe


class CrusoePauseTests(unittest.TestCase):
    def setUp(self):
        hold = patch.object(crusoe_api, "CAPACITY_ACCESS_PAUSED", True)
        hold.start()
        self.addCleanup(hold.stop)

    def test_client_blocks_before_credentials_signing_or_network(self):
        with patch.dict(os.environ, {"CRUSOE_ACCESS_KEY_ID": "synthetic", "CRUSOE_SECRET_KEY": "YWJj"}), \
             patch.object(crusoe_api, "_signed_headers") as sign, \
             patch.object(crusoe_api.urllib.request, "build_opener") as network:
            with self.assertRaisesRegex(crusoe_api.CrusoeAPIError, "Organization suspended"):
                crusoe_api.fetch_capacities()
        sign.assert_not_called()
        network.assert_not_called()

    def test_fetcher_does_not_request_api_or_docs_with_or_without_keys(self):
        for env in [{}, {"CRUSOE_ACCESS_KEY_ID": "synthetic", "CRUSOE_SECRET_KEY": "YWJj"}]:
            with self.subTest(configured=bool(env)), patch.dict(os.environ, env, clear=True), \
                 patch.object(crusoe, "fetch_capacities") as fetch, \
                 patch.object(crusoe, "_fetch_docs") as docs:
                self.assertEqual(crusoe.fetch(), [])
                fetch.assert_not_called()
                docs.assert_not_called()

    def test_verifier_reports_paused_without_a_live_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            summary = Path(directory) / "summary.md"
            output = io.StringIO()
            with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}, clear=True), \
                 patch.object(check_crusoe, "fetch_capacities") as fetch, \
                 patch.object(check_crusoe, "_fetch_provider") as dispatcher, \
                 contextlib.redirect_stdout(output):
                self.assertEqual(check_crusoe.main(), 0)
            fetch.assert_not_called()
            dispatcher.assert_not_called()
            for text in [output.getvalue(), summary.read_text()]:
                self.assertIn("PAUSED", text)
                self.assertIn("No provider request made", text)
                self.assertIn("availability unknown", text)

    def test_pipeline_skips_cached_capacity_and_false_removed_events(self):
        old = AvailabilityRecord(
            "crusoe", "H100", "us-east1-a", "on_demand", "available",
            "provider_quantity", 8, instance_type="h100.8x", data_source="official_api",
            fetched_at="2026-09-17T13:00:00+00:00")
        with patch.object(main, "_fetch_provider") as fetch, \
             patch.object(store, "get_cached_records") as cache, \
             patch.object(store, "update_peer_cache") as update, \
             patch.object(store, "load_last_snapshot", return_value=[old]), \
             patch.object(main, "write_artifacts") as artifacts:
            manifest = main.run(providers=["crusoe"], test=True)
        fetch.assert_not_called()
        cache.assert_not_called()
        update.assert_not_called()
        self.assertEqual(manifest["provider_status"]["crusoe"]["status"], "paused")
        self.assertEqual(manifest["paused_providers"], ["crusoe"])
        self.assertEqual(manifest["failed_providers"], [])
        self.assertEqual(manifest["stale_providers"], [])
        self.assertEqual(manifest["record_count"], 0)
        self.assertEqual(manifest["diff_count"], 0)
        records, changes = artifacts.call_args.args[:2]
        self.assertEqual((records, changes), ([], []))
        page = render.render_confluence(records, changes, manifest, [])
        slack = "\n".join(render.render_slack(records, changes, manifest, []))
        for text in [page, slack]:
            self.assertIn("Crusoe", text)
            self.assertIn("paused", text.lower())
            self.assertIn("unknown", text.lower())


if __name__ == "__main__":
    unittest.main()
