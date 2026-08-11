#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pillow", "numpy", "scipy"]
# ///
"""Remove the opaque black no-data fill some Traficom layers serve off-sheet.

Yleiskartat renders the area outside its sheets as solid black rather than
leaving it transparent, so a viewer draws black wedges along every sheet edge.
Whole tiles are affected, and so are the tiles the sheet boundary crosses, which
come back part chart and part black.

Colour cannot separate the two: chart ink is pure (0,0,0) as well, and fills up
to 5% of an ordinary tile. Neither can shape alone. Eroding the black mask does
leave seeds inside the fill, but Traficom sets place names in heavy serif
capitals, and at native zoom their strokes survive the same erosion -- so the
local test called HELSINKI a no-data fill and deleted it, leaving the hollow
anti-aliased outline behind. It cost 6-12% of the ink on interior tiles.

Position separates them, at both scales. The fill is not a shape inside a tile
but a region of the tile grid: it lies beyond the last sheet and runs to the
edge of the data, so the same walk that finds the water outside the EEZ finds
it. Only tiles that walk reaches, and the chart tiles they touch, are examined
at all; the interior is never a candidate, whatever its ink looks like.

Within one of those tiles the same question is asked again in pixels: the fill
is what runs in from the borders the walk came through. Seeding it by shape
instead -- eroding the black and growing the seeds back -- fails on the tiles
that matter, because where the sheet edge crosses a tile at a shallow angle the
fill enters as a wedge only a few pixels wide, thinner than any erosion that
also spares a place name. Flooding in from the border has no width to lose.

Only the deepest zoom is examined, and every level below it is deleted for
downscale to rebuild. Running the detection per zoom asked the same question of
nine renderings of the same coastline and got nine answers, so the boundary
moved as you zoomed; and the answers below the deepest level were thrown away by
the downscale anyway. Averaging black into a parent turns it grey, which is
indistinguishable from chart content, so the order matters: strip, then
downscale.
"""

from __future__ import annotations

import argparse
import io
import os
import sqlite3
import sys
from collections import deque
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

RADIUS = 128        # a fill pixel is dark for this far in every direction.
                    # Wide enough that no disk fits through a gap in the dashed
                    # limit line, which is how the blank got into land at 10;
                    # Traficom's capitals run 10-16px across, so none can
                    # qualify. At 128 the disk is wider than a tile, so no
                    # passage inside one can admit it -- the fill has to be wide
                    # across the seam as well, which is what smooths the edge
DARK = 40           # the transition is one pixel; this catches it
BLEED = 2           # pixels erased past the fill, over the chart's own edge
TILE = 256
WHITE = 255         # an opaque pixel is blank paper if every channel is at
                    # least this. Most sheets render blank as ffffff, but the
                    # south-eastern ones render it fefefe, corner fill included,
                    # so at 255 nothing there is blank and none of it is found
BATCH = 2000        # tiles held in memory at once
MAX_CHART_NEIGHBOURS = 2
                    # a blank tile with more chart than this against it is
                    # held to be inside the chart, whatever it draws


# The four removals, in the order they run. Each is a separate question, and
# only two of them are risky: dropping a tile that is fill or blank throughout
# cannot touch chart, while the pixel floods need a drawn boundary to stop at.
# Most sheets simply end, with the water inside the same white as the blank
# outside and no line between them, so white-pixels is off by default.
STAGES = ("black-tiles", "black-pixels", "white-tiles", "white-pixels")
DEFAULT_STAGES = ("black-tiles", "black-pixels", "white-tiles")


def processing_stamp(radius: int = RADIUS, bleed: int = BLEED, *,
                     stages=DEFAULT_STAGES, white: int = WHITE,
                     limit: int = MAX_CHART_NEIGHBOURS) -> str:
    """What a run with these settings records as nodata_stripped.

    Written into the file and read back by anything deciding whether a
    published chart was built by the current recipe, so the two must be one
    definition rather than two strings that agree until one of them changes.
    """
    return (f"nodata-r{radius}-b{bleed}-w{white}-n{limit}:"
            + "+".join(s for s in STAGES if s in stages))


