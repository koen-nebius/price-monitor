"""Dated, scoped competitor quotes. Asking prices and signed deals stay separate."""
from collections import Counter
import csv
from datetime import date
from html import escape
from pathlib import Path
import re
from urllib.parse import urlparse

from intel_schema import validate_row, is_expired
from intel_quality import classify, prepay_known
from report_freshness import report_time

PRIORITY_PROVIDERS = ("coreweave", "lambda", "crusoe")
ALIASES = {"coreweave": "coreweave", "cw": "coreweave", "lambda": "lambda",
           "lambda labs": "lambda", "lambdalabs": "lambda", "crusoe": "crusoe", "crusoe cloud": "crusoe"}


def payment_known(row):
    """A default zero, monthly billing or an unrelated 0% is not a payment term."""
    if row.get("prepay_pct") in {None, ""}:
        return False
    if row.get("prepay_known") == "0":
        return False
    if row.get("prepay_known") == "1":
        return True  # the validator establishes that the explicit amount is valid
    if not prepay_known(row):
        return False
    if float(row.get("prepay_pct") or 0) > 0:
        return True
    notes = str(row.get("notes") or "")
    if re.search(r"(?:prepay(?:ment)?|upfront|up-front).{0,15}(?:unknown|unspecified|unclear)", notes, re.I):
        return False
    return bool(re.search(
        r"\b(?:no|zero|without)\s+(?:prepay(?:ment)?|upfront|up-front|down\s?payment)\b|"
        r"\b(?:prepay(?:ment)?|upfront|up-front|down\s?payment)\s*:?\s*0\s*%|"
        r"(?<![\d.])0\s*%\s*(?:prepay(?:ment)?|upfront|up-front|down\s?payment)\b", notes, re.I))


def load_rows(path):
    if not Path(path).exists(): return []
    with Path(path).open(newline="") as stream: return list(csv.DictReader(stream))


def qualification(row, as_of):
    now = report_time(as_of).date()
    reasons = validate_row(row)
    if reasons: return "invalid", reasons
    if is_expired(row, now): return "expired", ["Quote validity has expired"]
    stamp = row.get("source_observed_at")
    if not stamp:
        reasons.append("Quote observation date unknown; message date is only a reporting date")
    else:
        age = (now - date.fromisoformat(stamp)).days
        if age < 0 or age > 90: reasons.append("Quote observation outside the 90-day review window")
    status = row.get("quote_status") or "unknown"
    if status == "unknown": reasons.append("Asking price versus signed deal unknown")
    if status == "asking_price" and not row.get("expires_on"): reasons.append("Quote expiry unknown")
    for key in ("source_url", "instance_type", "region", "gpu_count", "currency", "tax_basis", "delivery_start", "delivery_end"):
        if not row.get(key) or str(row[key]).lower() in {"unknown", "unspecified", "global"}:
            reasons.append(key.replace("_", " ") + " unknown")
    if row.get("gpu_count_relation") not in {"exact", "minimum"}: reasons.append("Quantity relation unknown")
    if float(row.get("term_months") or 0) <= 0: reasons.append("Commitment term unknown")
    known = payment_known(row)
    if not known: reasons.append("Prepayment unknown")
    return ("reference_only" if reasons else "qualified_asking_price" if status == "asking_price" else "qualified_signed_deal"), reasons


