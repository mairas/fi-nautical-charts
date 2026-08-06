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
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

RADIUS = 4          # erosion radius; ink vanishes by 2, fills survive past 8
MIN_FILL = 64       # ignore specks: a real fill is far larger than this


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


def scan(src: Path, radius: int) -> None:
    con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    zs = [z for (z,) in con.execute("SELECT DISTINCT zoom_level FROM tiles ORDER BY zoom_level")]
    print(f"=== {src.name} ===")
    total = 0
    for z in zs:
        rows = con.execute("SELECT tile_column, tile_row, tile_data FROM tiles "
                           "WHERE zoom_level=?", (z,)).fetchall()
        hit = px = 0
        for x, row, blob in rows:
            a = np.asarray(Image.open(io.BytesIO(blob)).convert("RGBA"))
            n = int(nodata_mask(a, radius).sum())
            if n >= MIN_FILL:
                hit += 1
                px += n
        total += hit
        if hit:
            print(f"  z{z:<3} {hit:>6} of {len(rows):>7} tiles carry no-data fill "
                  f"({100 * hit / len(rows):5.1f}%), {px / 65536:.0f} tiles' worth of pixels")
    print(f"  {total} tiles would be rewritten")
    con.close()


def run(src: Path, out: Path, radius: int, jobs: int) -> None:
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
            rows = con.execute("SELECT tile_column, tile_row, tile_data FROM tiles "
                               "WHERE zoom_level=?", (z,)).fetchall()
            tasks = [(z, x, row, blob, radius) for x, row, blob in rows]
            zr = zd = 0
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
                                "tile_column=? AND tile_row=?", (sqlite3.Binary(data), rz, rx, rrow))
                    zr += 1
            con.commit()
            rewritten += zr
            dropped += zd
            if zr or zd:
                print(f"  z{z:<3} rewrote {zr:>6}, dropped {zd:>5}")

    con.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)",
                ("nodata_stripped", f"opaque-black-r{radius}"))
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
    args = p.parse_args()
    if not args.input.exists():
        sys.exit(f"no such file: {args.input}")
    if args.scan:
        scan(args.input, args.radius)
        return
    out = args.out or args.input.with_suffix(".stripped.mbtiles")
    run(args.input, out, args.radius, args.jobs or None)


if __name__ == "__main__":
    main()
