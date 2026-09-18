"""Publication-only freshness filter. Never rewrites archived observations."""
from collections import Counter
from datetime import datetime, time, timezone
import re

from price_corrections import correct_snapshot

MAX_QUOTE_AGE_HOURS = 48


def observation_time(value):
    """Parse ISO source timestamps, including variable fractional precision.

    Python 3.9 accepts only three or six fractional digits; upstream timestamps
    legitimately use other precisions. Preserve raw text and normalize only
    for the comparison clock.
    """
    value = str(value).replace("Z", "+00:00")
    value = re.sub(r"(T\d{2}:\d{2}:\d{2})\.(\d+)",
                   lambda match: match[1] + "." + (match[2] + "000000")[:6], value)
    result = datetime.fromisoformat(value)
    return result.replace(tzinfo=result.tzinfo or timezone.utc)


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
    return observation_time(value)


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
        elif row.provider == "nebius" and row.source_feed:
            reason = "external Nebius listing; own-price anchor uses the direct source"
        elif row.available is False:
            reason = "aggregator reports this offer unavailable; retained in source evidence"
        elif row.provider == "aws" and row.consumption_type == "capacity_block":
            reason = "retired static Capacity Block reference; use the dated published-rate feed"
        elif row.provider == "nebius" and row.consumption_type.startswith("committed") and not fresh_committed:
            reason = "committed reference expired (verified %s)" % (verified or "unknown")
        else:
            try:
                observed = observation_time(row.fetched_at)
                age = (now - observed).total_seconds() / 3600
                if age > MAX_QUOTE_AGE_HOURS or age < -24:
                    reason = "quote outside the 48-hour freshness window"
            except (ValueError, TypeError, AttributeError):
                reason = "quote observation time unknown"
            if not reason and row.source_feed in {"computeprices", "shadeform"}:
                if row.source_observed_at:
                    try:
                        source_time = observation_time(row.source_observed_at)
                        source_age = (now - source_time).total_seconds() / 3600
                        if source_age > MAX_QUOTE_AGE_HOURS or source_age < -24:
                            reason = "aggregator source update outside the 48-hour freshness window"
                    except (ValueError, TypeError, AttributeError):
                        reason = "aggregator source update time invalid"
                else:
                    reason = "aggregator source update time unknown"
        if reason:
            excluded[(row.provider, reason)] += 1
        else:
            eligible.append(row)
    notices = ["%s: %s (%s rows excluded)" % (p, reason, count)
               for (p, reason), count in sorted(excluded.items())]
    return eligible, notices
