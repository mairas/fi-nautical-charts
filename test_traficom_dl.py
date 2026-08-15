"""Tests for the downloader's refresh pass.

Refresh is the step a monthly run spends most of its hours in, and the one whose
result is hardest to read: it reports counts, not charts, and a wrong count looks
exactly like a right one. What is pinned here is the classification -- which
answer from the server means which thing -- because everything downstream is
decided by it, including whether the archive is allowed to remember that it was
checked at all.
"""

import argparse
import sqlite3
import types

import pytest

import traficom_dl


def make_archive(path, tiles, downloaded="2026-08-10"):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE metadata (name TEXT PRIMARY KEY, value TEXT)")
    con.execute("CREATE TABLE tiles (zoom_level INT, tile_column INT, "
                "tile_row INT, tile_data BLOB)")
    # the archive's own index. Without it INSERT OR REPLACE has nothing to
    # conflict on, so a re-stored tile is appended beside the old one.
    con.execute("CREATE UNIQUE INDEX tile_index ON tiles "
                "(zoom_level, tile_column, tile_row)")
    con.execute("CREATE TABLE _errors (z INT, x INT, y INT, "
                "PRIMARY KEY (z, x, y))")
    con.execute("INSERT INTO metadata VALUES ('downloaded', ?)", (downloaded,))
    con.execute("INSERT INTO metadata VALUES ('wmts_layer', 'Yleiskartat 250k public')")
    for z, x, y in tiles:
        con.execute("INSERT INTO tiles VALUES (?,?,?,?)",
                    (z, x, (1 << z) - 1 - y, b"tile"))
    con.commit()
    return con


@pytest.fixture
def archive(tmp_path):
    return tmp_path / "fi-yleiskartat250k-2026-06-02.mbtiles"


def run_refresh(monkeypatch, con, answers, z=8):
    """Drive one zoom of refresh with the server's answers decided by the test.

    `fetch` is the module's one call site for asking the server anything, and it
    is the only seam stubbed here: which tiles get asked about is what refresh
    decides, so a test that supplied the candidates too could not see it decide
    wrongly. Nothing reaches the network, and the loop under test is the real
    one.
    """
    monkeypatch.setattr(traficom_dl, "fetch",
                        lambda src, z, x, y, ims=None: (x, y, answers[(x, y)], b"new"))
    args = argparse.Namespace(mode="full", full_until=None, concurrency=1)
    src = traficom_dl.parse_source("wmts", "Yleiskartat 250k public")
    # No limits: a refresh reads them only to clamp a --bbox, and asking for the
    # real ones fetches GetCapabilities over the network from every test.
    return traficom_dl.refresh(con, args, src, limits=None, bbox=None, zooms=[z])


def test_a_refresh_asks_only_about_tiles_the_archive_holds(monkeypatch, archive):
    """Refresh re-checks coverage. New chart areas, and new zoom levels, are a
    fresh download's business.

    The download's descent asks for the children of every stored tile, which is
    one level past whatever the archive has. Traficom answers past a layer's
    real detail with near-blank tiles rather than 404, and those pass this
    module's blank test while failing the strip's -- so a refresh built on the
    descent grows a level that empties the whole set when it is processed.
    """
    con = make_archive(archive, [(8, 147, 73), (8, 148, 73)])
    asked = []

    def fetch(src, z, x, y, ims=None):
        asked.append((z, x, y))
        return (x, y, "notmodified", b"")

    monkeypatch.setattr(traficom_dl, "fetch", fetch)
    src = traficom_dl.parse_source("wmts", "Yleiskartat 250k public")
    args = argparse.Namespace(mode="full", full_until=None, concurrency=1)
    traficom_dl.refresh(con, args, src, limits=None, bbox=None, zooms=[8, 9])

    assert sorted(asked) == [(8, 147, 73), (8, 148, 73)]


def test_an_unchanged_tile_is_not_a_failure(monkeypatch, archive):
    """304 is the answer refresh exists to provoke: it asks with
    If-Modified-Since precisely so that a tile which has not been reseded costs
    a header and no body. Counting that as a failure makes a working sweep
    indistinguishable from a broken one -- and every tile in a quiet month
    answers this way."""
    con = make_archive(archive, [(8, 147, 73), (8, 148, 73)])
    counts = run_refresh(monkeypatch, con,
                         {(147, 73): "notmodified", (148, 73): "notmodified"})

    assert counts["checked"] == 2
    assert counts["errors"] == 0
    assert counts["updated"] == 0 and counts["removed"] == 0


