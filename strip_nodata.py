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

MIN_FILL = 64       # ignore specks: a real fill is far larger than this
FILL_FRACTION = .95 # above this a tile is off-sheet rather than chart
RADIUS = 64         # a fill pixel is dark for this far in every direction.
                    # Wide enough that no disk fits through a gap in the dashed
                    # limit line, which is how the blank got into land at 10;
                    # Traficom's capitals run 10-16px across, so none can qualify
DARK = 40           # the transition is one pixel; this catches it
BLEED = 2           # pixels erased past the fill, over the chart's own edge
TILE = 256
WHITE_LUM = 250     # above this an opaque pixel counts as blank paper
BATCH = 2000        # tiles held in memory at once


def processing_stamp(radius: int = RADIUS, bleed: int = BLEED, *,
                     offeez: bool) -> str:
    """What a run with these settings records as nodata_stripped.

    Written into the file and read back by anything deciding whether a
    published chart was built by the current recipe, so the two must be one
    definition rather than two strings that agree until one of them changes.
    """
    return f"opaque-black-disk{radius}-b{bleed}" + ("+offeez-pixel" if offeez else "")


def tile_has_marks(blob):
    """True if the tile carries anything but blank paper. Deliberately generous:
    one non-white opaque pixel is enough, so any doubt makes a tile untouchable."""
    a = np.asarray(Image.open(io.BytesIO(blob)).convert("RGBA"))
    op = a[..., 3] == 255
    if not op.any():
        return False
    return bool((op & (a[..., :3].mean(axis=2) <= WHITE_LUM)).any())


def classify(con, z):
    """(marked, featureless) tile sets at one zoom, in XYZ coordinates."""
    marked, feat = set(), set()
    for x, row, blob in con.execute("SELECT tile_column, tile_row, tile_data FROM tiles "
                                    "WHERE zoom_level=?", (z,)):
        (marked if tile_has_marks(blob) else feat).add((x, (1 << z) - 1 - row))
    return marked, feat


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
    """One tile's character: how much of its ink is black, and how much is not.

    Both counts are over opaque pixels only. Off-sheet fill routinely arrives
    with a transparent margin where the fetched tile runs past the served
    extent, and measuring against the whole 256x256 would read those tiles as
    only nine-tenths black and let them pass for chart.
    """
    z, x, row, blob = task
    a = np.asarray(Image.open(io.BytesIO(blob)).convert("RGBA"))
    opaque = a[..., 3] == 255
    black = opaque & (a[..., :3].max(axis=2) == 0)
    other = opaque & (a[..., :3].mean(axis=2) <= WHITE_LUM) & ~black
    n = int(opaque.sum())
    return x, row, int(black.sum()), int(other.sum()), n


def survey(con, z, pool):
    """One zoom's tiles as (ink, plain, black), in XYZ coordinates.

    `ink` is everything that draws in a colour other than black -- chart, and
    only chart. It is what walls the walk. `plain` is everything else: solid
    fill, blank paper, and the tiles that are part of each. `black` counts the
    opaque black pixels on every tile, so the caller can tell a tile with fill
    to remove from one with nothing on it.
    """
    ink, plain, black = set(), set(), {}
    for rows in batches(con, z, BATCH):
        tasks = [(z, x, row, blob) for x, row, blob in rows]
        for x, row, blk, other, opaque in pool.map(_survey, tasks, chunksize=32):
            xy = (x, (1 << z) - 1 - row)
            black[xy] = blk
            (ink if other > MIN_FILL else plain).add(xy)
    return ink, plain, black


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


def vacant(a: np.ndarray) -> np.ndarray:
    """Blank paper, or no data at all.

    The other thing Traficom renders where there is no chart: past the outer
    limit it draws nothing rather than black, so the same question -- what runs
    in from outside -- has a second answer in a second colour.
    """
    return (((a[..., 3] == 255) & (a[..., :3].mean(axis=2) > WHITE_LUM))
            | (a[..., 3] == 0))


PREDICATE = {"black": fillable, "white": vacant}


def _edges(task):
    """A tile's borders and corners, `margin` deep, under one predicate."""
    z, x, row, blob, margin, kind = task
    d = PREDICATE[kind](np.asarray(Image.open(io.BytesIO(blob)).convert("RGBA")))
    m = margin
    return x, row, {"l": d[:, :m].copy(), "r": d[:, -m:].copy(),
                    "t": d[:m, :].copy(), "b": d[-m:, :].copy(),
                    "lt": d[:m, :m].copy(), "rt": d[:m, -m:].copy(),
                    "lb": d[-m:, :m].copy(), "rb": d[-m:, -m:].copy()}


def edge_lines(con, z, want, pool, kind="black", margin=RADIUS):
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
                tasks.append((z, x, (1 << z) - 1 - y, r[0], margin, kind))
        for x, row, e in pool.map(_edges, tasks, chunksize=32):
            out[(x, (1 << z) - 1 - row)] = e
    return out


