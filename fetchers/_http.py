"""
Shared HTTP helper for fetchers.

GitHub Actions runner IPs are aggressively filtered by some pricing APIs
(Azure Retail Prices, Oracle apexapps, RunPod GraphQL). Two mitigations:

1. Full browser-like headers — Python's default `Python-urllib/3.x` UA and
   bot-ish UAs are the first thing WAFs reject; a complete browser header
   set passes most non-JS challenges.
2. Retries with exponential backoff + jitter — rate-limit style blocks
   (403/429/503) are often transient per-request, not per-IP-per-day.
"""
import logging
import random
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "identity",
    "Connection": "keep-alive",
}

# Status codes worth retrying — transient blocks and rate limits
_RETRYABLE = {403, 408, 429, 500, 502, 503, 504}


def http_get(url: str, headers: dict = None, data: bytes = None,
             timeout: int = 30, retries: int = 3) -> bytes:
    """
    GET (or POST when `data` is given) with browser headers and retries.
    Raises the last error if all attempts fail.
    """
    merged = dict(BROWSER_HEADERS)
    if headers:
        merged.update(headers)

    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=merged)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code not in _RETRYABLE:
                raise
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = e
        if attempt < retries - 1:
            delay = (2 ** attempt) + random.uniform(0, 1.5)
            logger.debug(f"http_get retry {attempt + 1}/{retries} for {url[:80]} "
                         f"after {last_err} — sleeping {delay:.1f}s")
            time.sleep(delay)
    raise last_err
