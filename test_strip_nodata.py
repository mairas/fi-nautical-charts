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
        if kind == "wedge-fill":
            # The sheet edge crossing the tile at a shallow angle, so the fill
            # tapers to a few pixels. Measured on Yleiskartat z9 at the Aland
            # boundary, where the surviving wedge was 8px across.
            for r in range(256):
                a[r, :max(0, 60 - r // 3)] = (0, 0, 0, 255)
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
    sn.run(src, out, jobs=1, offeez=False)
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
    sn.run(src, out, jobs=1, offeez=False)
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
    sn.run(src, out, jobs=1, offeez=False)

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
    sn.run(src, out, jobs=1, offeez=False)

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
    sn.run(src, out, jobs=1, offeez=False)

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
        sn.run(src, out, jobs=1, offeez=False)


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
               jobs=1, offeez=False)


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
    sn.run(src, out, jobs=1, offeez=False)
    before, after = read(src, 1, 0), read(out, 1, 0)
    assert black_px(before) > 5000
    assert black_px(after) < black_px(before) * 0.15
    assert thickest(after) <= 2 * sn.THIN


def test_the_anti_aliased_edge_of_the_fill_goes_with_it(tmp_path):
    """A pure-black test leaves the fill's own soft edge behind: a dark fringe
    tracing the boundary, which is what a viewer draws as a jagged black line."""
    src = build(tmp_path / "soft.mbtiles", {
        (0, 0): "fill",  (1, 0): "soft-fill",  (2, 0): "content",
        (0, 1): "fill",  (1, 1): "soft-fill",  (2, 1): "content",
    })
    out = tmp_path / "out.mbtiles"
    sn.run(src, out, jobs=1, offeez=False)
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
    sn.run(src, out, jobs=1, offeez=False)
    before, after = read(src, 1, 0), read(out, 1, 0)
    assert black_px(after[150:210, 140:200]) == black_px(before[150:210, 140:200])
    assert (after[:, :90, 3] == 0).all()          # and the fill did go


def test_a_stroke_touching_the_fill_is_not_followed_into_the_chart(tmp_path):
    """Connectivity is why the old code grew seeds through an opened mask: a
    ruled line abutting the fill would otherwise carry the flood down its whole
    length. Flooding from the border has the same exposure, so the body is
    opened before the flood and only dilated back a stroke's width after."""
    src = build(tmp_path / "line.mbtiles", {
        (0, 0): "fill",  (1, 0): "half-fill",  (2, 0): "content",
        (0, 1): "fill",  (1, 1): "half-fill",  (2, 1): "content",
    })
    out = tmp_path / "out.mbtiles"
    sn.run(src, out, jobs=1, offeez=False)
    before, after = read(src, 1, 0), read(out, 1, 0)
    lost = black_px(before[100:103, 95:]) - black_px(after[100:103, 95:])
    assert lost == 0, f"the flood ran {lost}px down a line abutting the fill"


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
    sn.run(src, out, jobs=1, offeez=False, bleed=bleed)
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
    sn.run(src, out, jobs=1, offeez=False, bleed=0)
    a = read(out, 1, 0)
    assert (a[:, :90, 3] == 0).all()
    assert dark_px(a[:, :90]) == 0


def test_the_bleed_is_recorded_in_the_stamp(tmp_path):
    """It changes the output, so a published chart built with one bleed must not
    read as current when the recipe asks for another."""
    src = build(tmp_path / "stamp.mbtiles", LAYOUT)
    out = tmp_path / "stamp-out.mbtiles"
    sn.run(src, out, jobs=1, offeez=False, bleed=4)
    con = sqlite3.connect(f"file:{out}?mode=ro", uri=True)
    stamp = con.execute("SELECT value FROM metadata WHERE name='nodata_stripped'").fetchone()[0]
    con.close()
    assert stamp == sn.processing_stamp(4, offeez=False)
    assert stamp != sn.processing_stamp(2, offeez=False)
