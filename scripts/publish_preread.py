"""
One-shot publisher: create/update a Confluence page from an HTML file in
analysis/. Used by the publish-preread.yml manual workflow because the
interactive Atlassian MCP connection is intermittent; runs on the same
CONFLUENCE_EMAIL / CONFLUENCE_API_TOKEN repo secrets as the intel merge.

Idempotent: looks the page up by title in the space and updates it (version+1)
if it exists, creates it otherwise.

Env: CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN (required)
     PREREAD_FILE  (default analysis/managed_db_preread.html)
     PREREAD_TITLE (default "Managed Database Pricing — Market Benchmark (Aiven)")
     PREREAD_SPACE (default "PR")
"""
import base64
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://nebius.atlassian.net/wiki/rest/api"


def _req(method: str, url: str, auth: str, body: dict = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=45) as resp:
        return json.loads(resp.read() or b"{}")


def main() -> int:
    email = os.environ.get("CONFLUENCE_EMAIL")
    token = os.environ.get("CONFLUENCE_API_TOKEN")
    if not (email and token):
        print("CONFLUENCE_EMAIL / CONFLUENCE_API_TOKEN not set — cannot publish")
        return 1
    auth = base64.b64encode(f"{email}:{token}".encode()).decode()

    path = Path(os.environ.get("PREREAD_FILE", "analysis/managed_db_preread.html"))
    title = os.environ.get("PREREAD_TITLE",
                           "Managed Database Pricing — Market Benchmark (Aiven)")
    space = os.environ.get("PREREAD_SPACE", "PR")
    html = path.read_text()

    q = urllib.parse.urlencode({"title": title, "spaceKey": space, "expand": "version"})
    found = _req("GET", f"{BASE}/content?{q}", auth).get("results", [])

    payload = {
        "type": "page",
        "title": title,
        "space": {"key": space},
        "body": {"storage": {"value": html, "representation": "storage"}},
    }
    if found:
        page = found[0]
        payload["version"] = {"number": page["version"]["number"] + 1,
                              "message": "refreshed by publish_preread.py"}
        out = _req("PUT", f"{BASE}/content/{page['id']}", auth, payload)
        print(f"UPDATED page {page['id']} to v{out.get('version', {}).get('number')}: "
              f"{out.get('_links', {}).get('base', '')}{out.get('_links', {}).get('webui', '')}")
    else:
        out = _req("POST", f"{BASE}/content", auth, payload)
        print(f"CREATED page {out.get('id')}: "
              f"{out.get('_links', {}).get('base', '')}{out.get('_links', {}).get('webui', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
