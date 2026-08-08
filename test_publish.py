"""Tests for the publish step.

The failure this step exists to prevent is silent: an earlier upload truncated
three of five files and nothing noticed for six days. So most of what is
asserted here is what happens when something goes wrong -- the published set
must be left exactly as it was.
"""

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

import publish

META = {
    "wmts_layer": "veneilykartat public",
    "source_updated": "2026-06-21",
    "source_updated_oldest": "2025-05-30",
    "downloaded": "2026-06-21",
    "nodata_stripped": "opaque-black-r4+offeez-tilelevel",
    "downscaled": "2026-08-08",
    "downscale_source_zoom": "15",
    "downscale_filter": "box-2x-premultiplied",
    "minzoom": "3",
    "maxzoom": "15",
    "name": "Veneilykartat 2026-06-21",
}


def make_mbtiles(path: Path, meta: dict[str, str] | None = None,
                 payload: bytes = b"tile", tiles: int = 1) -> Path:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE metadata (name TEXT PRIMARY KEY, value TEXT)")
    con.execute("CREATE TABLE tiles (zoom_level INT, tile_column INT, tile_row INT, tile_data BLOB)")
    con.executemany("INSERT INTO metadata VALUES (?,?)", (META if meta is None else meta).items())
    con.executemany("INSERT INTO tiles VALUES (3,?,1,?)",
                    [(i, payload) for i in range(tiles)])
    con.commit()
    con.close()
    return path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def manifest_of(dest: Path) -> dict:
    return json.loads((dest / publish.MANIFEST).read_text())


def charts_in(dest: Path) -> list[str]:
    return sorted(p.name for p in dest.glob("fi-*.mbtiles"))


@pytest.fixture
def dest(tmp_path):
    d = tmp_path / "charts"
    d.mkdir()
    return d


# -- naming ------------------------------------------------------------------

def test_published_name_comes_from_the_edition_in_the_file():
    assert publish.published_name(META) == "fi-veneilykartat-2026-06-21.mbtiles"


def test_every_real_layer_keeps_the_name_it_is_served_under():
    """These five are live. A change here changes URLs clients already hold."""
    served = {
        "Merikarttasarjat public": "fi-merikarttasarjat-2026-06-29.mbtiles",
        "Rannikkokartat public": "fi-rannikkokartat-2026-06-29.mbtiles",
        "Satamakartat": "fi-satamakartat-2026-06-29.mbtiles",
        "Veneilykartat public": "fi-veneilykartat-2026-06-21.mbtiles",
        "Yleiskartat 250k public": "fi-yleiskartat250k-2026-06-02.mbtiles",
    }
    for layer, expected in served.items():
        edition = expected.removesuffix(".mbtiles")[-10:]
        got = publish.published_name({"wmts_layer": layer, "source_updated": edition})
        assert got == expected, layer


def test_a_file_with_no_recorded_edition_is_refused(tmp_path, dest):
    src = make_mbtiles(tmp_path / "x.mbtiles",
                       {k: v for k, v in META.items() if k != "source_updated"})
    with pytest.raises(publish.Unpublishable, match="no layer and edition recorded"):
        publish.publish([src], dest)


@pytest.mark.parametrize("layer", ["*", "../../evil", "erikoiskartat public", "a/b"])
def test_metadata_that_would_escape_the_naming_scheme_is_refused(tmp_path, dest, layer):
    """The name reaches open(), glob() and unlink(). A glob metacharacter in it
    widens the retirement sweep; a separator leaves the destination."""
    src = make_mbtiles(tmp_path / "x.mbtiles", dict(META, wmts_layer=layer))
    with pytest.raises(publish.Unpublishable, match="not a name this publishes"):
        publish.publish([src], dest)
    assert charts_in(dest) == []


# -- the happy path ----------------------------------------------------------

