"""Production cache selection with isolated disk state and no network/posts."""
import csv
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from capacity import main, store
from capacity.schema import AvailabilityRecord


def observation(source="official_api", age_hours=2):
    api = source == "official_api"
    return AvailabilityRecord(
        "crusoe", "H100", "us-east1-a" if api else "global", "on_demand",
        "available", "provider_quantity" if api else "listed_offering", 3.0,
        instance_type="h100.8x" if api else "",
        data_source=source,
        fetched_at=(datetime.now(timezone.utc) - timedelta(hours=age_hours)).isoformat(),
    )


class CrusoePipelineTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.directory = Path(directory)
        self.stack.enter_context(patch.object(store, "STORE_DIR", self.directory))
        for name, filename in [
            ("PEER_CACHE_FILE", "peer_cache.json"),
            ("LAST_SNAPSHOT_FILE", "last_snapshot.json"),
            ("MANIFEST_FILE", "run_manifest.json"),
            ("HISTORY_FILE", "history.csv"),
        ]:
            self.stack.enter_context(patch.object(store, name, self.directory / filename))
        self.fetch = self.stack.enter_context(patch.object(main, "_fetch_provider", return_value=[]))
        self.artifacts = self.stack.enter_context(patch.object(main, "write_artifacts"))

    def seed_cache(self, records, age_hours=2):
        store.PEER_CACHE_FILE.write_text(json.dumps({"crusoe": {
            "fetched_at": (datetime.now(timezone.utc) - timedelta(hours=age_hours)).isoformat(),
            "records": [r.to_dict() for r in records],
        }}))

    def run_pipeline(self, env):
        with patch.dict(os.environ, env, clear=True), self.assertLogs(level="INFO"):
            return main.run(providers=["crusoe"], test=False)

    def test_configured_api_failure_rejects_cached_documentation(self):
        self.seed_cache([observation("web_scrape")])
        manifest = self.run_pipeline({"CRUSOE_ACCESS_KEY_ID": "synthetic", "CRUSOE_SECRET_KEY": "synthetic"})
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["provider_status"]["crusoe"], {"status": "failed", "record_count": 0})
        self.assertEqual(manifest["failed_providers"], ["crusoe"])
        self.assertEqual(manifest["stale_providers"], [])
        self.assertEqual(self.artifacts.call_args.args[0], [])
        self.assertEqual(store.load_last_snapshot(), [])

    def test_partial_credentials_also_reject_docs_cache(self):
        for env in [{"CRUSOE_ACCESS_KEY_ID": "partial"}, {"CRUSOE_SECRET_KEY": "partial"}]:
            with self.subTest(env_keys=list(env)):
                self.seed_cache([observation("web_scrape")])
                manifest = self.run_pipeline(env)
                self.assertEqual(manifest["provider_status"]["crusoe"]["status"], "failed")
                self.assertEqual(self.artifacts.call_args.args[0], [])

    def test_api_cache_is_explicitly_stale_and_keeps_original_observed_time(self):
        api, docs = observation(age_hours=23), observation("web_scrape")
        self.seed_cache([docs, api], age_hours=23)
        cache_before = store.PEER_CACHE_FILE.read_text()
        manifest = self.run_pipeline({"CRUSOE_ACCESS_KEY_ID": "synthetic", "CRUSOE_SECRET_KEY": "synthetic"})
        self.assertEqual(manifest["stale_providers"], ["crusoe"])
        self.assertEqual(manifest["live_provider_count"], 0)
        status = manifest["provider_status"]["crusoe"]
        self.assertEqual((status["status"], status["record_count"]), ("cached", 1))
        self.assertAlmostEqual(status["cache_age_hours"], 23, places=1)
        self.assertEqual(self.artifacts.call_args.args[0], [api])
        self.assertEqual(store.load_last_snapshot()[0].fetched_at, api.fetched_at)
        self.assertEqual(store.PEER_CACHE_FILE.read_text(), cache_before)
        with store.HISTORY_FILE.open() as handle:
            history = list(csv.DictReader(handle))
        self.assertEqual(len(history), 1)
        self.assertEqual((history[0]["instance_type"], history[0]["fetched_at"]), ("h100.8x", api.fetched_at))

    def test_expired_api_cache_is_not_republished(self):
        self.seed_cache([observation(age_hours=49)], age_hours=49)
        manifest = self.run_pipeline({"CRUSOE_ACCESS_KEY_ID": "synthetic", "CRUSOE_SECRET_KEY": "synthetic"})
        self.assertEqual(manifest["provider_status"]["crusoe"]["status"], "failed")
        self.assertEqual(self.artifacts.call_args.args[0], [])

    def test_no_credentials_preserves_labelled_legacy_footprint_cache(self):
        docs = observation("web_scrape")
        self.seed_cache([docs])
        manifest = self.run_pipeline({})
        self.assertEqual(manifest["provider_status"]["crusoe"]["status"], "cached")
        rows = self.artifacts.call_args.args[0]
        self.assertEqual((rows[0].metric_type, rows[0].data_source), ("listed_offering", "web_scrape"))

    def test_success_updates_cache_with_exact_api_observations(self):
        api = observation(age_hours=0)
        self.seed_cache([observation("web_scrape")])
        self.fetch.return_value = [api]
        manifest = self.run_pipeline({"CRUSOE_ACCESS_KEY_ID": "synthetic", "CRUSOE_SECRET_KEY": "synthetic"})
        self.assertEqual(manifest["provider_status"]["crusoe"], {"status": "live", "record_count": 1})
        self.assertEqual(manifest["stale_providers"], [])
        cached, age = store.get_cached_records("crusoe")
        self.assertEqual(cached, [api])
        self.assertLess(age, .01)
        self.assertEqual(self.artifacts.call_args.args[0], [api])


if __name__ == "__main__":
    unittest.main()
