"""Write a curation result back into facet as an album (idempotent by name)."""
from __future__ import annotations

import sqlite3
from datetime import datetime

from .datasource import Photo


def write_album(db_path: str, name: str, photos: list[Photo]) -> int:
    """Create/replace a facet album named `name` from `photos`, ordered
    chronologically. Returns the new album id."""
    ordered = sorted(photos, key=lambda p: (p.day or "", p.dt or datetime.min))  # noqa: DTZ901
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "DELETE FROM album_photos WHERE album_id IN (SELECT id FROM albums WHERE name=?)",
            (name,),
        )
        conn.execute("DELETE FROM albums WHERE name=?", (name,))
        cur = conn.execute(
            "INSERT INTO albums (name, description) VALUES (?, ?)",
            (name, f"Auto-curated candidates ({len(photos)} items)"),
        )
        album_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO album_photos (album_id, photo_path, position) VALUES (?, ?, ?)",
            [(album_id, p.path, i) for i, p in enumerate(ordered)],
        )
        conn.commit()
        return album_id
    finally:
        conn.close()
