"""Album Curator review router.

Consumes the read-only ``curator`` selection engine (``curator.curate``) and
exposes a review surface: run the curation, list the candidate pool grouped by
day → event, tick/untick individual items, and save the kept set as a facet
album for export.

Persistence of the human edits lives in the ``curator_selection`` side table
(``photo_path`` → ``kept``), NOT a column on ``photos`` — ``photos`` is rewritten
wholesale on rescan (see CLAUDE.md), so a per-photo flag there would be wiped.
The structural pool (buckets, per-item metadata, the auto-selection) is cached in
``curator_pool_cache`` by ``run`` so ``candidates`` / ``toggle`` are cheap reads
that just overlay the user's kept set over the auto-selection. ``run`` is the only
expensive call (loads photos + reverse-geocodes, a few seconds); it seeds the side
table with the auto-selection without clobbering existing edits (``INSERT OR
IGNORE``), so ``save_album`` has something to save even before the user touches a
tick.
"""

import json
import logging
import os
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from api.auth import CurrentUser, get_optional_user, require_edition
from api.database import get_db
from curator import CuratorConfig, curate, rank
from db import DEFAULT_DB_PATH

router = APIRouter(tags=["curator"])
logger = logging.getLogger(__name__)

# Extensions the review UI should badge as clips. The engine only emits stills
# today (video-as-candidate is roadmap 9c), but the item ``type`` is derived
# from the path so clips render correctly the moment they appear in the pool.
_VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".mts", ".m2ts"}


# --- Request models ---

class ToggleRequest(BaseModel):
    id: str = Field(..., min_length=1)
    selected: bool


class SaveAlbumRequest(BaseModel):
    name: str = Field("Curated Final", min_length=1, max_length=200)


# --- Side-table bootstrap ---

def _init_tables(conn) -> None:
    """Create the curator side tables on first use (idempotent)."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS curator_selection ("
        "  photo_path TEXT PRIMARY KEY,"
        "  kept INTEGER NOT NULL DEFAULT 0"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS curator_pool_cache ("
        "  id INTEGER PRIMARY KEY CHECK (id = 1),"
        "  payload TEXT NOT NULL,"
        "  created_at TEXT DEFAULT (datetime('now'))"
        ")"
    )


# --- Pool construction ---

def _item_type(path: str) -> str:
    return "video" if os.path.splitext(path)[1].lower() in _VIDEO_EXTS else "photo"


def _thumb_url(path: str) -> str:
    return f"/thumbnail?path={quote(path, safe='')}&size=320"


def _build_pool(result, cfg: CuratorConfig) -> dict:
    """Turn a ``CurationResult`` into the cacheable pool payload.

    One representative per slot per bucket. A slot's rep is the auto-pick when
    that slot was picked (so every auto-selected photo is visible and marked),
    otherwise the slot's best photo. ``auto_selected`` records the engine's
    choice; the user's overlay is applied at read time.
    """
    buckets = []
    total = 0
    for b in result.buckets:
        picks = result.picks_by_bucket.get(b.id, [])
        items = []
        for slot in b.slots:
            pick = next((p for p in picks if any(p is q for q in slot)), None)
            rep = pick if pick is not None else rank.slot_best(slot, cfg)
            items.append({
                "id": rep.path,
                "type": _item_type(rep.path),
                "thumb": _thumb_url(rep.path),
                "caption": rep.caption,
                "category": rep.category,
                "moment": rep.moment,
                "score": round(rep.aggregate, 1),
                "duration": None,
                "auto_selected": pick is not None,
            })
        total += len(items)
        buckets.append({
            "id": b.id,
            "day": b.day,
            "location": b.location,
            "event": b.event,
            "quota": result.quotas.get(b.id, 0),
            "items": items,
        })
    return {"target": cfg.target_count, "total": total, "buckets": buckets}


# --- Read helpers ---

def _load_pool(conn) -> Optional[dict]:
    row = conn.execute("SELECT payload FROM curator_pool_cache WHERE id = 1").fetchone()
    if not row:
        return None
    try:
        return json.loads(row["payload"])
    except (ValueError, TypeError):
        return None


def _kept_map(conn) -> dict[str, int]:
    return {
        r["photo_path"]: r["kept"]
        for r in conn.execute("SELECT photo_path, kept FROM curator_selection")
    }


def _is_selected(item: dict, kept: dict[str, int]) -> bool:
    """Overlay the user's edit over the auto-selection (auto used when no row)."""
    row = kept.get(item["id"])
    return bool(row) if row is not None else bool(item["auto_selected"])


def _candidates_payload(conn) -> dict:
    """Assemble the response: pool + overlaid selection + running count."""
    pool = _load_pool(conn)
    if pool is None:
        return {"target": CuratorConfig().target_count, "total": 0,
                "selected_count": 0, "needs_run": True, "buckets": []}
    kept = _kept_map(conn)
    selected_count = 0
    out_buckets = []
    for b in pool["buckets"]:
        out_items = []
        for it in b["items"]:
            sel = _is_selected(it, kept)
            if sel:
                selected_count += 1
            out_items.append({
                "id": it["id"], "type": it["type"], "thumb": it["thumb"],
                "caption": it["caption"], "category": it["category"],
                "moment": it["moment"], "score": it["score"],
                "duration": it["duration"], "selected": sel,
            })
        out_buckets.append({
            "id": b["id"], "day": b["day"], "location": b["location"],
            "event": b["event"], "quota": b["quota"], "items": out_items,
        })
    return {"target": pool["target"], "total": pool["total"],
            "selected_count": selected_count, "needs_run": False,
            "buckets": out_buckets}


