import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from market_events.registry import SourceRegistry, SourceSpec
from market_events.tavily import TavilyAdapter, TavilyConfig


ROOT = Path(__file__).resolve().parents[1]


class RegistryAndTavilyTests(unittest.TestCase):
    def test_registry_has_fifteen_public_official_sources(self):
        registry = SourceRegistry.load(ROOT / "config" / "source_registry.json")
        self.assertEqual(len(registry), 15)
        self.assertTrue(all(source.public_only for source in registry.all()))
        self.assertTrue(all(source.official_domains for source in registry.all()))

    def test_registry_rejects_internal_sources(self):
        with self.assertRaisesRegex(ValueError, "forbidden internal"):
            SourceSpec(
                source_id="bad",
                provider="Bad",
                official_domains=("nebius.atlassian.net",),
                known_urls=("https://nebius.atlassian.net/wiki/example",),
            ).validate()

    def test_config_rejects_embedded_api_key(self):
        with self.assertRaisesRegex(ValueError, "environment"):
            TavilyConfig.from_dict({"api_key": "secret"})

    def test_search_is_restricted_to_registered_query_and_domain(self):
        registry = SourceRegistry.load(ROOT / "config" / "source_registry.json")
        source = registry.get("digitalocean_official")
        calls = []

        def transport(endpoint, payload, headers, timeout):
            calls.append((endpoint, payload, headers, timeout))
            return {
                "request_id": "request-1",
                "results": [
                    {
                        "url": "https://www.digitalocean.com/blog/price-changes-gpus",
                        "title": "GPU pricing",
                        "content": "H100 PAYG $4.41 per GPU hour",
                    }
                ],
            }

        adapter = TavilyAdapter(transport=transport)
        with patch.dict(os.environ, {"TAVILY_API_KEY": "test-only"}):
            documents = adapter.search(source, source.discovery_queries[0])
        self.assertEqual(len(documents), 1)
        endpoint, payload, headers, _ = calls[0]
        self.assertEqual(endpoint, "search")
        self.assertEqual(payload["include_domains"], ["digitalocean.com"])
        self.assertFalse(payload["include_answer"])
        self.assertNotIn("safe_search", payload)
        self.assertNotIn("test-only", json.dumps(payload))
        self.assertTrue(headers["Authorization"].startswith("Bearer "))

        with self.assertRaisesRegex(ValueError, "pre-registered"):
            adapter.search(source, "arbitrary customer quote")

    def test_extract_and_crawl_cannot_leave_official_domain(self):
        registry = SourceRegistry.load(ROOT / "config" / "source_registry.json")
        source = registry.get("digitalocean_official")
        calls = []

        def transport(endpoint, payload, headers, timeout):
            calls.append((endpoint, payload))
            return {"results": [], "request_id": "request-2"}

        adapter = TavilyAdapter(transport=transport)
        with patch.dict(os.environ, {"TAVILY_API_KEY": "test-only"}):
            with self.assertRaisesRegex(ValueError, "not in official_domains"):
                adapter.extract(source, ["https://nebius.atlassian.net/wiki/example"])
            adapter.crawl(source, source.crawl_roots[0])

        endpoint, payload = calls[-1]
        self.assertEqual(endpoint, "crawl")
        self.assertFalse(payload["allow_external"])
        self.assertEqual(payload["select_domains"], ["digitalocean.com"])


if __name__ == "__main__":
    unittest.main()
