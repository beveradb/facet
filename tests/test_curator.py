"""Unit tests for the album curator — pure-function coverage plus a temp-DB
end-to-end smoke. Designed to run without the real library: synthetic Photo
records exercise every stage, including the graceful-degradation paths that keep
the feature generic (no GPS / no captions / single contributor / missing dates).
"""
# Tests use naive local datetimes (matching facet's date_taken) and dict() for ergonomics.
# ruff: noqa: DTZ001, C408
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import numpy as np
import pytest

from curator import CuratorConfig, curate
from curator.allocate import _apportion, allocate_quotas
from curator.bucketing import Bucket, build_buckets
from curator.config import config_from_dict, load_config
from curator.datasource import Photo, load_photos
from curator.dedup import dedup_bucket
from curator.emit import write_contact_sheet
from curator.exporter import export_album
from curator.geocode import assign_contributors, assign_locations
from curator.rank import photo_score, pick_bucket
from curator.select import CurationResult, _coverage_pass
from curator.writeback import write_album

BASE = datetime(2026, 7, 22, 12, 0, 0)


def mkphoto(i=0, **over) -> Photo:
    d = dict(
        path=f"/p/{i}.jpg", filename=f"IMG_{i}.jpg", dt=BASE + timedelta(minutes=i),
        lat=None, lon=None, category="concert", aggregate=6.0, aesthetic=6.0,
        comp=6.0, face_quality=6.0, face_ratio=0.0, eyes_open=1.0, expression=0.5,
        is_blink=0, is_rejected=0, is_junk=False, is_dup_lead=0, duplicate_group_id=None, burst_group_id=None,
        phash=None, caption=None, moment="concert", moment_conf=0.5, emb=None, img_emb=None,
        camera="TestCam", persons=[],
    )
    d.update(over)
    return Photo(**d)


# ---------------------------------------------------------------- apportion / allocate

def test_apportion_respects_total_floors_caps():
    keys = ["a", "b", "c"]
    alloc = _apportion(keys, total=10, weights={"a": 3, "b": 1, "c": 1},
                       floors={"a": 1, "b": 1, "c": 1}, caps={"a": 5, "b": 5, "c": 5})
    assert sum(alloc.values()) == 10
    assert all(alloc[k] >= 1 for k in keys)           # floors honored
    assert all(alloc[k] <= 5 for k in keys)           # caps honored
    assert alloc["a"] >= alloc["b"]                    # heavier weight gets more


def test_apportion_caps_total_at_available():
    alloc = _apportion(["a", "b"], total=100, weights={"a": 1, "b": 1},
                       floors={"a": 0, "b": 0}, caps={"a": 3, "b": 4})
    assert sum(alloc.values()) == 7                    # can't exceed available slots


def test_allocate_every_day_gets_its_floor():
    cfg = CuratorConfig(min_per_day=2, min_per_event=1)
    # day 1: one big event (10 slots); day 2: one tiny event (1 slot)
    b1 = Bucket("2026-07-22", "X", "concert"); b1.slots = [[mkphoto(i)] for i in range(10)]
    b2 = Bucket("2026-07-23", "X", "dining"); b2.slots = [[mkphoto(99)]]
    quotas = allocate_quotas([b1, b2], target=6, cfg=cfg)
    assert quotas[b2.id] >= 1                          # tiny day still represented
    assert sum(quotas.values()) == 6
    assert quotas[b1.id] <= 10 and quotas[b2.id] <= 1  # never over available


# ---------------------------------------------------------------- bucketing

def test_bucketing_splits_on_day_location_and_gap():
    cfg = CuratorConfig(scene_gap_minutes=30)
    photos = [
        mkphoto(0, dt=datetime(2026, 7, 22, 10, 0), location="A"),
        mkphoto(1, dt=datetime(2026, 7, 22, 10, 5), location="A"),   # same scene
        mkphoto(2, dt=datetime(2026, 7, 22, 12, 0), location="A"),   # >30min gap -> new scene
        mkphoto(3, dt=datetime(2026, 7, 22, 12, 3), location="B"),   # location change -> new scene
        mkphoto(4, dt=datetime(2026, 7, 23, 10, 0), location="B"),   # new day -> new scene
    ]
    buckets, undated = build_buckets(photos, cfg)
    assert not undated
    assert len(buckets) == 4
    assert buckets[0].day == "2026-07-22" and len(buckets[0].photos) == 2


