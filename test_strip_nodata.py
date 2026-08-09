"""Tests for stripping the off-sheet fill.

The hazard this guards is not failing to remove the fill; it is removing chart
ink that happens to be thick. Traficom sets place names in heavy serif capitals,
and at native zoom their strokes survive the erosion that is supposed to tell
solid fill from line work. So most of what is asserted here is what must
*survive*.
"""

import io
import sqlite3


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
        if kind == "fill+margin":
            # Off-sheet, but the fetched tile ran past the served extent, so a
            # strip of it came back transparent. Measured against all 65536
            # pixels this reads as 94.5% black; against its opaque pixels, 100%.
            a[:, :] = (0, 0, 0, 255)
            a[242:, :] = (0, 0, 0, 0)
        if kind == "fill+speck":
            # Off-sheet apart from a few pixels of a label that crossed the cut.
            a[:, :] = (0, 0, 0, 255)
            a[4:12, 4:12] = (200, 60, 130, 255)
        if kind == "fill+paper":
            # A sheet corner: half off-sheet, half blank paper, no ink at all.
            a[:, :] = (255, 255, 255, 255)
            a[:, :180] = (0, 0, 0, 255)
        if kind == "half-fill+type":
            # The sheet edge, with a place name set clear of it. Strokes 10px
            # wide: measured against real Traficom capitals, whose distance
            # transform runs 5-8, i.e. 10-16px across.
            a[:, :90] = (0, 0, 0, 255)
            a[100:103, :90] = (0, 0, 0, 255)
            a[150:210, 140:150] = (0, 0, 0, 255)   # stem
            a[150:210, 190:200] = (0, 0, 0, 255)   # stem
            a[175:185, 140:200] = (0, 0, 0, 255)   # crossbar
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


@pytest.mark.parametrize("kind", ["fill+margin", "fill+speck", "fill+paper"])
def test_off_sheet_tiles_that_are_not_perfectly_uniform_are_still_stripped(tmp_path, kind):
    """Real off-sheet tiles are rarely a clean 256x256 of black. They come with
    a transparent margin where the fetch ran past the extent, with a few pixels
    of a label that crossed the sheet cut, or as a corner that is half blank
    paper. Each of those used to fall into a class that was exempt."""
    layout = dict(LAYOUT)
    layout[(0, 1)] = kind
    src = build(tmp_path / "src.mbtiles", layout)
    out = tmp_path / "out.mbtiles"
    sn.run(src, out, radius=sn.RADIUS, jobs=1, offeez=False)

    a = read(out, 0, 1)
    left = 0 if a is None else black_px(a)
    assert left == 0, f"{left} px of off-sheet fill survived on a {kind} tile"


def test_heavy_type_on_a_straddling_tile_survives(tmp_path):
    """Being examined is not being stripped. A place name near the sheet edge
    is thick enough to seed the erosion exactly as fill does, so on the one ring
    of tiles the walk does hand to the local test, the original defect is still
    reachable -- and 205 tiles at z13 of yleiskartat are in that ring."""
    layout = dict(LAYOUT)
    for y in (0, 1, 2):
        layout[(1, y)] = "half-fill+type"
    src = build(tmp_path / "src.mbtiles", layout)
    out = tmp_path / "out.mbtiles"
    sn.run(src, out, radius=sn.RADIUS, jobs=1, offeez=False)

    a = read(out, 1, 1)
    assert a is not None
    letter = int((a[150:210, 140:200, 3] == 255).sum())
    expected = int((read_source_kind("half-fill+type")[150:210, 140:200, 3] == 255).sum())
    assert letter == expected, f"the letterform lost {expected - letter} of {expected} px"
    assert (a[:, :90, 3] == 0).all(), "and the fill must still go"


def test_a_chart_tile_meeting_the_fill_only_at_a_corner_is_examined(tmp_path):
    """A sheet edge crossing the grid diagonally leaves chart tiles whose only
    contact with the off-sheet region is a corner."""
    layout = {(0, 0): "fill", (1, 0): "content", (2, 0): "content",
              (0, 1): "content", (1, 1): "half-fill", (2, 1): "content",
              (0, 2): "content", (1, 2): "content", (2, 2): "content"}
    src = build(tmp_path / "src.mbtiles", layout)
    out = tmp_path / "out.mbtiles"
    sn.run(src, out, radius=sn.RADIUS, jobs=1, offeez=False)

    a = read(out, 1, 1)
    assert a is not None
    assert (a[:, :90, 3] == 0).all(), "diagonal-only neighbour of the fill was skipped"


def test_a_run_that_would_leave_solid_fill_behind_refuses(tmp_path, monkeypatch):
    """The guard that would have caught the previous selection before it shipped:
    the survey has already counted the black on every tile, so a tile that is
    nothing but fill and was not examined is a fact, not an inference."""
    src = build(tmp_path / "src.mbtiles", LAYOUT)
    out = tmp_path / "out.mbtiles"
    monkeypatch.setattr(sn, "edge_tiles", lambda ink, plain, black: (set(), {}))

    with pytest.raises(sn.Leaked, match="would ship"):
        sn.run(src, out, radius=sn.RADIUS, jobs=1, offeez=False)


def test_solid_black_the_walk_cannot_reach_stops_the_run(tmp_path):
    """A tile enclosed by chart that is nothing but black is unexplained: the
    walk says it is not off-sheet, and no chart draws a solid 256x256 square.
    Rather than guess -- strip it and risk eating a legend panel, or keep it and
    ship a black hole -- refuse and name the tile. It does not occur in any of
    the Traficom layers, so this costs nothing until the day it means something."""
    layout = {(x, y): "content" for x in range(3) for y in range(3)}
    layout[(1, 1)] = "fill"
    src = build(tmp_path / "enclosed.mbtiles", layout)

    with pytest.raises(sn.Leaked, match=r"\(1, 1\)"):
        sn.run(src, tmp_path / "enclosed-out.mbtiles",
               radius=sn.RADIUS, jobs=1, offeez=False)


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


def read_source_kind(kind: str):
    return np.asarray(Image.open(io.BytesIO(tile(kind))).convert("RGBA"))
