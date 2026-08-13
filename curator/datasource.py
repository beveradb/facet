"""Read scored photos + faces from a facet SQLite DB (read-only)."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

# EXIF-style and ISO date strings both appear in facet's date_taken.
_DATE_FORMATS = ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S")


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    head = s.strip()[:19]
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(head, fmt)  # noqa: DTZ007 (facet date_taken is naive local)
        except ValueError:
            continue
    return None


def _decode_embedding(blob) -> np.ndarray | None:
    if not blob:
        return None
    try:
        arr = np.frombuffer(blob, dtype=np.float32)
    except (TypeError, ValueError):
        return None
    if arr.size == 0:
        return None
    norm = np.linalg.norm(arr)
    return arr / norm if norm else arr


@dataclass(eq=False)  # identity semantics: list.remove()/`in` must match THIS object,
class Photo:          # not another photo with equal field values (breaks the coverage swap)
    path: str
    filename: str
    dt: datetime | None
    lat: float | None
    lon: float | None
    category: str | None
    aggregate: float
    aesthetic: float
    comp: float
    face_quality: float
    face_ratio: float
    eyes_open: float
    expression: float
    is_blink: int
    is_rejected: int
    is_junk: bool
    is_dup_lead: int
    duplicate_group_id: int | None
    burst_group_id: int | None
    phash: str | None
    caption: str | None
    moment: str | None
    moment_conf: float
    emb: np.ndarray | None        # caption embedding (semantic event merge)
    img_emb: np.ndarray | None    # image embedding (visual near-duplicate dedup)
    camera: str | None
    persons: list[int] = field(default_factory=list)
    # enriched downstream:
    contributor: str = ""
    location: str | None = None
    day: str | None = None


_PHOTO_COLUMNS = """
    path, filename, date_taken, gps_latitude, gps_longitude, category,
    aggregate, aesthetic, comp_score, face_quality, face_ratio,
    eyes_open_score, expression_score,
    is_blink, is_rejected, is_duplicate_lead, duplicate_group_id, burst_group_id,
    phash, caption, narrative_moment, narrative_moment_confidence,
    caption_embedding, clip_embedding, camera_model, junk_kind
"""


def load_photos(db_path: str) -> list[Photo]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(f"SELECT {_PHOTO_COLUMNS} FROM photos").fetchall()
        faces: dict[str, list[int]] = {}
        for photo_path, person_id in conn.execute(
            "SELECT photo_path, person_id FROM faces WHERE person_id IS NOT NULL"
        ):
            faces.setdefault(photo_path, []).append(person_id)
    finally:
        conn.close()

    photos = []
    for r in rows:
        photos.append(
            Photo(
                path=r["path"],
                filename=r["filename"],
                dt=_parse_dt(r["date_taken"]),
                lat=r["gps_latitude"],
                lon=r["gps_longitude"],
                category=r["category"],
                aggregate=r["aggregate"] or 0.0,
                aesthetic=r["aesthetic"] or 0.0,
                comp=r["comp_score"] or 0.0,
                face_quality=r["face_quality"] or 0.0,
                face_ratio=r["face_ratio"] or 0.0,
                eyes_open=r["eyes_open_score"] if r["eyes_open_score"] is not None else 1.0,
                expression=r["expression_score"] if r["expression_score"] is not None else 0.5,
                is_blink=r["is_blink"] or 0,
                is_rejected=r["is_rejected"] or 0,
                is_junk=bool(r["junk_kind"] and r["junk_kind"] != "not_junk"),
                is_dup_lead=r["is_duplicate_lead"] or 0,
                duplicate_group_id=r["duplicate_group_id"],
                burst_group_id=r["burst_group_id"],
                phash=r["phash"],
                caption=r["caption"],
                moment=r["narrative_moment"],
                moment_conf=r["narrative_moment_confidence"] or 0.0,
                emb=_decode_embedding(r["caption_embedding"]),
                img_emb=_decode_embedding(r["clip_embedding"]),
                camera=r["camera_model"],
                persons=faces.get(r["path"], []),
            )
        )
    return photos


def load_persons(db_path: str) -> dict[int, str | None]:
    """person_id -> name. Name is None for unnamed clusters; the coverage pass
    only ensures NAMED (main) people appear, so we must not fabricate names."""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT id, name FROM persons").fetchall()
    finally:
        conn.close()
    return {pid: (name or None) for pid, name in rows}
