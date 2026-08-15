# Handoff — Dedicated Album Curator UI (fresh session)

**For:** a new Claude Code session with a clean context window.
**Owns:** the full-stack **Album Curator review UI** — a FastAPI router + an Angular
feature view — inside the facet app.
**Does NOT own:** the curator selection *engine* (`curator/` package) or video
frame-sampling / video-as-candidate work (roadmap 9c) — those stay with the
orchestrator session. You consume the engine via a small documented contract.

Work in the worktree: **`/Users/andrew/Projects/beveradb/facet-album-curator`**
(branch `feat/sess-20260812-1922-album-curator`). The venv + models + the scored
DB live in the sibling **main clone** `/Users/andrew/Projects/beveradb/facet`
(`venv/`, `pretrained_models/`, `liquicity.db`). Run Python with that venv
(`/Users/andrew/Projects/beveradb/facet/venv/bin/python`) and, for git push, use
`direnv exec /Users/andrew/Projects/beveradb/facet-album-curator <git cmd>` (the
personal `beveradb` GH_TOKEN is in `/Users/andrew/Projects/beveradb/.envrc`).

---

## 1. What the user wants

A dedicated page to **review and hand-edit the auto-curated selection**:
- See every candidate the curator considered, **grouped by day → event**, with the
  auto-**selected** items visually distinct from the **not-selected** ones.
- Both **photos and videos**.
- **Tick / untick** any item to add/remove it from the final set (easy, fast,
  keyboard-friendly), with a running count toward the target (~100).
- **Save** the edited selection; from it the user exports the final album.

Context: the curator auto-selects ~120 candidates (target 100 × 1.2 buffer) from
~787 stills, deduped and junk-filtered, grouped into ~58 day/location/event
buckets. The user then removes ~20 and adds ~10-20 videos to land at ~100.

## 2. Architecture (build both halves)

### 2a. Backend — `api/routers/curator.py` (new)
Register it in `api/__init__.py` (import near line 352-385, `app.include_router`
near 387-420 — bare `app.include_router(curator_router)`, no prefix; spell full
`/api/...` paths on each route). Mirror `api/routers/albums.py` for structure.

Call the engine directly:
```python
from curator import curate, CuratorConfig          # curator/ is importable from repo root
result = curate(db_path, CuratorConfig())            # db_path = api.database DEFAULT_DB_PATH
# result.buckets: list[Bucket]; each b.day, b.location, b.event, b.slots (list[list[Photo]])
# result.picks_by_bucket: {bucket.id -> [Photo,...]}  (the AUTO-SELECTED reps)
# result.selected: list[Photo]  (flattened auto-selection)
# Photo fields you need: .path .filename .caption .category .moment .aggregate
#   .face_ratio  (see curator/datasource.py Photo)
```
The **candidate pool** per bucket = one representative per slot
(`curator.rank.slot_best(slot, cfg)`); a rep is *selected* iff it's in
`picks_by_bucket[bucket.id]`. Non-selected reps are the "not selected" pool.

**Persistence of user edits — side table (do NOT add a column to `photos`; it is
rewritten on rescan — CLAUDE.md).** Create `curator_selection(photo_path TEXT
PRIMARY KEY, kept INTEGER)` via a small `init` on first use. Toggle writes it;
the candidates read merges it over the auto-selection (auto value used when a
path has no row). Export/album generation reads `kept`.

Endpoints (all JSON under `/api`; gate writes with `require_edition` from
`api/auth.py`, reads with `get_optional_user`; DB via `with get_db() as conn:`
from `api/database.py`):
- `POST /api/curator/run` (`require_edition`) — run `curate()`, cache the pool
  into `curator_selection` seeded with the auto-selection (only for paths not
  already user-edited), return the same payload as `GET /candidates`. Running is
  a few seconds (loads photos + reverse-geocodes); fine to run on demand.
- `GET /api/curator/candidates` (`get_optional_user`) — return:
  ```jsonc
  { "target": 100, "selected_count": 118, "total": 240,
    "buckets": [ { "id": "...", "day": "2026-07-24", "location": "Harenkarspel, NL",
        "event": "concert", "quota": 6,
        "items": [ CandidateItem, ... ] } ] }
  ```
  `CandidateItem`:
  ```jsonc
  { "id": "<photos.path>",         // stable id = the DB path string
    "type": "photo",                // or "video" (see §3)
    "thumb": "/thumbnail?path=<url-encoded path>&size=320",  // ready for <img src>
    "caption": "…", "category": "concert", "moment": "nightlife",
    "score": 7.4, "selected": true, "duration": null }
  ```
- `POST /api/curator/toggle` (`require_edition`) body `{ "id": "<path>", "selected": bool }`
  → upsert `curator_selection`, return `{ id, selected, selected_count }`.
- `POST /api/curator/save_album` (`require_edition`) body `{ "name": "Curated Final" }`
  → (re)create an album from all `kept=1` rows (reuse the albums insert pattern /
  `curator.writeback.write_album`). Returns `{ album_id, count }`.

