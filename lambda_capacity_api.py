"""Read-only Lambda instance discovery using the documented current API host.

Only GET /api/v1/instance-types is exposed. No account resources, deployments,
reservations or writes. Bearer authentication is documented by Lambda at
https://docs.lambda.ai/public-cloud/cloud-api/ .
"""
import json
import os
import urllib.error
import urllib.request

API_URL = "https://cloud.lambda.ai/api/v1/instance-types"
API_KEY_ENV = "LAMBDA_API_KEY"
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_ERRORS = frozenset((
    "Lambda API key is missing or invalid",
    "Lambda instance discovery returned unexpected HTTP status",
    "Lambda instance discovery response exceeded size limit",
    "Lambda instance discovery HTTP error",
    "Lambda instance discovery transport or JSON error",
    "Lambda instance discovery response has no data mapping",
    "Lambda instance discovery returned unsupported pagination",
))


class LambdaAPIError(RuntimeError):
    """Only static diagnostics and an optional HTTP status can escape the client."""

    def __init__(self, message, http_status=None):
        super().__init__(message if message in _ERRORS else "Lambda instance discovery failed")
        self.http_status = http_status if type(http_status) is int and 100 <= http_status <= 599 else None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_instance_types() -> dict:
    """Fetch the fixed instance-type endpoint with no redirects or request body."""
    key = os.environ.get(API_KEY_ENV, "").strip()
    if not key or any(ord(char) < 33 or ord(char) > 126 for char in key):
        raise LambdaAPIError("Lambda API key is missing or invalid")
    request = urllib.request.Request(API_URL, headers={
        "Authorization": "Bearer " + key,
        "Accept": "application/json",
        "User-Agent": "price-monitor-capacity/1.0",
    }, method="GET")
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=30) as response:
            if response.status != 200:
                raise LambdaAPIError("Lambda instance discovery returned unexpected HTTP status",
                                     http_status=response.status)
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(raw) > _MAX_RESPONSE_BYTES:
                raise LambdaAPIError("Lambda instance discovery response exceeded size limit")
            payload = json.loads(raw)
    except urllib.error.HTTPError as exc:
        raise LambdaAPIError("Lambda instance discovery HTTP error", http_status=exc.code) from None
    except (urllib.error.URLError, OSError, ValueError, UnicodeError):
        raise LambdaAPIError("Lambda instance discovery transport or JSON error") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise LambdaAPIError("Lambda instance discovery response has no data mapping")
    if payload.get("next_page_token") or payload.get("next_token") or payload.get("next"):
        raise LambdaAPIError("Lambda instance discovery returned unsupported pagination")
    return payload
