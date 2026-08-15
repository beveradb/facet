# Completion report — Dedicated Album Curator UI

**For:** the orchestrator session that wrote
[`2026-08-13-curator-ui-handoff.md`](2026-08-13-curator-ui-handoff.md).
**From:** the fresh session that owned the full-stack review UI.
**Branch:** `feat/sess-20260812-1922-album-curator` → PR #1
(https://github.com/beveradb/facet/pull/1).
**Status:** ✅ done — all 6 acceptance criteria met, 26 tests passing, docs updated,
pushed. This is a summary + the **as-built contract** so 9c (video-as-candidate)
and any further engine work can integrate cleanly.

---

## 1. What shipped

Commits added this session (on top of the handoff commit `5f147be`):

| Commit | What |
|---|---|
| `ba552b0` | `feat(curator): API router (run/candidates/toggle/save)` |
| `b23640d` | `feat(curator): Angular review view` |
| *(docs-review)* | `docs(curator): document the review UI (VIEWER + CURATOR.md)` |
| *(test-review)* | `test(curator): re-run edit preservation, overlay/type helpers, service routes` |

**Backend** — `api/routers/curator.py` (registered in `api/__init__.py`), consumes
`curator.curate` read-only. **Frontend** — `client/src/app/features/curator/`
(`curator.component.ts` + spec), `core/services/curator.service.ts` (+ spec), lazy
`/curator` route in `app.routes.ts`. **Docs** — `docs/VIEWER.md` API catalogue gained
a Curator section; `docs/CURATOR.md` gained a "Review in the viewer" section + status/
roadmap refresh.

**Nothing in `curator/` (the engine) was touched.** The UI consumes it via the
documented entry points only (`curate`, `CuratorConfig`, `CurationResult`,
`rank.slot_best`).

---

## 2. As-built API contract

All under `/api`, no router prefix. Writes gated `require_edition`, reads
`get_optional_user`. Items are keyed by **`photos.path`**.

- `POST /api/curator/run` `[Edition]` → runs `curate(DEFAULT_DB_PATH, CuratorConfig())`,
  caches the pool, seeds the kept side table with the auto-selection, returns the
  `candidates` payload.
- `GET /api/curator/candidates` → the payload below.
- `POST /api/curator/toggle` `[Edition]` body `{id, selected}` → `{id, selected, selected_count}`.
- `POST /api/curator/save_album` `[Edition]` body `{name}` (default `"Curated Final"`)
  → `{album_id, count}`. Idempotent by (name, user).

**Candidates payload:**
```jsonc
{ "target": 100, "total": 240, "selected_count": 118, "needs_run": false,
  "buckets": [
    { "id": "2026-07-24|Harenkarspel, NL|concert#3", "day": "2026-07-24",
      "location": "Harenkarspel, NL", "event": "concert", "quota": 6,
      "items": [
        { "id": "<photos.path>", "type": "photo", "duration": null,
          "thumb": "/thumbnail?path=<url-encoded>&size=320",
          "caption": "…", "category": "concert", "moment": "nightlife",
          "score": 7.4, "selected": true } ] } ] }
```

`score` = `round(photo.aggregate, 1)` (the familiar 0–10 facet score, not the
internal within-bucket rank score). One item per **slot**: the slot's rep is its
auto-pick when it was picked (so every selected photo is visible and marked),
otherwise `rank.slot_best(slot, cfg)`.

---

## 3. Deviations from the handoff (read these)

The handoff was followed closely; these are the intentional differences:

1. **Two side tables, not one.** As specified: `curator_selection(photo_path TEXT
   PRIMARY KEY, kept INTEGER)`. **Added:** `curator_pool_cache(id=1, payload TEXT,
   created_at)` — a single-row JSON cache of the structural pool. Rationale: it lets
   `GET /candidates` (anonymous-readable) be a cheap read/overlay instead of
   re-running the several-second `curate()` on every load, and makes `run` vs
   `candidates` semantically distinct. Both tables are created **lazily** on first
   use (`_init_tables`), per the handoff's "small init on first use" — they are *not*
   in `db/schema.py`.
2. **`needs_run` flag.** `candidates` returns `needs_run: true` (empty buckets) until
   `run` has populated the cache once. The client auto-runs when `needs_run && edition`.
3. **`run` seeds the kept table** with the auto-selection via `INSERT OR IGNORE`
   (preserves prior edits) so `save_album` has content even before the user touches a
   tick. `save_album` derives its set from the **current pool overlaid with edits**
   (not raw `kept=1` rows), so orphan rows from an earlier pool are ignored.
4. **`selected_count` is computed over the current pool** (kept row if present, else
   auto), so `candidates` and `toggle` always agree.
5. **i18n:** the UI uses plain English string literals, **not** the `TranslatePipe`.
   Deliberate scope call to avoid a partial 6-language i18n sync
   (`.claude/patterns/i18n-sync.md`) for a first cut. If you want it localized, that's
   a clean follow-up: extract ~15 strings into the locale files.
6. **Keyboard:** implemented click + **enter/space** toggle on focusable tiles
   (keyboard-friendly). Arrow-key grid navigation (a "strong plus" in the handoff) was
   **not** implemented — listed as roadmap polish.

---

## 4. Video integration contract (what 9c must produce)

The UI is already video-ready and treats items **generically by `type`**. For a clip
to appear correctly, the engine's pool just needs a `Photo`-shaped record whose:

- **`.path`** has a video extension (`.mp4/.mov/.m4v/.avi/.mkv/.webm/.mts/.m2ts`) —
  `_item_type()` derives `type: "video"` from that, so the ▶ badge renders. No API
  change needed if you route clips through the same `curate()` pool.
- **thumbnail** resolves via the existing `/thumbnail?path=…` route (a stored keyframe
  BLOB on that path). If clips won't have a `photos.thumbnail` row, either (a) store a
  keyframe there, or (b) add the `GET /api/curator/video_thumb?...` route the handoff
  anticipated and have `_thumb_url()` branch on `type` — **one line** in
  `api/routers/curator.py`.
- **duration** — currently hard-coded `"duration": null` in `_build_pool` (stills have
  none). To show `▶ 12s`, add a `duration` field to the engine's video record and set
  `"duration": rep.duration` there. The frontend already renders it.

Everything else (bucket/quota/rank/dedup/coverage) flows unchanged — the UI never
assumes stills-only.

---

## 5. Verification

- **Backend:** `tests/test_curator_router.py` — **14 passing** (run/candidates/toggle/
  save happy paths; edition gating on all three writes; anonymous read; item shape;
  **re-run preserves edits**; pure-fn overlay/`_item_type`/`_thumb_url`). Uses the
  shared `edition_client`/`regular_client`/`anonymous_client` fixtures — no
  `mock.patch` on auth deps.
- **Frontend:** `curator.component.spec.ts` (**8**) — load, count derivation, day
  grouping, optimistic toggle, revert-on-error, edition gate, save, auto-run;
  `curator.service.spec.ts` (**4**) — pins the `/api/curator/*` route contract.
  `tsc --noEmit` clean.

Run them:
```bash
# backend (sibling clone's venv)
/Users/andrew/Projects/beveradb/facet/venv/bin/python -m pytest tests/test_curator_router.py -q
# frontend — this worktree has NO client/node_modules; symlink the sibling clone's:
ln -s /Users/andrew/Projects/beveradb/facet/client/node_modules client/node_modules   # once
cd client && node_modules/.bin/ng test --include='**/curator*/**/*.spec.ts' \
                                        --include='**/services/curator.service.spec.ts'
cd client && node_modules/.bin/tsc --noEmit -p tsconfig.json
```

---

## 6. Acceptance criteria — final status

1. ✅ `/curator` renders the auto-selection grouped day → event; selected (ring +
   filled check) vs not-selected (dimmed/grayscale + empty check) obvious; photos now,
   videos when present.
2. ✅ Tick/untick persists across reload (`curator_selection`), running count updates
   live (a `computed` over item state).
3. ✅ Save produces a facet album from the kept set, chronologically ordered, openable
   in the gallery.
4. ✅ Backend pytest (run/candidates/toggle/save, auth fixtures) + frontend Vitest
   (toggle + count) + `tsc --noEmit` clean.
5. ✅ House conventions: Tailwind-only, signals, zoneless, pipes-not-methods (no method
   calls in template expressions), `photos.path` id, side table (not a `photos`
   column), `require_edition` for writes.
6. ✅ Small commits pushed to PR #1.

---

## 7. Suggested follow-ups (not owned here)

- **9c video wiring** — per §4; ~2 small edits in the router if you add a video-thumb
  route + `duration`.
- **i18n** — extract the UI strings into the 6 locale files (§3.5).
- **UX polish** (roadmap in `docs/CURATOR.md`): arrow-key grid navigation,
  coverage-warning surfacing (`CurationResult.coverage_warnings` is already returned by
  the engine but not yet shown), per-bucket quota-fill bars, and an editable album name
  on Save.
- **Config passthrough** — `run` uses `CuratorConfig()` defaults. If you want per-album
  tuning from the UI, thread overrides (target, multiplier, …) through `POST
  /curator/run`'s body into `CuratorConfig(**overrides)`.