def _selected_count(conn) -> int:
    pool = _load_pool(conn)
    if pool is None:
        return 0
    kept = _kept_map(conn)
    return sum(
        1 for b in pool["buckets"] for it in b["items"] if _is_selected(it, kept)
    )


# --- Endpoints ---

@router.post("/api/curator/run")
def run_curation(user: CurrentUser = Depends(require_edition)):
    """Run the curator, cache the pool, seed the kept set with the auto-selection.

    Seeding is ``INSERT OR IGNORE`` so a re-run refreshes the pool and picks up
    new candidates without discarding edits the user already made.
    """
    cfg = CuratorConfig()
    result = curate(DEFAULT_DB_PATH, cfg)
    pool = _build_pool(result, cfg)

    with get_db() as conn:
        _init_tables(conn)
        conn.execute("DELETE FROM curator_pool_cache")
        conn.execute(
            "INSERT INTO curator_pool_cache (id, payload) VALUES (1, ?)",
            (json.dumps(pool),),
        )
        seed = [
            (it["id"], 1 if it["auto_selected"] else 0)
            for b in pool["buckets"] for it in b["items"]
        ]
        conn.executemany(
            "INSERT OR IGNORE INTO curator_selection (photo_path, kept) VALUES (?, ?)",
            seed,
        )
        conn.commit()
        return _candidates_payload(conn)


@router.get("/api/curator/candidates")
def get_candidates(user: Optional[CurrentUser] = Depends(get_optional_user)):
    """Return the candidate pool grouped by day → event with the current selection.

    ``needs_run`` is true when the pool has never been computed — the client
    then triggers ``run``.
    """
    with get_db() as conn:
        _init_tables(conn)
        return _candidates_payload(conn)


@router.post("/api/curator/toggle")
def toggle_selection(body: ToggleRequest, user: CurrentUser = Depends(require_edition)):
    """Tick/untick a single candidate. Upserts the kept flag, returns the count."""
    with get_db() as conn:
        _init_tables(conn)
        conn.execute(
            "INSERT INTO curator_selection (photo_path, kept) VALUES (?, ?) "
            "ON CONFLICT(photo_path) DO UPDATE SET kept = excluded.kept",
            (body.id, 1 if body.selected else 0),
        )
        conn.commit()
        return {"id": body.id, "selected": body.selected,
                "selected_count": _selected_count(conn)}


@router.post("/api/curator/save_album")
def save_album(body: SaveAlbumRequest, user: CurrentUser = Depends(require_edition)):
    """(Re)create a facet album from the kept set, ordered chronologically.

    Replaces any album of the same name owned by this user so repeated saves
    are idempotent. The kept set is the current pool overlaid with edits, so it
    matches exactly what the user sees ticked (orphan rows from an earlier pool
    are ignored).
    """
    with get_db() as conn:
        _init_tables(conn)
        pool = _load_pool(conn)
        kept = _kept_map(conn)
        selected_paths = []
        if pool is not None:
            for b in pool["buckets"]:
                for it in b["items"]:
                    if _is_selected(it, kept):
                        selected_paths.append(it["id"])

        # Order chronologically by capture date (nulls last), then path.
        ordered = _order_by_date(conn, selected_paths)

        user_id = user.user_id if user else None
        conn.execute(
            "DELETE FROM album_photos WHERE album_id IN "
            "(SELECT id FROM albums WHERE name = ? AND (user_id = ? OR (user_id IS NULL AND ? IS NULL)))",
            (body.name, user_id, user_id),
        )
        conn.execute(
            "DELETE FROM albums WHERE name = ? AND (user_id = ? OR (user_id IS NULL AND ? IS NULL))",
            (body.name, user_id, user_id),
        )
        cur = conn.execute(
            "INSERT INTO albums (user_id, name, description) VALUES (?, ?, ?)",
            (user_id, body.name, f"Curated selection ({len(ordered)} items)"),
        )
        album_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO album_photos (album_id, photo_path, position) VALUES (?, ?, ?)",
            [(album_id, path, i) for i, path in enumerate(ordered)],
        )
        conn.commit()
        return {"album_id": album_id, "count": len(ordered)}


def _order_by_date(conn, paths: list[str]) -> list[str]:
    """Sort ``paths`` by their ``date_taken`` (unknown dates sort last)."""
    if not paths:
        return []
    placeholders = ",".join(["?"] * len(paths))
    dates = {
        r["path"]: r["date_taken"]
        for r in conn.execute(
            f"SELECT path, date_taken FROM photos WHERE path IN ({placeholders})", paths
        )
    }
    far_future = "9999"

    def key(p: str):
        return (dates.get(p) or far_future, p)

    return sorted(paths, key=key)
