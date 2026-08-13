# Album Curator — Design

**Status:** Draft for review (written autonomously 2026-08-12; Andrew to review)
**Feature:** A reusable facet feature that curates *any* folder of media into a smaller, coverage-balanced album — validated on the Liquicity 2026 trip as the first fixture
**Author:** Claude (brainstorming session)
**Related:** `/Users/andrew/Downloads/ALBUM_CURATOR_BRIEF.md` (original brief)

---

## 0. Product goal & genericity (the north star)

The deliverable is a **reusable facet feature**: point it at *any* local folder
of media and get a smaller, coverage-balanced curated album out — usable by
Andrew and others on any album, and a candidate to contribute upstream to
`ncoevoet/facet`. **Liquicity 2026 is the first fixture / proving ground, not the
target.** Its specifics (a festival, Amsterdam, a Pixel-owning contributor) must
never be hard-coded into the algorithm — they are either **auto-detected from the
data** or **exposed as config**. Andrew has flagged keeping this in view as the
priority even while we first prove feasibility on the one album.

**Generic-by-design principles (hold the line on these):**
- **No trip / place / person / device hard-coding.** Verified: the algorithm has
  zero references to Liquicity/Amsterdam/festival/Pixel/place names. Reference
  contributor, locations, days, events and persons are all derived at runtime.
- **Everything tunable lives in the `curator` config block (§6)** with defaults
  that work sight-unseen; a new album overrides only what it needs.
- **Degrade gracefully — must produce a sensible album on plain holiday snaps.**
  No GPS → day+gap+semantic bucketing only. No captions → skip semantic
  merge/moments, fall back to score+time. One contributor → contributor logic is
  a no-op. Missing dates → excluded with a warning.
- **Input = any folder facet can scan → a facet DB.** The curator only *reads*
  the DB, so it inherits facet's format support and is decoupled from how the
  library was built. Output (candidates album + manifest + export folder) is
  equally generic.

| Looks trip-specific | How it's actually generic |
|---|---|
| Timezone `Europe/Amsterdam` | **Removed** — unused in v1 (buckets on EXIF-local date); cross-contributor tz normalization is a v2, config-driven + skew-detected concern, never a fixed default. |
| "Pixel is the spine" | Reference contributor = the device with the most GPS, **auto-detected**. Any owner/device works; zero GPS → skipped entirely. |
| Amsterdam → festival arc | Locations are reverse-geocoded from whatever GPS exists; place names are **data, not code**. |
| 8-day festival timeline | Days/events derived from timestamps + gaps; works for a weekend or a month. |
| Named crew | Person coverage reads facet's clusters; no persons → the pass is a no-op. |

Liquicity-derived numbers throughout this doc (787 photos, 8 days, GPS split,
etc.) are **illustrative of the fixture** for sanity-checking behavior — they are
not requirements. Every section below should be read as "for an arbitrary
album, using this album to make it concrete."

## 1. Problem & goal

Reduce a large shared trip album (Liquicity 2026: **787 stills** from ≥3
contributors over **8 days**) to a curated **≤100-item** album that *tells the
story of the trip* — every day and place represented, every main person shown
flatteringly, genuine highlights included — without the exhausting manual N-way
comparison across the whole set.

**This is not culling.** Blur/blink/burst/aesthetic scoring (all of which facet
already does) do not solve it. The hard part is **selection under coverage
constraints**: decide which items *collectively* represent the trip.

**Core method (from the brief, confirmed):** don't globally rank and take top-N.
Instead **bucket → allocate a quota per bucket → rank only within buckets →
person-coverage pass → emit ~150 candidates** for a human final cut to 100.

## 2. What we learned from the data (drives the design)

Verified on this dataset (see memory `liquicity-data-shape`, `facet-pipeline-insights`):