def test_bucket_ids_unique_across_identical_scenes():
    # two scenes with the SAME day/location/event, split by a gap, must not collide
    cfg = CuratorConfig(scene_gap_minutes=30)
    photos = [
        mkphoto(0, dt=datetime(2026, 7, 22, 10, 0), location="A", moment="other"),
        mkphoto(1, dt=datetime(2026, 7, 22, 12, 0), location="A", moment="other"),
    ]
    buckets, _ = build_buckets(photos, cfg)
    assert len(buckets) == 2
    assert len({b.id for b in buckets}) == 2          # unique ids despite identical labels


def test_bucketing_separates_undated():
    cfg = CuratorConfig()
    photos = [mkphoto(0), mkphoto(1, dt=None)]
    buckets, undated = build_buckets(photos, cfg)
    assert len(undated) == 1 and len(buckets) == 1


def test_bucketing_semantic_merge_across_lull():
    cfg = CuratorConfig(scene_gap_minutes=10, merge_cos=0.5)
    e = np.ones(8, dtype=np.float32); e /= np.linalg.norm(e)
    photos = [
        mkphoto(0, dt=datetime(2026, 7, 22, 10, 0), location="A", emb=e),
        mkphoto(1, dt=datetime(2026, 7, 22, 10, 30), location="A", emb=e),  # gap>10 but same embedding
    ]
    buckets, _ = build_buckets(photos, cfg)
    assert len(buckets) == 1                           # merged back into one event


# ---------------------------------------------------------------- dedup

def test_dedup_groups_by_duplicate_group_and_phash():
    cfg = CuratorConfig(phash_max=4)
    photos = [
        mkphoto(0, duplicate_group_id=7),
        mkphoto(1, duplicate_group_id=7),               # same dup group
        mkphoto(2, phash="ffff", ),
        mkphoto(3, phash="fffe"),                        # hamming 1 <= 4
        mkphoto(4, phash="0000"),                        # far
    ]
    b = Bucket("d", "l", "e"); b.photos = photos
    dedup_bucket(b, cfg)
    sizes = sorted(len(s) for s in b.slots)
    assert sizes == [1, 2, 2]                            # {0,1} {2,3} {4}
    assert sum(len(s) for s in b.slots) == 5             # partitions all photos


def test_dedup_same_scene_by_image_embedding_and_time():
    cfg = CuratorConfig(scene_cos=0.9, same_scene_minutes=5)
    e = np.ones(8, dtype=np.float32); e /= np.linalg.norm(e)
    far = np.array([1, -1, 1, -1, 1, -1, 1, -1], dtype=np.float32); far /= np.linalg.norm(far)
    photos = [
        mkphoto(0, dt=datetime(2026, 7, 22, 10, 0), img_emb=e, camera="Pixel"),
        mkphoto(1, dt=datetime(2026, 7, 22, 10, 2), img_emb=e, camera="iPhone"),  # same scene, two phones
        mkphoto(2, dt=datetime(2026, 7, 22, 10, 3), img_emb=far, camera="Pixel"),  # visually distinct
        mkphoto(3, dt=datetime(2026, 7, 22, 11, 0), img_emb=e, camera="Pixel"),   # same look, 1h later -> keep
    ]
    b = Bucket("d", "l", "e"); b.photos = photos
    dedup_bucket(b, cfg)
    assert len(b.slots) == 3                             # {0,1} collapse; 2 and 3 stay separate


def test_dedup_by_burst_group():
    cfg = CuratorConfig()
    photos = [mkphoto(0, burst_group_id=5), mkphoto(1, burst_group_id=5), mkphoto(2, burst_group_id=None)]
    b = Bucket("d", "l", "e"); b.photos = photos
    dedup_bucket(b, cfg)
    assert sorted(len(s) for s in b.slots) == [1, 2]     # burst {0,1} collapses, 2 alone


# ---------------------------------------------------------------- rank

def test_photo_score_penalises_blink_and_rewards_quality():
    cfg = CuratorConfig()
    good = mkphoto(0, aggregate=8, aesthetic=8, comp=8)
    blink = mkphoto(1, aggregate=8, aesthetic=8, comp=8, is_blink=1)
    assert photo_score(good, cfg) > photo_score(blink, cfg)


