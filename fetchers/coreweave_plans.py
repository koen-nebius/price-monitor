"""Public commercial-plan terms, separate from rates and bookable capacity."""
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser

URL = "https://www.coreweave.com/coreweave-capacity-plans"
LAST_CATALOGUE_OFFERS = []
LAST_FETCH_HEALTH = {}


class _Tables(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables, self.rows, self.cells, self.text = [], None, None, None

    def handle_starttag(self, tag, attrs):
        if tag == "table": self.rows = []
        elif tag == "tr" and self.rows is not None: self.cells = []
        elif tag in {"td", "th"} and self.cells is not None: self.text = []

    def handle_data(self, text):
        if self.text is not None: self.text.append(text)

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self.text is not None:
            self.cells.append(" ".join(" ".join(self.text).split()))
            self.text = None
        elif tag == "tr" and self.cells is not None:
            self.rows.append(self.cells); self.cells = None
        elif tag == "table" and self.rows is not None:
            self.tables.append(self.rows); self.rows = None


def parse(html, observed_at):
    parser = _Tables(); parser.feed(html)
    plans = ("Flex Reservations", "Reservations", "Spot", "On-demand")
    purchase = ("flex_reservation", "reservation", "spot", "on_demand")
    for table in parser.tables:
        if not table: continue
        header = table[0][-4:]
        if len(header) != 4 or not all(cell.startswith(plan) for cell, plan in zip(header, plans)):
            continue
        rows = {row[0]: row[1:] for row in table[1:] if len(row) == 5}
        required = ("Capacity", "Interruptible", "Pay-as-you-go", "Commitment")
        if not all(key in rows for key in required):
            raise ValueError("Incomplete capacity-plan table")
        return [dict(provider="coreweave", product_id=plan, gpu_model="", region="unknown",
                     product_family="gpu_compute_plan", purchase_type=kind, price_status="plan_terms",
                     gpu_count=None, gpu_count_relation="unknown", source_url=URL,
                     observed_at=observed_at, retrieved_at=observed_at,
                     description="Portfolio-level plan terms; no GPU, quantity, price or available capacity established.",
                     commercial_terms={key.lower().replace("-", "_").replace(" ", "_"): rows[key][i]
                                       for key in required})
                for i, (plan, kind) in enumerate(zip(plans, purchase))]
    raise ValueError("Capacity-plan table not found")


def fetch(regions=None):
    global LAST_CATALOGUE_OFFERS, LAST_FETCH_HEALTH
    LAST_CATALOGUE_OFFERS = []
    try:
        req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0 (price-monitor/1.0)"})
        with urllib.request.urlopen(req, timeout=30) as response:
            html = response.read(4_000_001)
        if len(html) > 4_000_000: raise ValueError("Page exceeds size limit")
        stamp = datetime.now(timezone.utc).isoformat()
        LAST_CATALOGUE_OFFERS = parse(html.decode("utf-8"), stamp)
        LAST_FETCH_HEALTH = {"status": "catalogue_only", "catalogue_count": len(LAST_CATALOGUE_OFFERS),
                             "reason": "Public plan terms; numeric charges and GPU-specific coverage require separate evidence"}
    except Exception as exc:
        LAST_FETCH_HEALTH = {"status": "failed", "reason": "Public capacity-plan retrieval or parsing failed",
                             "error_code": type(exc).__name__}
    return []