def blank(a: np.ndarray, white: int = WHITE) -> np.ndarray:
    """Blank paper, and nothing else.

    Every channel has to reach `white`. Anything below it is something the
    cartographer drew -- the anti-aliased skirt of a sounding, the pale tint at
    the edge of a depth area, a hairline at less than full contrast. Judging by
    mean luminance instead put all of that on the no-data side of the line, and
    the flood then treats a figure's own soft edge as more of the blank it is
    standing in.

    The level is a setting rather than 255 because Traficom does not render
    blank the same everywhere: the south-eastern sheets draw it fefefe, and one
    step of slack there is the difference between finding all of it and finding
    none of it.
    """
    return a[..., :3].min(axis=2) >= white


def tile_has_marks(blob, white: int = WHITE):
    """True if the tile carries anything but blank paper. Deliberately generous:
    one non-white opaque pixel is enough, so any doubt makes a tile untouchable."""
    a = np.asarray(Image.open(io.BytesIO(blob)).convert("RGBA"))
    op = a[..., 3] == 255
    if not op.any():
        return False
    return bool((op & ~blank(a, white)).any())


def enclosed(marked, feat, limit: int = MAX_CHART_NEIGHBOURS):
    """Blank tiles with more than `limit` chart tiles against them.

    Open water carries soundings, but not on every tile: a tile between two of
    them draws nothing at all and is blank by every local test, so the grid walk
    crosses it and the drop takes it. Where a sheet simply ends there is no line
    to stop either, and the loss runs inward tile by tile.

    What separates such a tile from one past the last sheet is how much chart is
    against it. Counted over the four sides only, the same connectivity the walk
    uses: a blank tile below a straight sheet edge has one chart tile on it and
    must stay removable, and over eight it would have three and nothing at any
    straight edge could ever go.
    """
    return {t for t in feat
            if sum((t[0] + dx, t[1] + dy) in marked
                   for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))) > limit}


def classify(con, z, white: int = WHITE, limit: int = MAX_CHART_NEIGHBOURS):
    """(marked, featureless) tile sets at one zoom, in XYZ coordinates.

    Tiles the neighbour count holds are returned as marked, so they are walls to
    the walk as well as safe from the drop -- otherwise the walk crosses them
    and takes the next tile in instead.
    """
    marked, feat = set(), set()
    for x, row, blob in con.execute("SELECT tile_column, tile_row, tile_data FROM tiles "
                                    "WHERE zoom_level=?", (z,)):
        (marked if tile_has_marks(blob, white) else feat).add((x, (1 << z) - 1 - row))
    held = enclosed(marked, feat, limit)
    return marked | held, feat - held


def walk_outside(marked, feat):
    """Every cell the walk reaches from beyond the data, empty ones included.

    A marked tile is a wall. That is what makes the dashed EEZ line work here:
    at pixel scale the fill slips between the dashes, but every tile the line
    crosses holds some of them, so at tile scale the fence is unbroken.

    Cells with no tile at all are outside by definition and the walk crosses
    them freely -- and they are returned, because a chart tile at the very edge
    of the data has nothing but empty space on its outward side, and a caller
    asking "does this tile face the outside?" has to be able to see it.
    """
    present = marked | feat
    if not present:
        return set()
    xs = [p[0] for p in present]
    ys = [p[1] for p in present]
    x0, x1, y0, y1 = min(xs) - 1, max(xs) + 1, min(ys) - 1, max(ys) + 1
    seen, q = set(), deque()
    for x in range(x0, x1 + 1):
        for y in (y0, y1):
            if (x, y) not in seen:
                seen.add((x, y)); q.append((x, y))
    for y in range(y0, y1 + 1):
        for x in (x0, x1):
            if (x, y) not in seen:
                seen.add((x, y)); q.append((x, y))
    while q:
        x, y = q.popleft()
        for n in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if not (x0 <= n[0] <= x1 and y0 <= n[1] <= y1) or n in seen or n in marked:
                continue
            seen.add(n); q.append(n)
    return seen


def flood_outside(marked, feat):
    """Featureless tiles reachable from beyond the data.

    Those the walk cannot reach are enclosed by chart content -- open water
    between soundings -- and are kept."""
    return feat & walk_outside(marked, feat)


def _survey(task):
    """One tile's character: how much of it is off-sheet fill, and how much is
    neither fill nor blank paper -- which is to say, chart.

    Both counts are over opaque pixels only. Off-sheet fill routinely arrives
    with a transparent margin where the fetched tile runs past the served
    extent, and measuring against the whole 256x256 would read those tiles as
    only nine-tenths fill and let them pass for chart.
    """
    z, x, row, blob, white = task
    a = np.asarray(Image.open(io.BytesIO(blob)).convert("RGBA"))
    opaque = a[..., 3] == 255
    fill = opaque & (a[..., :3].max(axis=2) <= DARK)
    other = opaque & ~fill & ~blank(a, white)
    return x, row, int(fill.sum()), int(other.sum()), int(opaque.sum())


