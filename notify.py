"""
Post daily results to Slack and update Confluence.

Required env vars:
  SLACK_WEBHOOK_URL       — Slack incoming webhook URL
  CONFLUENCE_EMAIL        — your Atlassian account email
  CONFLUENCE_API_TOKEN    — Atlassian API token (id.atlassian.com/manage-profile/security/api-tokens)
"""
import base64
import json
import logging
import os
import sys
import urllib.request
from pathlib import Path

from config import CONFLUENCE_CLOUD_ID, CONFLUENCE_PAGE_ID, CONFLUENCE_PAGE_TITLE

logger = logging.getLogger(__name__)

STORE_DIR = Path(__file__).parent / "store"
SLACK_MSG_FILE = STORE_DIR / "slack_message.txt"
CONFLUENCE_BODY_FILE = STORE_DIR / "confluence_body.html"

CONFLUENCE_API = f"https://api.atlassian.com/ex/confluence/{CONFLUENCE_CLOUD_ID}/wiki/rest/api/content/{CONFLUENCE_PAGE_ID}"


def _auth_header() -> str:
    email = os.environ.get("CONFLUENCE_EMAIL", "")
    token = os.environ.get("CONFLUENCE_API_TOKEN", "")
    if not email or not token:
        raise EnvironmentError("CONFLUENCE_EMAIL and CONFLUENCE_API_TOKEN must be set")
    creds = base64.b64encode(f"{email}:{token}".encode()).decode()
    return f"Basic {creds}"


def post_slack(message: str) -> None:
    webhook = os.environ.get("SLACK_WEBHOOK_URL", "")
    token = os.environ.get("SLACK_BOT_TOKEN", "")

    if webhook:
        payload = json.dumps({"text": message}).encode()
        req = urllib.request.Request(webhook, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Slack webhook returned {resp.status}")
        logger.info("Slack: message posted via webhook")
    elif token:
        payload = json.dumps({"channel": "C0B4Y471YN4", "text": message}).encode()
        req = urllib.request.Request(
            "https://slack.com/api/chat.postMessage",
            data=payload,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
        if not result.get("ok"):
            raise RuntimeError(f"Slack API error: {result.get('error')}")
        logger.info("Slack: message posted via bot token")
    else:
        raise EnvironmentError("Set SLACK_WEBHOOK_URL or SLACK_BOT_TOKEN")


def update_confluence(body_html: str) -> None:
    auth = _auth_header()

    # Fetch current version
    req = urllib.request.Request(CONFLUENCE_API, headers={"Authorization": auth, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        current = json.loads(resp.read())
    current_version = current["version"]["number"]

    payload = json.dumps({
        "version": {"number": current_version + 1},
        "title": CONFLUENCE_PAGE_TITLE,
        "type": "page",
        "body": {
            "storage": {
                "value": body_html,
                "representation": "storage",
            }
        },
    }).encode()

    req = urllib.request.Request(
        CONFLUENCE_API,
        data=payload,
        method="PUT",
        headers={
            "Authorization": auth,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())
    logger.info(f"Confluence: updated to version {result['version']['number']}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    errors = []

    # Slack (optional — skip gracefully if no credentials configured)
    slack_configured = os.environ.get("SLACK_WEBHOOK_URL") or os.environ.get("SLACK_BOT_TOKEN")
    if not slack_configured:
        logger.warning("Slack: no credentials set (SLACK_WEBHOOK_URL or SLACK_BOT_TOKEN) — skipping")
    elif SLACK_MSG_FILE.exists():
        try:
            post_slack(SLACK_MSG_FILE.read_text().strip())
        except Exception as e:
            logger.error(f"Slack failed: {e}")
            errors.append(str(e))
    else:
        logger.warning(f"No Slack message file at {SLACK_MSG_FILE}")

    # Confluence
    if CONFLUENCE_BODY_FILE.exists():
        try:
            update_confluence(CONFLUENCE_BODY_FILE.read_text())
        except Exception as e:
            logger.error(f"Confluence failed: {e}")
            errors.append(str(e))
    else:
        logger.warning(f"No Confluence body file at {CONFLUENCE_BODY_FILE}")

    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