Photos are referenced by **`photos.path`** everywhere (NOT signed rowids — that
scheme is only `api/routers/frame.py`'s kiosk). Thumbnails are served at
**`/thumbnail?path=…&size=…`** (root, not `/api`) — see `api/routers/thumbnails.py:129`.

### 2b. Frontend — new Angular feature `client/src/app/features/curator/`
- Add a lazy standalone route in `client/src/app/app.routes.ts` (mirror `/albums`
  at lines 58-63): `{ path: 'curator', canActivate:[authGuard], loadComponent: () =>
  import('./features/curator/curator.component').then(m => m.CuratorComponent) }`.
- A `CuratorService` in `client/src/app/core/services/` mirroring
  `album.service.ts` (inject `ApiService`; methods `run()`, `candidates()`,
  `toggle(id, selected)`, `saveAlbum(name)`; declare the TS interfaces inline).
- `CuratorComponent` (standalone, signals, zoneless, **Tailwind utilities only**,
  `host: { class: '…' }`, **pipes not method calls** in templates):
  - Fetch candidates into a signal; render **day sections → event sub-groups →
    thumbnail grid**. Selected items get a clear checked/highlighted state,
    not-selected are dimmed with an empty check. Reuse the thumbnail-tile pattern
    from `albums.component.ts:88-92` (`<img [src]="item.thumb">` directly — the
    API already hands you a ready `thumb` URL, so you don't need the
    `thumbnailUrl` pipe) or the richer `shared/components/photo-card` if useful.
  - Click / key toggles selection **optimistically** (mirror the pattern in
    `features/gallery/gallery.store.ts:756` `toggleFavorite`: in-flight guard →
    optimistic signal patch → `await firstValueFrom(api.post('/curator/toggle', …))`
    → reconcile / revert on error).
  - A sticky header with the **running selected count vs target** and a **"Save"**
    button (calls `save_album`). A per-day count is a nice touch.
  - Keyboard: arrow-key navigation + space to toggle is a strong plus (the gallery
    is keyboard-first; press `?` shows shortcuts).

## 3. Videos (coordinate with the orchestrator session)

Videos are being made first-class by the orchestrator (roadmap 9c): each clip
becomes a synthetic candidate flowing through the same pipeline. **Contract for
you:** a `CandidateItem` may have `type: "video"`, a non-null `duration`, and a
`thumb` that is a normal image URL (a stored keyframe served by the API — the
orchestrator will add a `/api/curator/video_thumb?...` route or embed a data-URI;
either way `thumb` is drop-in for `<img src>`). **Build the UI treating items
generically by `type`** — render a ▶ duration badge for videos; everything else is
identical. If `type:"video"` items aren't in the payload yet when you start,
build against photos and they'll appear once 9c lands (no UI change needed).

## 4. House conventions (from a codebase scan — follow exactly)

- Router: `router = APIRouter(tags=["curator"])`, full `/api/...` paths per route,
  register in `api/__init__.py`. DB: `with get_db() as conn:` (sync) or
  `get_async_db()` (async) from `api/database.py`. Auth deps in `api/auth.py`
  (`require_edition`, `get_optional_user`); **never `mock.patch` them** — in tests
  use `app.dependency_overrides[...]` or the `edition_client` / `regular_client` /
  `anonymous_client` fixtures in `tests/conftest.py`.
- Albums API to mirror: `api/routers/albums.py` (`POST /api/albums` :468,
  `POST /api/albums/{id}/photos` :793 idempotent `INSERT OR IGNORE`,
  `GET …/photos` async :860). Toggle backend precedent: `api/routers/faces.py`
  `toggle_rejected` :438.
- Angular exemplars: routes `client/src/app/app.routes.ts` (albums :58-69);
  house-style component `client/src/app/shared/components/photo-card/photo-card.component.ts`
  (input()/output()/signal()/effect+untracked, Tailwind-only, pipes);
  HTTP base `client/src/app/core/services/api.service.ts` (`baseUrl='/api'`,
  `thumbnailUrl()` :42); domain service `client/src/app/core/services/album.service.ts`;
  optimistic-toggle store `client/src/app/features/gallery/gallery.store.ts` :756.
- Build/verify: backend `cd <worktree> && /…/facet/venv/bin/python -m pytest tests/…`;
  client `cd client && npm run test` (Vitest) and `npx tsc --noEmit -p tsconfig.json`.
  Angular skills exist: `signal-patterns`, `test-creation`, `css-layout-patterns`,
  `effect-safety-validator`, `frontend-design` — use them.

## 5. Acceptance criteria
1. `/curator` route renders the auto-selection grouped by day → event; selected vs
   not-selected visually obvious; photos and (when present) videos.
2. Tick/untick persists across reload (side table), running count updates live.
3. "Save" produces a facet album from the kept set (openable in the gallery).
4. Backend: a pytest covering run/candidates/toggle/save (use the auth fixtures).
   Frontend: a Vitest for the component's toggle + count logic. `tsc --noEmit` clean.
5. Follows the house conventions in §4 (Tailwind-only, signals, pipes, side table).
6. Commit in small steps on the branch and push (via `direnv exec … git push`);
   the PR is #1 (https://github.com/beveradb/facet/pull/1) — add commits to it.

## 6. Reference
- Full design spec: `docs/superpowers/specs/2026-08-12-album-curator-design.md`
  (§4 architecture, §5 algorithm, §8 build order incl. 9c video plan).
- User-facing doc: `docs/CURATOR.md`.
- Engine entry points: `curator/__init__.py` (`curate`, `CuratorConfig`,
  `CurationResult`), `curator/writeback.py` (`write_album`),
  `curator/exporter.py` (`export_album`, `--add-videos`).
