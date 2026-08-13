"""Collapse near-duplicates within a bucket into single quota slots.

Two photos are the same slot if facet already grouped them (duplicate_group_id),
their perceptual hashes are close, or — the cross-contributor case — their caption
embeddings are close and they were shot at nearly the same time (same moment, two
phones). Each slot later contributes at most one photo to the candidate set.
"""
from __future__ import annotations

from .bucketing import Bucket
from .datasource import Photo


def _phash_hamming(a: str | None, b: str | None) -> int | None:
    if not a or not b or len(a) != len(b):
        return None
    try:
        return (int(a, 16) ^ int(b, 16)).bit_count()
    except ValueError:
        return None


def _same_slot(p: Photo, q: Photo, cfg) -> bool:
    # facet's own groupings: exact/near duplicates and rapid bursts
    if p.duplicate_group_id is not None and p.duplicate_group_id == q.duplicate_group_id:
        return True
    if p.burst_group_id is not None and p.burst_group_id == q.burst_group_id:
        return True
    # cheap catch for truly near-identical frames
    ham = _phash_hamming(p.phash, q.phash)
    if ham is not None and ham <= cfg.phash_max:
        return True
    # same scene: visually near-identical IMAGE embeddings shot close in time.
    # Image embeddings (not captions) are what separate "same scene, people moved"
    # from genuinely different shots; this also collapses the same moment captured
    # by two different phones. Calibrated on real near-dupes (clip cos 0.90-0.98)
    # vs a distinct follow-up shot of the same subject (0.85).
    return (
        p.img_emb is not None
        and q.img_emb is not None
        and p.dt is not None
        and q.dt is not None
        and abs((p.dt - q.dt).total_seconds()) <= cfg.same_scene_minutes * 60
        and float(p.img_emb @ q.img_emb) >= cfg.scene_cos
    )


def dedup_bucket(bucket: Bucket, cfg) -> None:
    """Populate bucket.slots via union-find over near-duplicate relations."""
    photos = bucket.photos
    parent = list(range(len(photos)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(photos)):
        for j in range(i + 1, len(photos)):
            if _same_slot(photos[i], photos[j], cfg):
                parent[find(i)] = find(j)

    groups: dict[int, list[Photo]] = {}
    for i, p in enumerate(photos):
        groups.setdefault(find(i), []).append(p)
    bucket.slots = list(groups.values())