def test_an_unchanged_tile_is_not_recorded_for_repair(monkeypatch, archive):
    """`_errors` is the list `--repair` re-fetches. A tile that answered
    correctly does not belong on it, and 800k that do turn a repair into a
    fresh download."""
    con = make_archive(archive, [(8, 147, 73)])
    run_refresh(monkeypatch, con, {(147, 73): "notmodified"})

    con = sqlite3.connect(archive)
    assert con.execute("SELECT count(*) FROM _errors").fetchone()[0] == 0


def test_an_unchanged_tile_is_left_in_the_archive(monkeypatch, archive):
    """Not modified means keep, and it must not be confused with the empty
    answer that means the server has withdrawn the tile."""
    con = make_archive(archive, [(8, 147, 73)])
    run_refresh(monkeypatch, con, {(147, 73): "notmodified"})

    con = sqlite3.connect(archive)
    assert con.execute("SELECT count(*) FROM tiles").fetchone()[0] == 1


def test_a_sweep_that_found_nothing_new_still_advances_the_watermark(
        monkeypatch, archive):
    """The date is the next run's If-Modified-Since. Refusing to move it after a
    sweep in which nothing failed leaves every later run asking the same stale
    question and getting the same answer, for as long as the archive lives."""
    con = make_archive(archive, [(8, 147, 73)], downloaded="2026-08-10")
    run_refresh(monkeypatch, con, {(147, 73): "notmodified"})

    con = sqlite3.connect(archive)
    assert traficom_dl.get_meta(con, "downloaded") != "2026-08-10"


def test_a_genuine_failure_is_still_a_failure(monkeypatch, archive):
    """The counting change must not swallow the case the counter was written
    for: a tile whose new edition did not transfer stays on the repair list and
    holds the watermark back."""
    con = make_archive(archive, [(8, 147, 73)], downloaded="2026-08-10")
    counts = run_refresh(monkeypatch, con, {(147, 73): "err"})

    assert counts["errors"] == 1
    con = sqlite3.connect(archive)
    assert con.execute("SELECT count(*) FROM _errors").fetchone()[0] == 1
    assert traficom_dl.get_meta(con, "downloaded") == "2026-08-10"


def test_a_reseded_tile_is_stored_and_counted(monkeypatch, archive):
    con = make_archive(archive, [(8, 147, 73)])
    counts = run_refresh(monkeypatch, con, {(147, 73): "ok"})

    assert counts["updated"] == 1 and counts["errors"] == 0
    con = sqlite3.connect(archive)
    assert con.execute("SELECT tile_data FROM tiles").fetchone()[0] == b"new"


def test_a_withdrawn_tile_is_removed(monkeypatch, archive):
    con = make_archive(archive, [(8, 147, 73)])
    counts = run_refresh(monkeypatch, con, {(147, 73): "empty"})

    assert counts["removed"] == 1 and counts["errors"] == 0
    con = sqlite3.connect(archive)
    assert con.execute("SELECT count(*) FROM tiles").fetchone()[0] == 0


def test_a_bbox_still_narrows_a_refresh(monkeypatch, archive):
    """`--bbox` selects an area to re-check. The candidates come from the
    archive now rather than from a generated rectangle, and the clamp has to
    survive that move: a flag that selects nothing and says nothing is worse
    than one that is refused."""
    con = make_archive(archive, [(8, 147, 73), (8, 200, 73)])
    asked = []

    def fetch(src, z, x, y, ims=None):
        asked.append((x, y))
        return (x, y, "notmodified", b"")

    monkeypatch.setattr(traficom_dl, "fetch", fetch)
    src = traficom_dl.parse_source("wmts", "Yleiskartat 250k public")
    args = argparse.Namespace(mode="full", full_until=None, concurrency=1)
    # exactly the ground under tile (147, 73)
    bbox = (traficom_dl.x2lon(147, 8), traficom_dl.y2lat(74, 8),
            traficom_dl.x2lon(148, 8), traficom_dl.y2lat(73, 8))
    traficom_dl.refresh(con, args, src, limits={8: (0, 255, 0, 255)},
                        bbox=bbox, zooms=[8])

    assert asked == [(147, 73)]