def test_publish_places_the_file_under_its_edition_name(tmp_path, dest):
    src = make_mbtiles(tmp_path / "work.processed.mbtiles")
    publish.publish([src], dest)
    target = dest / "fi-veneilykartat-2026-06-21.mbtiles"
    assert target.exists()
    assert sha256(target) == sha256(src)


def test_manifest_records_size_digest_edition_and_processing(tmp_path, dest):
    src = make_mbtiles(tmp_path / "work.processed.mbtiles")
    publish.publish([src], dest)

    (entry,) = manifest_of(dest)["charts"]
    target = dest / "fi-veneilykartat-2026-06-21.mbtiles"
    assert entry["filename"] == target.name
    assert entry["bytes"] == target.stat().st_size
    assert entry["sha256"] == sha256(target)
    assert entry["source_edition"] == "2026-06-21"
    assert entry["source_edition_oldest"] == "2025-05-30"
    assert entry["name"] == "Veneilykartat 2026-06-21"
    assert entry["processing"] == ("opaque-black-r4+offeez-tilelevel; "
                                   "box-2x-premultiplied from z15 on 2026-08-08")
    assert manifest_of(dest)["pipeline"] not in ("", None)


@pytest.mark.parametrize("meta,expected", [
    ({"nodata_stripped": "opaque-black-r4"}, "opaque-black-r4"),
    ({"downscale_filter": "box-2x-premultiplied", "downscale_source_zoom": "15",
      "downscaled": "2026-08-08"}, "box-2x-premultiplied from z15 on 2026-08-08"),
    ({}, "none"),
])
def test_processing_describes_the_steps_that_actually_ran(meta, expected):
    assert publish.processing(meta) == expected


def test_the_digest_in_the_manifest_describes_the_published_bytes(tmp_path, dest):
    """Hashing the source would defeat the point: the manifest has to be a
    statement about the file a downloader will actually get."""
    src = make_mbtiles(tmp_path / "work.processed.mbtiles")
    publish.publish([src], dest)
    src.unlink()

    (entry,) = manifest_of(dest)["charts"]
    assert entry["sha256"] == sha256(dest / entry["filename"])


def test_a_copy_spanning_many_chunks_is_hashed_and_written_whole(tmp_path, dest, monkeypatch):
    monkeypatch.setattr(publish, "CHUNK", 512)
    src = make_mbtiles(tmp_path / "big.mbtiles", payload=b"x" * 4096, tiles=64)
    assert src.stat().st_size > 512 * 8

    publish.publish([src], dest)

    target = dest / "fi-veneilykartat-2026-06-21.mbtiles"
    assert sha256(target) == sha256(src)
    assert manifest_of(dest)["charts"][0]["sha256"] == sha256(src)


def test_stage_returns_the_digest_of_the_source_it_read(tmp_path):
    """The read-back check only means something if these are two independent
    reads: stage hashes what it read from the source, digest_of re-reads from
    disk. If stage hashed its own output the comparison could never fail."""
    src = make_mbtiles(tmp_path / "src.mbtiles", payload=b"y" * 900, tiles=8)
    staged = tmp_path / "staged.mbtiles"

    written = publish.stage(src, staged)

    assert written == sha256(src)
    assert staged.read_bytes() == src.read_bytes()


# -- refusing bad input ------------------------------------------------------

