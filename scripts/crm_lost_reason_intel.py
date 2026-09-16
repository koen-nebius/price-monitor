#!/usr/bin/env python3
"""
Mine competitor prices and accepted prices from CRM closed-lost free text
(2026-09-16). Writes store/crm_intel_candidates.csv for review; nothing is filed
into intel.csv automatically because the texts need judgement ("$1 more expensive",
"mid-$3 range", TPU offers).

Two statement types are extracted per closed-lost opportunity with a GPU line item:
  competitor_quote  a competitor's price appears ("Scaleway 4$ with prepay",
                    "bare metal vendors ... going at $3.88", "$3.6 offer for 3500 GB300")
  accepted_price    the customer accepted OUR price and we lost for capacity/timing
                    ("ready to sign for 16 H200s ... at $3.20", any NO_CAPACITY loss):
                    willingness to pay, stronger than a quote

Sources: Salesforce ODS for closes on/after 2026-08-10 (closed_lost_reason_description,
loss_reason, opportunity_closed_lost_competitors; line items for GPU, our price, GPUs,
term); the frozen HubSpot mirror before it (free text sits in closed_lost_reason_name,
the category in closed_lost_reason_desc: the columns are swapped there).

Runs LOCALLY only (YT venv), e.g.
    ~/nebo/analytics/libs/data_clients/yt_data_client/yt_mcp/.venv/bin/python \
        scripts/crm_lost_reason_intel.py --since 2026-01-01

Confidentiality: the candidates file carries account names and is INTERNAL (same
policy as intel.csv notes); never paste rows into wide channels.
"""
import csv
import os
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "store" / "crm_intel_candidates.csv"
CUTOVER = "2026-08-10"
HS_TABLE = "//home/dwh/nemax-prod/data/cdm/crm/deal_review_line_items_enriched"
SF_OPP = "//home/dwh/nemax-prod/data/ods/salesforce/opportunity"
SF_OLI = "//home/dwh/nemax-prod/data/ods/salesforce/opportunity_line_item"

COMPETITORS = ["CoreWeave", "Coreweave", "Lambda", "Crusoe", "Nscale", "Fluidstack", "FluidStack", "Together", "RunPod",
               "IREN", "Iren", "DataCrunch", "Verda", "AWS", "Azure", "GCP", "Google", "Oracle", "OCI", "Scaleway",
               "Hyperstack", "Vultr", "Lightning", "PaleBlueDot", "Pale Blue Dot", "Deutsche Telekom", "Voltage Park",
               "GMI", "Nebul", "SpaceX", "Yotta", "Firmus", "Sesterce", "Ori", "TPU", "TPUs"]
PRICE_RE = re.compile(r"(?<![\d.])(?:\$\s?(\d{1,2}(?:[.,]\d{1,2})?)|(\d{1,2}(?:[.,]\d{1,2})?)\s?\$)(?!\d)")
GPU_RE = re.compile(r"\b(GB300|GB200|B300|B200|H200|H100|VR|Vera Rubin)s?\b", re.I)
ACCEPT_RE = re.compile(r"ready to sign|would sign|agreed to|committed to sign|was prepared to sign|accepted", re.I)
COMP_HINT_RE = re.compile(r"offer|cheaper|less expensive|lower price|quote|going at|competitor|went with|chose|elsewhere|"
                          r"another provider|other provider|more expensive|match", re.I)


def _client():
    home = Path.home()
    os.environ.setdefault("YT_PROXY", "https://planck.yt.nebius.yt")
    os.environ.setdefault("YT_CONFIG_PROFILE", "planck")
    os.environ.setdefault("REQUESTS_CA_BUNDLE", str(home / ".yt" / "ca-certificates" / "planck"))
    if not os.environ.get("YT_TOKEN"):
        tok = home / ".yt" / "token_planck"
        if tok.exists():
            os.environ["YT_TOKEN"] = tok.read_text().strip()
    sys.path.insert(0, str(home / "nebo" / "analytics" / "libs" / "data_clients" / "yt_data_client"))
    from yt_data_client import YtDataClient  # noqa: E402
    return YtDataClient()