def survey(con, z, pool, white: int = WHITE):
    """One zoom's tiles as (chart, plain, fill counts, solid), in XYZ.

    `chart` is every tile carrying a single pixel that is neither fill nor blank
    paper. One is enough: a tile with anything drawn on it is not a tile to
    remove, so it walls the walk and can never be a whole-tile candidate. Its
    fill, if it has any, comes off by the pixel flood instead, which is also
    what takes the anti-aliased skirt where the fill meets the sheet edge.

    `plain` is everything else -- fill, blank paper, and the tiles that are part
    of each. `fill` counts the fill pixels on every tile so the caller can tell
    one with fill to remove from one with nothing on it, and `solid` names the
    tiles that are fill and nothing else.
    """
    chart, plain, fill, solid = set(), set(), {}, set()
    for rows in batches(con, z, BATCH):
        tasks = [(z, x, row, blob, white) for x, row, blob in rows]
        for x, row, f, other, opaque in pool.map(_survey, tasks, chunksize=32):
            xy = (x, (1 << z) - 1 - row)
            fill[xy] = f
            (chart if other else plain).add(xy)
            if opaque and f == opaque:
                solid.add(xy)
    return chart, plain, fill, solid


def fillable(a: np.ndarray) -> np.ndarray:
    """Pixels that are either the fill's own black or no data at all.

    Where the fetch ran past the served extent the tile comes back transparent,
    and that is the same thing the fill is: not chart. Counting it as black is
    what lets the radius test find a fill that runs along the data's edge as a
    band a few pixels wide -- too thin to qualify on its own, but part of one
    wide region once the emptiness beside it counts.
    """
    return (((a[..., :3].max(axis=2) <= DARK) & (a[..., 3] == 255))
            | (a[..., 3] == 0))


def vacant(a: np.ndarray, white: int = WHITE) -> np.ndarray:
    """Blank paper, or no data at all.

    The other thing Traficom renders where there is no chart: past the outer
    limit it draws nothing rather than black, so the same question -- what runs
    in from outside -- has a second answer in a second colour.
    """
    return ((a[..., 3] == 255) & blank(a, white)) | (a[..., 3] == 0)


def nodata_test(kind: str, white: int = WHITE):
    """The test for one of the two colours Traficom renders no chart in.

    Bound to its level rather than looked up bare, because the white one has a
    setting and the black one does not, and a worker is handed the kind as a
    string.
    """
    return fillable if kind == "black" else lambda a: vacant(a, white)


def _edges(task):
    """A tile's borders and corners, `margin` deep, under one predicate."""
    z, x, row, blob, margin, kind, white = task
    d = nodata_test(kind, white)(
        np.asarray(Image.open(io.BytesIO(blob)).convert("RGBA")))
    m = margin
    return x, row, {"l": d[:, :m].copy(), "r": d[:, -m:].copy(),
                    "t": d[:m, :].copy(), "b": d[-m:, :].copy(),
                    "lt": d[:m, :m].copy(), "rt": d[:m, -m:].copy(),
                    "lb": d[-m:, :m].copy(), "rb": d[-m:, -m:].copy()}


def edge_lines(con, z, want, pool, kind="black", margin=RADIUS, white=WHITE):
    """The border strips of every tile a candidate needs to be padded with.

    A tile cannot tell fill from ink at its own edge without knowing what is on
    the other side, and the tile grid alone cannot say: a sheet edge crossing
    diagonally gives a chart tile whose neighbour is chart in every straight
    direction and fill only past a corner.
    """
    out = {}
    for i in range(0, len(want), BATCH):
        chunk = want[i:i + BATCH]
        tasks = []
        for x, y in chunk:
            r = con.execute("SELECT tile_data FROM tiles WHERE zoom_level=? AND "
                            "tile_column=? AND tile_row=?",
                            (z, x, (1 << z) - 1 - y)).fetchone()
            if r:
                tasks.append((z, x, (1 << z) - 1 - y, r[0], margin, kind, white))
        for x, row, e in pool.map(_edges, tasks, chunksize=32):
            out[(x, (1 << z) - 1 - row)] = e
    return out


