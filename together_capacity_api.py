"""Read-only Together discovery, restricted to two fixed official GET endpoints.

Inference instance headroom describes dedicated inference replicas. Cluster
regions describe supported offerings only, not live cluster availability.
"""
import json
import os
import urllib.error
import urllib.request

API_URL = "https://api.together.ai/v2/public/inference-instance-types"
CLUSTER_REGIONS_URL = "https://api.together.ai/v1/compute/regions"
API_KEY_ENV = "TOGETHER_API_KEY"
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_ALLOWED_URLS = frozenset((API_URL, CLUSTER_REGIONS_URL))


class TogetherAPIError(RuntimeError):
    """Static diagnostic with optional HTTP status; no remote body or credential."""

    def __init__(self, message, http_status=None):
        super().__init__(message)
        self.http_status = http_status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _get_json(url: str) -> dict:
    if url not in _ALLOWED_URLS:
        raise TogetherAPIError("Together discovery URL is not allowed")
    key = os.environ.get(API_KEY_ENV, "").strip()
    if not key or any(ord(char) < 33 or ord(char) > 126 for char in key):
        raise TogetherAPIError("Together API key is missing or invalid")
    request = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + key, "Accept": "application/json",
        "User-Agent": "price-monitor-capacity/1.0",
    }, method="GET")
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=30) as response:
            if response.status != 200:
                raise TogetherAPIError("Together discovery returned unexpected HTTP status",
                                       http_status=response.status)
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(raw) > _MAX_RESPONSE_BYTES:
                raise TogetherAPIError("Together discovery response exceeded size limit")
            payload = json.loads(raw)
    except urllib.error.HTTPError as exc:
        raise TogetherAPIError("Together discovery HTTP error", http_status=exc.code) from None
    except (urllib.error.URLError, OSError, ValueError, UnicodeError):
        raise TogetherAPIError("Together discovery transport or JSON error") from None
    if not isinstance(payload, dict):
        raise TogetherAPIError("Together discovery response is not an object")
    if payload.get("next_page_token") or payload.get("next_token") or payload.get("next"):
        raise TogetherAPIError("Together discovery returned unsupported pagination")
    return payload


def fetch_instance_types() -> dict:
    """Return dedicated inference instance types, not GPU-cluster capacity."""
    payload = _get_json(API_URL)
    if not isinstance(payload.get("data"), list):
        raise TogetherAPIError("Together inference response has no data array")
    return payload


def fetch_cluster_regions() -> dict:
    """Return supported cluster regions/types; this endpoint reports no stock."""
    payload = _get_json(CLUSTER_REGIONS_URL)
    if not isinstance(payload.get("regions"), list):
        raise TogetherAPIError("Together cluster response has no regions array")
    return payload
