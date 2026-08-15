"""Quota allocation: distribute the candidate budget across buckets.

Two-level apportionment — first across days (sub-linear in photo count so a huge
day can't dominate, with a per-day floor so no day drops out), then across each
day's events. Quotas never exceed a bucket's available slots; totals are settled
with a largest-remainder pass so the budget is hit exactly.
"""
from __future__ import annotations

from collections import defaultdict


def _apportion(keys, total, weights, floors, caps) -> dict:
    """Distribute `total` across keys by weights, honoring floors and caps."""
    total = min(total, sum(caps.values()))
    alloc = {k: min(floors.get(k, 0), caps[k]) for k in keys}
    remaining = total - sum(alloc.values())

    while remaining > 0:
        headroom = [k for k in keys if alloc[k] < caps[k]]
        if not headroom:
            break
        wsum = sum(weights[k] for k in headroom) or 1.0
        fracs = []
        assigned = 0
        for k in headroom:
            share = weights[k] / wsum * remaining
            base = min(int(share), caps[k] - alloc[k])
            alloc[k] += base
            assigned += base
            fracs.append((share - int(share), k))
        remaining -= assigned
        if assigned == 0:  # all shares < 1: give one to the largest fraction
            for _, k in sorted(fracs, reverse=True):
                if alloc[k] < caps[k]:
                    alloc[k] += 1
                    remaining -= 1
                    break
            else:
                break
    return alloc


def allocate_quotas(buckets, target: int, cfg) -> dict[str, int]:
    by_day = defaultdict(list)
    for b in buckets:
        by_day[b.day].append(b)

    day_caps = {d: sum(len(b.slots) for b in bs) for d, bs in by_day.items()}
    day_weights = {d: (day_caps[d] ** cfg.day_weight_exponent) for d in by_day}
    day_floors = {d: min(cfg.min_per_day, day_caps[d]) for d in by_day}
    day_quota = _apportion(list(by_day), target, day_weights, day_floors, day_caps)

    quotas: dict[str, int] = {}
    for d, bs in by_day.items():
        keys = [b.id for b in bs]
        caps = {b.id: len(b.slots) for b in bs}
        weights = {b.id: (len(b.slots) ** cfg.event_weight_exponent) for b in bs}
        floors = {b.id: min(cfg.min_per_event, caps[b.id]) for b in bs}
        quotas.update(_apportion(keys, day_quota[d], weights, floors, caps))
    return quotas
