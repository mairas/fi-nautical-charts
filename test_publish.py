"""Tests for the publish step.

The failure this step exists to prevent is silent: an earlier upload truncated
three of five files and nothing noticed for six days. So most of what is
asserted here is what happens when something goes wrong -- the published set
must be left exactly as it was.
"""

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

import publish

META = {
    "wmts_layer": "veneilykartat public",
    "source_updated": "2026-06-21",
    "source_updated_oldest": "2026-05-30",
    "downloaded": "2026-06-21",
    "nodata_stripped": "opaque-black-r4+offeez-tilelevel",
    "downscaled": "2026-08-08",
    "downscale_source_zoom": "15",
    "downscale_filter": "box-2x-premultiplied",
    "minzoom": "3",
    "maxzoom": "15",
    "name": "Veneilykartat 2026-06-21",
}


def make_mbtiles(path: Path, meta: dict = None, payload: bytes = b"tile") -> Path:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE metadata (name TEXT PRIMARY KEY, value TEXT)")
    con.execute("CREATE TABLE tiles (zoom_level INT, tile_column INT, tile_row INT, tile_data BLOB)")
    con.executemany("INSERT INTO metadata VALUES (?,?)", (META if meta is None else meta).items())
    con.execute("INSERT INTO tiles VALUES (3,1,1,?)", (payload,))
    con.commit()
    con.close()
    return path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_published_name_comes_from_the_edition_in_the_file():
    assert publish.published_name(META) == "fi-veneilykartat-2026-06-21.mbtiles"


def test_a_file_with_no_recorded_edition_is_refused(tmp_path):
    src = make_mbtiles(tmp_path / "x.mbtiles", {k: v for k, v in META.items()
                                                if k != "source_updated"})
    dest = tmp_path / "charts"
    dest.mkdir()
    with pytest.raises(publish.Unpublishable):
        publish.publish([src], dest)


def test_publish_places_the_file_under_its_edition_name(tmp_path):
    src = make_mbtiles(tmp_path / "work.processed.mbtiles")
    dest = tmp_path / "charts"
    dest.mkdir()

    publish.publish([src], dest)

    target = dest / "fi-veneilykartat-2026-06-21.mbtiles"
    assert target.exists()
    assert sha256(target) == sha256(src)


def test_manifest_records_size_digest_edition_and_processing(tmp_path):
    src = make_mbtiles(tmp_path / "work.processed.mbtiles")
    dest = tmp_path / "charts"
    dest.mkdir()

    publish.publish([src], dest)

    manifest = json.loads((dest / publish.MANIFEST).read_text())
    (entry,) = manifest["charts"]
    target = dest / "fi-veneilykartat-2026-06-21.mbtiles"
    assert entry["filename"] == target.name
    assert entry["bytes"] == target.stat().st_size
    assert entry["sha256"] == sha256(target)
    assert entry["source_edition"] == "2026-06-21"
    assert entry["processing"] == ("opaque-black-r4+offeez-tilelevel; "
                                   "box-2x-premultiplied from z15 on 2026-08-08")
    assert manifest["pipeline"]


def test_the_digest_describes_the_published_bytes_not_the_source(tmp_path):
    """Hashing the source would defeat the point: the manifest has to be a
    statement about the file a downloader will actually get."""
    src = make_mbtiles(tmp_path / "work.processed.mbtiles")
    dest = tmp_path / "charts"
    dest.mkdir()

    digests = []
    real = publish.digest_of

    def spy(path):
        digests.append(Path(path))
        return real(path)

    publish.digest_of = spy
    try:
        publish.publish([src], dest)
    finally:
        publish.digest_of = real

    assert all(p.parent == dest for p in digests), digests


def test_reading_a_wal_mode_chart_leaves_no_sidecars_in_the_served_directory(tmp_path):
    """Opening a WAL-mode SQLite file creates -wal/-shm beside it, even read-only.
    The destination is a directory a web server exposes, so it must end a publish
    holding charts and a manifest and nothing else."""
    dest = tmp_path / "charts"
    dest.mkdir()
    src = make_mbtiles(tmp_path / "work.processed.mbtiles")
    con = sqlite3.connect(src)
    con.execute("PRAGMA journal_mode=WAL")
    con.commit()
    con.close()

    publish.publish([src], dest)

    assert sorted(p.name for p in dest.iterdir()) == [
        publish.MANIFEST, "fi-veneilykartat-2026-06-21.mbtiles"]


