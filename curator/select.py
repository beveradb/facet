"""Orchestrator: DB -> enrich -> bucket -> dedup -> allocate -> rank -> coverage."""
from __future__ import annotations

from dataclasses import dataclass, field

from . import allocate, bucketing, dedup, geocode, rank
from .datasource import Photo, load_persons, load_photos


@dataclass
class CuratorConfig:
    target_count: int = 100
    candidate_multiplier: float = 1.5
    timezone: str = "Europe/Amsterdam"
    min_per_day: int = 2
    min_per_event: int = 1
    scene_gap_minutes: int = 45
    merge_cos: float = 0.82
    phash_max: int = 8
    dup_cos: float = 0.90
    same_moment_minutes: int = 3
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
    coverage_warnings: list[str] = field(default_factory=list)
    picks_by_bucket: dict[str, list[Photo]] = field(default_factory=dict)


def _flattering(p: Photo, cfg) -> bool:
    return (
        not p.is_blink
        and p.face_quality >= cfg.min_face_quality
        and p.eyes_open >= cfg.min_eyes_open
    )


def _coverage_pass(result: CurationResult, cfg: CuratorConfig) -> None:
    selected = {id(p) for p in result.selected}
    for pid, name in result.persons.items():
        shots = [p for p in result.selected if pid in p.persons and _flattering(p, cfg)]
        if len(shots) >= cfg.min_shots_per_person:
            continue
        # best unselected flattering photo of this person
        cands = [
            p
            for b in result.buckets
            for slot in b.slots
            for p in slot
            if pid in p.persons and _flattering(p, cfg) and id(p) not in selected
        ]
        if not cands:
            result.coverage_warnings.append(f"{name}: no flattering shot available at all")
            continue
        cand = max(cands, key=lambda p: rank.photo_score(p, cfg))
        # find the bucket that owns cand; try to swap its weakest pick above the floor
        target_bucket = next(
            (b for b in result.buckets if any(cand in slot for slot in b.slots)), None
        )
        if target_bucket is None:
            continue
        picks = result.picks_by_bucket.get(target_bucket.id, [])
        floor = min(cfg.min_per_event, len(target_bucket.slots))
        if len(picks) <= floor:
            result.coverage_warnings.append(f"{name}: covering bucket at floor; not forced")
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
    photos = load_photos(db_path)
    persons = load_persons(db_path)

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
        picks_by_bucket=picks_by_bucket,
    )
    _coverage_pass(result, cfg)
    return result
