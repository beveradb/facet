"""Emit a self-contained contact-sheet HTML of a curation result.

Thumbnails are read from facet's `photos.thumbnail` BLOB and inlined as base64 so
the page is a single portable file. Picks are grouped by day -> event with each
bucket's quota fill, plus a summary and any person-coverage warnings — enough to
do the human cut to the final album offline.
"""
from __future__ import annotations

import base64
import html
import sqlite3

from .rank import photo_score
from .select import CurationResult


def _load_thumbs(db_path: str, paths: list[str]) -> dict[str, str]:
    if not paths:
        return {}
    conn = sqlite3.connect(db_path)
    try:
        out: dict[str, str] = {}
        for chunk_start in range(0, len(paths), 500):
            chunk = paths[chunk_start : chunk_start + 500]
            q = f"SELECT path, thumbnail FROM photos WHERE path IN ({','.join('?' * len(chunk))})"
            for path, blob in conn.execute(q, chunk):
                if blob:
                    out[path] = base64.b64encode(blob).decode("ascii")
        return out
    finally:
        conn.close()


def _card(p, thumb_b64: str | None, cfg) -> str:
    img = (
        f'<img src="data:image/jpeg;base64,{thumb_b64}" loading="lazy">'
        if thumb_b64
        else '<div class="noimg">no thumb</div>'
    )
    cap = html.escape((p.caption or "")[:160])
    meta = f"{html.escape(p.filename)} · {photo_score(p, cfg):.1f} · {html.escape(p.moment or '')}"
    return f'<figure>{img}<figcaption><b>{meta}</b><br>{cap}</figcaption></figure>'


def write_contact_sheet(result: CurationResult, db_path: str, out_path: str, cfg) -> str:
    thumbs = _load_thumbs(db_path, [p.path for p in result.selected])

    by_day: dict[str, int] = {}
    for p in result.selected:
        by_day[p.day] = by_day.get(p.day, 0) + 1
    day_row = " · ".join(f"{d}: <b>{n}</b>" for d, n in sorted(by_day.items()))

    warns = "".join(f"<li>{html.escape(w)}</li>" for w in result.coverage_warnings)
    warn_block = f'<div class="warn"><b>Coverage warnings</b><ul>{warns}</ul></div>' if warns else ""

    sections = []
    for b in sorted(result.buckets, key=lambda b: (b.day, b.location or "", b.event)):
        picks = result.picks_by_bucket.get(b.id, [])
        if not picks:
            continue
        cards = "".join(_card(p, thumbs.get(p.path), cfg) for p in sorted(picks, key=lambda p: p.dt or 0))
        sections.append(
            f'<section><h2>{html.escape(b.label())} '
            f'<span class="q">quota {result.quotas.get(b.id, 0)} / {len(b.slots)} slots</span></h2>'
            f'<div class="grid">{cards}</div></section>'
        )

    doc = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Curated candidates ({len(result.selected)})</title>
<style>
 body{{font:14px/1.4 system-ui,sans-serif;margin:0;background:#111;color:#eee}}
 header{{padding:16px 24px;background:#1b1b1b;position:sticky;top:0;border-bottom:1px solid #333}}
 h1{{margin:0 0 6px;font-size:18px}} h2{{font-size:15px;margin:24px 24px 8px;color:#8cf}}
 .q{{color:#888;font-weight:normal;font-size:12px}}
 .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px;padding:0 24px}}
 figure{{margin:0;background:#1b1b1b;border-radius:8px;overflow:hidden}}
 figure img,.noimg{{width:100%;aspect-ratio:1;object-fit:cover;display:block;background:#222}}
 .noimg{{display:flex;align-items:center;justify-content:center;color:#666}}
 figcaption{{padding:6px 8px;font-size:11px;color:#bbb}}
 .warn{{margin:10px 0 0;color:#fc8}} .warn ul{{margin:4px 0}}
</style></head><body>
<header><h1>Curated candidates — {len(result.selected)} of {cfg.candidate_budget} budget
 (target {cfg.target_count})</h1>
 <div>Per day: {day_row}</div>
 <div style="color:#888;margin-top:4px">Reference contributor: {html.escape(str(result.reference))}
 · {len(result.buckets)} buckets · {len(result.undated)} undated excluded</div>
 {warn_block}</header>
{''.join(sections)}
</body></html>"""

    with open(out_path, "w") as f:
        f.write(doc)
    return out_path