| Finding | Design consequence |
|---|---|
| **GPS present on 429/787 stills — all from the owner's Pixel 9 Pro** (other contributors' location stripped by Google). `mdls` lies; `exiftool`/facet `gps_latitude` is truth. | Owner's Pixel = **trusted spine**. Reverse-geocode it → a **time→location track**; assign every other photo the nearest-in-time location. Location axis is *recovered*, not absent. |
| **Captions are excellent** (accurate scene/place/people, reads in-frame text). `caption_embedding` is 1152-dim SigLIP2. | Captions + embeddings are the **semantic backbone** for event bucketing and dedup. |
| **Narrative moments good only after captions** (`other` 66%→28% once captions exist). | Pipeline must run `--generate-captions` → `--recompute-moments` before curation. |
| **Faces cluster well** — a core recurring crew of ~7 people. | Person-coverage pass is viable off `faces.person_id`. |
| **Qwen tags noisy/generic.** | Do not rely on `photos.tags`. |
| **Clean 8-day timeline**; peak day 205 vs light day 11; ~13% keep rate. | Day is the primary axis; quota needs a **per-day floor** so light days aren't dropped. |
| **Multi-contributor clock skew** (saw ~4h filename-vs-EXIF offset; devices write different UTC offsets). | **Contributor-aware**: Pixel = reference clock; detect/flag others' skew. |
| **10 stills had no capture date** (WhatsApp/UUID/motion-photo). | Fixed by a **metadata-repair pre-step** (mtime→festival-local; already applied, script `facet-curator/repair_dates.py`). |
| **187 videos** (facet can't ingest). | **v2**: frame-sampling. v1 emits a separate video manifest for manual slotting. |

## 3. Scope (v1) & decisions

**In scope (v1):** stills-only curation → ~150 candidate album + manifest;
integrated as a facet feature (algorithm prototyped standalone first, then API +
UI); person-coverage as a **soft** preference; export folder for **Google Photos
re-upload**.

**Deferred (v2+):** video frame-sampling; automatic clock-skew *correction*
(v1 only detects/flags); the second multi-city album ("Antwerp/Köln/Munich") as
a genericity test.

**Decisions made (Andrew's answers + autonomous defaults):**
- Build posture: **integrated facet feature**, but **prototype the selection
  algorithm as a decoupled read-only module + CLI first**, wire API/UI once proven.
- Videos: **frame-sampling (v2)**; v1 excludes but manifests them.
- Person coverage: **strong preference (soft)** — never break the per-day floor
  for it; surface unmet coverage as warnings.
- Destination: **Google Photos re-upload** — export chosen originals + manifest.
- "Unflattering": **not modeled by AI** — human pass; UI surfaces per-face
  eyes-open/expression hints.
- Config-driven throughout (a `curator` block in `scoring_config.json`) for
  **genericity / reuse on future trips** and a clean upstream contribution.

## 4. Architecture

Decoupled, in a new `curator/` package. The algorithm reads facet's SQLite DB
**read-only** and emits a list of photo paths + a manifest; writeback (album,
export) is separate. This keeps the novel logic testable in isolation (brief §6)
and swappable.

```
curator/
  __init__.py
  config.py        # load/validate the `curator` block; defaults
  datasource.py    # read photos+faces+embeddings from facet DB → in-memory Photo records (read-only)
  geocode.py       # reverse-geocode GPS spine → time→location track → assign location to every photo
  contributors.py  # identify contributors (camera/filename); pick reference; detect clock skew
  bucketing.py     # day → location → time-gap scene → semantic refine → Bucket list
  dedup.py         # collapse near-duplicate groups (phash + duplicate_group_id + cross-contributor embedding) to one slot
  allocate.py      # quota per bucket summing to target*multiplier, weighted, with floors (largest-remainder)
  rank.py          # within-bucket photo score; pick each bucket's quota
  coverage.py      # soft person-coverage second pass (swap within nearest bucket)
  select.py        # orchestrator: ties the pipeline together → CurationResult
  writeback.py     # create facet album from result; export originals+manifest folder
  emit.py          # candidates.json + contact-sheet HTML
curate.py          # CLI entry (prototype); later mirrored by api/routers/curator.py + Angular UI
```

**Data flow:**
```
facet DB ──datasource──▶ [Photo]  ──geocode/contributors──▶ enriched [Photo]
   └▶ bucketing ▶ [Bucket]  ▶ dedup (collapse slots)
        ▶ allocate (quota per bucket, Σ=~150, floors)
        ▶ rank (pick quota per bucket)
        ▶ coverage (soft person swaps)
        ▶ CurationResult ▶ {album writeback | candidates.json | contact sheet | export folder}
```

Later, the **integrated feature** wraps `select.py` behind
`POST /api/curator/run` (params = curator config overrides), returns the
CurationResult; the Angular UI adds a **"Curate"** action and a **review view**
(day/event columns, quota fill bars, coverage warnings) that reuses the existing
gallery's keyboard-first reject + 7s-undo for the human cut to 100.

## 5. The algorithm (the novel work)

### 5.1 Enrichment
- **Contributors:** group by `camera_model` + filename prefix (`PXL_`, `IMG_`,
  Galaxy numeric, …). Reference = contributor with the most GPS coverage (Pixel).
- **Location track:** reverse-geocode every GPS'd photo (`reverse_geocoder`,
  single-threaded — the multiprocessing path crashes under our invocation). Build
  a sorted `(timestamp → city/region/country)` track from the reference. Assign
  every photo (GPS or not) the **nearest-in-time** location. Cache into facet's
  `location_names` grid table where possible.
- **Clock skew (detect-only v1):** for each non-reference contributor, estimate a
  constant offset that best aligns their photo-time density to the reference's
  event structure; if |offset| exceeds a threshold with high confidence, **flag**
  it (and optionally apply if `curator.deskew.apply=true`, default false).

### 5.2 Bucketing — day → location → event
1. **Day** = the calendar date of each photo's **EXIF-local `date_taken`** (v1;
   no timezone conversion — see §0). Primary narrative axis. *(v2: optional
   cross-contributor tz normalization once clock-skew detection lands.)*
