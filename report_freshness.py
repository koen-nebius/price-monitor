"""Publication-only freshness filter. Never rewrites archived observations."""
from collections import Counter
from datetime import datetime, time, timezone

from price_corrections import correct_snapshot

MAX_QUOTE_AGE_HOURS = 48


def report_time(value=None):
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        return value.replace(tzinfo=value.tzinfo or timezone.utc)
    for fmt in ("%B %d, %Y", "%Y-%m-%d"):
        try:
            day = datetime.strptime(str(value), fmt).date()
            now = datetime.now(timezone.utc)
            # Today's renderers use the same wall-clock convention as main.
            # Historical date-only rebuilds use an explicit end-of-day boundary.
            return now if day == now.date() else datetime.combine(day, time.max, timezone.utc)
        except ValueError:
            pass
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def committed_reference_fresh(as_of=None):
    from config import NEBIUS_COMMITTED_PRICES_VERIFIED_DATE, NEBIUS_COMMITTED_STALE_DAYS
    try:
        verified = datetime.strptime(NEBIUS_COMMITTED_PRICES_VERIFIED_DATE, "%Y-%m-%d").date()
        age = (report_time(as_of).date() - verified).days
        return 0 <= age <= NEBIUS_COMMITTED_STALE_DAYS, verified.isoformat(), age
    except (ValueError, TypeError):
        return False, None, None


def publication_records(records, as_of=None):
    """Return eligible copied observations and explicit exclusion notices."""
    now = report_time(as_of)
    fresh_committed, verified, _ = committed_reference_fresh(now)
    excluded = Counter()
    eligible = []
    corrected = correct_snapshot(records, include_excluded=True)
    for row in corrected:
        reason = None
        if not row.comparison_eligible:
            reason = row.correction_reason or "source correction"
        elif row.provider == "aws" and row.consumption_type == "capacity_block":
            reason = "retired static Capacity Block reference; use the dated published-rate feed"
        elif row.provider == "nebius" and row.consumption_type.startswith("committed") and not fresh_committed:
            reason = "committed reference expired (verified %s)" % (verified or "unknown")
        else:
            try:
                observed = datetime.fromisoformat(row.fetched_at.replace("Z", "+00:00"))
                observed = observed.replace(tzinfo=observed.tzinfo or timezone.utc)
                age = (now - observed).total_seconds() / 3600
                if age > MAX_QUOTE_AGE_HOURS or age < -24:
                    reason = "quote outside the 48-hour freshness window"
            except (ValueError, TypeError, AttributeError):
                reason = "quote observation time unknown"
        if reason:
            excluded[(row.provider, reason)] += 1
        else:
            eligible.append(row)
    notices = ["%s: %s (%s rows excluded)" % (p, reason, count)
               for (p, reason), count in sorted(excluded.items())]
    return eligible, notices