def surround(xy, edges, outward=frozenset()):
    """What lies just past each of a tile's eight sides and corners.

    A side maps to the neighbour's facing piece, or to None -- meaning solid
    no-data -- where there is no tile at all.

    It also maps to None on the sides the walk called outward, whatever the
    neighbour draws there. Those are tiles the walk reached, which is to say
    tiles carrying no chart; that is the same fact an absent neighbour states,
    and it deserves the same padding. Reading their pixels instead asks a
    predicate that knows only one of the two ways Traficom renders "no chart" --
    black past a sheet edge, white past the outer limit -- so where the two meet,
    the black pass reads the white side as chart and walls itself out of its own
    fill. Measured on the tile where they meet at 59 31'N: 704px of fill left at
    radius 64 and 13,104 at 128, against 188 and 227 once the walk is believed.
    """
    x, y = xy
    pad = {}
    for (dx, dy), side in AROUND.items():
        n = None if side in outward else edges.get((x + dx, y + dy))
        pad[side] = None if n is None else n[FACING[side]]
    return pad


# dx, dy over the eight tiles around one, and the tile borders each lies past.
# A sheet edge crossing the grid diagonally leaves chart tiles whose only
# contact with the fill is a corner, so the diagonals are here too.
AROUND = {(1, 0): "r", (-1, 0): "l", (0, 1): "b", (0, -1): "t",
          (1, 1): "rb", (1, -1): "rt", (-1, 1): "lb", (-1, -1): "lt"}

FACING = {"l": "r", "r": "l", "t": "b", "b": "t",
          "lt": "rb", "rb": "lt", "rt": "lb", "lb": "rt"}


def regions(margin: int) -> dict:
    """Where each side's padding sits in the widened array."""
    m = margin
    return {"l": np.s_[m:-m, :m], "r": np.s_[m:-m, -m:],
            "t": np.s_[:m, m:-m], "b": np.s_[-m:, m:-m],
            "lt": np.s_[:m, :m], "rt": np.s_[:m, -m:],
            "lb": np.s_[-m:, :m], "rb": np.s_[-m:, -m:]}


def facing(tiles, reached) -> dict:
    """For each tile, which of its sides the outside lies past.

    Empty for a tile the outside does not touch, so the caller can use this to
    pick candidates as well as to aim them.
    """
    out = {}
    for x, y in tiles:
        sides = frozenset(s for (dx, dy), s in AROUND.items()
                          if (x + dx, y + dy) in reached)
        if sides:
            out[(x, y)] = sides
    return out


def edge_tiles(chart, plain, fill):
    """Tiles where off-sheet fill can be, and only those.

    The fill is not a shape to recognise inside a tile. It is a region of the
    tile grid: it lies beyond the last sheet and runs to the edge of the data,
    so the same walk that finds the water outside the EEZ finds it. A tile the
    walk cannot reach is enclosed by chart, and whatever black it holds is ink
    -- however thick, and Traficom sets place names heavy enough that no local
    shape test can tell them from fill.

    The walk is a protective mask, not a classifier: everything it reaches that
    carries black is examined, and so is every chart tile it touches. Deciding
    candidacy by class instead would exempt whole classes of tile that plainly
    hold fill -- a sheet corner that is half fill and half blank paper draws no
    colour at all, and is neither chart nor wholly off-sheet.

    Returns the off-sheet tiles and the chart tiles that abut them.
    """
    reached = walk_outside(chart, plain)
    offsheet = {t for t in reached & plain if fill.get(t, 0)}
    return offsheet, facing(chart, reached)


def wholly_offsheet(a: np.ndarray) -> bool:
    """True if every opaque pixel on this tile is off-sheet fill.

    The one question about fill a single tile can answer. Whether a dark region
    *within* a chart tile is fill or a place name needs the tile grid to settle,
    but a tile that draws no chart at all cannot be showing a place name,
    however solid its black -- so a caller with no positional context, like the
    downloader, can still act on this much.

    One drawn pixel is enough to fail it. A tile carrying anything at all is not
    a tile to discard whole; the pixel flood is what takes fill off a tile that
    also has chart on it.
    """
    opaque = a[..., 3] == 255
    n = int(opaque.sum())
    if not n:
        return False
    return int((opaque & (a[..., :3].max(axis=2) <= DARK)).sum()) == n


