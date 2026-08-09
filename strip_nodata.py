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

Position separates them. The fill is not a shape inside a tile but a region of
the tile grid: it lies beyond the last sheet and runs to the edge of the data,
so the same walk that finds the water outside the EEZ finds it. Only tiles that
walk reaches, and the chart tiles they touch, are examined at all; the interior
is never a candidate, whatever its ink looks like. Within those tiles, eroding
seeds the fill and growing them back recovers it exactly to its own hard edge --
which is genuinely hard, the source anti-aliases the boundary not at all -- but
the growth runs through the solid black only, since at a sheet edge the chart's
own ink abuts the fill and connectivity would otherwise follow it out.

Run this on the raw download, before downscaling. Averaging black into a parent
turns it grey, and grey is indistinguishable from chart content.
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

RADIUS = 4          # erosion radius; ink vanishes by 2, fills survive past 8
MIN_FILL = 64       # ignore specks: a real fill is far larger than this
FILL_FRACTION = .95 # above this a tile is off-sheet rather than chart
THIN = 2            # strokes narrower than this survive as ink, not fill
TILE = 256
WHITE_LUM = 250     # above this an opaque pixel counts as blank paper
BATCH = 2000        # tiles held in memory at once


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


def flood_outside(marked, feat):
    """Featureless tiles reachable from beyond the data, walking the tile grid.

    A marked tile is a wall. That is what makes the dashed EEZ line work here:
    at pixel scale the fill slips between the dashes, but every tile the line
    crosses holds some of them, so at tile scale the fence is unbroken.

    Featureless tiles the walk cannot reach are enclosed by chart content --
    open water between soundings -- and are kept."""
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
    return feat & seen


def offsheet_plan(con, zs):
    """Which tiles to delete at each zoom, agreed across every zoom.

    The deepest zoom sets the boundary, because it resolves it most finely.
    Coarser zooms cannot refine it: a coarse tile is marked when any part of its
    ground carries content, so it can say "something here" but never where.

    Ancestors follow their descendants. A tile whose deeper detail survives is
    depicting something, whatever its own rendering shows, so it stays -- without
    that the pyramid gains holes that appear and vanish as you zoom."""
    deep = max(zs)
    drops = {z: flood_outside(*classify(con, z)) for z in zs}
    present_deep = {(x, (1 << deep) - 1 - r) for x, r in
                    con.execute("SELECT tile_column, tile_row FROM tiles WHERE zoom_level=?", (deep,))}
    kept_deep = present_deep - drops[deep]
    plan = {}
    for z in zs:
        s = deep - z
        protected = {(d[0] >> s, d[1] >> s) for d in kept_deep} if s >= 0 else set()
        plan[z] = drops[z] - protected
    return plan, drops


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


# dx, dy over the eight tiles around one, and the tile borders each lies past.
# A sheet edge crossing the grid diagonally leaves chart tiles whose only
# contact with the fill is a corner, so the diagonals are here too.
AROUND = {(1, 0): "r", (-1, 0): "l", (0, 1): "b", (0, -1): "t",
          (1, 1): "rb", (1, -1): "rt", (-1, 1): "lb", (-1, -1): "lt"}

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
    reached = flood_outside(ink, plain)
    offsheet = {t for t in reached if black.get(t, 0) >= MIN_FILL}
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


def reaching(mask: np.ndarray, sides) -> np.ndarray:
    """The parts of the mask that run off the tile past one of `sides`.

    Off-sheet fill continues into the tile the walk came from, so on a chart
    tile it always meets the border facing that tile. A place name set near the
    sheet edge does not, and that is the whole difference between them once the
    grid has narrowed the question to one tile.
    """
    labels, n = ndimage.label(mask)
    if not n:
        return mask
    keep = set()
    for s in sides:
        keep.update(np.unique(labels[BORDER[s]]).tolist())
    keep.discard(0)
    return np.isin(labels, list(keep))


def nodata_mask(a: np.ndarray, radius: int, protect: bool = True, sides=None) -> np.ndarray:
    """Pixels belonging to a solid opaque-black region rather than to chart ink.

    `protect` buys ink safety with a little unremoved fill, so it is for tiles
    that carry chart. On a tile the walk placed wholly outside the last sheet
    there is no ink to protect, and the opening would only strand the fill in
    corners too narrow to survive it.

    `sides` names the tile borders the off-sheet region lies past, and confines
    the result to black that reaches one of them. Without it the local shape
    test is all that stands between a heavy place name and deletion, which is
    not enough: Traficom's capitals seed the erosion exactly as a fill does.
    """
    black = (a[..., :3].max(axis=2) == 0) & (a[..., 3] == 255)
    if not black.any():
        return black
    if not protect:
        return black
    k = np.ones((2 * radius + 1, 2 * radius + 1), bool)
    seeds = ndimage.binary_erosion(black, structure=k)
    if not seeds.any():
        return np.zeros_like(black)
    # Grown back through the solid part of the black, not through all of it.
    # Reconstruction follows connectivity, and at a sheet edge the chart's own
    # ink abuts the fill, so propagating through every black pixel runs down
    # those strokes and takes them too. An opening drops anything thinner than
    # ink while leaving a straight boundary exactly where it was, which is what
    # the fill has -- the source anti-aliases it not at all.
    solid = ndimage.binary_opening(black, structure=np.ones((2 * THIN + 1,) * 2, bool))
    grown = ndimage.binary_propagation(seeds, mask=solid)
    return reaching(grown, sides) if sides else grown