def test_pick_bucket_takes_top_quota():
    cfg = CuratorConfig()
    b = Bucket("d", "l", "e")
    b.slots = [[mkphoto(0, aggregate=9)], [mkphoto(1, aggregate=5)], [mkphoto(2, aggregate=7)]]
    picks = pick_bucket(b, quota=2, cfg=cfg)
    got = sorted(p.aggregate for p in picks)
    assert got == [7.0, 9.0]                             # the two best


# ---------------------------------------------------------------- coverage

def test_coverage_swaps_in_uncovered_person_when_affordable():
    cfg = CuratorConfig(min_shots_per_person=1, min_face_quality=5.0, min_eyes_open=0.5,
                        min_per_event=1, max_swap_cost=10.0)
    # bucket with 2 slots: a selected weak no-person shot, plus an unselected shot of person 1
    weak = mkphoto(0, aggregate=5.5, face_ratio=0.3, face_quality=6.0, persons=[])
    covers = mkphoto(1, aggregate=5.0, face_ratio=0.3, face_quality=7.0, persons=[1])
    b = Bucket("d", "l", "e"); b.slots = [[weak], [covers]]
    result = CurationResult(selected=[weak], buckets=[b], quotas={b.id: 1},
                            persons={1: "Alice"}, contributors={}, reference=None,
                            locations={}, undated=[], picks_by_bucket={b.id: [weak]})
    _coverage_pass(result, cfg)
    assert covers in result.selected and weak not in result.selected


def test_coverage_warns_when_person_absent():
    cfg = CuratorConfig(min_shots_per_person=1)
    p = mkphoto(0, persons=[])
    b = Bucket("d", "l", "e"); b.slots = [[p]]
    result = CurationResult(selected=[p], buckets=[b], quotas={b.id: 1},
                            persons={9: "Ghost"}, contributors={}, reference=None,
                            locations={}, undated=[], picks_by_bucket={b.id: [p]})
    _coverage_pass(result, cfg)
    assert any("Ghost" in w for w in result.coverage_warnings)


def test_coverage_ignores_unnamed_clusters():
    cfg = CuratorConfig(min_shots_per_person=1)
    p = mkphoto(0, persons=[])
    b = Bucket("d", "l", "e"); b.slots = [[p]]
    result = CurationResult(selected=[p], buckets=[b], quotas={b.id: 1},
                            persons={1: None}, contributors={}, reference=None,   # unnamed cluster
                            locations={}, undated=[], picks_by_bucket={b.id: [p]})
    _coverage_pass(result, cfg)
    assert result.coverage_warnings == [] and result.selected == [p]  # untouched


def test_coverage_never_duplicates_a_scene():
    # a named person's only covering shot shares a slot with an already-selected photo
    cfg = CuratorConfig(min_shots_per_person=1, min_face_quality=5.0, min_eyes_open=0.5, max_swap_cost=99)
    rep = mkphoto(0, face_ratio=0.3, face_quality=6.0, persons=[])
    same_scene = mkphoto(1, face_ratio=0.3, face_quality=8.0, persons=[1])  # SAME slot, covers person 1
    b = Bucket("d", "l", "e"); b.slots = [[rep, same_scene]]                # one slot, two photos
    result = CurationResult(selected=[rep], buckets=[b], quotas={b.id: 1},
                            persons={1: "Alice"}, contributors={}, reference=None,
                            locations={}, undated=[], picks_by_bucket={b.id: [rep]})
    _coverage_pass(result, cfg)
    assert same_scene not in result.selected and result.selected == [rep]  # no scene duplication


# ---------------------------------------------------------------- config

def test_config_from_dict_flattens_nested_and_cli_overrides():
    block = {
        "target_count": 50, "scene_gap_minutes": 20,
        "dedup": {"phash_max": 3},
        "coverage": {"min_shots_per_person": 2},
        "rank_weights": {"aggregate": 0.4, "aesthetic": 0.2, "composition": 0.2, "face_quality": 0.2},
    }
    cfg = config_from_dict(block, target_count=99)          # CLI override supplied
    assert cfg.target_count == 99                            # override beats block
    assert cfg.scene_gap_minutes == 20                       # block value applied
    assert cfg.phash_max == 3                                # nested dedup flattened
    assert cfg.min_shots_per_person == 2                     # nested coverage flattened
    assert cfg.rank_weights["aggregate"] == 0.4