class Leaked(RuntimeError):
    """Tiles hold solid fill that the walk did not reach, so it would ship."""


def leaked(solid, candidates, z):
    """Refuse to leave a tile that is nothing but fill unexamined.

    Selecting by position is only as good as the walk, and a walk that stops
    short leaves fill in the output while every counter reports success --
    which is exactly how the previous selection shipped. This costs nothing:
    the survey has already said which tiles are fill and nothing else.
    """
    missed = sorted(solid - candidates)
    if missed:
        raise Leaked(
            f"z{z}: {len(missed)} tiles are solid fill but the walk did not "
            f"reach them, so their fill would ship: "
            f"{missed[:5]}{' ...' if len(missed) > 5 else ''}")


def widen(dark: np.ndarray, pad: dict, margin: int) -> np.ndarray:
    """The tile's dark mask with a margin of what lies past each side.

    Without it nothing within `margin` of a seam can satisfy the radius test,
    because the disk hangs off the array -- and the fill always meets a seam,
    since that is how it got here. A missing neighbour pads solid: beyond the
    data is outside, and outside is what the fill is.

    The corners are filled from the diagonal neighbours rather than left blank.
    A blank corner is a square of not-fill `margin` on a side, sitting exactly
    where a sheet edge crossing the grid diagonally puts its fill.
    """
    m = margin
    big = np.zeros((dark.shape[0] + 2 * m, dark.shape[1] + 2 * m), bool)
    big[m:-m, m:-m] = dark
    for side, sl in regions(m).items():
        strip = pad.get(side)
        big[sl] = True if strip is None else strip
    return big


def nodata_mask(a: np.ndarray, pad: dict | None = None, radius: int = RADIUS,
                bleed: int = BLEED, kind: str = "black",
                outward: frozenset | None = None,
                white: int = WHITE) -> np.ndarray:
    """Pixels belonging to off-sheet fill rather than to chart ink.

    A fill pixel is dark for `radius` in every direction, and that is the whole
    test for what may start a removal. Nothing narrower than twice the radius
    can satisfy it, so a place name -- Traficom sets them at 10-16px across --
    cannot be reached from anywhere, however the tile is bounded.

    The fill is then whatever that test finds running in **from the sides the
    outside lies past**, which the tile-grid walk has already named in
    `outward`. Direction is half the method, not a refinement of it: the other
    sides face more chart, and a tile at the limit has open water on them --
    white, wide, and every bit as qualified to start a flood as the blank beyond
    the limit is. Seeded from all four, one such tile empties both sides of the
    boundary it straddles. `outward` of None seeds from anywhere, which is only
    right when the caller has established there is nothing to protect.

    The margin carries the neighbouring tiles' own pixels -- their real ones, so
    a band that is thin here but wide a few pixels into the next tile is still
    found, and the flood is free to travel through it. Barring it from the
    margins that are not outward stops it following the fill along a boundary
    that runs diagonally across the grid, which leaves a row of peaks standing
    where a tile could not reach its own outside without crossing a neighbour.

    `pad` of None means the tile is wholly outside the last sheet. Nothing there
    is chart, so nothing needs protecting.
    """
    dark = nodata_test(kind, white)(a)
    if not dark.any() or pad is None:
        return dark
    big = widen(dark, pad, radius)
    if big.all():
        # Nothing within a radius of this tile is chart. Said explicitly because
        # the distance transform below has no answer for an array with no
        # background in it, and returns a small number rather than an infinite
        # one -- which reads as "no fill here" and keeps the lot.
        return np.ones_like(dark)
    # Erosion and dilation by a disk, as distances rather than as a kernel: a
    # 129x129 structuring element over a 384x384 tile is billions of comparisons,
    # the same answer costs two linear passes. Euclidean, so a stroke crossing
    # at 45 degrees is measured the same as one crossing square -- a chessboard
    # metric would make it 1.41x harder to keep.
    core = ndimage.distance_transform_edt(big) > radius
    if not core.any():
        return np.zeros_like(dark)
    rim = np.zeros(big.shape, bool)
    where = regions(radius)
    for side in (where if outward is None else outward):
        rim[where[side]] = True
    out = ndimage.binary_propagation(core & rim, mask=core)
    if not out.any():
        return np.zeros_like(dark)
    out = (ndimage.distance_transform_edt(~out) <= radius) & big
    return spread(out, bleed)[radius:-radius, radius:-radius]