def build_quote_report(rows, as_of):
    now = report_time(as_of)
    # Filter the historical window before dedupe: a later report carrying more
    # complete terms must not remove the evidence available on an earlier day.
    rows = [r for r in rows if (r.get("message_date") or "") <= now.date().isoformat()]
    valid_rows = [r for r in rows if not validate_row(r)]
    kept, removed, review = classify(valid_rows)
    invalid_rows = [r for r in rows if validate_row(r)]
    observations = []
    for row in kept + invalid_rows:
        provider = ALIASES.get((row.get("provider_name") or "").strip().lower())
        if provider not in PRIORITY_PROVIDERS: continue
        # A later report must not appear in an earlier offline report rebuild.
        if row.get("message_date", "") > now.date().isoformat(): continue
        status, reasons = qualification(row, now)
        observations.append({"provider": provider, "gpu": row.get("gpu_model", ""), "status": status,
                             "quote_status": row.get("quote_status") or "unknown", "reasons": reasons,
                             "source_observed_at": row.get("source_observed_at") or "",
                             "reported_at": row.get("message_date") or "", "expires_on": row.get("expires_on") or "",
                             "price_per_gpu_hour_usd": row.get("price_per_gpu_hour_usd"),
                             "region": row.get("region") or "unknown", "instance_type": row.get("instance_type") or "unknown",
                             "gpu_variant": row.get("gpu_variant") or "unknown",
                             "currency": row.get("currency") or "unknown", "tax_basis": row.get("tax_basis") or "unknown",
                             "gpu_count": row.get("gpu_count") or None, "gpu_count_relation": row.get("gpu_count_relation") or "unknown",
                             "term_months": row.get("term_months") or None,
                             "prepay_pct": row.get("prepay_pct") if payment_known(row) else None,
                             "delivery_start": row.get("delivery_start") or "", "delivery_end": row.get("delivery_end") or "",
                             "source_url": row.get("source_url") or "", "quote_id": row.get("quote_id") or "",
                             "message_ts": row.get("message_ts") or ""})
    summary = [{"provider": p, "observations": sum(o["provider"] == p for o in observations),
                "statuses": dict(Counter(o["status"] for o in observations if o["provider"] == p))}
               for p in PRIORITY_PROVIDERS]
    return {"schema_version": 1, "as_of": now.isoformat(), "providers": summary, "observations": observations,
            "duplicate_records_removed": len(removed), "similar_records_for_review": len(review),
            "scope": "Priority-neocloud quote evidence. Asking prices, signed deals and reports of unknown closing status "
                     "are separate. Qualification means required fields are present, not independent verification or current stock. "
                     "No inferred terms, expiry, quantity, source date or approval."}


def render_quote_report(report):
    esc = lambda value: escape(str(value), quote=True)
    h = ['<h3>Priority-neocloud negotiated evidence</h3>', '<p>' + esc(report["scope"]) + '</p>',
         '<table><tbody><tr><th>Provider / GPU</th><th>Evidence</th><th>Price and scope</th><th>Dates</th><th>Qualification gaps</th></tr>']
    for row in report["observations"]:
        price = '$' + str(row["price_per_gpu_hour_usd"]) + '/GPU-h'
        quantity = str(row["gpu_count"] or "unknown") + ('+' if row['gpu_count_relation'] == 'minimum' else '')
        prepay = str(row["prepay_pct"]) + '%' if row["prepay_pct"] is not None else 'unknown'
        scope = f'{price}; {row["currency"]}, {row["tax_basis"]}; {row["instance_type"]}; {row["gpu_variant"]}; {row["region"]}; {quantity} GPUs; term {row["term_months"] or "unknown"} months; prepay {prepay}'
        dates = f'Observed {row["source_observed_at"] or "unknown"}; reported {row["reported_at"]}; expires {row["expires_on"] or "unknown"}; delivery {row["delivery_start"] or "unknown"} to {row["delivery_end"] or "unknown"}'
        values = (row["provider"] + ' / ' + row["gpu"], row["quote_status"] + ' / ' + row["status"], scope, dates,
                  '; '.join(row["reasons"]) or 'Required fields present; evidence review still applies')
        cells = [esc(value) for value in values]
        try:
            link = urlparse(row["source_url"])
            if link.scheme in {"https", "http"} and link.hostname and not link.username and not link.password:
                cells[1] += '<br/><a href="' + esc(row["source_url"]) + '">Source evidence</a>'
        except ValueError:
            pass
        if row["quote_id"]:
            cells[1] += '<br/>Quote ' + esc(row["quote_id"])
        h.append('<tr>' + ''.join('<td>' + value + '</td>' for value in cells) + '</tr>')
    if not report["observations"]: h.append('<tr><td colspan="5">No quote records found for the priority providers.</td></tr>')
    h.append('</tbody></table>')
    return '\n'.join(h)