def test_load_config_missing_file_falls_back_to_defaults():
    cfg = load_config(None)
    assert cfg.target_count == 100 and cfg.candidate_multiplier == 1.2


# ---------------------------------------------------------------- genericity / degradation

def test_contributors_reference_is_most_gps_device():
    photos = [
        mkphoto(0, filename="PXL_1.jpg", camera="Pixel", lat=52.0, lon=4.0),
        mkphoto(1, filename="PXL_2.jpg", camera="Pixel", lat=52.1, lon=4.1),
        mkphoto(2, filename="IMG_3.jpg", camera="iPhone"),
    ]
    counts, reference = assign_contributors(photos)
    assert reference is not None and "Pixel" in reference
    assert set(counts) == {p.contributor for p in photos}


def test_assign_locations_no_gps_is_noop():
    photos = [mkphoto(0), mkphoto(1)]                    # no lat/lon anywhere
    assert assign_locations(photos) == {}
    assert all(p.location is None for p in photos)


def test_single_contributor_and_no_embeddings_still_buckets():
    cfg = CuratorConfig()
    photos = [mkphoto(i, dt=datetime(2026, 7, 22, 10, i)) for i in range(3)]  # no emb, one camera
    buckets, _ = build_buckets(photos, cfg)
    for b in buckets:
        dedup_bucket(b, cfg)
    assert sum(len(b.slots) for b in buckets) == 3       # nothing crashes, all placed


# ---------------------------------------------------------------- datasource + end-to-end

@pytest.fixture
def tiny_db(tmp_path):
    db = tmp_path / "photos.db"
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE photos (
        path TEXT, filename TEXT, date_taken TEXT, gps_latitude REAL, gps_longitude REAL,
        category TEXT, aggregate REAL, aesthetic REAL, comp_score REAL, face_quality REAL,
        face_ratio REAL, eyes_open_score REAL, expression_score REAL, is_blink INT,
        is_rejected INT, is_duplicate_lead INT, duplicate_group_id INT, burst_group_id INT, phash TEXT,
        caption TEXT, narrative_moment TEXT, narrative_moment_confidence REAL,
        caption_embedding BLOB, clip_embedding BLOB, camera_model TEXT, thumbnail BLOB, junk_kind TEXT)""")
    conn.execute("CREATE TABLE faces (photo_path TEXT, person_id INT)")
    conn.execute("CREATE TABLE persons (id INT, name TEXT)")
    conn.execute("CREATE TABLE albums (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, description TEXT)")
    conn.execute("CREATE TABLE album_photos (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "album_id INT, photo_path TEXT, position INT)")
    for i in range(6):
        day = 22 + (i // 3)
        conn.execute(
            "INSERT INTO photos (path, filename, date_taken, aggregate, aesthetic, comp_score, "
            "face_quality, face_ratio, eyes_open_score, expression_score, category, narrative_moment, "
            "narrative_moment_confidence, camera_model) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"/p/{i}.jpg", f"IMG_{i}.jpg", f"2026:07:{day} 1{i}:00:00", 6.0 + i * 0.1, 6.0, 6.0,
             6.0, 0.0, 1.0, 0.5, "concert", "concert", 0.5, "TestCam"),
        )
    conn.commit()
    conn.close()
    return str(db)


def test_load_photos_and_curate_end_to_end(tiny_db):
    photos = load_photos(tiny_db)
    assert len(photos) == 6 and all(p.dt is not None for p in photos)
    result = curate(tiny_db, CuratorConfig(target_count=4, candidate_multiplier=1.0, min_per_day=1))
    assert result.selected                               # produced a non-empty selection
    days = {p.day for p in result.selected}
    assert days == {"2026-07-22", "2026-07-23"}          # both days represented


def test_junk_and_rejected_excluded_from_candidacy(tmp_path):
    db = tmp_path / "j.db"
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE photos (path TEXT, filename TEXT, date_taken TEXT, gps_latitude REAL,
        gps_longitude REAL, category TEXT, aggregate REAL, aesthetic REAL, comp_score REAL, face_quality REAL,
        face_ratio REAL, eyes_open_score REAL, expression_score REAL, is_blink INT, is_rejected INT,
        is_duplicate_lead INT, duplicate_group_id INT, burst_group_id INT, phash TEXT, caption TEXT,
        narrative_moment TEXT, narrative_moment_confidence REAL, caption_embedding BLOB, clip_embedding BLOB,
        camera_model TEXT, junk_kind TEXT)""")
    conn.execute("CREATE TABLE faces (photo_path TEXT, person_id INT)")
    conn.execute("CREATE TABLE persons (id INT, name TEXT)")

    def ins(path, hour, junk=None, rej=0):
        conn.execute(
            "INSERT INTO photos (path, filename, date_taken, aggregate, camera_model, junk_kind, is_rejected) "
            "VALUES (?,?,?,?,?,?,?)",
            (path, path, f"2026:07:22 1{hour}:00:00", 6.0, "Cam", junk, rej),
        )
    ins("/n1.jpg", 0); ins("/n2.jpg", 1)
    ins("/doc.jpg", 2, junk="document")
    ins("/rej.jpg", 3, rej=1)
    conn.commit(); conn.close()

    result = curate(str(db), CuratorConfig(min_per_day=1))
    names = {p.filename for p in result.selected}
    assert "/doc.jpg" not in names and "/rej.jpg" not in names
    assert result.excluded_junk == 1 and result.excluded_rejected == 1
    assert "/n1.jpg" in names


