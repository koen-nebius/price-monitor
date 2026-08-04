"""Small Tavily REST adapter with hard official-public boundaries.

The adapter supports Search, Extract and Crawl, but accepts only registered
public queries and official-domain URLs. It never reads internal evidence.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from .models import stable_hash
from .registry import SourceSpec, assert_public_text


@dataclass(frozen=True, kw_only=True)
class TavilyConfig:
    api_key_env: str = "TAVILY_API_KEY"
    api_base: str = "https://api.tavily.com"
    timeout_seconds: int = 60
    search_depth: str = "basic"
    extract_depth: str = "basic"
    max_results: int = 10
    crawl_max_depth: int = 1
    crawl_max_breadth: int = 20
    crawl_limit: int = 30

    @classmethod
    def from_dict(cls, data: dict) -> "TavilyConfig":
        if "api_key" in data:
            raise ValueError("API keys must come from the environment, not config files")
        result = cls(**data)
        if not result.api_base.startswith("https://api.tavily.com"):
            raise ValueError("api_base must use the Tavily HTTPS API")
        return result


@dataclass(frozen=True, kw_only=True)
class RawDocument:
    source_id: str
    url: str
    title: str
    content: str
    collected_at: str
    operation: str
    request_id: str = ""

    def content_hash(self) -> str:
        return stable_hash({"source_id": self.source_id, "url": self.url, "content": self.content})

    def to_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "url": self.url,
            "title": self.title,
            "content": self.content,
            "collected_at": self.collected_at,
            "operation": self.operation,
            "request_id": self.request_id,
            "raw_document_hash": self.content_hash(),
        }


Transport = Callable[[str, dict, dict, int], dict]


class TavilyAdapter:
    def __init__(self, config: TavilyConfig | None = None, transport: Transport | None = None):
        self.config = config or TavilyConfig()
        self._transport = transport or self._urllib_transport

    def _key(self) -> str:
        key = os.environ.get(self.config.api_key_env, "")
        if not key:
            raise RuntimeError(
                f"{self.config.api_key_env} is required only for an executed Tavily run"
            )
        return key

    def _urllib_transport(self, endpoint: str, payload: dict, headers: dict, timeout: int) -> dict:
        request = urllib.request.Request(
            f"{self.config.api_base.rstrip('/')}/{endpoint}",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Tavily {endpoint} failed with HTTP {exc.code}") from exc

    def _post(self, endpoint: str, payload: dict) -> dict:
        key = self._key()
        return self._transport(
            endpoint,
            payload,
            {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            self.config.timeout_seconds,
        )

    @staticmethod
    def _documents(source: SourceSpec, operation: str, response: dict) -> list[RawDocument]:
        now = datetime.now(timezone.utc).isoformat()
        request_id = str(response.get("request_id") or "")
        documents = []
        for row in response.get("results", []):
            url = str(row.get("url") or "")
            if not url:
                continue
            try:
                source.assert_official_url(url)
            except ValueError:
                # Tavily domain filters are a discovery hint, not a trust boundary.
                # Ignore any stray result instead of ingesting it or failing the
                # source's otherwise valid official documents.
                continue
            content = str(row.get("raw_content") or row.get("content") or "").strip()
            if not content:
                continue
            documents.append(
                RawDocument(
                    source_id=source.source_id,
                    url=url,
                    title=str(row.get("title") or ""),
                    content=content,
                    collected_at=now,
                    operation=operation,
                    request_id=request_id,
                )
            )
        return documents

    def search(self, source: SourceSpec, query: str) -> list[RawDocument]:
        source.validate()
        if query not in source.discovery_queries:
            raise ValueError("search query must be pre-registered for the public source")
        assert_public_text(query, "search query")
        payload = {
            "query": query,
            "search_depth": self.config.search_depth,
            "topic": "general",
            "max_results": self.config.max_results,
            "include_domains": list(source.official_domains),
            "include_answer": False,
            "include_raw_content": "markdown",
            "include_images": False,
        }
        return self._documents(source, "search", self._post("search", payload))

    def extract(self, source: SourceSpec, urls: list[str]) -> list[RawDocument]:
        source.validate()
        if not urls:
            return []
        for url in urls:
            source.assert_official_url(url)
            assert_public_text(url, "extract URL")
        payload = {
            "urls": urls,
            "extract_depth": self.config.extract_depth,
            "include_images": False,
            "include_favicon": False,
            "format": "markdown",
            "include_usage": True,
        }
        return self._documents(source, "extract", self._post("extract", payload))

    def crawl(self, source: SourceSpec, root_url: str) -> list[RawDocument]:
        source.validate()
        if root_url not in source.crawl_roots:
            raise ValueError("crawl root must be pre-registered for the public source")
        source.assert_official_url(root_url)
        assert_public_text(root_url, "crawl URL")
        payload = {
            "url": root_url,
            "instructions": "Find public GPU pricing, availability, product, and capacity announcements.",
            "max_depth": self.config.crawl_max_depth,
            "max_breadth": self.config.crawl_max_breadth,
            "limit": self.config.crawl_limit,
            "select_domains": list(source.official_domains),
            "allow_external": False,
            "include_images": False,
            "extract_depth": self.config.extract_depth,
            "format": "markdown",
            "include_usage": True,
        }
        return self._documents(source, "crawl", self._post("crawl", payload))
