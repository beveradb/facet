"""Tests for the Album Curator review router (api/routers/curator.py).

Uses the shared ``edition_client`` / ``regular_client`` / ``anonymous_client``
fixtures from ``tests/conftest.py`` — ``app.dependency_overrides`` is the only
mechanism that actually bypasses the captured ``Depends()`` references.

The curator engine reads the same test DB the router writes to (both resolve
``db.DEFAULT_DB_PATH``), so seeding rows into ``photos`` exercises the full
run → candidates → toggle → save flow end to end.
"""

import sqlite3

import pytest

from api.routers.curator import _item_type, _thumb_url
from db import DEFAULT_DB_PATH


def _insert_photos(rows):
    """Insert minimal photo rows. Each row: (path, filename, date_taken, aggregate)."""
    conn = sqlite3.connect(DEFAULT_DB_PATH)
    try:
        conn.execute("DELETE FROM photos")
        conn.executemany(
            "INSERT INTO photos (path, filename, date_taken, aggregate, aesthetic, "
            "comp_score, face_ratio, face_quality) VALUES (?, ?, ?, ?, ?, ?, 0.0, 0.0)",
            [(p, f, d, agg, agg, agg) for (p, f, d, agg) in rows],
        )
        conn.commit()
    finally:
        conn.close()


def _reset_curator_tables():
    conn = sqlite3.connect(DEFAULT_DB_PATH)
    try:
        conn.execute("DROP TABLE IF EXISTS curator_selection")
        conn.execute("DROP TABLE IF EXISTS curator_pool_cache")
        conn.commit()
    finally:
        conn.close()


# Two days, three photos each — six single-photo slots across (at least) two
# buckets. All dated, no GPS, so bucketing/dedup run without geocoding.
_PHOTOS = [
    ("/lib/d1a.jpg", "d1a.jpg", "2026-07-24 10:00:00", 9.0),
    ("/lib/d1b.jpg", "d1b.jpg", "2026-07-24 10:05:00", 8.0),
    ("/lib/d1c.jpg", "d1c.jpg", "2026-07-24 12:00:00", 7.0),
    ("/lib/d2a.jpg", "d2a.jpg", "2026-07-25 09:00:00", 6.5),
    ("/lib/d2b.jpg", "d2b.jpg", "2026-07-25 09:30:00", 5.0),
    ("/lib/d2c.jpg", "d2c.jpg", "2026-07-25 15:00:00", 4.0),
]


@pytest.fixture()
def client(edition_client):
    _reset_curator_tables()
    _insert_photos(_PHOTOS)
    yield edition_client
    _reset_curator_tables()


class TestRun:
    def test_run_returns_pool_grouped_by_day(self, client):
        resp = client.post("/api/curator/run")
        assert resp.status_code == 200
        body = resp.json()
        assert body["needs_run"] is False
        assert body["target"] == 100
        assert body["total"] == len(_PHOTOS)
        # Every photo is a candidate item somewhere in the pool.
        item_ids = {it["id"] for b in body["buckets"] for it in b["items"]}
        assert item_ids == {p[0] for p in _PHOTOS}
        # Both days are represented.
        days = {b["day"] for b in body["buckets"]}
        assert days == {"2026-07-24", "2026-07-25"}
        # With a target far above the pool size everything auto-selects.
        assert body["selected_count"] == len(_PHOTOS)

    def test_run_seeds_kept_so_save_has_content(self, client):
        client.post("/api/curator/run")
        # A brand-new pool with no user edits still saves the auto-selection.
        resp = client.post("/api/curator/save_album", json={"name": "Curated Final"})
        assert resp.status_code == 200
        assert resp.json()["count"] == len(_PHOTOS)

    def test_rerun_preserves_user_edits(self, client):
        """A second run refreshes the pool WITHOUT clobbering edits.

        Guards the ``INSERT OR IGNORE`` seeding — a switch to ``INSERT OR
        REPLACE`` would silently re-select an item the user un-ticked.
        """
        client.post("/api/curator/run")
        client.post("/api/curator/toggle", json={"id": "/lib/d1a.jpg", "selected": False})

        rerun = client.post("/api/curator/run").json()
        assert rerun["selected_count"] == len(_PHOTOS) - 1
        toggled = next(
            it for b in rerun["buckets"] for it in b["items"] if it["id"] == "/lib/d1a.jpg"
        )
        assert toggled["selected"] is False

    def test_run_requires_edition(self, regular_client):
        assert regular_client.post("/api/curator/run").status_code == 403


class TestCandidates:
    def test_candidates_before_run_signals_needs_run(self, client):
        resp = client.get("/api/curator/candidates")
        assert resp.status_code == 200
        body = resp.json()
        assert body["needs_run"] is True
        assert body["buckets"] == []
        assert body["selected_count"] == 0

    def test_candidates_readable_anonymously(self, anonymous_client):
        # Public read path: no auth, still returns a well-formed payload.
        resp = anonymous_client.get("/api/curator/candidates")
        assert resp.status_code == 200
        assert "buckets" in resp.json()

    def test_candidate_item_shape(self, client):
        client.post("/api/curator/run")
        body = client.get("/api/curator/candidates").json()
        item = body["buckets"][0]["items"][0]
        assert set(item) == {
            "id", "type", "thumb", "caption", "category", "moment",
            "score", "duration", "selected",
        }
        assert item["type"] == "photo"
        assert item["thumb"].startswith("/thumbnail?path=")
        assert isinstance(item["selected"], bool)


