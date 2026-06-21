"""
Tavily fetch fallback — read-escalation ladder for JS-rendered / blocked pages.

Ported from the ml-hiring-leads pilot's tavily_client.read_posting: when a plain
urllib fetch returns nothing or a JS-rendered shell (SF Compute Next.js, Lambda
behind Cloudflare, Hyperstack's marketing page), escalate basic extract -> advanced
extract -> single-page crawl until real content comes back. Tavily renders the page
server-side, so this retires the "scrape returned nothing -> serve stale cache" class.

Uses the Tavily REST API directly (stdlib only) so it runs in the GHA pipeline; the
MCP Tavily tools are only available to the interactive/CCR agent. Auth: TAVILY_API_KEY
(Tavily is Nebius-owned, so credits are an internal concern). Graceful: with no key or
on any error it returns "" and the caller keeps its existing behaviour.
"""
import json
import logging
import os
import time
import urllib.request

logger = logging.getLogger(__name__)

_API = "https://api.tavily.com"


def _key() -> str:
    return os.environ.get("TAVILY_API_KEY", "")


def _post(endpoint: str, payload: dict, timeout: int = 40) -> dict:
    key = _key()
    if not key:
        return {}
    body = json.dumps(payload).encode()
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                f"{_API}/{endpoint}",
                data=body,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except Exception as e:
            wait = 2 ** attempt
            logger.warning(f"tavily {endpoint} failed ({e}); retry in {wait}s")
            time.sleep(wait)
    logger.error(f"tavily {endpoint} failed after retries")
    return {}


def _extract(url: str, depth: str) -> str:
    res = _post("extract", {"urls": [url], "extract_depth": depth, "format": "markdown"})
    results = res.get("results", [])
    return results[0].get("raw_content", "") if results else ""


def _crawl_one(url: str) -> str:
    res = _post("crawl", {"url": url, "max_depth": 0, "limit": 1})
    results = res.get("results", [])
    if not results:
        return ""
    best = max(results, key=lambda r: len(r.get("raw_content", "")))
    return best.get("raw_content", "")


def _is_thin(text: str, min_chars: int) -> bool:
    return not text or len(text.strip()) < min_chars


def fetch_text(url: str, min_chars: int = 400) -> str:
    """
    Return the cleanest page text Tavily can get, escalating only when thin.
    Returns "" if TAVILY_API_KEY is unset or every tier fails — caller then keeps
    whatever it already had (no behaviour change without a key).
    """
    if not _key():
        return ""
    text = _extract(url, "basic")
    if not _is_thin(text, min_chars):
        return text
    adv = _extract(url, "advanced")
    if not _is_thin(adv, min_chars):
        return adv
    crawled = _crawl_one(url)               # last resort: pulls JS-rendered content
    candidates = [t for t in (crawled, adv, text) if t]
    return max(candidates, key=len) if candidates else ""


def available() -> bool:
    return bool(_key())
