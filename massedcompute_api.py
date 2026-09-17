"""Read-only Massed Compute inventory client. Credentials never enter stored records."""
import json
import os
import time
import urllib.error
import urllib.request

API_URL = "https://vm.massedcompute.com/api/mcp"
SOURCE_URL = "https://vm-docs.massedcompute.com/docs/mcp/tools"
SECRET_NAME = "MASSED_COMPUTE_API_KEY"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
RESPONSE_DEADLINE_SECONDS = 30

class MassedAPIError(RuntimeError):
    pass

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def _read_message(response, request_id):
    """Bound response size/time and select this request's SSE response."""
    if "text/event-stream" not in response.headers.get("Content-Type", ""):
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise MassedAPIError("MCP response exceeds size limit")
        return json.loads(raw) if raw else {}
    total = 0
    data_lines = []
    deadline = time.monotonic() + RESPONSE_DEADLINE_SECONDS
    while True:
        if time.monotonic() > deadline:
            raise MassedAPIError("MCP stream exceeded time limit")
        line = response.readline(MAX_RESPONSE_BYTES - total + 1)
        total += len(line)
        if total > MAX_RESPONSE_BYTES:
            raise MassedAPIError("MCP response exceeds size limit")
        text = line.decode().rstrip("\r\n")
        if not text and data_lines:
            candidate = json.loads("\n".join(data_lines))
            data_lines = []
            if not isinstance(candidate, dict):
                raise MassedAPIError("MCP stream returned invalid message")
            if candidate.get("id") == request_id:
                return candidate
        elif text.startswith("data:"):
            data_lines.append(text[5:].lstrip(" "))
        if not line:
            break
    raise MassedAPIError("MCP returned no matching response")

def fetch_inventory():
    key = os.environ.get(SECRET_NAME, "").strip()
    if not key:
        raise MassedAPIError("MASSED_COMPUTE_API_KEY is not configured")
    opener = urllib.request.build_opener(_NoRedirect)
    session = None

    def rpc(method, request_id=None, params=None):
        nonlocal session
        message = {"jsonrpc": "2.0", "method": method}
        if request_id is not None:
            message["id"] = request_id
        if params is not None:
            message["params"] = params
        headers = {"Authorization": "Bearer " + key,
                   "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream",
                   "User-Agent": "price-monitor-inventory-check/1.0"}
        if session:
            headers["Mcp-Session-Id"] = session
        req = urllib.request.Request(API_URL, data=json.dumps(message).encode(),
                                     headers=headers, method="POST")
        try:
            with opener.open(req, timeout=30) as response:
                session = response.headers.get("Mcp-Session-Id") or session
                if response.status == 202:
                    if request_id is None:
                        return {}
                    raise MassedAPIError("MCP request returned no response")
                obj = _read_message(response, request_id)
        except urllib.error.HTTPError as error:
            raise MassedAPIError("Massed inventory HTTP %s" % error.code) from None
        except (urllib.error.URLError, OSError, ValueError, UnicodeError):
            raise MassedAPIError("Massed inventory transport or JSON error") from None
        if request_id is None and obj == {}:
            return {}
        if (not isinstance(obj, dict) or obj.get("jsonrpc") != "2.0"
                or type(obj.get("id")) is not type(request_id)
                or obj.get("id") != request_id or obj.get("error") is not None):
            raise MassedAPIError("Massed inventory RPC rejected")
        result = obj.get("result")
        if not isinstance(result, dict):
            raise MassedAPIError("Massed inventory RPC returned invalid result")
        return result

    rpc("initialize", 1, {"protocolVersion": "2025-03-26", "capabilities": {},
        "clientInfo": {"name": "price-monitor-inventory", "version": "1.0"}})
    rpc("notifications/initialized")
    # This client can only request the inventory read. It never calls deployment,
    # account billing, SSH-key management, or any other account operation.
    result = rpc("tools/call", 2, {"name": "gpu_inventory_list", "arguments": {}})
    if result.get("isError"):
        raise MassedAPIError("Massed inventory tool failed")
    payload = result.get("structuredContent")
    if payload is None:
        content = result.get("content", [])
        if not isinstance(content, list):
            raise MassedAPIError("Massed inventory tool returned invalid content")
        for block in content:
            if not isinstance(block, dict):
                raise MassedAPIError("Massed inventory tool returned invalid content")
            if block.get("type") == "text":
                try:
                    candidate = json.loads(block.get("text", ""))
                except (ValueError, TypeError):
                    continue
                if isinstance(candidate, dict) and "gpu_inventory" in candidate:
                    payload = candidate
                    break
    if not isinstance(payload, dict) or not isinstance(payload.get("gpu_inventory"), dict):
        raise MassedAPIError("Massed inventory schema is invalid")
    if not payload["gpu_inventory"]:
        raise MassedAPIError("Massed inventory is empty")
    return payload
