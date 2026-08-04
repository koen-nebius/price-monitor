"""Official-public source registry and public-only safety checks."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


FORBIDDEN_MARKERS = {
    "nebius.atlassian.net",
    "slack.com/archives",
    "enterprise.slack.com",
    "confluence",
    "jira",
    "tenant_id",
    "customer_name",
    "deal_id",
    "account_id",
}


def canonical_domain(value: str) -> str:
    value = value.strip().lower().rstrip(".")
    return value[4:] if value.startswith("www.") else value


def domain_for_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError(f"only absolute HTTPS URLs are allowed: {url}")
    return canonical_domain(parsed.hostname)


def assert_public_text(value: str, field_name: str) -> None:
    lowered = value.lower()
    found = sorted(marker for marker in FORBIDDEN_MARKERS if marker in lowered)
    if found:
        raise ValueError(f"{field_name} contains forbidden internal marker(s): {found}")


@dataclass(frozen=True, kw_only=True)
class SourceSpec:
    source_id: str
    provider: str
    official_domains: tuple[str, ...]
    public_only: bool = True
    known_urls: tuple[str, ...] = ()
    discovery_queries: tuple[str, ...] = ()
    crawl_roots: tuple[str, ...] = ()
    event_kinds: tuple[str, ...] = ("pricing", "capacity")
    default_region: str = "Global"
    daily_extract: bool = True
    weekly_discovery: bool = True
    metadata: dict = field(default_factory=dict)

    def validate(self) -> None:
        if not self.source_id or not self.provider:
            raise ValueError("source_id and provider are required")
        if not self.public_only:
            raise ValueError(f"{self.source_id}: Tavily sources must be public_only=true")
        if not self.official_domains:
            raise ValueError(f"{self.source_id}: at least one official domain is required")
        domains = {canonical_domain(value) for value in self.official_domains}
        for domain in domains:
            assert_public_text(domain, "official_domain")
            if domain.endswith(".internal") or domain in {"localhost", "127.0.0.1"}:
                raise ValueError(f"{self.source_id}: non-public domain is forbidden: {domain}")
        for url in (*self.known_urls, *self.crawl_roots):
            self.assert_official_url(url)
            assert_public_text(url, "source URL")
        for query in self.discovery_queries:
            assert_public_text(query, "discovery query")
        unknown_kinds = set(self.event_kinds) - {"pricing", "capacity"}
        if unknown_kinds:
            raise ValueError(f"{self.source_id}: unknown event_kinds {sorted(unknown_kinds)}")

    def assert_official_url(self, url: str) -> None:
        domain = domain_for_url(url)
        allowed = {canonical_domain(value) for value in self.official_domains}
        if not any(domain == base or domain.endswith(f".{base}") for base in allowed):
            raise ValueError(
                f"{self.source_id}: URL domain {domain} is not in official_domains {sorted(allowed)}"
            )

    @classmethod
    def from_dict(cls, data: dict) -> "SourceSpec":
        known = {
            "source_id",
            "provider",
            "official_domains",
            "public_only",
            "known_urls",
            "discovery_queries",
            "crawl_roots",
            "event_kinds",
            "default_region",
            "daily_extract",
            "weekly_discovery",
            "metadata",
        }
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"unknown source fields: {sorted(unknown)}")
        payload = dict(data)
        for name in (
            "official_domains",
            "known_urls",
            "discovery_queries",
            "crawl_roots",
            "event_kinds",
        ):
            if name in payload:
                payload[name] = tuple(payload[name])
        result = cls(**payload)
        result.validate()
        return result


class SourceRegistry:
    def __init__(self, sources: list[SourceSpec]):
        ids = [source.source_id for source in sources]
        if len(ids) != len(set(ids)):
            raise ValueError("source_id values must be unique")
        for source in sources:
            source.validate()
        self._sources = {source.source_id: source for source in sources}

    @classmethod
    def load(cls, path: str | Path) -> "SourceRegistry":
        with Path(path).open() as handle:
            payload = json.load(handle)
        if payload.get("registry_version") != 1:
            raise ValueError("unsupported or missing registry_version")
        return cls([SourceSpec.from_dict(item) for item in payload.get("sources", [])])

    def all(self) -> list[SourceSpec]:
        return list(self._sources.values())

    def get(self, source_id: str) -> SourceSpec:
        try:
            return self._sources[source_id]
        except KeyError as exc:
            raise KeyError(f"unknown source_id: {source_id}") from exc

    def __len__(self) -> int:
        return len(self._sources)