def test_a_truncated_source_is_refused(tmp_path, dest):
    """The read-back cannot catch this: a faithful copy of a truncated file is
    faithful. Both producers run journal_mode=OFF, so a killed writer leaves
    exactly this."""
    good = make_mbtiles(tmp_path / "good.mbtiles", payload=b"z" * 2048, tiles=32)
    survivor = make_mbtiles(tmp_path / "prev.mbtiles", dict(META, source_updated="2026-05-30"))
    publish.publish([survivor], dest)

    src = tmp_path / "short.mbtiles"
    src.write_bytes(good.read_bytes()[: good.stat().st_size // 2])

    with pytest.raises(publish.Unpublishable, match="truncated"):
        publish.publish([src], dest)
    assert charts_in(dest) == ["fi-veneilykartat-2026-05-30.mbtiles"]


def test_a_source_that_is_not_a_database_is_refused(tmp_path, dest):
    src = tmp_path / "junk.mbtiles"
    src.write_bytes(b"not a database at all")
    with pytest.raises(publish.Unpublishable, match="not an SQLite database"):
        publish.publish([src], dest)


def test_a_source_with_an_uncheckpointed_wal_is_refused(tmp_path, dest):
    """Staging copies the database file alone, so anything still in the -wal
    would be dropped -- and the digest check would confirm the loss."""
    src = make_mbtiles(tmp_path / "wal.mbtiles")
    con = sqlite3.connect(src)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("INSERT INTO tiles VALUES (9,9,9,?)", (b"pending",))
    con.commit()

    with pytest.raises(publish.Unpublishable, match="uncheckpointed"):
        publish.publish([src], dest)
    con.close()


def test_a_missing_source_is_refused(tmp_path, dest):
    with pytest.raises(publish.Unpublishable, match="does not exist"):
        publish.publish([tmp_path / "nope.mbtiles"], dest)


def test_a_missing_destination_is_refused(tmp_path):
    src = make_mbtiles(tmp_path / "work.mbtiles")
    with pytest.raises(publish.Unpublishable, match="not a directory"):
        publish.publish([src], tmp_path / "nowhere")


def test_publishing_a_file_already_in_the_destination_is_refused(dest):
    src = make_mbtiles(dest / "fi-veneilykartat-2026-06-21.mbtiles")
    with pytest.raises(publish.Unpublishable, match="already in the destination"):
        publish.publish([src], dest)
    assert src.exists()


# -- retirement --------------------------------------------------------------

def test_a_superseded_edition_is_retired(tmp_path, dest):
    stale = make_mbtiles(dest / "fi-veneilykartat-2026-05-30.mbtiles",
                         dict(META, source_updated="2026-05-30"))
    other = make_mbtiles(dest / "fi-satamakartat-2026-06-29.mbtiles",
                         dict(META, wmts_layer="Satamakartat", source_updated="2026-06-29"))
    src = make_mbtiles(tmp_path / "work.processed.mbtiles")

    publish.publish([src], dest)

    assert not stale.exists()
    assert other.exists(), "only the same layer's older edition may be retired"
    assert (dest / "fi-veneilykartat-2026-06-21.mbtiles").exists()


def test_two_editions_of_one_layer_in_a_run_are_refused(tmp_path, dest):
    """Publishing both would retire each as the other's predecessor, and the
    layer would disappear from the served directory."""
    survivor = make_mbtiles(dest / "fi-veneilykartat-2026-01-01.mbtiles",
                            dict(META, source_updated="2026-01-01"))
    a = make_mbtiles(tmp_path / "a.mbtiles", dict(META, source_updated="2026-06-21"))
    b = make_mbtiles(tmp_path / "b.mbtiles", dict(META, source_updated="2026-07-19"))

    with pytest.raises(publish.Unpublishable, match="a run publishes one edition"):
        publish.publish([a, b], dest)

    assert charts_in(dest) == [survivor.name]


def test_every_traficom_layer_gets_a_name_of_its_own():
    """Two layers sharing a slug would fight over one filename, and each run
    would retire the other's chart. The erikoiskartat companions are genitive
    ('Rannikkokarttojen'), which is what keeps them apart from the base layers."""
    layers = ["Merikarttasarjat public", "Rannikkokartat public", "Satamakartat",
              "Veneilykartat public", "Yleiskartat 250k public",
              "Rannikkokarttojen erikoiskartat", "Veneilykarttojen erikoiskartat",
              "Satamakarttojen erikoiskartat"]
    prefixes = [publish.layer_prefix({"wmts_layer": l, "source_updated": "2026-06-29"})
                for l in layers]
    assert len(set(prefixes)) == len(layers), sorted(prefixes)


def test_an_erikoiskartat_companion_does_not_retire_its_base_layer(tmp_path, dest):
    base = make_mbtiles(dest / "fi-rannikkokartat-2026-06-29.mbtiles",
                        dict(META, wmts_layer="Rannikkokartat public",
                             source_updated="2026-06-29"))
    src = make_mbtiles(tmp_path / "erikois.mbtiles",
                       dict(META, wmts_layer="Rannikkokarttojen erikoiskartat",
                            source_updated="2026-07-15"))

    publish.publish([src], dest)

    assert base.exists(), "the base layer's chart must survive"
    assert (dest / "fi-rannikkokarttojen-2026-07-15.mbtiles").exists()


def test_a_newer_edition_is_not_retired_by_republishing_an_older_one(tmp_path, dest):
    newer = make_mbtiles(dest / "fi-veneilykartat-2026-07-15.mbtiles",
                         dict(META, source_updated="2026-07-15"))
    src = make_mbtiles(tmp_path / "older.mbtiles")

    publish.publish([src], dest)

    assert newer.exists(), "retirement is by edition, not by 'anything else'"


def test_files_that_are_not_this_tool_s_names_are_never_retired(tmp_path, dest):
    variant = make_mbtiles(dest / "fi-veneilykartat-2026-05-30.downscaled.mbtiles",
                           dict(META, source_updated="2026-05-30"))
    src = make_mbtiles(tmp_path / "work.mbtiles")

    publish.publish([src], dest)

    assert variant.exists()


def test_republishing_the_same_edition_keeps_the_file(tmp_path, dest):
    src = make_mbtiles(tmp_path / "work.processed.mbtiles")
    publish.publish([src], dest)

    src2 = make_mbtiles(tmp_path / "work2.processed.mbtiles", payload=b"redone")
    publish.publish([src2], dest)

    target = dest / "fi-veneilykartat-2026-06-21.mbtiles"
    assert target.exists()
    assert sha256(target) == sha256(src2), "the rebuilt bytes must win"


# -- failure leaves the served set alone -------------------------------------

def test_a_corrupted_staging_copy_leaves_the_published_set_untouched(tmp_path, dest, monkeypatch):
    stale = make_mbtiles(dest / "fi-veneilykartat-2026-05-30.mbtiles",
                         dict(META, source_updated="2026-05-30"))
    stale_digest = sha256(stale)
    src = make_mbtiles(tmp_path / "work.processed.mbtiles")

    real = publish.stage

    def truncating(source, staged):
        real(source, staged)
        staged.write_bytes(staged.read_bytes()[:32])
        return hashlib.sha256(source.read_bytes()).hexdigest()

    monkeypatch.setattr(publish, "stage", truncating)

    with pytest.raises(publish.Unpublishable, match="does not match its source"):
        publish.publish([src], dest)

    assert stale.exists() and sha256(stale) == stale_digest
    assert charts_in(dest) == [stale.name]
    assert list((dest / publish.STAGING).iterdir()) == []


def test_one_bad_file_does_not_publish_the_good_ones_either(tmp_path, dest):
    """A run either publishes its whole set or none of it: a manifest that
    describes a half-updated directory is worse than no new manifest."""
    good = make_mbtiles(tmp_path / "good.processed.mbtiles")
    bad = make_mbtiles(tmp_path / "bad.processed.mbtiles",
                       dict(META, wmts_layer="Satamakartat", source_updated="nope"))

    with pytest.raises(publish.Unpublishable):
        publish.publish([good, bad], dest)

    assert charts_in(dest) == []
    assert not (dest / publish.MANIFEST).exists()


def test_a_failure_while_staging_the_second_set_publishes_neither(tmp_path, dest, monkeypatch):
    """The multi-file rollback: the first set stages fine, the second dies."""
    a = make_mbtiles(tmp_path / "a.mbtiles")
    b = make_mbtiles(tmp_path / "b.mbtiles",
                     dict(META, wmts_layer="Satamakartat", source_updated="2026-06-29"))
    real = publish.stage
    calls = []

    def failing(source, staged):
        calls.append(source)
        if len(calls) == 2:
            raise OSError(28, "No space left on device")
        return real(source, staged)

    monkeypatch.setattr(publish, "stage", failing)

    with pytest.raises(OSError):
        publish.publish([a, b], dest)

    assert charts_in(dest) == []
    assert list((dest / publish.STAGING).iterdir()) == []


def test_a_failure_after_the_first_rename_is_reported_as_partial(tmp_path, dest, monkeypatch):
    a = make_mbtiles(tmp_path / "a.mbtiles")
    b = make_mbtiles(tmp_path / "b.mbtiles",
                     dict(META, wmts_layer="Satamakartat", source_updated="2026-06-29"))
    real = os.replace
    calls = []

    def failing(src, dst):
        calls.append(dst)
        if len(calls) == 2:
            raise OSError(5, "I/O error")
        return real(src, dst)

    monkeypatch.setattr(publish.os, "replace", failing)

    with pytest.raises(publish.PartiallyPublished, match="1 of 2"):
        publish.publish([a, b], dest)


# -- the served directory stays clean ----------------------------------------

def test_reading_a_wal_mode_chart_leaves_no_sidecars_in_the_served_directory(tmp_path, dest):
    """Opening a WAL-mode SQLite file creates -wal/-shm beside it, even
    read-only. The destination is a directory a web server exposes."""
    stale = make_mbtiles(dest / "fi-satamakartat-2026-06-29.mbtiles",
                         dict(META, wmts_layer="Satamakartat", source_updated="2026-06-29"))
    con = sqlite3.connect(stale)
    con.execute("PRAGMA journal_mode=WAL")
    con.commit()
    con.close()
    for sidecar in dest.glob("*-wal"):
        sidecar.unlink()
    for sidecar in dest.glob("*-shm"):
        sidecar.unlink()

    src = make_mbtiles(tmp_path / "work.processed.mbtiles")
    publish.publish([src], dest)

    assert not list(dest.glob("*-wal")) and not list(dest.glob("*-shm"))


def test_debris_from_a_killed_run_is_swept_by_the_next_one(tmp_path, dest):
    """A SIGKILL leaves a staged copy of up to 4 GB behind, and nothing else
    would ever remove it."""
    staging = dest / publish.STAGING
    staging.mkdir()
    debris = staging / "fi-rannikkokartat-2026-01-01.mbtiles"
    debris.write_bytes(b"half a chart")

    src = make_mbtiles(tmp_path / "work.processed.mbtiles")
    result = publish.publish([src], dest)

    assert not debris.exists()
    assert [p.name for p in result["swept"]] == [debris.name]


def test_staging_happens_outside_the_files_the_web_server_lists(tmp_path, dest):
    src = make_mbtiles(tmp_path / "work.processed.mbtiles")
    seen = []
    real = publish.stage

    def watching(source, staged):
        seen.append(staged)
        return real(source, staged)

    publish.stage, saved = watching, publish.stage
    try:
        publish.publish([src], dest)
    finally:
        publish.stage = saved

    assert seen and all(p.parent == dest / publish.STAGING for p in seen)


def test_a_second_run_will_not_start_while_one_holds_the_destination(tmp_path, dest):
    src = make_mbtiles(tmp_path / "work.processed.mbtiles")
    with publish.exclusive(dest):
        with pytest.raises(publish.Unpublishable, match="refusing to run concurrently"):
            publish.publish([src], dest)


# -- the manifest describes the directory ------------------------------------

def test_manifest_carries_every_published_chart_not_just_this_run(tmp_path, dest):
    publish.publish([make_mbtiles(tmp_path / "a.mbtiles")], dest)
    harbour = dict(META, wmts_layer="Satamakartat", source_updated="2026-06-29")
    publish.publish([make_mbtiles(tmp_path / "b.mbtiles", harbour)], dest)

    entries = {e["filename"]: e for e in manifest_of(dest)["charts"]}
    assert set(entries) == {"fi-veneilykartat-2026-06-21.mbtiles",
                            "fi-satamakartat-2026-06-29.mbtiles"}
    for name, entry in entries.items():
        assert entry["sha256"] == sha256(dest / name)
        assert entry["bytes"] == (dest / name).stat().st_size


def test_a_retired_edition_is_not_listed_in_the_manifest(tmp_path, dest):
    make_mbtiles(dest / "fi-veneilykartat-2026-05-30.mbtiles",
                 dict(META, source_updated="2026-05-30"))
    publish.publish([make_mbtiles(tmp_path / "work.mbtiles")], dest)

    assert [e["filename"] for e in manifest_of(dest)["charts"]] == [
        "fi-veneilykartat-2026-06-21.mbtiles"]


def test_an_unreadable_chart_in_the_destination_still_yields_a_manifest(tmp_path, dest):
    """A partial file from before this tool existed is exactly what the manifest
    is for. Aborting on it would publish the charts and then leave the previous
    run's manifest describing files that are gone."""
    junk = dest / "fi-enc-2026-01-01.mbtiles"
    junk.write_bytes(b"not a database")
    src = make_mbtiles(tmp_path / "work.processed.mbtiles")

    publish.publish([src], dest)

    entries = {e["filename"]: e for e in manifest_of(dest)["charts"]}
    assert entries[junk.name]["readable"] is False
    assert entries[junk.name]["sha256"] == sha256(junk)
    assert "readable" not in entries["fi-veneilykartat-2026-06-21.mbtiles"]


def test_the_manifest_declares_its_own_format_version(tmp_path, dest):
    """A consumer that meets a version it does not know should refuse rather
    than guess, which it can only do if the number is there from the start."""
    publish.publish([make_mbtiles(tmp_path / "a.mbtiles")], dest)
    assert manifest_of(dest)["schema"] == publish.SCHEMA


def test_each_chart_carries_the_layer_key_that_survives_a_new_edition(tmp_path, dest):
    """`filename` changes every edition, so it is not something a consumer can
    track a chart by. Without this the only handle is parsing the filename."""
    publish.publish([make_mbtiles(tmp_path / "a.mbtiles")], dest)
    (first,) = manifest_of(dest)["charts"]

    newer = make_mbtiles(tmp_path / "b.mbtiles", dict(META, source_updated="2026-07-19"))
    publish.publish([newer], dest)
    (second,) = manifest_of(dest)["charts"]

    assert first["layer"] == "fi-veneilykartat"
    assert second["layer"] == first["layer"]
    assert second["filename"] != first["filename"]


def test_the_layer_key_prefixes_the_filename(tmp_path, dest):
    harbour = dict(META, wmts_layer="Yleiskartat 250k public", source_updated="2026-06-02")
    publish.publish([make_mbtiles(tmp_path / "a.mbtiles", harbour)], dest)

    (entry,) = manifest_of(dest)["charts"]
    assert entry["layer"] == "fi-yleiskartat250k"
    assert entry["filename"].startswith(entry["layer"] + "-")


def test_an_unreadable_chart_has_no_layer_to_report(tmp_path, dest):
    junk = dest / "fi-enc-2026-01-01.mbtiles"
    junk.write_bytes(b"not a database")
    publish.publish([make_mbtiles(tmp_path / "a.mbtiles")], dest)

    entries = {e["filename"]: e for e in manifest_of(dest)["charts"]}
    assert entries[junk.name]["layer"] is None


def test_generated_is_a_utc_instant_not_a_bare_local_date(tmp_path, dest):
    """Two publishes on one day are routine -- a scheduled run and a manual
    retry -- and a consumer polling for changes needs to tell them apart."""
    publish.publish([make_mbtiles(tmp_path / "a.mbtiles")], dest)

    generated = manifest_of(dest)["generated"]
    assert generated.endswith("+00:00")
    assert len(generated) == len("2026-08-08T12:00:00+00:00")