def spread(mask: np.ndarray, n: int) -> np.ndarray:
    """Grow a mask by n pixels in every direction, through whatever is there.

    The opening that found the body already returns it to its own boundary
    wherever the fill is thicker than the kernel, so all of this is deliberate
    over-reach. It is unconditional on purpose: gating the growth on darkness is
    what left single pixels of the boundary behind, since neither the flood nor
    a dark-gated dilation can reach a protrusion the opening removed.
    """
    if n <= 0:
        return mask
    return ndimage.binary_dilation(mask, structure=np.ones((3, 3), bool), iterations=n)


def _strip(task):
    z, x, row, blob, pad, outward, radius, bleed, kind, white = task
    img = Image.open(io.BytesIO(blob)).convert("RGBA")
    a = np.asarray(img).copy()
    m = nodata_mask(a, pad, radius, bleed, kind, outward, white)
    n = int(m.sum())
    if not n:
        return None
    a[m] = (255, 255, 255, 0)          # off-sheet: white, and transparent
    if a[..., 3].max() == 0:
        return (z, x, row, None, n)    # nothing left: drop the tile
    buf = io.BytesIO()
    Image.fromarray(a, "RGBA").save(buf, format="PNG")
    return (z, x, row, buf.getvalue(), n)


def batches(con, z, size):
    """One zoom's tiles in fixed-size batches, walking the key order.

    Not a plain cursor: the caller deletes and updates rows as it goes, and not
    LIMIT/OFFSET either, since a delete shifts every later offset and would skip
    tiles. Advancing past the last key read is stable under both."""
    last = (-1, -1)
    while True:
        rows = con.execute(
            "SELECT tile_column, tile_row, tile_data FROM tiles WHERE zoom_level=? "
            "AND (tile_column, tile_row) > (?, ?) ORDER BY tile_column, tile_row LIMIT ?",
            (z, last[0], last[1], size)).fetchall()
        if not rows:
            return
        yield rows
        last = (rows[-1][0], rows[-1][1])


def scan(src: Path, jobs: int, white: int = WHITE) -> None:
    """What a run would examine, by the same reckoning the run uses.

    Answering this with the local shape test alone -- as a dry run naturally
    wants to, having no output to survey against -- would count every interior
    tile whose type is thick enough to seed the erosion, which is exactly the
    set the run exists not to touch.
    """
    con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    deep = max(z for (z,) in con.execute("SELECT DISTINCT zoom_level FROM tiles"))
    print(f"=== {src.name} ===")
    with ProcessPoolExecutor(max_workers=jobs) as pool:
        chart, plain, fill, _ = survey(con, deep, pool, white)
    offsheet, straddling = edge_tiles(chart, plain, fill)
    px = sum(fill.get(t, 0) for t in offsheet)
    print(f"  z{deep:<3} {len(offsheet):>6} off-sheet and {len(straddling):>5} "
          f"straddling of {len(chart) + len(plain):>7} tiles, {px / 65536:.0f} "
          f"tiles' worth of fill beyond the sheets")
    print(f"  {len(offsheet) + len(straddling)} tiles would be examined")
    con.close()


def pixel_pass(con, z, whole, straddle, kind, radius, bleed, pool, white=WHITE):
    """Erase one colour of no-data from one zoom. Returns (rewritten, dropped).

    `whole` are tiles that are nothing but it. `straddle` maps each chart tile
    the outside touches to the sides it touches from -- both the pixels those
    sides carry and the direction the flood may enter by.
    """
    if not whole and not straddle:
        return 0, 0
    rewritten = dropped = 0
    # only the neighbours whose pixels are actually read: an outward one is
    # padded solid on the walk's word, so its tile never needs decoding for that
    # candidate
    want = sorted({(x + dx, y + dy) for (x, y), sides in straddle.items()
                   for (dx, dy), side in AROUND.items() if side not in sides}
                  | straddle.keys())
    edges = edge_lines(con, z, want, pool, kind, radius, white)
    todo = sorted(whole | straddle.keys())
    for i in range(0, len(todo), BATCH):
        tasks = []
        for x, y in todo[i:i + BATCH]:
            row = (1 << z) - 1 - y
            blob = con.execute("SELECT tile_data FROM tiles WHERE zoom_level=? AND "
                               "tile_column=? AND tile_row=?", (z, x, row)).fetchone()
            pad = (None if (x, y) in whole
                   else surround((x, y), edges, straddle[(x, y)]))
            if blob:
                tasks.append((z, x, row, blob[0], pad, straddle.get((x, y)),
                              radius, bleed, kind, white))
        for res in pool.map(_strip, tasks, chunksize=32):
            if res is None:
                continue
            rz, rx, rrow, data, _ = res
            if data is None:
                con.execute("DELETE FROM tiles WHERE zoom_level=? AND tile_column=? "
                            "AND tile_row=?", (rz, rx, rrow))
                dropped += 1
            else:
                con.execute("UPDATE tiles SET tile_data=? WHERE zoom_level=? AND "
                            "tile_column=? AND tile_row=?",
                            (sqlite3.Binary(data), rz, rx, rrow))
                rewritten += 1
        con.commit()
    return rewritten, dropped