def test_a_superseded_edition_is_removed(tmp_path):
    dest = tmp_path / "charts"
    dest.mkdir()
    stale = make_mbtiles(dest / "fi-veneilykartat-2026-05-30.mbtiles")
    other = make_mbtiles(dest / "fi-satamakartat-2026-06-29.mbtiles")
    src = make_mbtiles(tmp_path / "work.processed.mbtiles")

    publish.publish([src], dest)

    assert not stale.exists()
    assert other.exists(), "only the same layer's older edition may be removed"
    assert (dest / "fi-veneilykartat-2026-06-21.mbtiles").exists()


def test_republishing_the_same_edition_keeps_the_file(tmp_path):
    dest = tmp_path / "charts"
    dest.mkdir()
    src = make_mbtiles(tmp_path / "work.processed.mbtiles")
    publish.publish([src], dest)

    src2 = make_mbtiles(tmp_path / "work2.processed.mbtiles", payload=b"redone")
    publish.publish([src2], dest)

    target = dest / "fi-veneilykartat-2026-06-21.mbtiles"
    assert target.exists()
    assert sha256(target) == sha256(src2), "the rebuilt bytes must win"


def test_a_corrupted_staging_copy_leaves_the_published_set_untouched(tmp_path, monkeypatch):
    dest = tmp_path / "charts"
    dest.mkdir()
    stale = make_mbtiles(dest / "fi-veneilykartat-2026-05-30.mbtiles")
    stale_digest = sha256(stale)
    src = make_mbtiles(tmp_path / "work.processed.mbtiles")

    def truncating_copy(source, staged):
        staged.write_bytes(source.read_bytes()[:32])
        return hashlib.sha256(source.read_bytes()).hexdigest()

    monkeypatch.setattr(publish, "stage", truncating_copy)

    with pytest.raises(publish.Unpublishable):
        publish.publish([src], dest)

    assert stale.exists() and sha256(stale) == stale_digest
    assert not (dest / "fi-veneilykartat-2026-06-21.mbtiles").exists()
    assert not list(dest.glob(".*incoming*")), "staging debris left behind"


def test_one_bad_file_does_not_publish_the_good_ones_either(tmp_path):
    """A run either publishes its whole set or none of it: a manifest that
    describes a half-updated directory is worse than no new manifest."""
    dest = tmp_path / "charts"
    dest.mkdir()
    good = make_mbtiles(tmp_path / "good.processed.mbtiles")
    bad = make_mbtiles(tmp_path / "bad.processed.mbtiles",
                       {k: v for k, v in META.items() if k != "source_updated"})

    with pytest.raises(publish.Unpublishable):
        publish.publish([good, bad], dest)

    assert not (dest / "fi-veneilykartat-2026-06-21.mbtiles").exists()
    assert not (dest / publish.MANIFEST).exists()


def test_two_builds_of_one_layer_in_a_single_run_are_refused(tmp_path):
    """`publish work/*.mbtiles` sweeps up a layer's stripped *and* downscaled
    build. They stage to the same name, so one would quietly overwrite the
    other and which one wins would depend on argument order."""
    dest = tmp_path / "charts"
    dest.mkdir()
    stripped = make_mbtiles(tmp_path / "vk.stripped.mbtiles", payload=b"stripped")
    processed = make_mbtiles(tmp_path / "vk.processed.mbtiles", payload=b"downscaled")

    with pytest.raises(publish.Unpublishable, match="both publish as"):
        publish.publish([stripped, processed], dest)

    assert not (dest / "fi-veneilykartat-2026-06-21.mbtiles").exists()


def test_manifest_carries_every_published_chart_not_just_this_run(tmp_path):
    dest = tmp_path / "charts"
    dest.mkdir()
    publish.publish([make_mbtiles(tmp_path / "a.mbtiles")], dest)

    harbour = dict(META, wmts_layer="satamakartat public", source_updated="2026-06-29")
    publish.publish([make_mbtiles(tmp_path / "b.mbtiles", harbour)], dest)

    manifest = json.loads((dest / publish.MANIFEST).read_text())
    assert {e["filename"] for e in manifest["charts"]} == {
        "fi-veneilykartat-2026-06-21.mbtiles",
        "fi-satamakartat-2026-06-29.mbtiles",
    }
