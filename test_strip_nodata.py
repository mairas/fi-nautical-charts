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
    elif kind == "dim-blank":
        # The south-eastern sheets render blank a step below white, corner fill
        # included, so nothing on them is blank at the default level.
        a[:, :] = (254, 254, 254, 255)
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
        if kind == "corner-fill":
            # a sheet edge crossing the grid diagonally: the fill takes a corner
            # of this tile and continues into the two it shares those edges with
            for r in range(256):
                a[r, :max(0, 150 - r)] = (0, 0, 0, 255)
        if kind == "fill-right":
            a[:, 106:] = (0, 0, 0, 255)
        if kind == "fill-bottom":
            a[106:, :] = (0, 0, 0, 255)
        if kind == "wedge-fill":
            # The sheet edge crossing the tile at a shallow angle, so the fill
            # tapers to a few pixels. Measured on Yleiskartat z9 at the Aland
            # boundary, where the surviving wedge was 8px across.
            for r in range(256):
                a[r, :max(0, 60 - r // 3)] = (0, 0, 0, 255)
        if kind == "open":
            # Water well inside the limit: blank white but for one mark, which
            # is all it takes to be chart. This is what has to survive, and what
            # a tile below it can seed a flood on if direction is not consulted.
            a[:, :] = (255, 255, 255, 255)
            a[4:12, 4:12] = (150, 40, 140, 255)
        if kind == "limit":
            # The outer limit: chart above the line, nothing below it. Both
            # halves are blank white, which is what makes direction the only
            # thing that tells them apart.
            a[:, :] = (255, 255, 255, 255)
            a[128:131, :] = (150, 40, 140, 255)
        if kind == "soft-fill":
            # Off-sheet to the left with an anti-aliased boundary. The source
            # does soften it, whatever the hard-edge assumption said: the
            # measured residue ran to a mean RGB of 2 over 300-odd pixels.
            a[:, :90] = (0, 0, 0, 255)
            for i, v in enumerate((6, 18, 40, 90, 160)):
                a[:, 90 + i] = (v, v, v, 255)
    buf = io.BytesIO()
    Image.fromarray(a, "RGBA").save(buf, format="PNG")
    return buf.getvalue()


def build(path: Path, layout: dict[tuple[int, int], str], z: int = Z) -> Path:
    """Write one zoom's tiles. Called twice on the same path to stack levels."""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS tiles (zoom_level INTEGER, tile_column INTEGER, "
                "tile_row INTEGER, tile_data BLOB)")
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS ti ON tiles "
                "(zoom_level, tile_column, tile_row)")
    con.execute("INSERT OR REPLACE INTO metadata VALUES ('name','test')")
    for (x, y), kind in layout.items():
        con.execute("INSERT INTO tiles VALUES (?,?,?,?)",
                    (z, x, (1 << z) - 1 - y, tile(kind)))
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


def stamp_of(path: Path) -> str:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    r = con.execute("SELECT value FROM metadata WHERE name='nodata_stripped'").fetchone()
    con.close()
    return r[0]


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
    sn.run(src, out, jobs=1, stages=("black-tiles", "black-pixels"))
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
    rewrite an interior tile.

    Interior, not every tile: beyond the last one there is no data, and no data
    is off-sheet by definition, so the outermost ring is at a real edge of the
    chart and the run is right to look at it. What the ring loses is dark within
    a radius of that edge -- which on these layers is open water, and on this
    fixture is the end of a ruled line."""
    layout = {(x, y): ("content+type" if (x + y) % 2 else "content")
              for x in range(5) for y in range(5)}
    src = build(tmp_path / "nofill.mbtiles", layout)
    out = tmp_path / "nofill-out.mbtiles"
    sn.run(src, out, jobs=1, stages=("black-tiles", "black-pixels"))
    for x in range(1, 4):
        for y in range(1, 4):
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
    sn.run(src, out, jobs=1, stages=("black-tiles", "black-pixels"))

    a = read(out, 0, 1)
    left = 0 if a is None else black_px(a)
    assert left == 0, f"{left} px of off-sheet fill survived on a {kind} tile"


def test_a_label_crossing_the_sheet_cut_keeps_its_tile(tmp_path):
    """A whole tile goes only when it is fill and nothing else. Eight pixels
    square of a label that crossed the cut is content, so the tile stays and the
    fill comes off around it -- the pixel flood's job, not the tile drop's."""
    layout = dict(LAYOUT)
    layout[(0, 1)] = "fill+speck"
    src = build(tmp_path / "speck.mbtiles", layout)
    out = tmp_path / "speck-out.mbtiles"
    sn.run(src, out, jobs=1, stages=("black-tiles", "black-pixels"))

    a = read(out, 0, 1)
    assert a is not None, "the tile was dropped although it carried a label"
    kept = int(((a[..., 3] == 255) & (a[..., :3].max(axis=2) > sn.DARK)).sum())
    assert kept, "the label was erased with the fill around it"
    assert black_px(a) == 0, "the fill did not come off"


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
    sn.run(src, out, jobs=1, stages=("black-tiles", "black-pixels"))

    a = read(out, 1, 1)
    assert a is not None
    letter = int((a[150:210, 140:200, 3] == 255).sum())
    expected = int((read_source_kind("half-fill+type")[150:210, 140:200, 3] == 255).sum())
    assert letter == expected, f"the letterform lost {expected - letter} of {expected} px"
    assert (a[:, :90, 3] == 0).all(), "and the fill must still go"


def test_fill_crossing_the_grid_diagonally_is_removed(tmp_path):
    """A sheet edge crossing at an angle takes a corner of a tile and continues
    into the two it shares those edges with. What identifies it as fill is that
    it goes on past the seam, which is a question about the neighbour's pixels
    and not about which cells of the grid the walk reached."""
    layout = {(0, 0): "fill",         (1, 0): "fill-bottom", (2, 0): "content",
              (0, 1): "fill-right",   (1, 1): "corner-fill", (2, 1): "content",
              (0, 2): "content",      (1, 2): "content",     (2, 2): "content"}
    src = build(tmp_path / "src.mbtiles", layout)
    out = tmp_path / "out.mbtiles"
    sn.run(src, out, jobs=1, stages=("black-tiles", "black-pixels"))
    a = read(out, 1, 1)
    assert a is not None, "a tile carrying chart must not be dropped"
    assert (a[0, :100, 3] == 0).all(), "the fill corner survived"
    fresh = np.asarray(Image.open(io.BytesIO(tile("corner-fill"))).convert("RGBA"))
    assert black_px(a[:, 200:]) == black_px(fresh[:, 200:]), "the chart half was damaged"


def test_dark_that_does_not_continue_past_a_seam_is_left_alone(tmp_path):
    """The other half of the same rule, and the one that protects type. A place
    name is dark and can be thick, but it stops inside the tile; nothing about
    which cells the walk reached can make it fill."""
    layout = {(0, 0): "fill",    (1, 0): "content",      (2, 0): "content",
              (0, 1): "fill",    (1, 1): "content+type", (2, 1): "content",
              (0, 2): "fill",    (1, 2): "content",      (2, 2): "content"}
    src = build(tmp_path / "src.mbtiles", layout)
    out = tmp_path / "out.mbtiles"
    sn.run(src, out, jobs=1, stages=("black-tiles", "black-pixels"))
    a = read(out, 1, 1)
    fresh = np.asarray(Image.open(io.BytesIO(tile("content+type"))).convert("RGBA"))
    assert black_px(a) == black_px(fresh), "chart ink was altered"


def test_a_run_that_would_leave_solid_fill_behind_refuses(tmp_path, monkeypatch):
    """The guard that would have caught the previous selection before it shipped:
    the survey has already counted the black on every tile, so a tile that is
    nothing but fill and was not examined is a fact, not an inference."""
    src = build(tmp_path / "src.mbtiles", LAYOUT)
    out = tmp_path / "out.mbtiles"
    monkeypatch.setattr(sn, "edge_tiles", lambda chart, plain, fill: {})

    # black-pixels alone: the tile stage before it drops solid fill outright, so
    # with both running there would be none left for the guard to find
    with pytest.raises(sn.Leaked, match="would ship"):
        sn.run(src, out, jobs=1, stages=("black-pixels",))


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
               jobs=1, stages=("black-tiles", "black-pixels"))


def test_every_level_below_the_deepest_is_dropped(tmp_path):
    """Detection runs once, at the level that resolves the boundary most finely.

    The levels below are separate renderings of the same coastline, each with
    its own fill; asking the same question of all nine put the boundary in a
    different place at every zoom. They go, and downscale rebuilds them from the
    one that was cleaned -- so what is asserted here is that they are gone, not
    that they are clean."""
    src = tmp_path / "pyramid.mbtiles"
    build(src, LAYOUT)
    build(src, {(0, 0): "fill", (1, 0): "half-fill", (2, 0): "content"}, z=Z - 1)
    out = tmp_path / "pyramid-out.mbtiles"
    sn.run(src, out, jobs=1, stages=("black-tiles", "black-pixels"))

    con = sqlite3.connect(f"file:{out}?mode=ro", uri=True)
    levels = [z for (z,) in con.execute("SELECT DISTINCT zoom_level FROM tiles")]
    minzoom = con.execute("SELECT value FROM metadata WHERE name='minzoom'").fetchone()
    pending = con.execute(
        "SELECT value FROM metadata WHERE name='pyramid_pending'").fetchone()
    con.close()
    assert levels == [Z], f"levels below the deepest survived: {levels}"
    assert (a := read(out, 1, 1)) is not None and (a[:, :90, 3] == 0).all(), \
        "and the deepest level must still have been stripped"
    assert minzoom == (str(Z),), \
        "the file claims a pyramid it does not have"
    assert pending == (str(Z - 1),), \
        "nothing records the floor downscale is meant to rebuild to"


def _limit_pad():
    """A tile at the outer limit: chart to the north, nothing to the south.

    The ink line runs the full width of the padded array, so the only way from
    one side of it to the other is through the line itself.
    """
    side = np.ones((256, 256), bool)
    side[128:131, :] = False
    return {"t": np.ones((256, 256), bool), "b": None, "l": side, "r": side.copy(),
            "lt": np.ones((256, 256), bool), "rt": np.ones((256, 256), bool),
            "lb": None, "rb": None}


def test_the_flood_enters_only_from_the_sides_the_outside_lies_past():
    """The bug this is here for: seeded from the whole rim, a tile straddling
    the outer limit empties *both* sides of it. The blank past the limit and the
    open water inside it are the same white, so nothing local separates them --
    only which way the tile-grid walk came in."""
    a = np.asarray(Image.open(io.BytesIO(tile("limit"))).convert("RGBA"))
    m = sn.nodata_mask(a, _limit_pad(), kind="white",
                       outward=frozenset({"b", "lb", "rb"}))
    assert m[131 + sn.BLEED:, :].all(), "the blank past the limit stayed"
    assert not m[:128 - sn.BLEED, :].any(), "water inside the limit was erased"


def test_seeding_from_every_side_is_what_empties_both_halves():
    """The counterpart, pinning why the direction is needed rather than assumed:
    with no direction given, the same tile loses the water too."""
    a = np.asarray(Image.open(io.BytesIO(tile("limit"))).convert("RGBA"))
    m = sn.nodata_mask(a, _limit_pad(), kind="white", outward=None)
    assert m[:128 - sn.BLEED, :].all()


def test_a_dimmer_blank_is_found_only_once_the_level_is_lowered(tmp_path):
    """Traficom does not render blank the same everywhere: the south-eastern
    sheets draw it a step below white. At the default level none of those tiles
    is blank and none of them is removed, and the two runs must not claim the
    same recipe."""
    layout = {(x, 0): "content" for x in range(3)}
    layout |= {(x, 1): "dim-blank" for x in range(3)}

    kept = tmp_path / "dim-kept.mbtiles"
    sn.run(build(tmp_path / "dim.mbtiles", layout), kept, jobs=1,
           stages=("black-tiles", "black-pixels", "white-tiles"))
    assert read(kept, 1, 1) is not None, \
        "a shade below white was taken for blank at the default level"

    gone = tmp_path / "dim-gone.mbtiles"
    sn.run(build(tmp_path / "dim2.mbtiles", layout), gone, jobs=1,
           stages=("black-tiles", "black-pixels", "white-tiles"), white=254)
    assert read(gone, 1, 1) is None, "still not blank at 254"
    assert stamp_of(kept) != stamp_of(gone), \
        "two recipes, one stamp: nothing can tell the files apart"


def test_water_between_soundings_is_held_by_its_neighbours(tmp_path):
    """A tile with no sounding on it is blank by every local test, so the walk
    crosses it and the drop takes it -- and where a sheet simply ends there is
    nothing to stop that running inward. What separates such a tile from one
    past the last sheet is how much chart is against it."""
    layout = {(1, 1): "blank"}          # water between soundings draws nothing
    layout |= {(0, 1): "content", (2, 1): "content", (1, 0): "content"}
    layout |= {(1, 2): "blank", (0, 0): "blank", (2, 0): "blank",
               (0, 2): "blank", (2, 2): "blank"}
    src = build(tmp_path / "held.mbtiles", layout)
    out = tmp_path / "held-out.mbtiles"
    sn.run(src, out, jobs=1, stages=("black-tiles", "black-pixels", "white-tiles"))

    assert read(out, 1, 2) is None, "blank past the sheet was kept"
    assert read(out, 1, 1) is not None, \
        "water with three chart tiles against it was dropped"


def test_the_neighbour_count_still_lets_a_straight_edge_go(tmp_path):
    """The counterpart: below a straight run of chart a blank tile has one
    chart tile on it, and must stay removable. Counted over eight neighbours it
    would have three, and nothing at any straight edge could ever go."""
    layout = {(x, 0): "content" for x in range(3)}
    layout |= {(x, 1): "blank" for x in range(3)}
    src = build(tmp_path / "edge.mbtiles", layout)
    out = tmp_path / "edge-out.mbtiles"
    sn.run(src, out, jobs=1, stages=("black-tiles", "black-pixels", "white-tiles"))

    assert all(read(out, x, 1) is None for x in range(3)), \
        "blank below a straight sheet edge was held"


def test_water_inside_the_limit_survives_a_whole_run(tmp_path):
    """End to end, through the tile drop that precedes the white pass.

    The row above the limit is open water, so the tile the limit crosses has
    blank white on both of the sides that matter -- which is the arrangement
    that made this leak in the first place, and the reason the fixture is four
    rows deep rather than three."""
    layout = {(x, 0): "content" for x in range(3)}
    layout |= {(x, 1): "open" for x in range(3)}
    layout |= {(x, 2): "limit" for x in range(3)}
    layout |= {(x, 3): "blank" for x in range(3)}
    src = build(tmp_path / "limit.mbtiles", layout)
    out = tmp_path / "limit-out.mbtiles"
    sn.run(src, out, jobs=1, stages=sn.STAGES)

    assert read(out, 1, 3) is None, "the blank past the limit was kept"
    a = read(out, 1, 2)
    assert a is not None, "the tile the limit crosses was dropped"
    assert (a[131 + sn.BLEED:, :, 3] == 0).all(), "the blank past the limit stayed"
    assert (a[:128 - sn.BLEED, :, 3] == 255).all(), "water inside the limit went"
    assert np.array_equal(read(out, 1, 1), read_source_kind("open")), \
        "the open water a row further in was touched at all"


def test_a_blank_neighbour_is_named_outward_rather_than_left_to_the_pixels():
    """Why the flood is free to travel through the margin.

    What would let it round a corner into the chart is a neighbour that is blank
    throughout and yet not outward: the flood would cross it, come back in over
    the far side, and erase ground it could not have walked to. The tile grid
    does not produce one. Blank throughout is what featureless means, the walk
    crosses featureless cells, so the side is named outward -- and a flood
    entering from it is entering where the outside actually is.

    Barring those margins instead is what leaves peaks standing wherever the
    boundary runs diagonally, since a tile whose only outward side is a corner
    cannot then reach the rest of its own outside."""
    marked = {(1, 1), (1, 2), (2, 1), (2, 2)}
    feat = {(1, 0)}
    reached = sn.walk_outside(marked, feat)
    assert (1, 0) in reached, "the walk did not cross a featureless cell"
    assert "t" in sn.facing(marked, reached)[(1, 1)]


def test_a_band_too_thin_to_hold_a_core_is_still_reached(tmp_path):
    """The other half of the same rule, and why the confined flood is let into
    the outward margins rather than held to the tile. This band is 8px wide on
    the tile's own ground; no pixel of it is blank for a radius in every
    direction. Every core pixel is in the margin the absent neighbour fills, so
    a flood held to the tile finds no seed at all and the band ships."""
    a = np.zeros((256, 256, 4), np.uint8)
    a[:, :] = PAPER
    a[:, :8] = (255, 255, 255, 255)
    pad = {"l": None, "lt": None, "lb": None}
    pad |= {s: np.zeros((256, 256), bool) for s in ("r", "t", "b", "rt", "rb")}
    m = sn.nodata_mask(a, pad, kind="white",
                       outward=frozenset({"l", "lt", "lb"}))
    # away from the ends, where the paper above and below closes in on it
    assert m[sn.RADIUS:-sn.RADIUS, :8].all(), \
        f"the band survived ({int(m[:, :8].sum())} of 2048 px)"


def test_the_outside_counts_whichever_colour_it_is_drawn_in(tmp_path):
    """Traficom renders no-chart two ways: black past a sheet edge, white past
    the outer limit. Each pass knows one of them, so where the two meet, the
    black pass reads the white side as chart and walls itself out of its own
    fill -- the flood has no seed at all, because every pixel of the margin the
    walk called outward reads as solid.

    The walk has already settled it: a tile it reached carries no chart. Padding
    those sides solid is the same statement an absent neighbour makes."""
    a = np.zeros((256, 256, 4), np.uint8)
    a[:, :] = (0, 0, 0, 255)                     # nothing but off-sheet fill
    R = sn.RADIUS
    white = np.zeros((256, 256, 4), np.uint8)
    white[:, :] = (255, 255, 255, 255)           # the outer limit's blank
    edges = {(1, 0): sn._edges((Z, 1, 0, tile("blank"), "black", sn.WHITE))[2]}
    pad = sn.surround((1, 1), edges, frozenset({"t", "lt", "rt"}))
    assert pad["t"] is None, "the walk said outward and the padding argued"
    m = sn.nodata_mask(a, pad, outward=frozenset({"t", "lt", "rt"}))
    assert m.all(), f"the fill survived ({65536 - int(m.sum())} px kept)"


def test_a_tile_whose_whole_neighbourhood_is_fill_is_erased(tmp_path):
    """The degenerate case the distance transform cannot answer: an array with
    no background in it has no distance to one, and scipy returns a small number
    rather than an infinite one -- which reads as "no fill found" and would keep
    a tile that is nothing but fill, in the middle of more of it."""
    a = np.zeros((256, 256, 4), np.uint8)
    a[:, :] = (0, 0, 0, 255)
    pad = {s: np.ones((256, 256), bool) for s in sn.AROUND.values()}
    assert sn.nodata_mask(a, pad).all()


def test_a_diagonal_neighbour_pads_the_corner(tmp_path):
    """The fill reaches this tile only across a corner, so the only pixels that
    can say it continues past the seam are the diagonal neighbour's."""
    src = build(tmp_path / "diag.mbtiles", {
        (0, 0): "fill",    (1, 0): "content", (2, 0): "content",
        (0, 1): "content", (1, 1): "content", (2, 1): "content",
    })
    out = tmp_path / "diag-out.mbtiles"
    sn.run(src, out, jobs=1, stages=("black-tiles", "black-pixels"))
    edges = {}
    con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    for (x, y) in [(0, 0)]:
        r = con.execute("SELECT tile_data FROM tiles WHERE zoom_level=? AND "
                        "tile_column=? AND tile_row=?", (Z, x, (1 << Z) - 1 - y)).fetchone()
        _, _, e = sn._edges((Z, x, 0, r[0], "black", sn.WHITE))
        edges[(x, y)] = e
    con.close()
    pad = sn.surround((1, 1), edges)
    assert pad["lt"] is not None and pad["lt"].all(), "the corner came back empty"
    assert pad["l"] is None and pad["t"] is None, "only the diagonal is present"


def test_the_stamp_records_the_settings_and_the_stages_that_ran(stripped):
    """Two runs that removed different things must not claim the same recipe,
    so every setting that changes the outcome is in the stamp, and so is the
    list of stages -- a file stripped of fill only is not the same file."""
    stamp = stamp_of(stripped)
    assert f"r{sn.RADIUS}" in stamp and f"b{sn.BLEED}" in stamp, stamp
    assert f"w{sn.WHITE}" in stamp and f"n{sn.MAX_CHART_NEIGHBOURS}" in stamp, stamp
    assert stamp.endswith("black-tiles+black-pixels"), stamp
    assert "white" not in stamp.split(":")[1], "a stage that did not run is named"


_SOURCE = {}


def read_source(x: int, y: int):
    """The unmodified tile, for comparison."""
    key = LAYOUT[(x, y)]
    if key not in _SOURCE:
        _SOURCE[key] = np.asarray(Image.open(io.BytesIO(tile(key))).convert("RGBA"))
    return _SOURCE[key]


def read_source_kind(kind: str):
    return np.asarray(Image.open(io.BytesIO(tile(kind))).convert("RGBA"))


def dark_px(a) -> int:
    return int(((a[..., :3].max(axis=2) <= sn.DARK) & (a[..., 3] == 255)).sum())


def thickest(a) -> int:
    """Width of the fattest dark region left, in pixels across."""
    from scipy import ndimage
    m = (a[..., :3].max(axis=2) <= sn.DARK) & (a[..., 3] == 255)
    n = 0
    while m.any():
        n += 2
        m = ndimage.binary_erosion(m)
    return n


def test_a_fill_wedge_too_thin_to_erode_is_still_removed(tmp_path):
    """What shipped and was wrong. Where the sheet edge crosses a tile at a
    shallow angle the fill tapers below any erosion kernel that also spares a
    place name, so seeding by shape left the whole wedge on the chart -- a
    jagged black edge and stray triangles along every diagonal boundary.

    Flooding from the border has no width to lose on the way in. What survives
    is the last of the taper, where the wedge is narrower than the opening that
    keeps the flood off abutting line work -- a stroke's width, and no more.
    This fixture tapers across a whole tile, which is the worst case; on the
    Aland boundary at z9 the same tile went from 487 stray dark pixels to 5.
    """
    src = build(tmp_path / "wedge.mbtiles", {
        (0, 0): "fill",  (1, 0): "wedge-fill",  (2, 0): "content",
        (0, 1): "fill",  (1, 1): "wedge-fill",  (2, 1): "content",
    })
    out = tmp_path / "out.mbtiles"
    sn.run(src, out, jobs=1, stages=("black-tiles", "black-pixels"))
    before, after = read(src, 1, 0), read(out, 1, 0)
    assert black_px(before) > 5000
    assert black_px(after) < black_px(before) * 0.15
    assert thickest(after) <= 2 * sn.RADIUS


def test_the_anti_aliased_edge_of_the_fill_goes_with_it(tmp_path):
    """A pure-black test leaves the fill's own soft edge behind: a dark fringe
    tracing the boundary, which is what a viewer draws as a jagged black line."""
    src = build(tmp_path / "soft.mbtiles", {
        (0, 0): "fill",  (1, 0): "soft-fill",  (2, 0): "content",
        (0, 1): "fill",  (1, 1): "soft-fill",  (2, 1): "content",
    })
    out = tmp_path / "out.mbtiles"
    sn.run(src, out, jobs=1, stages=("black-tiles", "black-pixels"))
    # x=93 and x=94 hold the last two ramp steps, 90 and 160: paper, not fill
    boundary = read(out, 1, 0)[:, :93]
    assert dark_px(boundary) == 0
    assert (boundary[..., 3] == 0).all()


def test_type_on_a_straddling_tile_survives_the_flood(tmp_path):
    """The flood enters from the border the outside lies past, reaches the fill
    and stops. A place name set clear of that border is not connected to it and
    keeps every stroke."""
    src = build(tmp_path / "type.mbtiles", {
        (0, 0): "fill",  (1, 0): "half-fill+type",  (2, 0): "content",
        (0, 1): "fill",  (1, 1): "half-fill+type",  (2, 1): "content",
    })
    out = tmp_path / "out.mbtiles"
    sn.run(src, out, jobs=1, stages=("black-tiles", "black-pixels"))
    before, after = read(src, 1, 0), read(out, 1, 0)
    assert black_px(after[150:210, 140:200]) == black_px(before[150:210, 140:200])
    assert (after[:, :90, 3] == 0).all()          # and the fill did go


def test_a_stroke_abutting_the_fill_keeps_its_far_end(tmp_path):
    """Line work meeting the fill is the normal arrangement at a sheet edge, so
    it is the case that would bleed if the fill were found by connectivity.

    It is not. What is taken is what the radius test finds and the dilation puts
    back, so a stroke loses the pixels within a radius of the fill and keeps the
    rest of its length."""
    src = build(tmp_path / "line.mbtiles", {
        (0, 0): "fill",  (1, 0): "half-fill",  (2, 0): "content",
        (0, 1): "fill",  (1, 1): "half-fill",  (2, 1): "content",
    })
    out = tmp_path / "out.mbtiles"
    sn.run(src, out, jobs=1, stages=("black-tiles", "black-pixels"))
    after = read(out, 1, 0)
    kept = np.flatnonzero((after[101, :, :3].max(axis=1) == 0) & (after[101, :, 3] == 255))
    assert kept.max() == 229, "the far end of the line was followed"
    assert kept.min() - 90 <= 2 * sn.RADIUS + sn.BLEED
    assert (after[:, :90, 3] == 0).all(), "and the fill did go"


@pytest.mark.parametrize("bleed", [0, 1, 2, 3, 4, 5])
def test_the_bleed_past_the_fill_is_adjustable(tmp_path, bleed):
    """Each extra pixel of bleed erases one more column of the chart's edge.

    `half-fill` puts the fill in columns 0-89 and paper beyond, so the erased
    run on a row carrying no ink is the fill plus whatever the bleed reached.
    """
    src = build(tmp_path / f"bleed{bleed}.mbtiles", {
        (0, 0): "fill",  (1, 0): "half-fill",  (2, 0): "content",
        (0, 1): "fill",  (1, 1): "half-fill",  (2, 1): "content",
    })
    out = tmp_path / f"bleed{bleed}-out.mbtiles"
    sn.run(src, out, jobs=1, stages=("black-tiles", "black-pixels"), bleed=bleed)
    row = read(out, 1, 0)[200, :, 3]
    assert int((row == 0).sum()) == 90 + bleed


def test_a_bleed_of_zero_still_erases_the_fill_it_found(tmp_path):
    """The opening shaves THIN pixels off the body before the flood, so even
    with no bleed the mask has to grow that much back or the fill is under-cut
    by its own detection."""
    src = build(tmp_path / "zero.mbtiles", {
        (0, 0): "fill",  (1, 0): "half-fill",  (2, 0): "content",
        (0, 1): "fill",  (1, 1): "half-fill",  (2, 1): "content",
    })
    out = tmp_path / "zero-out.mbtiles"
    sn.run(src, out, jobs=1, stages=("black-tiles", "black-pixels"), bleed=0)
    a = read(out, 1, 0)
    assert (a[:, :90, 3] == 0).all()
    assert dark_px(a[:, :90]) == 0


def test_the_bleed_is_recorded_in_the_stamp(tmp_path):
    """It changes the output, so a published chart built with one bleed must not
    read as current when the recipe asks for another."""
    src = build(tmp_path / "stamp.mbtiles", LAYOUT)
    out = tmp_path / "stamp-out.mbtiles"
    sn.run(src, out, jobs=1, stages=("black-tiles", "black-pixels"), bleed=4)
    con = sqlite3.connect(f"file:{out}?mode=ro", uri=True)
    stamp = con.execute("SELECT value FROM metadata WHERE name='nodata_stripped'").fetchone()[0]
    con.close()
    assert stamp == sn.processing_stamp(sn.RADIUS, 4, stages=("black-tiles", "black-pixels"))
    assert stamp != sn.processing_stamp(sn.RADIUS, 2, stages=("black-tiles", "black-pixels"))
