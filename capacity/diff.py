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
        if n.metric_type == "stock_status_label":
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
            entries.append(CapacityDiffEntry(
                *key[:4], instance_type=key[4], change_type="removed",
                old_state=o.state, old_value=o.metric_value,
                detail="signal disappeared from source",
            ))

    logger.info(f"Capacity diff: {len(entries)} changes")
    return entries
