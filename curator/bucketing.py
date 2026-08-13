"""Bucketing: day -> location -> time-gap scene -> semantic refine.

A Bucket is one coherent event: a contiguous run of photos on one day, at one
place, within a capture-time gap, optionally merged across a lull when the
captions are semantically close. Photos without a date can't be placed and are
returned separately.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

import numpy as np

from .datasource import Photo


@dataclass
class Bucket:
    day: str
    location: str | None
    event: str
    seq: int = -1  # unique index within the run; assigned by build_buckets
    photos: list[Photo] = field(default_factory=list)
    slots: list[list[Photo]] = field(default_factory=list)  # after dedup; each slot = near-dupes

    @property
    def id(self) -> str:
        # Must be unique: two scenes can share day/location/event (split by a gap),
        # and quotas/picks are keyed by id — a collision silently drops a bucket.
        return f"{self.day}|{self.location or '?'}|{self.event}#{self.seq}"

    def label(self) -> str:
        loc = self.location or "?"
        return f"{self.day} · {loc} · {self.event}"


def _mean_emb(photos: list[Photo]) -> np.ndarray | None:
    embs = [p.emb for p in photos if p.emb is not None]
    if not embs:
        return None
    m = np.mean(np.stack(embs), axis=0)
    n = np.linalg.norm(m)
    return m / n if n else m


def _event_label(photos: list[Photo]) -> str:
    moments = [p.moment for p in photos if p.moment and p.moment != "other"]
    if moments:
        return max(set(moments), key=moments.count)
    return "other"


def build_buckets(photos: list[Photo], cfg) -> tuple[list[Bucket], list[Photo]]:
    dated = [p for p in photos if p.dt is not None]
    undated = [p for p in photos if p.dt is None]
    for p in dated:
        p.day = p.dt.strftime("%Y-%m-%d")
    dated.sort(key=lambda p: p.dt)

    gap = timedelta(minutes=cfg.scene_gap_minutes)
    raw: list[Bucket] = []
    cur: Bucket | None = None
    for p in dated:
        new_scene = (
            cur is None
            or p.day != cur.day
            or (p.location or None) != (cur.location or None)
            or (p.dt - cur.photos[-1].dt) > gap
        )
        if new_scene:
            cur = Bucket(day=p.day, location=p.location, event="other")
            raw.append(cur)
        cur.photos.append(p)

    # Semantic merge: fold an adjacent same-day/place scene into the previous one
    # when their mean caption embeddings are close (one event split by a lull).
    merged: list[Bucket] = []
    for b in raw:
        if merged:
            prev = merged[-1]
            same_context = prev.day == b.day and (prev.location or None) == (b.location or None)
            e1, e2 = _mean_emb(prev.photos), _mean_emb(b.photos)
            if same_context and e1 is not None and e2 is not None and float(e1 @ e2) >= cfg.merge_cos:
                prev.photos.extend(b.photos)
                continue
        merged.append(b)

    for i, b in enumerate(merged):
        b.event = _event_label(b.photos)
        b.seq = i
    return merged, undated