def drop(con, z, tiles) -> int:
    """Delete whole tiles at one zoom. Returns how many."""
    for x, y in tiles:
        con.execute("DELETE FROM tiles WHERE zoom_level=? AND tile_column=? "
                    "AND tile_row=?", (z, x, (1 << z) - 1 - y))
    con.commit()
    return len(tiles)


def run(src: Path, out: Path, jobs: int, stages=DEFAULT_STAGES,
        radius: int = RADIUS, bleed: int = BLEED, white: int = WHITE,
        limit: int = MAX_CHART_NEIGHBOURS) -> None:
    # built beside the target and moved into place at the end, so a run that
    # dies partway cannot leave a valid-looking partial chart where the good
    # one was
    tmp = out.with_suffix(out.suffix + ".partial")
    if tmp.exists():
        tmp.unlink()
    con = sqlite3.connect(str(tmp))
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")
    con.execute("ATTACH DATABASE ? AS src", (str(src),))
    con.execute("CREATE TABLE metadata (name TEXT PRIMARY KEY, value TEXT)")
    con.execute("CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, "
                "tile_row INTEGER, tile_data BLOB)")
    con.execute("CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row)")
    con.execute("INSERT INTO tiles SELECT * FROM src.tiles")
    con.execute("INSERT INTO metadata SELECT * FROM src.metadata")
    con.commit()

    zs = [z for (z,) in con.execute("SELECT DISTINCT zoom_level FROM tiles ORDER BY zoom_level")]
    deep = max(zs)
    rewritten = dropped = 0
    with ProcessPoolExecutor(max_workers=jobs) as pool:
        if "black-tiles" in stages:
            # Tiles that are fill and nothing else, and that the walk reaches
            # from beyond the data. Nothing is drawn on them, so this cannot
            # take chart however solid the fill is.
            chart, plain, fill, solid = survey(con, deep, pool, white)
            gone = drop(con, deep, solid & walk_outside(chart, plain))
            dropped += gone
            print(f"  z{deep} fill: {gone} tiles dropped whole "
                  f"of {len(chart) + len(plain)}")

        if "black-pixels" in stages:
            # Re-surveyed: the drop above has changed what each remaining tile
            # faces, and a tile whose fill neighbour is now an empty cell is
            # padded from the walk's word rather than from the neighbour.
            chart, plain, fill, solid = survey(con, deep, pool, white)
            offsheet, straddling = edge_tiles(chart, plain, fill)
            leaked(solid, offsheet | straddling.keys(), deep)
            r, d = pixel_pass(con, deep, offsheet, straddling,
                              "black", radius, bleed, pool, white)
            print(f"  z{deep} fill: {len(offsheet)} off-sheet, "
                  f"{len(straddling)} straddling, {r} rewritten, {d} dropped")
            rewritten += r
            dropped += d

        if "white-tiles" in stages:
            gone = drop(con, deep, flood_outside(*classify(con, deep, white, limit)))
            dropped += gone
            print(f"  z{deep} blank: {gone} tiles dropped whole")

        if "white-pixels" in stages:
            # Dropping tiles can only take one that is blank throughout. Where
            # the limit crosses one, the blank half stays -- the same shape of
            # leftover the fill used to leave, in the other colour. Trimming it
            # is a pixel flood, and a flood needs a drawn boundary to stop at:
            # where a sheet simply ends, the water inside is the same white as
            # the blank outside and it takes both.
            marked, feat = classify(con, deep, white, limit)
            # walk_outside, not flood_outside: the drop above has just deleted
            # the blank tiles, so what a boundary tile now faces is an empty
            # cell rather than a featureless neighbour
            reached = walk_outside(marked, feat)
            straddle = facing(marked, reached)
            wr, wd = pixel_pass(con, deep, set(), straddle, "white",
                                radius, bleed, pool, white)
            print(f"  z{deep} limit: {len(straddle)} straddling, {wr} rewritten, "
                  f"{wd} dropped")
            rewritten += wr
            dropped += wd

    # Every level below the one just cleaned is a separate rendering of the same
    # coastline and still carries its fill. Downscale rebuilds them all from
    # here, so leaving them would ship whichever the rebuild happened not to
    # overwrite.
    #
    # What is left is not a chart, and it says so. `minzoom` becomes the one
    # level it holds, because a file that claims a pyramid it does not have will
    # pass every check that reads the header and none that looks at the tiles;
    # `pyramid_pending` carries the floor downscale is to rebuild to, and
    # downscale clears it. Publish refuses anything still carrying it.
    stale = [z for z in zs if z < deep]
    if stale:
        con.execute("DELETE FROM tiles WHERE zoom_level<?", (deep,))
        con.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)",
                    ("pyramid_pending", str(min(stale))))
        con.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)",
                    ("minzoom", str(deep)))
        con.commit()
        print(f"  dropped z{min(stale)}..z{deep - 1} for downscale to rebuild from z{deep}")

    con.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)",
                ("nodata_stripped",
                 processing_stamp(radius, bleed, stages=stages,
                                  white=white, limit=limit)))
    con.commit()
    con.execute("VACUUM")
    con.close()
    os.replace(tmp, out)
    print(f"done: {out}  ({rewritten} rewritten, {dropped} dropped)")