2. **Location split:** within a day, a change in assigned location (e.g.
   Amsterdam → Harenkarspel) is a hard event boundary.
3. **Time-gap scenes:** within a (day, location), start a new scene when the gap
   to the previous photo exceeds `curator.scene_gap_minutes` (default 45).
4. **Semantic refine:**
   - **Merge** adjacent scenes if mean `caption_embedding` cosine ≥
     `merge_cos` (0.82) *and* same `narrative_moment` — avoids splitting one
     event across a lull.
   - **Split** a scene if its caption embeddings are clearly bimodal
     (2-means silhouette > `split_sil`) — separates two activities with no time gap.
   Each final **Bucket** = `(day, location, event_label)` where `event_label`
   comes from the dominant `narrative_moment` / a caption keyword.

### 5.3 Dedup → slots
Collapse near-duplicates so a similarity group takes **one** quota slot:
- facet's `duplicate_group_id` / `is_duplicate_lead`,
- `phash` Hamming ≤ `phash_max` (default 8) within a bucket,
- **cross-contributor**: same-moment (time within N min) + `caption_embedding`
  (or `clip_embedding`) cosine ≥ `dup_cos` (0.9) — catches "same moment, different
  phones" the brief flags.
Each slot keeps its best member (by §5.4 score); others are recorded as
alternates in the manifest.

### 5.4 Within-bucket photo score
Reuse facet's Top-Picks philosophy. For a photo:
```
base = 0.30*aggregate + w_a*aesthetic + w_c*comp_score (+ w_f*face_quality if face_ratio≥0.2)
penalties: is_blink, is_rejected, is_duplicate (non-lead), low eyes_open/expression on the main face
```
Weights from `curator.rank_weights` (default mirrors `top_picks_weights`). Rank
slots within each bucket descending.