class TestToggle:
    def test_toggle_persists_and_updates_count(self, client):
        run = client.post("/api/curator/run").json()
        assert run["selected_count"] == len(_PHOTOS)

        target = "/lib/d1a.jpg"
        off = client.post("/api/curator/toggle", json={"id": target, "selected": False})
        assert off.status_code == 200
        assert off.json()["selected_count"] == len(_PHOTOS) - 1

        # Persists across a fresh read (side table).
        body = client.get("/api/curator/candidates").json()
        assert body["selected_count"] == len(_PHOTOS) - 1
        toggled = next(
            it for b in body["buckets"] for it in b["items"] if it["id"] == target
        )
        assert toggled["selected"] is False

        # Re-ticking restores it.
        on = client.post("/api/curator/toggle", json={"id": target, "selected": True})
        assert on.json()["selected_count"] == len(_PHOTOS)

    def test_toggle_requires_edition(self, regular_client):
        resp = regular_client.post(
            "/api/curator/toggle", json={"id": "/lib/d1a.jpg", "selected": False}
        )
        assert resp.status_code == 403


class TestSaveAlbum:
    def test_save_reflects_user_edits(self, client):
        client.post("/api/curator/run")
        client.post("/api/curator/toggle", json={"id": "/lib/d1a.jpg", "selected": False})
        client.post("/api/curator/toggle", json={"id": "/lib/d2c.jpg", "selected": False})

        resp = client.post("/api/curator/save_album", json={"name": "Curated Final"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == len(_PHOTOS) - 2
        assert isinstance(body["album_id"], int)

        # The album contains exactly the kept set, chronologically ordered.
        conn = sqlite3.connect(DEFAULT_DB_PATH)
        try:
            conn.row_factory = sqlite3.Row
            paths = [
                r["photo_path"]
                for r in conn.execute(
                    "SELECT ap.photo_path FROM album_photos ap "
                    "JOIN albums a ON a.id = ap.album_id WHERE a.name = ? ORDER BY ap.position",
                    ("Curated Final",),
                )
            ]
        finally:
            conn.close()
        assert "/lib/d1a.jpg" not in paths
        assert "/lib/d2c.jpg" not in paths
        assert len(paths) == len(_PHOTOS) - 2
        assert paths.index("/lib/d1b.jpg") < paths.index("/lib/d2a.jpg")

        # Re-saving the same name is idempotent (replaces, not appends).
        second = client.post("/api/curator/save_album", json={"name": "Curated Final"})
        assert second.json()["count"] == len(_PHOTOS) - 2

    def test_save_requires_edition(self, regular_client):
        resp = regular_client.post("/api/curator/save_album", json={"name": "X"})
        assert resp.status_code == 403


class TestPoolHelpers:
    """Pure-function coverage of the overlay + item-type logic — the branches the
    full run/toggle flow can't cheaply reach (a partial auto-selection needs a
    120+ photo pool; a video candidate needs the not-yet-shipped 9c engine)."""

    def test_is_selected_overlay(self):
        from api.routers.curator import _is_selected

        auto_on = {"id": "/p/a.jpg", "auto_selected": True}
        auto_off = {"id": "/p/b.jpg", "auto_selected": False}

        # No user row → the engine's auto value stands.
        assert _is_selected(auto_on, {}) is True
        assert _is_selected(auto_off, {}) is False       # the "not selected" render path
        # A user edit overrides the auto value in either direction.
        assert _is_selected(auto_on, {"/p/a.jpg": 0}) is False   # un-ticked an auto pick
        assert _is_selected(auto_off, {"/p/b.jpg": 1}) is True   # added a non-auto candidate

    def test_item_type_detects_video(self):
        from api.routers.curator import _item_type

        assert _item_type("/lib/IMG_1.jpg") == "photo"
        assert _item_type("/lib/clip.mp4") == "video"
        assert _item_type("/lib/CLIP.MOV") == "video"      # case-insensitive
        assert _item_type("/lib/no_extension") == "photo"

    def test_thumb_url_encodes_path(self):
        from api.routers.curator import _thumb_url

        url = _thumb_url("/lib/a b&c.jpg")
        assert url == "/thumbnail?path=%2Flib%2Fa%20b%26c.jpg&size=320"


# --- Video candidates (9c): thumbnail routing ---

def _seed_video_thumb(path, data=b"\xff\xd8KEYFRAME"):
    conn = sqlite3.connect(DEFAULT_DB_PATH)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS curator_video_thumbs (path TEXT PRIMARY KEY, thumbnail BLOB)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO curator_video_thumbs (path, thumbnail) VALUES (?, ?)",
            (path, data),
        )
        conn.commit()
    finally:
        conn.close()


def test_thumb_url_branches_photo_vs_video():
    assert _thumb_url("/lib/a.jpg").startswith("/thumbnail?")
    assert _thumb_url("/lib/clip.mp4").startswith("/api/curator/video_thumb?")
    assert _item_type("/lib/clip.MOV") == "video" and _item_type("/lib/a.jpg") == "photo"


def test_video_thumb_serves_keyframe_and_404s(anonymous_client):
    _seed_video_thumb("/lib/clip.mp4", b"\xff\xd8KEYFRAME")
    ok = anonymous_client.get("/api/curator/video_thumb", params={"path": "/lib/clip.mp4"})
    assert ok.status_code == 200
    assert ok.content == b"\xff\xd8KEYFRAME"
    assert ok.headers["content-type"] == "image/jpeg"
    missing = anonymous_client.get("/api/curator/video_thumb", params={"path": "/lib/nope.mp4"})
    assert missing.status_code == 404
