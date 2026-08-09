"""Tests for stripping the off-sheet fill.

The hazard this guards is not failing to remove the fill; it is removing chart
ink that happens to be thick. Traficom sets place names in heavy serif capitals,
and at native zoom their strokes survive the erosion that is supposed to tell
solid fill from line work. So most of what is asserted here is what must
*survive*.
"""

import io
import sqlite3
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

import strip_nodata as sn

Z = 10
PAPER = (236, 228, 205, 255)
SEA = (198, 226, 240, 255)


def tile(kind: str) -> bytes:
    a = np.zeros((256, 256, 4), np.uint8)
    if kind == "fill":
        a[:, :] = (0, 0, 0, 255)
    elif kind == "blank":
        a[:, :] = (255, 255, 255, 255)
    else:
        a[:, :] = PAPER
        a[10:60, :] = SEA
        a[100:103, 20:230] = (0, 0, 0, 255)      # a thin ruled line: line work
        if kind == "content+type":
            a[150:200, 40:90] = (0, 0, 0, 255)   # a heavy letterform
        if kind == "half-fill":
            # Off-sheet to the left, running off the tile edge. Chart ink is not
            # drawn over it, so the line restarts where the sheet does -- abutting
            # the fill, which is the arrangement a real sheet edge produces.
            a[:, :90] = (0, 0, 0, 255)
            a[100:103, :90] = (0, 0, 0, 255)
    buf = io.BytesIO()
    Image.fromarray(a, "RGBA").save(buf, format="PNG")
    return buf.getvalue()


def build(path: Path, layout: dict[tuple[int, int], str]) -> Path:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE metadata (name TEXT PRIMARY KEY, value TEXT)")
    con.execute("CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, "
                "tile_row INTEGER, tile_data BLOB)")
    con.execute("CREATE UNIQUE INDEX ti ON tiles (zoom_level, tile_column, tile_row)")
    con.execute("INSERT INTO metadata VALUES ('name','test')")
    for (x, y), kind in layout.items():
        con.execute("INSERT INTO tiles VALUES (?,?,?,?)",
                    (Z, x, (1 << Z) - 1 - y, tile(kind)))
    con.commit()
    con.close()
    return path


def read(path: Path, x: int, y: int):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    r = con.execute("SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? "
                    "AND tile_row=?", (Z, x, (1 << Z) - 1 - y)).fetchone()
    con.close()
    if r is None:
        return None
    return np.asarray(Image.open(io.BytesIO(r[0])).convert("RGBA"))


def black_px(a) -> int:
    return int(((a[..., :3].max(axis=2) == 0) & (a[..., 3] == 255)).sum())


# A strip of chart four tiles wide. Column 0 is off the last sheet entirely,
# column 1 straddles the sheet edge, columns 2 and 3 are interior chart.
LAYOUT = {
    (0, 0): "fill",         (1, 0): "half-fill",  (2, 0): "content",      (3, 0): "content",
    (0, 1): "fill",         (1, 1): "half-fill",  (2, 1): "content+type", (3, 1): "content",
    (0, 2): "fill",         (1, 2): "half-fill",  (2, 2): "content",      (3, 2): "content+type",
}


@pytest.fixture
def stripped(tmp_path):
    src = build(tmp_path / "src.mbtiles", LAYOUT)
    out = tmp_path / "out.mbtiles"
    sn.run(src, out, radius=sn.RADIUS, jobs=1, offeez=False)
    return out


def test_heavy_type_on_an_interior_tile_survives(stripped):
    """The regression. A 50x50 letterform survives a radius-4 erosion, so the
    local shape test calls it fill; nothing outside can reach that tile."""
    a = read(stripped, 2, 1)
    assert a is not None
    assert black_px(a) == black_px(read_source(2, 1)), "interior ink was altered"


def test_a_second_interior_tile_with_type_survives(stripped):
    a = read(stripped, 3, 2)
    assert black_px(a) == black_px(read_source(3, 2))


def test_plain_interior_chart_tiles_are_untouched(stripped):
    for xy in [(2, 0), (3, 0), (3, 1), (2, 2)]:
        a = read(stripped, *xy)
        assert black_px(a) == black_px(read_source(*xy)), f"tile {xy} altered"


def test_the_fill_beyond_the_last_sheet_is_removed(stripped):
    """Wholly off-sheet tiles carry nothing to keep."""
    for y in (0, 1, 2):
        a = read(stripped, 0, y)
        assert a is None or a[..., 3].max() == 0, "off-sheet tile still has opaque pixels"


def test_the_fill_on_a_straddling_tile_is_removed_but_its_chart_is_not(stripped):
    a = read(stripped, 1, 1)
    assert a is not None, "a tile carrying chart must not be dropped"
    assert (a[:, :90, 3] == 0).all(), "off-sheet half should be transparent"
    assert (a[10:60, 120:200, :3] == np.array(SEA[:3])).all(), "chart half was damaged"


def test_line_work_abutting_the_fill_survives_on_a_straddling_tile(stripped):
    """Reconstruction follows connectivity, so ink touching the fill is the case
    that can bleed. At a sheet edge that adjacency is the normal arrangement."""
    a = read(stripped, 1, 1)
    kept = int((a[100:103, 95:230, 3] == 255).sum())
    assert kept > 300, f"line work was stripped ({kept} of 405 px kept)"


def test_a_chart_with_no_fill_at_all_is_left_completely_alone(tmp_path):
    """Four of the five layers are mostly interior. A run over one should not
    rewrite a single tile."""
    layout = {(x, y): ("content+type" if (x + y) % 2 else "content")
              for x in range(3) for y in range(3)}
    src = build(tmp_path / "nofill.mbtiles", layout)
    out = tmp_path / "nofill-out.mbtiles"
    sn.run(src, out, radius=sn.RADIUS, jobs=1, offeez=False)
    for (x, y) in layout:
        a = read(out, x, y)
        b = np.asarray(Image.open(io.BytesIO(tile(layout[(x, y)]))).convert("RGBA"))
        assert a is not None and np.array_equal(a, b), f"tile {x},{y} was rewritten"


def test_the_stamp_records_that_only_edges_were_examined(stripped):
    con = sqlite3.connect(f"file:{stripped}?mode=ro", uri=True)
    stamp = dict(con.execute("SELECT name, value FROM metadata"))["nodata_stripped"]
    con.close()
    assert "edge" in stamp, stamp


_SOURCE = {}


def read_source(x: int, y: int):
    """The unmodified tile, for comparison."""
    key = LAYOUT[(x, y)]
    if key not in _SOURCE:
        _SOURCE[key] = np.asarray(Image.open(io.BytesIO(tile(key))).convert("RGBA"))
    return _SOURCE[key]
