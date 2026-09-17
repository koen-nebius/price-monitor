"""Read-only Crusoe capacity client, signed as documented for the current v1 API.

Auth: https://docs.cloud.crusoe.ai/reference/api/
Schema: https://api.crusoecloud.com/v1/openapi.json (CapacityV1).
Only GET /v1/capacities is exposed: no projects, billing or deployment calls.
"""
import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone

API_URL = "https://api.cloud.crusoe.ai/v1/capacities"
API_PATH = "/v1/capacities"
ACCESS_KEY_ENV = "CRUSOE_ACCESS_KEY_ID"
SECRET_KEY_ENV = "CRUSOE_SECRET_KEY"
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class CrusoeAPIError(RuntimeError):
    """Sanitized error: never contains keys, auth headers or remote content."""

    def __init__(self, message, http_status=None):
        super().__init__(message)
        self.http_status = http_status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def credentials_configured() -> bool:
    """Either nonempty credential activates the API path (partial pairs fail)."""
    return bool(os.environ.get(ACCESS_KEY_ENV, "").strip()
                or os.environ.get(SECRET_KEY_ENV, "").strip())


def _signed_headers(access_key: str, secret_key: str, timestamp: str) -> dict:
    if (not re.fullmatch(r"[A-Za-z0-9_-]+", access_key or "")
            or not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", secret_key or "")):
        raise CrusoeAPIError("Crusoe credentials are missing or invalid")
    try:
        decoded = base64.b64decode(secret_key + "=" * (-len(secret_key) % 4),
                                   altchars=b"-_", validate=True)
    except (binascii.Error, ValueError):
        raise CrusoeAPIError("Crusoe credentials are invalid") from None
    if not decoded:
        raise CrusoeAPIError("Crusoe credentials are invalid")
    # Empty query occupies its own line; final newline is part of the HMAC.
    payload = f"{API_PATH}\n\nGET\n{timestamp}\n".encode("ascii")
    signature = base64.urlsafe_b64encode(
        hmac.new(decoded, payload, hashlib.sha256).digest()
    ).decode("ascii").rstrip("=")
    return {"Authorization": f"Bearer 1.0:{access_key}:{signature}",
            "X-Crusoe-Timestamp": timestamp, "Accept": "application/json",
            "User-Agent": "price-monitor-capacity/1.0"}


def fetch_capacities() -> dict:
    """Fetch only the fixed capacity endpoint; no redirects or fallback host."""
    access_key = os.environ.get(ACCESS_KEY_ENV, "").strip()
    secret_key = os.environ.get(SECRET_KEY_ENV, "").strip()
    if not access_key or not secret_key:
        raise CrusoeAPIError("Both Crusoe API credentials are required")
    timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    headers = _signed_headers(access_key, secret_key, timestamp)
    request = urllib.request.Request(API_URL, headers=headers, method="GET")
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=30) as response:
            if response.status != 200:
                raise CrusoeAPIError("Crusoe capacity returned unexpected HTTP status",
                                     http_status=response.status)
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(raw) > _MAX_RESPONSE_BYTES:
                raise CrusoeAPIError("Crusoe capacity response exceeded size limit")
            payload = json.loads(raw)
    except urllib.error.HTTPError as exc:
        raise CrusoeAPIError(f"Crusoe capacity HTTP {exc.code}", http_status=exc.code) from None
    except (urllib.error.URLError, OSError, ValueError, UnicodeError):
        raise CrusoeAPIError("Crusoe capacity transport or JSON error") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise CrusoeAPIError("Crusoe capacity response has no items array")
    # v1 documents no pagination for capacities. Never silently accept a newly
    # paginated response as a complete observation.
    if payload.get("next_page_token") or payload.get("next_token"):
        raise CrusoeAPIError("Crusoe capacity returned unsupported pagination")
    return payload