def main():
    p = argparse.ArgumentParser(description="Strip opaque-black no-data fill from MBTiles")
    p.add_argument("input", type=Path)
    p.add_argument("--out", type=Path, help="default: <input>.stripped.mbtiles")
    p.add_argument("--jobs", type=int, default=0, help="worker processes (default: all cores)")
    p.add_argument("--bleed", type=int, default=BLEED,
                   help=f"pixels erased past the fill, over the chart's own edge "
                        f"(default {BLEED}); leaving fill shows, taking a little "
                        f"neatline does not")
    p.add_argument("--radius", type=int, default=RADIUS,
                   help=f"a fill pixel is dark for this far in every direction "
                        f"(default {RADIUS}); below half the width of a place name "
                        f"the type starts qualifying too")
    p.add_argument("--scan", action="store_true", help="report what would change, write nothing")
    p.add_argument("--stages", default=",".join(DEFAULT_STAGES),
                   help=f"which removals to run, comma separated, in any order "
                        f"(they always run in the order listed here): "
                        f"{', '.join(STAGES)}. Default {','.join(DEFAULT_STAGES)}. "
                        f"The two tile stages drop only tiles that are fill or "
                        f"blank throughout and so cannot take chart; the two "
                        f"pixel stages are floods and need a drawn boundary to "
                        f"stop at")
    p.add_argument("--white-level", type=int, default=WHITE,
                   help=f"an opaque pixel is blank paper when every channel is "
                        f"at least this (default {WHITE}). The south-eastern "
                        f"sheets render blank as fefefe, corner fill included, "
                        f"and need {WHITE - 1}")
    p.add_argument("--max-chart-neighbours", type=int, default=MAX_CHART_NEIGHBOURS,
                   help=f"a blank tile counts as blank only if at most this many "
                        f"of its four neighbours carry chart (default "
                        f"{MAX_CHART_NEIGHBOURS}); above it the tile is held to "
                        f"be water between soundings rather than paper past the "
                        f"last sheet")
    args = p.parse_args()
    if not args.input.exists():
        sys.exit(f"no such file: {args.input}")
    stages = tuple(s.strip() for s in args.stages.split(",") if s.strip())
    unknown = [s for s in stages if s not in STAGES]
    if unknown:
        sys.exit(f"unknown stage(s): {', '.join(unknown)}; "
                 f"choose from {', '.join(STAGES)}")
    if args.scan:
        scan(args.input, args.jobs or None, args.white_level)
        return
    out = args.out or args.input.with_suffix(".stripped.mbtiles")
    run(args.input, out, args.jobs or None, stages, args.radius,
        args.bleed, args.white_level, args.max_chart_neighbours)


if __name__ == "__main__":
    main()
