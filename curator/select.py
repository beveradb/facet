"""Orchestrator: DB -> enrich -> bucket -> dedup -> allocate -> rank -> coverage."""
from __future__ import annotations

from dataclasses import dataclass, field

from . import allocate, bucketing, dedup, geocode, rank
from .datasource import Photo, load_persons, load_photos


@dataclass
class CuratorConfig:
    target_count: int = 100
    candidate_multiplier: float = 1.2  # emit ~20% extra as a buffer for the human cut
    # Reserved for v2 cross-contributor timezone normalization (tied to clock-skew
    # detection). Unused in v1: bucketing runs on each photo's EXIF-local date, so
    # there is deliberately no trip-specific timezone default.
    timezone: str | None = None
    min_per_day: int = 2
    min_per_event: int = 1
    scene_gap_minutes: int = 45
    merge_cos: float = 0.82
    phash_max: int = 8
    scene_cos: float = 0.90         # image-embedding cosine above which two frames are the same scene
    same_scene_minutes: int = 5     # ...and must be shot within this window to collapse
    day_weight_exponent: float = 0.6
    event_weight_exponent: float = 0.7
    rank_weights: dict = field(
        default_factory=lambda: {
            "aggregate": 0.30,
            "aesthetic": 0.28,
            "composition": 0.18,
            "face_quality": 0.24,
        }
    )
    penalty_blink: float = 3.0
    penalty_rejected: float = 5.0
    penalty_face: float = 1.0
    # coverage
    min_shots_per_person: int = 1
    min_face_quality: float = 6.0
    min_eyes_open: float = 0.5
    max_swap_cost: float = 1.5

    @property
    def candidate_budget(self) -> int:
        return round(self.target_count * self.candidate_multiplier)


@dataclass
class CurationResult:
    selected: list[Photo]
    buckets: list
    quotas: dict[str, int]
    persons: dict[int, str]
    contributors: dict[str, int]
    reference: str | None
    locations: dict[str, int]
    undated: list[Photo]
    excluded_junk: int = 0
    excluded_rejected: int = 0
    coverage_warnings: list[str] = field(default_factory=list)
    picks_by_bucket: dict[str, list[Photo]] = field(default_factory=dict)


def _flattering(p: Photo, cfg) -> bool:
    return (
        not p.is_blink
        and p.face_quality >= cfg.min_face_quality
        and p.eyes_open >= cfg.min_eyes_open
    )


def _coverage_pass(result: CurationResult, cfg: CuratorConfig) -> None:
    photo_slot = {id(p): slot for b in result.buckets for slot in b.slots for p in slot}
    selected = {id(p) for p in result.selected}

    def scene_taken(p: Photo) -> bool:
        # is this photo's slot (scene) already represented in the selection?
        slot = photo_slot.get(id(p))
        return slot is not None and any(id(q) in selected for q in slot)

    for pid, name in result.persons.items():
        if not name:
            continue  # only ensure NAMED (main) people appear; unnamed clusters are ignored
        shots = [p for p in result.selected if pid in p.persons and _flattering(p, cfg)]
        if len(shots) >= cfg.min_shots_per_person:
            continue
        # best flattering photo of this person from a scene NOT already selected
        # (never duplicate a scene just to cover a person)
        cands = [
            p
            for b in result.buckets
            for slot in b.slots
            for p in slot
            if pid in p.persons and _flattering(p, cfg) and id(p) not in selected and not scene_taken(p)
        ]
        if not cands:
            result.coverage_warnings.append(f"{name}: no flattering uncovered shot available")
            continue
        cand = max(cands, key=lambda p: rank.photo_score(p, cfg))
        # find the bucket that owns cand; try to swap its weakest pick above the floor
        target_bucket = next(
            (b for b in result.buckets if any(cand in slot for slot in b.slots)), None
        )
        if target_bucket is None:
            continue
        # A within-bucket swap preserves the bucket's quota, so it can never drop
        # a day/event below its floor — we only need something to swap against.
        picks = result.picks_by_bucket.get(target_bucket.id, [])
        if not picks:
            result.coverage_warnings.append(f"{name}: covering bucket has no selected slot to swap")
            continue
        weakest = min(picks, key=lambda p: rank.photo_score(p, cfg))
        cost = rank.photo_score(weakest, cfg) - rank.photo_score(cand, cfg)
        if cost <= cfg.max_swap_cost:
            picks.remove(weakest)
            picks.append(cand)
            result.selected.remove(weakest)
            result.selected.append(cand)
            selected.discard(id(weakest))
            selected.add(id(cand))
        else:
            result.coverage_warnings.append(f"{name}: best swap cost {cost:.1f} exceeds budget")


def curate(db_path: str, cfg: CuratorConfig | None = None) -> CurationResult:
    cfg = cfg or CuratorConfig()
    all_photos = load_photos(db_path)
    persons = load_persons(db_path)

    # A curated trip album must never contain screenshots/documents/receipts/memes
    # (facet's junk detection) or photos the user already rejected.
    n_junk = sum(1 for p in all_photos if p.is_junk)
    n_rejected = sum(1 for p in all_photos if p.is_rejected and not p.is_junk)
    photos = [p for p in all_photos if not p.is_junk and not p.is_rejected]

    contributors, reference = geocode.assign_contributors(photos)
    locations = geocode.assign_locations(photos)

    buckets, undated = bucketing.build_buckets(photos, cfg)
    for b in buckets:
        dedup.dedup_bucket(b, cfg)

    quotas = allocate.allocate_quotas(buckets, cfg.candidate_budget, cfg)

    selected: list[Photo] = []
    picks_by_bucket: dict[str, list[Photo]] = {}
    for b in buckets:
        picks = rank.pick_bucket(b, quotas.get(b.id, 0), cfg)
        picks_by_bucket[b.id] = picks
        selected.extend(picks)

    result = CurationResult(
        selected=selected,
        buckets=buckets,
        quotas=quotas,
        persons=persons,
        contributors=contributors,
        reference=reference,
        locations=locations,
        undated=undated,
        excluded_junk=n_junk,
        excluded_rejected=n_rejected,
        picks_by_bucket=picks_by_bucket,
    )
    _coverage_pass(result, cfg)
    return result
