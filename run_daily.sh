#!/bin/bash
# Daily GPU price monitor — runs at 07:00 UTC via cron

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$SCRIPT_DIR/store/run.log"

# Load secrets from ~/.price-monitor-env if present
if [[ -f "$HOME/.price-monitor-env" ]]; then
    # shellcheck source=/dev/null
    source "$HOME/.price-monitor-env"
fi

{
    echo "=== $(date -u '+%Y-%m-%d %H:%M:%S UTC') ==="
    cd "$SCRIPT_DIR"
    python3 main.py

    # Post results to Slack + Confluence via direct API. Creds come from
    # ~/.price-monitor-env; notify.py warn-skips whichever half is unconfigured.
    # '||' so a failed post doesn't abort the run under set -e.
    python3 notify.py || echo "WARN: notify.py exited nonzero — post failed, see above"

    echo "=== done ==="
} >> "$LOG" 2>&1

# Keep log to last 500 lines
tail -n 500 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
