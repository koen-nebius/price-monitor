"""Massed Compute account catalogue from its official read-only MCP inventory.

Prices are per instance in USD cents, not per GPU. Exact SKU variants survive:
GPU count, spot, form factor, RAM and local storage must not be collapsed to a
cheapest provider/model observation at ingestion. Authentication establishes
an account catalogue price, not a universal public-list rate. Stock regions
are deliberately left to the capacity adapter: they do not establish regional
price applicability or multi-node fabric.
"""
import logging
import math
import re
from datetime import datetime, timezone
from typing import List, Optional

from massedcompute_api import API_URL, fetch_inventory
from schema import PriceRecord

logger = logging.getLogger(__name__)
SOURCE_URL = API_URL
PARSER_VERSION = "massedcompute-price-1.0"
_SKU = re.compile(r"^gpu_([1-9]\d*)x_(.+)$", re.I)
_COUNT = re.compile(r"^\s*(\d+)\s*[x×]\s*", re.I)
_MODEL = re.compile(r"(?<![a-z0-9])(GB300|GB200|B300|B200|H200|H100|L40S)(?![a-z0-9])", re.I)
_RTX = re.compile(r"(?<![a-z0-9])(?:rtx[\s_-]*)?pro[\s_-]*6000[\s_-]*blackwell(?![a-z0-9])", re.I)


def _models(text: str) -> set:
    models = {m.upper() for m in _MODEL.findall(text)}
    if _RTX.search(text):
        models.add("RTX6000")
    return models


def _positive(value, integer=False):
    """Accept finite numeric API fields, excluding booleans and silent rounding."""
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        return None
    if integer and value != int(value):
        return None
    return int(value) if integer else float(value)


def _form_factor(text: str, model: str) -> Optional[str]:
    explicit = set()
    if re.search(r"(?<![a-z0-9])sxm\d*(?![a-z0-9])", text, re.I):
        explicit.add("SXM")
    if re.search(r"(?<![a-z0-9])nvl(?![a-z0-9])", text, re.I):
        explicit.add("NVL")
    if re.search(r"(?<![a-z0-9])pci[\s_-]*e(?![a-z0-9])", text, re.I):
        explicit.add("PCIe")
    # NVL is a PCIe board variant; keep the more specific label if both appear.
    if "NVL" in explicit:
        explicit.discard("PCIe")
    if len(explicit) > 1:
        return None
    if explicit:
        return explicit.pop()
    return "PCIe" if model in {"L40S", "RTX6000"} else "unknown"


def normalize_offer(sku: str, entry: dict, fetched_at: str = "") -> Optional[PriceRecord]:
    """Normalize one exact inventory SKU; reject malformed or conflicting facts."""
    if not isinstance(sku, str) or not isinstance(entry, dict):
        return None
    match = _SKU.fullmatch(sku)
    instance = entry.get("instance_type")
    if not match or not isinstance(instance, dict):
        return None
    count = int(match.group(1))
    name = instance.get("name", sku)
    if not isinstance(name, str) or name.lower() != sku.lower():
        return None
    models = _models(match.group(2))
    if len(models) != 1:
        return None
    model = models.pop()
    description = instance.get("description", "")
    if not isinstance(description, str):
        return None
    described_count = _COUNT.match(description)
    if described_count and int(described_count.group(1)) != count:
        return None
    described_models = _models(description)
    if described_models and described_models != {model}:
        return None
    specs = instance.get("specs", {})
    if not isinstance(specs, dict):
        return None
    # Current API omits GPU count from specs. If later supplied, verify it
    # instead of allowing it to overwrite the count in the priced SKU.
    for field in ("gpus", "gpu_count"):
        if field in specs and _positive(specs[field], integer=True) != count:
            return None
    cents = _positive(instance.get("price_cents_per_hour"))
    if cents is None:
        return None
    text = sku + " " + description
    # Inventory currently contains ordinary hourly and explicitly labelled spot
    # SKUs. Do not silently treat a future reserved/committed tier as on-demand.
    if re.search(r"(?<![a-z0-9])(?:reserved|committed|preemptible)(?![a-z0-9])", text, re.I):
        return None
    consumption = "spot" if re.search(r"(?<![a-z0-9])spot(?![a-z0-9])", text, re.I) else "on_demand"
    form_factor = _form_factor(text, model)
    if form_factor is None:
        return None
    hourly = cents / 100.0
    return PriceRecord(
        provider="massedcompute", gpu_model=model, gpu_count=count,
        instance_type=sku, region="unspecified", consumption_type=consumption,
        price_per_hour_usd=hourly, price_per_gpu_hour_usd=hourly / count,
        vcpu=_positive(specs.get("vcpu_count"), integer=True),
        # Existing schema stores the source's published GiB quantity in ram_gb.
        ram_gb=_positive(specs.get("memory_gib")),
        storage_gb=_positive(specs.get("storage_gb")),
        fetched_at=fetched_at, source_url=SOURCE_URL, data_source="official_api",
        parser_version=PARSER_VERSION, price_basis="account_catalog",
        form_factor=form_factor, interconnect="unknown",
    )


def parse(payload: dict, fetched_at: str) -> List[PriceRecord]:
    inventory = payload.get("gpu_inventory") if isinstance(payload, dict) else None
    if not isinstance(inventory, dict):
        raise ValueError("Massed inventory response has no gpu_inventory object")
    records = []
    for sku, entry in inventory.items():
        record = normalize_offer(sku, entry, fetched_at)
        if record is not None:
            records.append(record)
    return records


def fetch() -> List[PriceRecord]:
    try:
        return parse(fetch_inventory(), datetime.now(timezone.utc).isoformat())
    except Exception as exc:
        # Never log authenticated request, response, or arbitrary error details.
        logger.error("Massed pricing fetch failed (%s)", type(exc).__name__)
        return []