def sf_query(since: str) -> str:
    return f"""
SELECT o.salesforce_opportunity_id AS opp_id, toString(o.close_dt) AS closed, o.opportunity_name AS account,
       coalesce(o.loss_reason, '') AS category, coalesce(o.opportunity_closed_lost_competitors, '') AS competitors,
       coalesce(o.closed_lost_reason_description, '') AS text, o.prepayment,
       arrayStringConcat(groupArray(li.product_code), ' | ') AS products,
       arrayStringConcat(groupArray(toString(round(if((li.product_code LIKE '%GB200%' OR li.product_code LIKE '%GB300%') AND li.unit_price >= 40, li.unit_price / 72, li.unit_price), 2))), ' | ') AS our_prices,
       arrayStringConcat(groupArray(toString(li.oli_total_gpu_count)), ' | ') AS gpus,
       arrayStringConcat(groupArray(toString(dateDiff('day', li.oli_start_dt, li.oli_end_dt))), ' | ') AS term_days
FROM (SELECT salesforce_opportunity_id, close_dt, opportunity_name, loss_reason, opportunity_closed_lost_competitors,
             closed_lost_reason_description, prepayment FROM `{SF_OPP}`
      WHERE is_closed AND NOT is_won AND NOT coalesce(is_deleted, false) AND close_dt >= toDate('{max(since, CUTOVER)}')
        AND NOT opportunity_name LIKE 'JGSanity%'
        AND (length(coalesce(closed_lost_reason_description, '')) > 12 OR coalesce(opportunity_closed_lost_competitors, '') != '')) AS o
LEFT JOIN (SELECT salesforce_opportunity_id, product_code, unit_price, oli_total_gpu_count, oli_start_dt, oli_end_dt FROM `{SF_OLI}`
           WHERE NOT coalesce(is_deleted, false) AND (product_code LIKE 'Nebius Platform:%' OR product_code LIKE 'Bare Metal:%')
             AND product_code NOT LIKE '%TF%') AS li
  ON o.salesforce_opportunity_id = li.salesforce_opportunity_id
GROUP BY opp_id, closed, account, category, competitors, text, o.prepayment
ORDER BY closed DESC"""


def hs_query(since: str) -> str:
    return f"""
SELECT crm_deal_id AS opp_id, toString(toDate(min(close_utc_dttm))) AS closed, any(company_names_string) AS account,
       any(coalesce(closed_lost_reason_desc, '')) AS category, '' AS competitors,
       any(coalesce(closed_lost_reason_name, '')) AS text, CAST(NULL, 'Nullable(Float64)') AS prepayment,
       arrayStringConcat(groupArray(coalesce(gpu_model_canonical, product_name)), ' | ') AS products,
       arrayStringConcat(groupArray(toString(round(if(lower(coalesce(unit,'')) IN ('rack','racks'), unit_price_calculated / 72, unit_price_calculated), 2))), ' | ') AS our_prices,
       arrayStringConcat(groupArray(toString(if(lower(coalesce(unit,'')) IN ('rack','racks'), resource_quantity * 72, resource_quantity))), ' | ') AS gpus,
       arrayStringConcat(groupArray(toString(dateDiff('day', consumption_start_utc_dttm, consumption_end_utc_dttm))), ' | ') AS term_days
FROM `{HS_TABLE}`
WHERE overview_status = 'Actual overview' AND deal_stage_name IN ('Closed lost', 'Closed Lost')
  AND close_utc_dttm >= toDateTime('{since} 00:00:00') AND close_utc_dttm < toDateTime('{CUTOVER} 00:00:00')
  AND length(coalesce(closed_lost_reason_name, '')) > 12
  AND coalesce(product_name, '') NOT ILIKE '%token factory%'
GROUP BY opp_id
ORDER BY closed DESC"""


