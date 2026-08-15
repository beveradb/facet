"""Video support: discovery, capture date/duration, and keyframe extraction.

facet is stills-only, so we sample a few keyframes per clip (spread across its
duration) and run THOSE through facet's normal image pipeline (category, caption,
moments, quality, embeddings). The per-frame results are later aggregated into a
per-video content summary. Capture date comes from the file mtime rendered in the
trip timezone (QuiverPhotos preserves the real instant there), matching how the
stills are dated.
"""
from __future__ import annotations

import base64
import html
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".avi")
STILL_EXTS = (".jpg", ".jpeg", ".heic", ".png")


@dataclass
class VideoMeta:
    idx: int
    path: str
    filename: str
    dt: datetime | None
    duration: float  # seconds

    @property
    def day(self) -> str | None:
        return self.dt.strftime("%Y-%m-%d") if self.dt else None


def ffprobe_duration(path: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        capture_output=True, text=True, check=False,
    ).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


def _capture_dt(path: str, tz: ZoneInfo) -> datetime | None:
    try:
        mtime = Path(path).stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(mtime, tz).replace(tzinfo=None)


def discover_videos(
    root: str, tz_name: str = "Europe/Amsterdam", exclude_live_photos: bool = True
) -> list[VideoMeta]:
    tz = ZoneInfo(tz_name)
    all_files = list(Path(root).rglob("*"))
    files = sorted(p for p in all_files if p.suffix.lower() in VIDEO_EXTS)
    if exclude_live_photos:
        # A Live-Photo .MOV shares its stem with a still (IMG_1234.HEIC/.MOV); those
        # are motion companions of a photo, not standalone clips — drop them.
        still_stems = {p.stem.lower() for p in all_files if p.suffix.lower() in STILL_EXTS}
        files = [p for p in files if p.stem.lower() not in still_stems]
    vids = []
    for i, p in enumerate(files):
        vids.append(
            VideoMeta(
                idx=i, path=str(p), filename=p.name,
                dt=_capture_dt(str(p), tz), duration=ffprobe_duration(str(p)),
            )
        )
    return vids


def keyframe_times(duration: float, n: int = 5) -> list[float]:
    """Evenly spaced sample times, avoiding the very start/end (often black)."""
    if duration <= 0:
        return [0.0]
    n = max(1, min(n, int(duration) + 1))
    return [round(duration * (k + 1) / (n + 1), 2) for k in range(n)]


def extract_keyframes(video: VideoMeta, out_dir: str, n: int = 5) -> list[str]:
    """Write up to n keyframes as JPEGs named `vid{idx:04d}__f{k}.jpg`. Returns paths."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    frames = []
    for k, t in enumerate(keyframe_times(video.duration, n)):
        dest = out / f"vid{video.idx:04d}__f{k}.jpg"
        r = subprocess.run(
            ["ffmpeg", "-y", "-ss", str(t), "-i", video.path,
             "-frames:v", "1", "-vf", "scale=384:-2", "-q:v", "4", str(dest)],
            capture_output=True, check=False,
        )
        if r.returncode == 0 and dest.exists():
            frames.append(str(dest))
    return frames


def video_id_from_frame(frame_filename: str) -> int | None:
    """`vid0007__f2.jpg` -> 7."""
    name = Path(frame_filename).name
    if name.startswith("vid") and "__" in name:
        try:
            return int(name[3:].split("__", 1)[0])
        except ValueError:
            return None
    return None


@dataclass
class VideoSummary:
    idx: int
    path: str
    filename: str
    dt: datetime | None
    duration: float
    caption: str | None          # representative (best frame's) caption
    all_captions: list[str]      # every frame caption, in quality order
    category: str | None
    moment: str
    aggregate: float
    aesthetic: float
    best_frame: str              # path to the best keyframe jpg (for the picker)
    n_frames: int
    mean_emb: list[float]        # mean normalized frame image-embedding (for auto-selection)

    @property
    def day(self) -> str | None:
        return self.dt.strftime("%Y-%m-%d") if self.dt else None


def aggregate_summaries(frames_db: str, videos: list[VideoMeta]) -> list[VideoSummary]:
    """Read facet's analysis of the extracted frames and roll it up per video."""
    import sqlite3
    from collections import Counter, defaultdict

    import numpy as np

    conn = sqlite3.connect(frames_db)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT path, filename, caption, category, narrative_moment, aggregate, "
            "aesthetic, clip_embedding FROM photos"
        ).fetchall()
    finally:
        conn.close()

    by_vid: dict[int, list] = defaultdict(list)
    for r in rows:
        vid = video_id_from_frame(r["filename"])
        if vid is not None:
            by_vid[vid].append(r)

    def emb(blob):
        if not blob:
            return None
        a = np.frombuffer(blob, dtype=np.float32)
        n = np.linalg.norm(a)
        return a / n if n else a

    vmap = {v.idx: v for v in videos}
    out = []
    for idx, frs in by_vid.items():
        v = vmap.get(idx)
        if not v:
            continue
        frs = sorted(frs, key=lambda r: (r["aggregate"] or 0), reverse=True)
        caps = [r["caption"] for r in frs if r["caption"]]
        cats = [r["category"] for r in frs if r["category"]]
        moms = [r["narrative_moment"] for r in frs if r["narrative_moment"] and r["narrative_moment"] != "other"]
        embs = [e for e in (emb(r["clip_embedding"]) for r in frs) if e is not None]
        mean_emb = np.mean(np.stack(embs), axis=0) if embs else None
        if mean_emb is not None:
            nrm = np.linalg.norm(mean_emb)
            mean_emb = (mean_emb / nrm) if nrm else mean_emb
        out.append(
            VideoSummary(
                idx=idx, path=v.path, filename=v.filename, dt=v.dt, duration=v.duration,
                caption=(caps[0] if caps else None), all_captions=caps,
                category=(Counter(cats).most_common(1)[0][0] if cats else None),
                moment=(Counter(moms).most_common(1)[0][0] if moms else "other"),
                aggregate=round(sum(r["aggregate"] or 0 for r in frs) / len(frs), 2),
                aesthetic=round(sum(r["aesthetic"] or 0 for r in frs) / len(frs), 2),
                best_frame=frs[0]["path"], n_frames=len(frs),
                mean_emb=(mean_emb.tolist() if mean_emb is not None else []),
            )
        )
    return sorted(out, key=lambda s: (s.day or "", s.dt or datetime.min))  # noqa: DTZ901


