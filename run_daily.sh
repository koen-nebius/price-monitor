#!/bin/bash
# Daily GPU price monitor — runs at 07:00 UTC via cron

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$SCRIPT_DIR/store/run.log"
CLAUDE_BIN="${CLAUDE_BIN:-/Users/koenbrormann/.local/bin/claude}"

# Load secrets from ~/.price-monitor-env if present
if [[ -f "$HOME/.price-monitor-env" ]]; then
    # shellcheck source=/dev/null
    source "$HOME/.price-monitor-env"
fi

{
    echo "=== $(date -u '+%Y-%m-%d %H:%M:%S UTC') ==="
    cd "$SCRIPT_DIR"
    python3 main.py

    # Post results via claude CLI (uses Slack + Confluence MCP connections)
    SLACK_MSG=$(cat "$SCRIPT_DIR/store/slack_message.txt" 2>/dev/null || echo "")
    if [[ -n "$SLACK_MSG" ]]; then
        "$CLAUDE_BIN" --dangerously-skip-permissions -p "$(cat <<'PROMPT'
Post the contents of /Users/koenbrormann/Claude PM/price-monitor/store/slack_message.txt to Slack channel #competitor-pricing (channel ID C0B4Y471YN4).
Then update Confluence page ID 1831469419 (cloud ID 3213098a-816e-4aeb-8073-44b4d40f3fdc) with the contents of /Users/koenbrormann/Claude PM/price-monitor/store/confluence_body.html — use the Storage format representation and increment the version number.
Do not add any commentary; just post the message and update the page exactly as written.
PROMPT
)" 2>&1 | tail -5
    fi

    echo "=== done ==="
} >> "$LOG" 2>&1

# Keep log to last 500 lines
tail -n 500 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