def classify(row: dict) -> list:
    text = row["text"] or ""
    cat = (row["category"] or "").lower()
    comps = {c for c in COMPETITORS if re.search(rf"\b{re.escape(c)}\b", text)}
    if row.get("competitors"):
        comps |= {c.strip() for c in str(row["competitors"]).split(";") if c.strip() and c.strip() != "Other"}
    prices = [float((a or b).replace(",", ".")) for a, b in PRICE_RE.findall(text)]
    prices = [p for p in prices if 0.3 <= p <= 40]
    gpus_in_text = sorted({m.upper().replace("VERA RUBIN", "VR") for m in GPU_RE.findall(text)})
    out = []
    capacity_loss = "capacity" in cat or "capacity" in text.lower()
    if prices and (comps or COMP_HINT_RE.search(text)) and not (ACCEPT_RE.search(text) and not comps):
        out.append({"type": "competitor_quote", "competitor": "; ".join(sorted(comps)) or "undisclosed",
                    "prices_in_text": " | ".join(f"{p:.2f}" for p in prices), "gpus_in_text": " | ".join(gpus_in_text)})
    if capacity_loss or ACCEPT_RE.search(text):
        out.append({"type": "accepted_price", "competitor": "; ".join(sorted(comps)),
                    "prices_in_text": " | ".join(f"{p:.2f}" for p in prices), "gpus_in_text": " | ".join(gpus_in_text)})
    if not out and comps:
        out.append({"type": "competitor_named", "competitor": "; ".join(sorted(comps)), "prices_in_text": "", "gpus_in_text": " | ".join(gpus_in_text)})
    return out


COLUMNS = ["extracted", "source", "opp_id", "closed", "account", "type", "competitor", "prices_in_text", "gpus_in_text",
           "category", "our_products", "our_prices", "our_gpus", "term_days", "prepayment", "text", "filed"]


def main(argv) -> int:
    since = argv[argv.index("--since") + 1] if "--since" in argv else "2026-01-01"
    dry = "--dry-run" in argv
    client = _client()
    today = date.today().isoformat()
    rows = []
    for source, q in (("salesforce", sf_query(since)), ("hubspot", hs_query(since))):
        if source == "hubspot" and since >= CUTOVER:
            continue
        res = client.run_query_to_df(q, engine="chyt", return_query_link=True)
        df = res["df"]
        n = 0 if df is None else len(df)
        print(f"{source}: {n} closed-lost opportunities with text  ({res['query_link']})")
        for r in ([] if df is None else df.to_dict("records")):
            for c in classify(r):
                rows.append({"extracted": today, "source": source, "opp_id": r["opp_id"], "closed": r["closed"], "account": r["account"],
                             **c, "category": r["category"], "our_products": r["products"], "our_prices": r["our_prices"],
                             "our_gpus": r["gpus"], "term_days": r["term_days"], "prepayment": r["prepayment"],
                             "text": (r["text"] or "").replace("\n", " ")[:600], "filed": ""})
    by_type = {}
    for r in rows:
        by_type[r["type"]] = by_type.get(r["type"], 0) + 1
    print(f"{len(rows)} candidates: {by_type}")
    if dry:
        for r in [x for x in rows if x["type"] == "competitor_quote"][:12]:
            print(f"  {r['closed']} {r['account'][:28]:28} {r['competitor'][:22]:22} ${r['prices_in_text']:<14} ours={r['our_prices'][:20]} :: {r['text'][:110]}")
        return 0
    existing = {}
    if OUT.exists():
        with open(OUT, newline="") as f:
            existing = {(r["opp_id"], r["type"]): r for r in csv.DictReader(f)}
    for r in rows:
        k = (r["opp_id"], r["type"])
        if k in existing and existing[k].get("filed"):
            r["filed"] = existing[k]["filed"]          # keep curation marks
        existing[k] = r
    out_rows = sorted(existing.values(), key=lambda r: r["closed"], reverse=True)
    with open(OUT, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS); w.writeheader(); w.writerows(out_rows)
    print(f"wrote {OUT} ({len(out_rows)} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
