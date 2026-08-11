"""Tests for rebuilding the pyramid.

The one that matters is how far down the rebuild reaches. `strip-nodata` hands
over a file with a single level of tiles, so the levels to regenerate cannot be
read off the tile table -- and a wrong answer there is silent: the run reports
success and the chart ships with one zoom level.
"""

import io
import sqlite3
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

import downscale as ds


def tile(colour=(200, 40, 40, 255)) -> bytes:
    a = np.zeros((256, 256, 4), np.uint8)
    a[:, :] = colour
    buf = io.BytesIO()
    Image.fromarray(a, "RGBA").save(buf, format="PNG")
    return buf.getvalue()


def build(path: Path, levels: dict[int, list[tuple[int, int]]],
          meta: dict[str, str] | None = None) -> Path:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE metadata (name TEXT PRIMARY KEY, value TEXT)")
    con.execute("CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, "
                "tile_row INTEGER, tile_data BLOB)")
    con.execute("CREATE UNIQUE INDEX ti ON tiles (zoom_level, tile_column, tile_row)")
    for k, v in (meta or {}).items():
        con.execute("INSERT INTO metadata VALUES (?,?)", (k, v))
    for z, cells in levels.items():
        for x, y in cells:
            con.execute("INSERT INTO tiles VALUES (?,?,?,?)",
                        (z, x, (1 << z) - 1 - y, tile()))
    con.commit()
    con.close()
    return path


def test_the_floor_comes_from_the_metadata_when_the_tiles_cannot_say():
    """What a stripped file looks like: one level of tiles, minzoom describing
    the chart it is about to become."""
    assert ds.floor_zoom([13], {"minzoom": "5", "maxzoom": "13"}) == 5


def test_a_file_that_kept_its_levels_still_uses_the_lowest_present():
    assert ds.floor_zoom([5, 6, 7], {"minzoom": "5"}) == 5


def test_a_metadata_floor_above_the_tiles_does_not_raise_it():
    """Whichever is lower wins, so a stale minzoom can never make the rebuild
    cover less than it did before."""
    assert ds.floor_zoom([4, 5, 6], {"minzoom": "6"}) == 4


@pytest.mark.parametrize("meta", [{}, {"minzoom": ""}, {"minzoom": "shallow"}])
def test_metadata_that_says_nothing_usable_falls_back_to_the_tiles(meta):
    assert ds.floor_zoom([9, 10], meta) == 9


def test_the_pending_floor_wins_over_a_truthful_minzoom():
    """A stripped file's minzoom describes what it holds -- one level -- so
    reading that would rebuild nothing. The floor it wants is recorded
    separately."""
    assert ds.floor_zoom([13], {"pyramid_pending": "5", "minzoom": "13"}) == 5


def test_the_rebuilt_file_no_longer_says_it_is_waiting(tmp_path):
    """publish refuses anything carrying the marker, so downscale has to clear
    it or nothing built this way could ever be published."""
    src = build(tmp_path / "pending.mbtiles",
                {12: [(x, y) for x in range(2048, 2052) for y in range(1200, 1204)]},
                {"minzoom": "12", "maxzoom": "12", "pyramid_pending": "9"})
    out = tmp_path / "rebuilt.mbtiles"
    ds.build(src, out, source_zoom=None, min_zoom=None, jobs=1)

    con = sqlite3.connect(f"file:{out}?mode=ro", uri=True)
    meta = dict(con.execute("SELECT name, value FROM metadata"))
    levels = sorted(z for (z,) in con.execute("SELECT DISTINCT zoom_level FROM tiles"))
    con.close()
    assert "pyramid_pending" not in meta, "the rebuilt file still reads as unfinished"
    assert levels == [9, 10, 11, 12] and meta["minzoom"] == "9"


def test_a_stripped_file_is_rebuilt_all_the_way_down(tmp_path):
    """End to end: one level in, the whole pyramid out."""
    src = build(tmp_path / "one-level.mbtiles",
                {12: [(x, y) for x in range(2048, 2052) for y in range(1200, 1204)]},
                {"minzoom": "9", "maxzoom": "12"})
    out = tmp_path / "rebuilt.mbtiles"
    ds.build(src, out, source_zoom=None, min_zoom=None, jobs=1)

    con = sqlite3.connect(f"file:{out}?mode=ro", uri=True)
    levels = dict(con.execute("SELECT zoom_level, COUNT(*) FROM tiles GROUP BY zoom_level"))
    meta = dict(con.execute("SELECT name, value FROM metadata"))
    con.close()
    assert sorted(levels) == [9, 10, 11, 12], f"rebuilt only {sorted(levels)}"
    assert levels[11] == 4 and levels[10] == 1
    assert meta["minzoom"] == "9" and meta["maxzoom"] == "12"
