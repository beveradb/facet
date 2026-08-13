"""Export a curated album to a folder of original files + manifest.

The human cut to the final ~100 happens in facet's gallery (reject flags on the
candidates album); this copies the album's surviving originals into a fresh
folder ready to re-upload (e.g. to Google Photos), skipping rejected photos and
de-colliding duplicate filenames across contributors.
"""
from __future__ import annotations

import csv
import json
import shutil
import sqlite3
from pathlib import Path


def export_album(db_path: str, album_name: str, dest_dir: str, include_rejected: bool = False) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT ap.position, p.path, p.filename, p.is_rejected, p.date_taken, p.caption
            FROM album_photos ap
            JOIN albums a ON a.id = ap.album_id
            JOIN photos p ON p.path = ap.photo_path
            WHERE a.name = ?
            ORDER BY ap.position
            """,
            (album_name,),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        raise SystemExit(f"No album named {album_name!r} (or it has no photos) in {db_path}")

    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)

    exported, skipped_rejected, missing = [], [], []
    used_names: set[str] = set()
    for r in rows:
        if r["is_rejected"] and not include_rejected:
            skipped_rejected.append(r["filename"])
            continue
        src = Path(r["path"])
        if not src.exists():
            missing.append(r["path"])
            continue
        name = src.name
        if name in used_names:  # de-collide same filename from different contributors
            name = f"{src.stem}__{len(used_names)}{src.suffix}"
        used_names.add(name)
        shutil.copy2(src, dest / name)
        exported.append(
            {"file": name, "source": str(src), "date": r["date_taken"], "caption": r["caption"]}
        )

    manifest = {
        "album": album_name,
        "exported": len(exported),
        "skipped_rejected": len(skipped_rejected),
        "missing_source": missing,
        "items": exported,
    }
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str))
    with (dest / "manifest.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["file", "date", "caption", "source"])
        for it in exported:
            w.writerow([it["file"], it["date"], it["caption"], it["source"]])
    return manifest