def surround(xy, edges):
    """What lies just past each of a tile's eight sides and corners.

    A side maps to the neighbour's facing piece, or to None where there is no
    tile at all -- beyond the data is outside, and outside is fill.
    """
    x, y = xy
    pad = {}
    for (dx, dy), side in AROUND.items():
        n = edges.get((x + dx, y + dy))
        pad[side] = None if n is None else n[FACING[side]]
    return pad


# dx, dy over the eight tiles around one, and the tile borders each lies past.
# A sheet edge crossing the grid diagonally leaves chart tiles whose only
# contact with the fill is a corner, so the diagonals are here too.
AROUND = {(1, 0): "r", (-1, 0): "l", (0, 1): "b", (0, -1): "t",
          (1, 1): "rb", (1, -1): "rt", (-1, 1): "lb", (-1, -1): "lt"}

FACING = {"l": "r", "r": "l", "t": "b", "b": "t",
          "lt": "rb", "rb": "lt", "rt": "lb", "lb": "rt"}

BORDER = {"l": np.s_[:, 0], "r": np.s_[:, -1], "t": np.s_[0, :], "b": np.s_[-1, :]}


def edge_tiles(ink, plain, black):
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
    reached = walk_outside(ink, plain)
    offsheet = {t for t in reached & plain if black.get(t, 0) >= MIN_FILL}
    straddling = {}
    for x, y in ink:
        sides = "".join(s for (dx, dy), s in AROUND.items()
                        if (x + dx, y + dy) in reached)
        if sides:
            straddling[(x, y)] = frozenset(sides)
    return offsheet, straddling


def wholly_offsheet(a: np.ndarray) -> bool:
    """True if the only thing on this tile is off-sheet fill.

    The one question about fill a single tile can answer. Whether a black
    region *within* a chart tile is fill or a place name needs the tile grid to
    settle, but a tile that draws no chart at all cannot be showing a place
    name, however solid its black -- so a caller with no positional context,
    like the downloader, can still act on this much.
    """
    opaque = a[..., 3] == 255
    n = int(opaque.sum())
    if not n:
        return False
    black = opaque & (a[..., :3].max(axis=2) == 0)
    other = opaque & (a[..., :3].mean(axis=2) <= WHITE_LUM) & ~black
    return (int(black.sum()) >= MIN_FILL and int(other.sum()) <= MIN_FILL
            and int(black.sum()) / n >= FILL_FRACTION)


class Leaked(RuntimeError):
    """Tiles hold solid fill that the walk did not reach, so it would ship."""