def test_selected_matches_picks_by_bucket(tiny_db):
    # the selection and the per-bucket picks must never desync (bucket-id uniqueness)
    result = curate(tiny_db, CuratorConfig(target_count=4, candidate_multiplier=1.0, min_per_day=1))
    assert len(result.selected) == sum(len(v) for v in result.picks_by_bucket.values())


def test_export_album_copies_survivors_and_skips_rejected(tmp_path):
    src = tmp_path / "src"; src.mkdir()
    f1 = src / "a.jpg"; f1.write_bytes(b"img1")
    f2 = src / "b.jpg"; f2.write_bytes(b"img2")
    db = tmp_path / "e.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE photos (path TEXT, filename TEXT, is_rejected INT, date_taken TEXT, caption TEXT)")
    conn.execute("CREATE TABLE albums (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("CREATE TABLE album_photos (album_id INT, photo_path TEXT, position INT)")
    conn.execute("INSERT INTO photos VALUES (?,?,?,?,?)", (str(f1), "a.jpg", 0, "2026:07:22 10:00:00", "cap a"))
    conn.execute("INSERT INTO photos VALUES (?,?,?,?,?)", (str(f2), "b.jpg", 1, "2026:07:22 11:00:00", "cap b"))
    conn.execute("INSERT INTO albums (id, name) VALUES (1, 'Cand')")
    conn.execute("INSERT INTO album_photos VALUES (1, ?, 0)", (str(f1),))
    conn.execute("INSERT INTO album_photos VALUES (1, ?, 1)", (str(f2),))
    conn.commit(); conn.close()

    dest = tmp_path / "out"
    m = export_album(str(db), "Cand", str(dest))
    assert m["exported"] == 1 and m["skipped_rejected"] == 1
    assert (dest / "a.jpg").exists() and not (dest / "b.jpg").exists()
    assert (dest / "manifest.json").exists() and (dest / "manifest.csv").exists()


def test_write_album_is_idempotent(tiny_db):
    result = curate(tiny_db, CuratorConfig(target_count=4, candidate_multiplier=1.0, min_per_day=1))
    aid = write_album(tiny_db, "Cand", result.selected)
    conn = sqlite3.connect(tiny_db)
    n = conn.execute("SELECT COUNT(*) FROM album_photos WHERE album_id=?", (aid,)).fetchone()[0]
    conn.close()
    assert n == len(result.selected)
    write_album(tiny_db, "Cand", result.selected)          # re-run replaces, not duplicates
    conn = sqlite3.connect(tiny_db)
    albums = conn.execute("SELECT COUNT(*) FROM albums WHERE name='Cand'").fetchone()[0]
    conn.close()
    assert albums == 1


def test_contact_sheet_renders(tiny_db, tmp_path):
    cfg = CuratorConfig(target_count=4, candidate_multiplier=1.0, min_per_day=1)
    result = curate(tiny_db, cfg)
    out = tmp_path / "sheet.html"
    write_contact_sheet(result, tiny_db, str(out), cfg)
    doc = out.read_text()
    assert "Curated candidates" in doc
    assert any(p.filename in doc for p in result.selected)  # picks are listed
