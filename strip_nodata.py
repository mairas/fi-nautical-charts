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
to 5% of an ordinary tile. Shape can. The no-data fill is a solid region tens of
pixels across; ink is strokes a few pixels wide. Eroding the black mask leaves
seeds only inside the fill, and growing those seeds back through the mask
recovers the region exactly to its own hard edge -- which is genuinely hard, the
source anti-aliases the boundary not at all.

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


def nodata_mask(a: np.ndarray, radius: int) -> np.ndarray:
    """Pixels belonging to a solid opaque-black region rather than to chart ink."""
    black = (a[..., :3].max(axis=2) == 0) & (a[..., 3] == 255)
    if not black.any():
        return black
    k = np.ones((2 * radius + 1, 2 * radius + 1), bool)
    seeds = ndimage.binary_erosion(black, structure=k)
    if not seeds.any():
        return np.zeros_like(black)
    return ndimage.binary_propagation(seeds, mask=black)


def _strip(task):
    z, x, row, blob, radius = task
    img = Image.open(io.BytesIO(blob)).convert("RGBA")
    a = np.asarray(img).copy()
    m = nodata_mask(a, radius)
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


def scan(src: Path, radius: int) -> None:
    con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    zs = [z for (z,) in con.execute("SELECT DISTINCT zoom_level FROM tiles ORDER BY zoom_level")]
    print(f"=== {src.name} ===")
    total = 0
    for z in zs:
        hit = px = seen = 0
        for x, row, blob in con.execute("SELECT tile_column, tile_row, tile_data FROM tiles "
                                        "WHERE zoom_level=?", (z,)):
            seen += 1
            a = np.asarray(Image.open(io.BytesIO(blob)).convert("RGBA"))
            n = int(nodata_mask(a, radius).sum())
            if n >= MIN_FILL:
                hit += 1
                px += n
        total += hit
        if hit:
            print(f"  z{z:<3} {hit:>6} of {seen:>7} tiles carry no-data fill "
                  f"({100 * hit / seen:5.1f}%), {px / 65536:.0f} tiles' worth of pixels")
    print(f"  {total} tiles would be rewritten")
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
            # a batch at a time: the deepest zoom of a chart series runs to
            # hundreds of thousands of tiles, and holding every blob -- once for
            # the read and again for the task list -- outgrows a small machine
            for rows in batches(con, z, BATCH):
                tasks = [(z, x, row, blob, radius) for x, row, blob in rows]
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

    stamp = f"opaque-black-r{radius}"
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
        scan(args.input, args.radius)
        return
    out = args.out or args.input.with_suffix(".stripped.mbtiles")
    run(args.input, out, args.radius, args.jobs or None, not args.skip_offeez)


if __name__ == "__main__":
    main()
