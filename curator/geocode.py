"""Enrichment: contributor identity, GPS reverse-geocoding, location track.

Only the album owner's device keeps GPS (Google strips other contributors'), so
we reverse-geocode the GPS'd photos into a time-ordered location track and assign
every photo — GPS or not — the nearest-in-time place. Contributor is derived from
camera model + filename prefix; the contributor with the most GPS is the trusted
reference clock.
"""
from __future__ import annotations

import re
import statistics
from bisect import bisect_left

from .datasource import Photo

_PREFIX_RE = re.compile(r"^([A-Za-z]+)[-_]")


def _contributor_key(p: Photo) -> str:
    m = _PREFIX_RE.match(p.filename or "")
    prefix = m.group(1).upper() if m else ""
    camera = (p.camera or "?").strip()
    return f"{camera}" if not prefix else f"{camera}/{prefix}"


def assign_contributors(photos: list[Photo]) -> tuple[dict[str, int], str | None]:
    """Tag each photo with .contributor; return (counts, reference_key)."""
    gps_counts: dict[str, int] = {}
    counts: dict[str, int] = {}
    for p in photos:
        key = _contributor_key(p)
        p.contributor = key
        counts[key] = counts.get(key, 0) + 1
        if p.lat is not None and p.lon is not None:
            gps_counts[key] = gps_counts.get(key, 0) + 1
    reference = max(gps_counts, key=gps_counts.get) if gps_counts else None
    return counts, reference


def _place_name(hit: dict) -> str:
    city = hit.get("name") or ""
    cc = hit.get("cc") or ""
    return f"{city}, {cc}".strip(", ") or "unknown"


def assign_locations(photos: list[Photo]) -> dict[str, str]:
    """Reverse-geocode GPS'd photos, build a time track, assign nearest-in-time
    location to every photo. Returns {place -> count}. Degrades gracefully when
    reverse_geocoder is unavailable or there is no GPS."""
    gps = [p for p in photos if p.lat is not None and p.lon is not None and p.dt is not None]
    if not gps:
        return {}

    # Reject spatial outliers (e.g. a stray photo from another continent) so they
    # don't poison the nearest-in-time assignment for unrelated timestamps.
    med_lat = statistics.median(p.lat for p in gps)
    med_lon = statistics.median(p.lon for p in gps)
    kept = [p for p in gps if abs(p.lat - med_lat) < 5 and abs(p.lon - med_lon) < 5]

    try:
        import reverse_geocoder as rg

        hits = rg.search([(p.lat, p.lon) for p in kept], mode=1)  # mode=1: single-threaded
    except Exception:  # noqa: BLE001 — geocoding is best-effort; degrade to day+gap
        return {}

    track = sorted((p.dt, _place_name(h)) for p, h in zip(kept, hits))
    if not track:
        return {}
    track_times = [t for t, _ in track]

    counts: dict[str, int] = {}
    for p in photos:
        if p.dt is None:
            continue
        i = bisect_left(track_times, p.dt)
        cands = []
        if i < len(track):
            cands.append(track[i])
        if i > 0:
            cands.append(track[i - 1])
        if not cands:
            continue
        _, place = min(cands, key=lambda tp: abs((tp[0] - p.dt).total_seconds()))
        p.location = place
        counts[place] = counts.get(place, 0) + 1
    return counts