def leaked(black, candidates, z, cap=TILE * TILE * FILL_FRACTION):
    """Refuse to leave a tile that is nothing but fill unexamined.

    Selecting by position is only as good as the walk, and a walk that stops
    short leaves black in the output while every counter reports success --
    which is exactly how the previous selection shipped. This costs nothing:
    the survey has already counted the black on every tile.
    """
    missed = sorted(t for t, n in black.items() if n >= cap and t not in candidates)
    if missed:
        raise Leaked(
            f"z{z}: {len(missed)} tiles are at least {FILL_FRACTION:.0%} solid black "
            f"but the walk did not reach them, so their fill would ship: "
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
    for side, sl in (("l", np.s_[m:-m, :m]), ("r", np.s_[m:-m, -m:]),
                     ("t", np.s_[:m, m:-m]), ("b", np.s_[-m:, m:-m]),
                     ("lt", np.s_[:m, :m]), ("rt", np.s_[:m, -m:]),
                     ("lb", np.s_[-m:, :m]), ("rb", np.s_[-m:, -m:])):
        strip = pad.get(side)
        big[sl] = True if strip is None else strip
    return big


def nodata_mask(a: np.ndarray, pad: dict | None = None, radius: int = RADIUS,
                bleed: int = BLEED, kind: str = "black") -> np.ndarray:
    """Pixels belonging to off-sheet fill rather than to chart ink.

    A fill pixel is dark for `radius` in every direction, and that is the whole
    test for what may start a removal. Nothing narrower than twice the radius
    can satisfy it, so a place name -- Traficom sets them at 10-16px across --
    cannot be reached from anywhere, however the tile is bounded.

    The fill is then whatever that test finds running in from the margin, which
    carries the neighbouring tiles' own pixels -- their real ones, so a band
    that is thin here but wide a few pixels into the next tile is still found.

    `pad` of None means the tile is wholly outside the last sheet. Nothing there
    is chart, so nothing needs protecting.
    """
    dark = PREDICATE[kind](a)
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
    rim = np.ones(big.shape, bool)
    rim[radius:-radius, radius:-radius] = False
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
    z, x, row, blob, pad, radius, bleed, kind = task
    img = Image.open(io.BytesIO(blob)).convert("RGBA")
    a = np.asarray(img).copy()
    m = nodata_mask(a, pad, radius, bleed, kind)
    n = int(m.sum())
    if n < MIN_FILL:
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


def scan(src: Path, jobs: int) -> None:
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
        ink, plain, black = survey(con, deep, pool)
    offsheet, straddling = edge_tiles(ink, plain, black)
    px = sum(black.get(t, 0) for t in offsheet)
    print(f"  z{deep:<3} {len(offsheet):>6} off-sheet and {len(straddling):>5} "
          f"straddling of {len(ink) + len(plain):>7} tiles, {px / 65536:.0f} "
          f"tiles' worth of fill beyond the sheets")
    print(f"  {len(offsheet) + len(straddling)} tiles would be examined")
    con.close()


def pixel_pass(con, z, whole, straddle, kind, radius, bleed, pool):
    """Erase one colour of no-data from one zoom. Returns (rewritten, dropped).

    `whole` are tiles that are nothing but it, `straddle` the chart tiles they
    touch -- the ones that need their neighbours' pixels before anything about
    them can be decided.
    """
    if not whole and not straddle:
        return 0, 0
    rewritten = dropped = 0
    want = sorted({(x + dx, y + dy) for (x, y) in straddle
                   for dx, dy in AROUND} | straddle)
    edges = edge_lines(con, z, want, pool, kind, radius)
    todo = sorted(((x, (1 << z) - 1 - y),
                   None if (x, y) in whole else surround((x, y), edges))
                  for (x, y) in (whole | straddle))
    for i in range(0, len(todo), BATCH):
        tasks = []
        for (x, row), pad in todo[i:i + BATCH]:
            blob = con.execute("SELECT tile_data FROM tiles WHERE zoom_level=? AND "
                               "tile_column=? AND tile_row=?", (z, x, row)).fetchone()
            if blob:
                tasks.append((z, x, row, blob[0], pad, radius, bleed, kind))
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


def run(src: Path, out: Path, jobs: int, offeez: bool, radius: int = RADIUS,
        bleed: int = BLEED) -> None:
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
        ink, plain, black = survey(con, deep, pool)
        offsheet, straddling = edge_tiles(ink, plain, black)
        leaked(black, offsheet | straddling.keys(), deep)
        print(f"  z{deep} {len(offsheet)} off-sheet, {len(straddling)} straddling "
              f"of {len(ink) + len(plain)} tiles")
        rewritten, dropped = pixel_pass(con, deep, offsheet, set(straddling),
                                        "black", radius, bleed, pool)
        print(f"  z{deep} rewrote {rewritten}, dropped {dropped}")

        if offeez:
            # classified from the black-stripped tiles, so removed fill cannot
            # pose as a marking and wall the flood out of its own region
            drops = flood_outside(*classify(con, deep))
            for x, y in drops:
                con.execute("DELETE FROM tiles WHERE zoom_level=? AND tile_column=? "
                            "AND tile_row=?", (deep, x, (1 << deep) - 1 - y))
            con.commit()
            print(f"  z{deep} off-EEZ: {len(drops)} tiles dropped whole")
            dropped += len(drops)

            # Dropping tiles can only take one that is blank throughout. Where
            # the limit crosses one, the blank half stays -- the same shape of
            # leftover the fill used to leave, in the other colour. Those tiles
            # are chart, so they get the same treatment: what runs in from the
            # margin, and only that.
            marked, feat = classify(con, deep)
            # walk_outside, not flood_outside: the drop above has just deleted
            # the blank tiles, so what a boundary tile now faces is an empty
            # cell rather than a featureless neighbour
            reached = walk_outside(marked, feat)
            straddle = {t for t in marked
                        if any((t[0] + dx, t[1] + dy) in reached for dx, dy in AROUND)}
            wr, wd = pixel_pass(con, deep, set(), straddle, "white",
                                radius, bleed, pool)
            print(f"  z{deep} limit: {len(straddle)} straddling, {wr} rewritten, "
                  f"{wd} dropped")
            rewritten += wr
            dropped += wd

    # Every level below the one just cleaned is a separate rendering of the same
    # coastline and still carries its fill. Downscale rebuilds them all from
    # here, so leaving them would ship whichever the rebuild happened not to
    # overwrite; metadata keeps minzoom, which is what says how far down to go.
    stale = [z for z in zs if z < deep]
    if stale:
        con.execute("DELETE FROM tiles WHERE zoom_level<?", (deep,))
        con.commit()
        print(f"  dropped z{min(stale)}..z{deep - 1} for downscale to rebuild from z{deep}")

    con.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)",
                ("nodata_stripped", processing_stamp(radius, bleed, offeez=offeez)))
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
    p.add_argument("--skip-offeez", action="store_true",
                   help="leave the blank white beyond the chart limits in place")
    args = p.parse_args()
    if not args.input.exists():
        sys.exit(f"no such file: {args.input}")
    if args.scan:
        scan(args.input, args.jobs or None)
        return
    out = args.out or args.input.with_suffix(".stripped.mbtiles")
    run(args.input, out, args.jobs or None, not args.skip_offeez, args.radius,
        args.bleed)


if __name__ == "__main__":
    main()