### 5.5 Quota allocation (Σ = target × candidate_multiplier)
`target=100`, `candidate_multiplier=1.5` → ~150 candidates.
- **Day weights:** `day_weight = clamp(count^0.6, …)` (sub-linear so the 205-photo
  day doesn't dominate) × optional significance (e.g. festival days > transit).
- **Per-day floor:** `min_per_day` (default 2) so no day drops out.
- **Event distribution:** within a day, split the day quota across its events
  proportional to (slot count)^0.7 with `min_per_event` (default 1).
- **Rounding:** largest-remainder to hit the target exactly; never exceed a
  bucket's available slots.

### 5.6 Person-coverage second pass (soft)
For each named person (rename step is a one-time human ~10 min) with fewer than
`min_shots_per_person` (default 1) *flattering* appearances among the selected
(flattering ≈ face_quality ≥ q and eyes_open ≥ e and not blink):
- find their best-covering **unselected** slot in their **nearest bucket**;
- swap it for the **weakest selected** slot in that bucket **iff** the score cost
  ≤ `max_swap_cost` **and** it doesn't drop the bucket below its floor.
- If no acceptable swap: **record an unmet-coverage warning** (don't force it).

### 5.7 Output
- **Facet album** "Curated Candidates — {trip} (~150)" via `albums`/`album_photos`
  (idempotent: replace on re-run).
- **`candidates.json`** manifest: per item `{path, day, location, event, bucket_id,
  rank_in_bucket, score, reason, alternates[]}` + summary (per-day/event counts,
  coverage report, skew flags, dropped-video manifest).
- **Contact-sheet HTML** (thumbnails grouped by day/event, quota fill) for a
  fast offline review even without the viewer.
- **Export step** (`curate.py export`): after the human cut (facet reject flags),
  copy the ~100 kept **originals** + manifest into a folder for Google re-upload.

## 6. Configuration (`scoring_config.json` → `curator`)
```json
"curator": {
  "target_count": 100,
  "candidate_multiplier": 1.5,
  "timezone": null,
  "min_per_day": 2,
  "min_per_event": 1,
  "scene_gap_minutes": 45,
  "merge_cos": 0.82, "split_silhouette": 0.55,
  "dedup": { "phash_max": 8, "dup_cos": 0.90, "same_moment_minutes": 3 },
  "rank_weights": { "aggregate": 0.30, "aesthetic": 0.28, "composition": 0.18, "face_quality": 0.24 },
  "day_weight_exponent": 0.6, "event_weight_exponent": 0.7,
  "coverage": { "min_shots_per_person": 1, "min_face_quality": 6.0, "min_eyes_open": 0.5, "max_swap_cost": 1.5 },
  "deskew": { "apply": false, "flag_threshold_minutes": 20 }
}
```
All tunable per album/trip → genericity requirement satisfied.

## 7. Testing
- **Unit:** day/tz boundary (incl. post-midnight festival shots); scene gap logic;
  semantic merge/split; dedup collapse (phash + cross-contributor); allocation
  (Σ = target, floors honored, largest-remainder, never > available); rank
  ordering + penalties; coverage swap (accept/reject/no-force).
- **Property:** quota sum invariant; every day/event represented ≥ floor.
- **Golden:** run on the 53-photo sample DB → assert stable bucket/quota shape.
- **Integration (post full run):** run on `liquicity.db` (787) → sanity-check the
  ~150 spread by day/location, coverage report, and eyeball the contact sheet.

## 8. Build order (phased)
1. `datasource` + `geocode` + `contributors` (+ tests) — enrichment proven on real DB.
2. `bucketing` + `dedup` (+ tests) — the core, inspected on the sample.
3. `allocate` + `rank` (+ tests) — end-to-end candidates.json on the sample.
4. `coverage` (+ tests).
5. `emit` (contact sheet) + `writeback` (album) — review the 150 in facet.
6. Full-DB integration pass + tuning.
7. **Integration:** `api/routers/curator.py` + Angular "Curate" action & review view.
8. `export` step for Google re-upload.
9. (v2) video frame-sampling.

## 9. Open questions for Andrew (non-blocking; sensible defaults chosen)
- **Significance weighting:** should festival days be weighted above transit days
  explicitly, or is sub-linear count weighting + floor enough? (Default: count-only.)
- **Candidate count:** 150 candidates for a 100 cut — good, or emit fewer/more?
- **Clock-skew correction:** enable `deskew.apply` once we've eyeballed the
  detected offsets? (Default: detect-and-flag only.)
- **Album/share:** also generate a facet no-login **share link** for the
  candidates album so others can weigh in before the cut? (Cheap to add.)
