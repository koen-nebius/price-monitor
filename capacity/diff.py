"""
Day-over-day availability diff.

The signal that matters for supply/demand reads is the TRANSITION:
sold_out -> available (restock), available -> sold_out (demand ate supply),
plus large moves in quantitative metrics (marketplace depth, stock counts,
lead times). Small metric wiggles are noise — thresholded below.
"""
import logging
from typing import Dict, List, Tuple

from capacity.schema import AvailabilityRecord, CapacityDiffEntry
from capacity.insights import is_lambda_instance

logger = logging.getLogger(__name__)

# Relative move in a quantitative metric worth reporting (e.g. Vast offer
# depth, Hyperstack stock counts). Lead times use an absolute day threshold.
METRIC_MOVE_PCT = 30.0
LEAD_TIME_MOVE_DAYS = 5.0

# States whose transitions are always reported (not_offered/unknown churn is
# usually a fetcher artifact, so only meaningful pairs alert).
_MEANINGFUL = {"available", "limited", "sold_out"}


def _key(r: AvailabilityRecord) -> Tuple[str, str, str, str, str]:
    # instance_type is part of the identity: RunPod's H100 SXM/NVL/PCIe are
    # three different products; collapsing them made the diff report phantom
    # "limited → available" flips whenever the surviving variant changed.
    return (r.provider, r.gpu_model, r.region, r.consumption_type, r.instance_type)


def _index(records: List[AvailabilityRecord]) -> Dict[Tuple, AvailabilityRecord]:
    out: Dict[Tuple, AvailabilityRecord] = {}
    for r in records:
        if r.provider == "lambda" and not is_lambda_instance(r):
            continue
        if r.provider == "together" and not (
                r.product_scope == "dedicated_inference"
                and r.metric_type == "inference_replicas"
                and r.data_source == "official_api" and r.instance_type
                and r.region != "global"):
            # Legacy synthesized rows are quarantined, not a disappearance or
            # stock change when the new product-specific feed takes over.
            continue
        # Keep the most-available state per key when duplicates exist
        cur = out.get(_key(r))
        if cur is None or _rank(r.state) < _rank(cur.state):
            out[_key(r)] = r
    return out


def _rank(state: str) -> int:
    order = {"available": 0, "limited": 1, "sold_out": 2, "not_offered": 3, "unknown": 4}
    return order.get(state, 5)


def compute_diff(new: List[AvailabilityRecord],
                 old: List[AvailabilityRecord]) -> List[CapacityDiffEntry]:
    new_idx, old_idx = _index(new), _index(old)
    entries: List[CapacityDiffEntry] = []

    for key, n in new_idx.items():
        o = old_idx.get(key)
        if o is None:
            if n.state in _MEANINGFUL:
                entries.append(CapacityDiffEntry(
                    *key[:4], instance_type=key[4], change_type="added",
                    new_state=n.state, new_value=n.metric_value,
                    detail=f"now tracked: {n.state}" + (f" ({n.detail})" if n.detail else ""),
                ))
            continue

        # A source upgrade is not a stock transition. Crusoe's historical
        # docs footprint and authenticated per-configuration quantities are
        # different observations, even if their identity happens to match.
        if n.provider == "crusoe" and (
                n.metric_type != o.metric_type or n.data_source != o.data_source):
            continue
        if n.provider == "together" and (
                n.product_scope != o.product_scope or n.gpu_count != o.gpu_count
                or n.metric_type != o.metric_type or n.data_source != o.data_source
                or n.quantity_relation != o.quantity_relation):
            continue
        if n.provider == "lambda" and (
                n.product_scope != o.product_scope or n.gpu_count != o.gpu_count
                or n.metric_type != o.metric_type or n.data_source != o.data_source):
            continue

        if n.state != o.state and (n.state in _MEANINGFUL or o.state in _MEANINGFUL):
            # unknown<->anything churn is fetcher noise, skip unless it involves
            # two meaningful states (e.g. available -> sold_out).
            if n.state in _MEANINGFUL and o.state in _MEANINGFUL:
                entries.append(CapacityDiffEntry(
                    *key[:4], instance_type=key[4], change_type="state_change",
                    old_state=o.state, new_state=n.state,
                    old_value=o.metric_value, new_value=n.metric_value,
                    detail=n.detail,
                ))
            continue

        # Same state — check quantitative moves. Ordinal label ranks
        # (stock_status_label) are not quantities: a "1 → 0 (-100%)" bullet is
        # noise when the state itself did not change.
        if n.metric_type in {"stock_status_label", "launchable_regions", "instance_launchability"}:
            continue
        if n.metric_type == "inference_replicas" and (
                n.quantity_relation != "RELATION_EQ" or o.quantity_relation != "RELATION_EQ"):
            # Two lower bounds do not establish a percentage change in stock.
            continue
        if n.metric_value is not None and o.metric_value is not None \
                and n.metric_type == o.metric_type:
            if n.metric_type == "lead_time_days":
                if abs(n.metric_value - o.metric_value) >= LEAD_TIME_MOVE_DAYS:
                    entries.append(CapacityDiffEntry(
                        *key[:4], instance_type=key[4], change_type="metric_move",
                        old_state=o.state, new_state=n.state,
                        old_value=o.metric_value, new_value=n.metric_value,
                        detail=f"lead time {o.metric_value:.0f}d → {n.metric_value:.0f}d",
                    ))
            elif o.metric_value > 0:
                pct = (n.metric_value - o.metric_value) / o.metric_value * 100
                if abs(pct) >= METRIC_MOVE_PCT:
                    entries.append(CapacityDiffEntry(
                        *key[:4], instance_type=key[4], change_type="metric_move",
                        old_state=o.state, new_state=n.state,
                        old_value=o.metric_value, new_value=n.metric_value,
                        detail=f"{n.metric_type} {o.metric_value:g} → {n.metric_value:g} ({pct:+.0f}%)",
                    ))

    for key, o in old_idx.items():
        if key not in new_idx and o.state in _MEANINGFUL:
            if o.provider == "lambda":
                # A missing SKU or unreadable region list says nothing about
                # stock. Only a complete comparable exact-SKU summary can
                # establish that a previously listed region was removed.
                summary_key = (o.provider, o.gpu_model, "global", o.consumption_type, o.instance_type)
                summary = new_idx.get(summary_key)
                if (o.region == "global" or summary is None
                        or summary.state not in {"available", "sold_out"}
                        or summary.gpu_count != o.gpu_count):
                    continue
            entries.append(CapacityDiffEntry(
                *key[:4], instance_type=key[4], change_type="removed",
                old_state=o.state, old_value=o.metric_value,
                detail=("region no longer listed as launchable for this exact VM SKU"
                        if o.provider == "lambda" else "signal disappeared from source"),
            ))

    logger.info(f"Capacity diff: {len(entries)} changes")
    return entries
