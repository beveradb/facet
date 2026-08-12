"""Within-bucket photo scoring and slot selection."""
from __future__ import annotations

from .datasource import Photo


def photo_score(p: Photo, cfg) -> float:
    w = cfg.rank_weights
    has_face = p.face_ratio >= 0.20
    if has_face:
        score = (
            w["aggregate"] * p.aggregate
            + w["aesthetic"] * p.aesthetic
            + w["composition"] * p.comp
            + w["face_quality"] * p.face_quality
        )
    else:
        # Split the face weight across aesthetic + composition when no real face.
        fq = w["face_quality"]
        score = (
            w["aggregate"] * p.aggregate
            + (w["aesthetic"] + fq / 2) * p.aesthetic
            + (w["composition"] + fq / 2) * p.comp
        )
    if p.is_blink:
        score -= cfg.penalty_blink
    if p.is_rejected:
        score -= cfg.penalty_rejected
    if has_face and (p.eyes_open < cfg.min_eyes_open or p.expression < 0.0):
        score -= cfg.penalty_face
    return score


def slot_best(slot: list[Photo], cfg) -> Photo:
    return max(slot, key=lambda p: photo_score(p, cfg))


def slot_score(slot: list[Photo], cfg) -> float:
    return photo_score(slot_best(slot, cfg), cfg)


def pick_bucket(bucket, quota: int, cfg) -> list[Photo]:
    """Return the representative photos of the top-`quota` slots in a bucket."""
    ranked = sorted(bucket.slots, key=lambda s: slot_score(s, cfg), reverse=True)
    return [slot_best(s, cfg) for s in ranked[:quota]]