def _summary_to_photo(s: dict, img_emb):
    """Build a synthetic video Photo from a summary dict + its image embedding."""
    from .datasource import Photo

    dt = None
    if s.get("dt"):
        try:
            dt = datetime.fromisoformat(str(s["dt"]))
        except ValueError:
            dt = None
    aes = float(s.get("aesthetic") or 0.0)
    return Photo(
        path=s["path"], filename=s.get("filename", ""), dt=dt,
        lat=None, lon=None, category=s.get("category"),
        aggregate=float(s.get("aggregate") or 0.0), aesthetic=aes, comp=aes,
        face_quality=0.0, face_ratio=0.0, eyes_open=1.0, expression=0.5,
        is_blink=0, is_rejected=0, is_junk=False, is_dup_lead=0,
        duplicate_group_id=None, burst_group_id=None, phash=None,
        caption=s.get("caption"), moment=s.get("moment") or "other", moment_conf=0.5,
        emb=None, img_emb=img_emb, camera=None,
        is_video=True, duration=s.get("duration"),
    )


def load_video_candidates(videos_json_path: str):
    """Synthetic Photo candidates from a videos.json (aggregate_summaries output),
    so clips flow through the same selection pipeline as stills (roadmap 9c)."""
    import json

    import numpy as np

    data = json.loads(Path(videos_json_path).read_text())
    out = []
    for s in data:
        mean_emb = s.get("mean_emb") or []
        img_emb = np.asarray(mean_emb, dtype=np.float32) if mean_emb else None
        out.append(_summary_to_photo(s, img_emb))
    return out


def store_video_candidates(db_path: str, summaries: list[dict]) -> int:
    """Persist video candidates into the DB so `curate()` (and the review API) can
    fold clips in without needing the videos.json file path."""
    import json
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS curator_video_candidates ("
            "  path TEXT PRIMARY KEY, summary TEXT NOT NULL)"
        )
        conn.execute("DELETE FROM curator_video_candidates")
        conn.executemany(
            "INSERT INTO curator_video_candidates (path, summary) VALUES (?, ?)",
            [(s["path"], json.dumps(s)) for s in summaries if s.get("path")],
        )
        conn.commit()
        return conn.total_changes
    finally:
        conn.close()


def load_video_candidates_from_db(db_path: str):
    """Synthetic video Photos from the curator_video_candidates table (empty list
    if the table is absent — so stills-only DBs are unaffected)."""
    import json
    import sqlite3

    import numpy as np

    conn = sqlite3.connect(db_path)
    try:
        try:
            rows = conn.execute("SELECT summary FROM curator_video_candidates").fetchall()
        except sqlite3.OperationalError:
            return []  # table not created yet
    finally:
        conn.close()
    out = []
    for (blob,) in rows:
        s = json.loads(blob)
        mean_emb = s.get("mean_emb") or []
        img_emb = np.asarray(mean_emb, dtype=np.float32) if mean_emb else None
        out.append(_summary_to_photo(s, img_emb))
    return out


