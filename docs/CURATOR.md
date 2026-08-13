# Album Curator

Turn a large library (a whole trip, a shared album with many contributors) into a
small, **coverage-balanced** curated album that tells the story — every day and
place represented, the main people shown, the genuine highlights included —
instead of globally ranking and taking the top-N (which over-selects the most
photogenic day).

It is **not** a culling pass. Blur/blink/burst/aesthetic scoring (which facet
already does) answer "is this photo good?". The curator answers the harder
question: "which ~100 photos *together* represent this trip?"

> Status: prototype (CLI + library). API route + gallery UI are planned; see
> [Roadmap](#roadmap). Design rationale lives in
> [docs/superpowers/specs/2026-08-12-album-curator-design.md](superpowers/specs/2026-08-12-album-curator-design.md).

## How it works

**Bucket → allocate a quota per bucket → rank only within buckets → cover the
people → emit candidates.** Comparisons stay *local* (within a bucket), which is
what makes it cheap and what stops one big day from dominating.

1. **Enrich** — group photos by contributor (camera + filename); reverse-geocode
   whatever GPS exists into a time→location track and assign every photo the
   nearest-in-time place (so contributors whose GPS was stripped still get a
   location).
2. **Bucket** — split by **day → location → capture-time gap**, then merge
   adjacent scenes whose captions are semantically close (one event split by a
   lull). Each bucket is one coherent event.
3. **Dedup** — collapse near-duplicates (facet dup-groups, perceptual hash, and
   the cross-contributor "same moment from two phones" case) so a similarity
   group takes **one** slot.
4. **Allocate** — distribute a candidate budget (default `target × 1.5`) across
   buckets: sub-linear in photo count (a huge day can't dominate) with a **floor
   per day** (no day drops out). Largest-remainder rounding hits the budget.
5. **Rank** — score photos within each bucket (facet aggregate/aesthetic/
   composition/face-quality, minus blink/reject penalties) and take the quota.
6. **Cover the people** — for each named person lacking a flattering shot, swap a
   marginal pick in their nearest bucket for one that covers them, if affordable;
   otherwise record a warning (a soft preference, never forced).
7. **Emit** — a candidates album (written back into facet), a JSON manifest, and
   a self-contained contact-sheet HTML for offline review.

## Prerequisites — build the index first

The curator reads a facet DB, so populate one with the **full** pipeline (a plain
scan is not enough — narrative moments are caption-derived):

```bash
python facet.py /path/to/folder --db library.db     # 1. scan + score
python facet.py --generate-captions --db library.db # 2. captions (VLM)
python facet.py --recompute-moments --db library.db # 3. relabel moments + embeddings
python facet.py --cluster-faces-force --db library.db  # 4. persons (for coverage)
```

## Quickstart

```bash
# Curate -> manifest + contact sheet + a facet album to review
python curate.py run --db library.db \
    --contact-sheet candidates.html --write-album "Curated Candidates"

# Open candidates.html (or the album in the viewer), reject what you don't want,
# then export the survivors' originals + manifest, ready to re-upload:
python curate.py export --db library.db --album "Curated Candidates" --dest ./curated_out
```

## CLI

| Command | Purpose |
|---|---|
| `run --db DB [--config C] [--target N] [--multiplier M] [--out J] [--contact-sheet H] [--write-album NAME]` | Curate into a candidate set |
| `export --db DB --album NAME --dest DIR [--include-rejected]` | Copy surviving originals + manifest to a folder |

## Configuration

Every knob has a sight-unseen default and can be overridden per album via a
`curator` block in `scoring_config.json` (pass `--config`). CLI flags override the
block, which overrides defaults. Keys (see spec §6): `target_count`,
`candidate_multiplier`, `min_per_day`, `min_per_event`, `scene_gap_minutes`,
`merge_cos`, `dedup.{phash_max,dup_cos,same_moment_minutes}`,
`rank_weights.{aggregate,aesthetic,composition,face_quality}`,
`day_weight_exponent`, `event_weight_exponent`,
`coverage.{min_shots_per_person,min_face_quality,min_eyes_open,max_swap_cost}`.

## Generic by design

The curator works on **any** folder facet can scan — it hard-codes nothing about
any particular trip. It **degrades gracefully**: no GPS → day+gap+semantic
bucketing only; no captions → semantic merge/moments skipped; a single
contributor → contributor logic is a no-op; missing dates → excluded with a
warning. The reference contributor, locations, days, events and persons are all
derived at runtime.

## Roadmap

- **API + gallery UI** — a "Curate" action and a review view (day/event columns,
  quota fill, coverage warnings) reusing facet's keyboard-first reject + undo.
- **Video** — frame-sample clips so they get their own quota (facet is stills-only
  today; the curator emits a separate video manifest in the meantime).
- **Clock-skew correction** — detect and optionally correct contributors whose
  device clock/timezone drifts from the reference.