def _strip(task):
    z, x, row, blob, radius, sides = task
    img = Image.open(io.BytesIO(blob)).convert("RGBA")
    a = np.asarray(img).copy()
    m = nodata_mask(a, radius, protect=bool(sides), sides=sides)
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


def scan(src: Path, radius: int, jobs: int) -> None:
    """What a run would examine, by the same reckoning the run uses.

    Answering this with the local shape test alone -- as a dry run naturally
    wants to, having no output to survey against -- would count every interior
    tile whose type is thick enough to seed the erosion, which is exactly the
    set the run exists not to touch.
    """
    con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    zs = [z for (z,) in con.execute("SELECT DISTINCT zoom_level FROM tiles ORDER BY zoom_level")]
    print(f"=== {src.name} ===")
    total = 0
    with ProcessPoolExecutor(max_workers=jobs) as pool:
        for z in zs:
            ink, plain, black = survey(con, z, pool)
            offsheet, straddling = edge_tiles(ink, plain, black)
            seen = len(ink) + len(plain)
            if offsheet or straddling:
                px = sum(black.get(t, 0) for t in offsheet)
                print(f"  z{z:<3} {len(offsheet):>6} off-sheet and {len(straddling):>5} "
                      f"straddling of {seen:>7} tiles, {px / 65536:.0f} tiles' worth "
                      f"of fill beyond the sheets")
            total += len(offsheet) + len(straddling)
    print(f"  {total} tiles would be examined")
    con.close()


def run(src: Path, out: Path, radius: int, jobs: int, offeez: bool) -> None:
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
    rewritten = dropped = 0
    with ProcessPoolExecutor(max_workers=jobs) as pool:
        for z in zs:
            zr = zd = 0
            ink, plain, black = survey(con, z, pool)
            offsheet, straddling = edge_tiles(ink, plain, black)
            candidates = offsheet | straddling.keys()
            leaked(black, candidates, z)
            todo = sorted(((x, (1 << z) - 1 - y), straddling.get((x, y)))
                          for (x, y) in candidates)
            if todo:
                print(f"  z{z:<3} {len(offsheet)} off-sheet, {len(straddling)} straddling "
                      f"of {len(ink) + len(plain)} tiles")
            for i in range(0, len(todo), BATCH):
                tasks = []
                for (x, row), sides in todo[i:i + BATCH]:
                    blob = con.execute("SELECT tile_data FROM tiles WHERE zoom_level=? AND "
                                       "tile_column=? AND tile_row=?", (z, x, row)).fetchone()
                    tasks.append((z, x, row, blob[0], radius, sides))
                for res in pool.map(_strip, tasks, chunksize=32):
                    if res is None:
                        continue
                    rz, rx, rrow, data, _ = res
                    if data is None:
                        con.execute("DELETE FROM tiles WHERE zoom_level=? AND tile_column=? "
                                    "AND tile_row=?", (rz, rx, rrow))
                        zd += 1
                    else:
                        con.execute("UPDATE tiles SET tile_data=? WHERE zoom_level=? AND "
                                    "tile_column=? AND tile_row=?",
                                    (sqlite3.Binary(data), rz, rx, rrow))
                        zr += 1
                con.commit()
            rewritten += zr
            dropped += zd
            if zr or zd:
                print(f"  z{z:<3} rewrote {zr:>6}, dropped {zd:>5}")

    stamp = f"opaque-black-r{radius}-edge"
    if offeez:
        # classified from the black-stripped tiles, so removed fill cannot pose
        # as a marking and wall the flood out of its own region
        print("  planning off-EEZ removal across all zooms ...")
        plan, drops = offsheet_plan(con, zs)
        for z in zs:
            dis = len(drops[z]) - len(plan[z])
            if drops[z]:
                print(f"  z{z:<3} off-EEZ: {len(drops[z]):>6} flood-reachable, "
                      f"{dis:>5} kept (deeper detail survives), {len(plan[z]):>6} to drop")
        total = 0
        for z in zs:
            for x, y in plan[z]:
                con.execute("DELETE FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
                            (z, x, (1 << z) - 1 - y))
                total += 1
            con.commit()
        print(f"  off-EEZ total: {total} tiles dropped, 0 modified")
        stamp += "+offeez-tilelevel"

    con.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", ("nodata_stripped", stamp))
    con.commit()
    con.execute("VACUUM")
    con.close()
    os.replace(tmp, out)
    print(f"done: {out}  ({rewritten} rewritten, {dropped} dropped)")


def main():
    p = argparse.ArgumentParser(description="Strip opaque-black no-data fill from MBTiles")
    p.add_argument("input", type=Path)
    p.add_argument("--out", type=Path, help="default: <input>.stripped.mbtiles")
    p.add_argument("--radius", type=int, default=RADIUS,
                   help=f"erosion radius separating fill from ink (default {RADIUS})")
    p.add_argument("--jobs", type=int, default=0, help="worker processes (default: all cores)")
    p.add_argument("--scan", action="store_true", help="report what would change, write nothing")
    p.add_argument("--skip-offeez", action="store_true",
                   help="leave the blank white beyond the chart limits in place")
    args = p.parse_args()
    if not args.input.exists():
        sys.exit(f"no such file: {args.input}")
    if args.scan:
        scan(args.input, args.radius, args.jobs or None)
        return
    out = args.out or args.input.with_suffix(".stripped.mbtiles")
    run(args.input, out, args.radius, args.jobs or None, not args.skip_offeez)


if __name__ == "__main__":
    main()
