#!/usr/bin/env python3
"""Album Curator CLI (prototype).

Reads a facet SQLite DB, runs the coverage-constrained selection pipeline, and
emits a candidate set: a JSON manifest, a printed summary, and (optionally) a
facet album written back into the DB.

    python curate.py --db liquicity.db --target 100 --out candidates.json
    python curate.py --db liquicity.db --write-album "Curated Candidates"
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict

from curator import CuratorConfig, curate
from curator.rank import photo_score


def _summary(result, cfg) -> dict:
    by_day = defaultdict(int)
    by_loc = defaultdict(int)
    for p in result.selected:
        by_day[p.day] += 1
        by_loc[p.location or "?"] += 1
    return {
        "target": cfg.target_count,
        "candidate_budget": cfg.candidate_budget,
        "selected": len(result.selected),
        "buckets": len(result.buckets),
        "undated_excluded": len(result.undated),
        "reference_contributor": result.reference,
        "contributors": result.contributors,
        "selected_by_day": dict(sorted(by_day.items())),
        "selected_by_location": dict(sorted(by_loc.items(), key=lambda kv: -kv[1])),
        "coverage_warnings": result.coverage_warnings,
    }


def _write_album(db_path: str, name: str, result) -> int:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DELETE FROM album_photos WHERE album_id IN (SELECT id FROM albums WHERE name=?)", (name,))
        conn.execute("DELETE FROM albums WHERE name=?", (name,))
        cur = conn.execute(
            "INSERT INTO albums (name, description) VALUES (?, ?)",
            (name, f"Auto-curated candidates ({len(result.selected)} items)"),
        )
        album_id = cur.lastrowid
        ordered = sorted(result.selected, key=lambda p: (p.day or "", p.dt or 0))
        conn.executemany(
            "INSERT INTO album_photos (album_id, photo_path, position) VALUES (?, ?, ?)",
            [(album_id, p.path, i) for i, p in enumerate(ordered)],
        )
        conn.commit()
        return album_id
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True)
    ap.add_argument("--target", type=int, default=100)
    ap.add_argument("--multiplier", type=float, default=1.5)
    ap.add_argument("--tz", default="Europe/Amsterdam")
    ap.add_argument("--out", default="candidates.json")
    ap.add_argument("--write-album", metavar="NAME", default=None)
    args = ap.parse_args()

    cfg = CuratorConfig(target_count=args.target, candidate_multiplier=args.multiplier, timezone=args.tz)
    result = curate(args.db, cfg)
    summary = _summary(result, cfg)

    manifest = {
        "summary": summary,
        "buckets": [
            {
                "id": b.id,
                "label": b.label(),
                "quota": result.quotas.get(b.id, 0),
                "slots": len(b.slots),
                "photos": len(b.photos),
                "picks": [
                    {
                        "path": p.path,
                        "filename": p.filename,
                        "score": round(photo_score(p, cfg), 2),
                        "caption": p.caption,
                        "moment": p.moment,
                        "contributor": p.contributor,
                    }
                    for p in result.picks_by_bucket.get(b.id, [])
                ],
            }
            for b in sorted(result.buckets, key=lambda b: (b.day, b.location or "", b.event))
        ],
    }
    with open(args.out, "w") as f:
        json.dump(manifest, f, indent=2, default=str)

    print("=== Album Curator ===")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    print(f"\n  manifest -> {args.out}")

    if args.write_album:
        album_id = _write_album(args.db, args.write_album, result)
        print(f"  wrote facet album '{args.write_album}' (id={album_id}, {len(result.selected)} photos)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