def store_video_thumbs(db_path: str, summaries: list[dict]) -> int:
    """Persist each clip's best keyframe (JPEG bytes) into `curator_video_thumbs`
    keyed by the clip path, so the curator API can serve `/api/curator/video_thumb`
    for video candidates (which have no `photos.thumbnail` row)."""
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS curator_video_thumbs "
            "(path TEXT PRIMARY KEY, thumbnail BLOB)"
        )
        n = 0
        for s in summaries:
            bf, path = s.get("best_frame"), s.get("path")
            if bf and path and Path(bf).exists():
                conn.execute(
                    "INSERT OR REPLACE INTO curator_video_thumbs (path, thumbnail) VALUES (?, ?)",
                    (path, Path(bf).read_bytes()),
                )
                n += 1
        conn.commit()
        return n
    finally:
        conn.close()


def _fmt_dur(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 60}:{sec % 60:02d}" if sec >= 60 else f"{sec}s"


def write_video_picker(summaries: list[dict], out_path: str, suggest: str = "10-20") -> str:
    """Interactive, self-contained HTML: video keyframes + AI summary grouped by day.
    Click to select; 'Download selection' writes the chosen source paths to a .txt
    that `curate.py export --add-videos` consumes."""
    by_day: dict[str, list[dict]] = {}
    for s in summaries:
        by_day.setdefault(s.get("day") or "?", []).append(s)

    sections = []
    for day in sorted(by_day):
        cards = []
        for s in by_day[day]:
            b64 = ""
            bf = s.get("best_frame")
            if bf and Path(bf).exists():
                b64 = base64.b64encode(Path(bf).read_bytes()).decode("ascii")
            img = (f'<img src="data:image/jpeg;base64,{b64}">' if b64 else '<div class="noimg">no frame</div>')
            meta = f"{_fmt_dur(s.get('duration', 0))} · {html.escape(s.get('category') or '?')} · {html.escape(s.get('moment') or '')}"
            cap = html.escape((s.get("caption") or "")[:180])
            path_attr = html.escape(s.get("path", ""), quote=True)
            cards.append(
                f'<figure class="vid" data-path="{path_attr}" onclick="tog(this)">'
                f'<div class="thumb">{img}<span class="dur">▶ {_fmt_dur(s.get("duration", 0))}</span></div>'
                f'<figcaption><b>{html.escape(s.get("filename",""))}</b> · <span class="m">{meta}</span>'
                f'<br>{cap}</figcaption></figure>'
            )
        sections.append(f'<section><h2>{html.escape(day)} <span class="n">{len(by_day[day])} clips</span></h2>'
                        f'<div class="grid">{"".join(cards)}</div></section>')

    doc = f"""<!doctype html><html><head><meta charset="utf-8"><title>Video picker</title>
<style>
 body{{font:14px/1.4 system-ui,sans-serif;margin:0;background:#111;color:#eee}}
 header{{padding:14px 24px;background:#1b1b1b;position:sticky;top:0;z-index:9;border-bottom:1px solid #333;display:flex;gap:16px;align-items:center}}
 h1{{margin:0;font-size:17px}} h2{{font-size:15px;margin:22px 24px 8px;color:#8cf}} .n{{color:#888;font-weight:normal;font-size:12px}}
 .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:10px;padding:0 24px}}
 figure{{margin:0;background:#1b1b1b;border-radius:8px;overflow:hidden;cursor:pointer;border:2px solid transparent}}
 figure.sel{{border-color:#3b82f6;box-shadow:0 0 0 2px #3b82f6}}
 .thumb{{position:relative}} figure img,.noimg{{width:100%;aspect-ratio:1;object-fit:cover;display:block;background:#222}}
 .noimg{{display:flex;align-items:center;justify-content:center;color:#666}}
 .dur{{position:absolute;right:6px;bottom:6px;background:#000a;padding:2px 6px;border-radius:4px;font-size:12px}}
 figcaption{{padding:6px 8px;font-size:11px;color:#bbb}} .m{{color:#8cf}}
 button{{background:#3b82f6;color:#fff;border:0;padding:8px 14px;border-radius:6px;font-size:14px;cursor:pointer}}
 #cnt{{font-weight:700}}
</style></head><body>
<header><h1>Video picker — {len(summaries)} clips</h1>
 <div>Selected: <span id="cnt">0</span> <span style="color:#888">(suggest {suggest})</span></div>
 <button onclick="dl()">⬇ Download selection</button>
 <span style="color:#888;font-size:12px">then: curate.py export --album "…" --dest … --add-videos videos_selected.txt</span>
</header>
{"".join(sections)}
<script>
 const sel=new Set();
 function tog(el){{const p=el.dataset.path; if(sel.has(p)){{sel.delete(p);el.classList.remove('sel');}}else{{sel.add(p);el.classList.add('sel');}} document.getElementById('cnt').textContent=sel.size;}}
 function dl(){{const b=new Blob([[...sel].join('\\n')],{{type:'text/plain'}});const a=document.createElement('a');a.href=URL.createObjectURL(b);a.download='videos_selected.txt';a.click();}}
</script>
</body></html>"""
    Path(out_path).write_text(doc)
    return out_path
