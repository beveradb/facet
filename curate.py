#!/usr/bin/env python3
"""Album Curator CLI (prototype).

Reads a facet SQLite DB, runs the coverage-constrained selection pipeline, and
emits a candidate set: a JSON manifest, a printed summary, and (optionally) a
facet album written back into the DB.

    # Curate -> manifest + contact sheet + facet album:
    python curate.py run --db photos.db --contact-sheet sheet.html --write-album "Curated Candidates"
    # After the human cut in facet, export surviving originals for re-upload:
    python curate.py export --db photos.db --album "Curated Candidates" --dest ./curated_out

Works on any facet DB built from any folder of media — no trip-specific
assumptions (see docs/superpowers/specs/2026-08-12-album-curator-design.md §0).
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict

from curator import CuratorConfig, curate
from curator.emit import write_contact_sheet
from curator.exporter import export_album
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


def cmd_run(args) -> int:
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

    if args.contact_sheet:
        write_contact_sheet(result, args.db, args.contact_sheet, cfg)
        print(f"  contact sheet -> {args.contact_sheet}")

    if args.write_album:
        album_id = _write_album(args.db, args.write_album, result)
        print(f"  wrote facet album '{args.write_album}' (id={album_id}, {len(result.selected)} photos)")
    return 0


def cmd_export(args) -> int:
    manifest = export_album(args.db, args.album, args.dest, include_rejected=args.include_rejected)
    print(f"=== Export '{args.album}' -> {args.dest} ===")
    print(f"  exported: {manifest['exported']}")
    print(f"  skipped (rejected): {manifest['skipped_rejected']}")
    if manifest["missing_source"]:
        print(f"  MISSING source files: {len(manifest['missing_source'])}")
    print(f"  manifest -> {args.dest}/manifest.json + manifest.csv")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="curate a DB into a candidate set")
    r.add_argument("--db", required=True)
    r.add_argument("--target", type=int, default=100)
    r.add_argument("--multiplier", type=float, default=1.5)
    r.add_argument("--tz", default=None,
                   help="reserved: cross-contributor tz normalization (v2); v1 buckets on EXIF-local date")
    r.add_argument("--out", default="candidates.json")
    r.add_argument("--contact-sheet", metavar="PATH", default=None,
                   help="write a self-contained HTML contact sheet of the candidates")
    r.add_argument("--write-album", metavar="NAME", default=None)
    r.set_defaults(func=cmd_run)

    e = sub.add_parser("export", help="copy an album's surviving originals + manifest to a folder")
    e.add_argument("--db", required=True)
    e.add_argument("--album", required=True)
    e.add_argument("--dest", required=True)
    e.add_argument("--include-rejected", action="store_true")
    e.set_defaults(func=cmd_export)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
