#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pillow", "numpy"]
# ///
"""Regenerate an MBTiles pyramid's lower zoom levels by properly anti-aliased
downscaling from the highest available level.

Traficom serves each chart's lower zoom levels as crude (often nearest-neighbour)
rescales of the native raster, so they look jagged. We can do better: take the
deepest level as the source of truth and rebuild every level below it with a
proper anti-aliasing reduction.

Method:
  - Cascade, one octave at a time (zmax -> zmax-1 -> ... -> zmin). Each level is
    built from the level just above it. Repeated exact-2x reduction is a standard
    mip pyramid and keeps every step well-conditioned.
  - Each output pixel is the average of the 2x2 block beneath it (box filter).
    For an exact 2x reduction that block lies entirely inside the tile's own four
    children, so adjacent output tiles are computed from consistent, continuous
    source data -- seam-free with no neighbour gutter needed. Box also avoids the
    ringing haloes a Lanczos kernel would add around the hard, high-contrast
    edges of chart line art.
  - Alpha is premultiplied before averaging so transparent off-sheet pixels do
    not bleed dark haloes into coastlines.
  - Sparse-aware: a parent tile is generated only where a child exists; no
    full-grid sweep.

Non-destructive: the source is opened read-only; results go to a new MBTiles
(source-zoom tiles copied verbatim, lower levels regenerated). Idempotent
input -> output, suitable for a CI build step.
"""

from __future__ import annotations

import argparse
import datetime
import io
import multiprocessing as mp
import os
import sqlite3
import sys
from pathlib import Path

import numpy as np
from PIL import Image

TILE = 256
CHUNK = 4000          # parents read + dispatched per batch (bounds memory)


def xyz_row(row_tms: int, z: int) -> int:
    return (2 ** z - 1) - row_tms


def decode(blob: bytes) -> np.ndarray:
    return np.asarray(Image.open(io.BytesIO(blob)).convert("RGBA"), dtype=np.float64)


def encode(arr: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(arr, "RGBA").save(buf, format="PNG")
    return buf.getvalue()


def box_downscale_2x(block: np.ndarray) -> np.ndarray:
    """block: (512,512,4) float RGBA -> (256,256,4) uint8, premultiplied-alpha box."""
    rgb, a = block[..., :3], block[..., 3:4] / 255.0
    pm = (rgb * a).reshape(TILE, 2, TILE, 2, 3).mean(axis=(1, 3))
    oa = a.reshape(TILE, 2, TILE, 2, 1).mean(axis=(1, 3))
    # Colour under a fully transparent pixel is undefined by alpha but not
    # unused: a renderer scaling the tile interpolates across the edge and pulls
    # it in, so substituting a constant paints a fringe along every chart border
    # in that colour. Carry the source's own colour through instead -- Traficom
    # writes white off-sheet, which is what the border should look like.
    plain = rgb.reshape(TILE, 2, TILE, 2, 3).mean(axis=(1, 3))
    with np.errstate(divide="ignore", invalid="ignore"):
        orgb = np.where(oa > 1e-6, pm / oa, plain)
    out = np.concatenate([np.clip(orgb, 0, 255), np.clip(oa * 255.0, 0, 255)], axis=2)
    return out.astype(np.uint8)


def scratch_path(path: Path) -> Path:
    """Where the build actually happens. The documented invocation writes over a
    deployed chart, and a run that dies partway would otherwise leave a
    syntactically valid one-level file at that path with no previous version
    behind it."""
    return path.with_suffix(path.suffix + ".partial")


def create_output(path: Path) -> sqlite3.Connection:
    if path.exists():
        path.unlink()
    con = sqlite3.connect(str(path), uri=True)
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")
    con.execute("CREATE TABLE metadata (name TEXT PRIMARY KEY, value TEXT)")
    con.execute("CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, "
                "tile_row INTEGER, tile_data BLOB)")
    con.execute("CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row)")
    return con


def _compute(task):
    """Worker side: decode children, box-downscale, encode. Pure CPU, no DB."""
    z, px, prow, children = task
    block = np.zeros((512, 512, 4), dtype=np.float64)
    # A quadrant with no child tile is off-sheet, which these charts render
    # white. Leaving it black would be invisible under alpha until the renderer
    # interpolates across the edge, and then it is a dark fringe.
    block[..., :3] = 255.0
    for dx, dy, blob in children:
        block[dy * TILE:dy * TILE + TILE, dx * TILE:dx * TILE + TILE] = decode(blob)
    out = box_downscale_2x(block)
    if int(out[..., 3].max()) == 0:
        return None
    return (z, px, prow, encode(out))


def regen_level(con: sqlite3.Connection, z: int, pool) -> int:
    """Build level z from its children at z+1 (already present in con).

    All SQLite I/O stays in this (main) process; the per-tile image work is
    fanned out to worker processes since every parent tile is independent.
    """
    cz = z + 1
    n1 = 2 ** cz
    parents = set()
    for cx, crow in con.execute("SELECT tile_column, tile_row FROM tiles WHERE zoom_level=?", (cz,)):
        parents.add((cx // 2, xyz_row(crow, cz) // 2))
    parents = list(parents)

    cur = con.cursor()

    def read_task(px, pyx):
        rows = ((n1 - 1) - 2 * pyx, (n1 - 1) - (2 * pyx + 1))
        got = cur.execute(
            "SELECT tile_column, tile_row, tile_data FROM tiles "
            "WHERE zoom_level=? AND tile_column IN (?,?) AND tile_row IN (?,?)",
            (cz, 2 * px, 2 * px + 1, rows[0], rows[1]),
        ).fetchall()
        if not got:
            return None
        children = [(cx - 2 * px, xyz_row(crow, cz) - 2 * pyx, blob) for cx, crow, blob in got]
        return (z, px, (2 ** z - 1) - pyx, children)

    made = 0
    for i in range(0, len(parents), CHUNK):
        tasks = [t for t in (read_task(px, pyx) for px, pyx in parents[i:i + CHUNK]) if t]
        if not tasks:
            continue
        results = pool.map(_compute, tasks, chunksize=16) if pool else [_compute(t) for t in tasks]
        batch = [r for r in results if r is not None]
        con.executemany("INSERT OR REPLACE INTO tiles VALUES (?,?,?,?)", batch)
        con.commit()
        made += len(batch)
    return made


def floor_zoom(zooms: list[int], meta: dict[str, str]) -> int:
    """How far down the pyramid should reach when nobody says.

    Not simply the lowest level present: strip_nodata cleans the deepest level
    and deletes the rest, since each of the others is a separate rendering of
    the same coastline carrying its own fill. What it leaves behind is one level
    of tiles and a metadata minzoom describing the chart it is going to become.
    """
    try:
        return min(min(zooms), int(meta["minzoom"]))
    except (KeyError, ValueError):
        return min(zooms)


def build(inp: Path, out: Path, source_zoom: int | None, min_zoom: int | None, jobs: int):
    src = sqlite3.connect(f"file:{inp}?mode=ro", uri=True)
    zooms = sorted(z for (z,) in src.execute("SELECT DISTINCT zoom_level FROM tiles"))
    meta = dict(src.execute("SELECT name, value FROM metadata"))
    src.close()
    if not zooms:
        sys.exit(f"{inp}: no tiles")
    szoom = source_zoom if source_zoom is not None else max(zooms)
    mzoom = min_zoom if min_zoom is not None else floor_zoom(zooms, meta)
    if mzoom >= szoom:
        sys.exit(f"nothing to do: min-zoom {mzoom} >= source-zoom {szoom}")

    print(f"{inp.name}: source z{szoom}, regenerate z{szoom - 1}..z{mzoom} -> {out.name}")
    tmp = scratch_path(out)
    con = create_output(tmp)
    con.execute("ATTACH DATABASE ? AS src", (f"file:{inp}?mode=ro",))
    con.execute("INSERT INTO tiles SELECT * FROM src.tiles WHERE zoom_level>=? OR zoom_level<?",
                (szoom, mzoom))
    con.execute("INSERT INTO metadata SELECT * FROM src.metadata")
    con.commit()
    kept = con.execute("SELECT COUNT(*) FROM tiles WHERE zoom_level=?", (szoom,)).fetchone()[0]
    print(f"  copied {kept} source tiles at z{szoom}  ({jobs} worker{'s' if jobs != 1 else ''})")

    pool = mp.Pool(jobs) if jobs > 1 else None
    try:
        for z in range(szoom - 1, mzoom - 1, -1):
            made = regen_level(con, z, pool)
            print(f"  z{z}: {made} tiles")
    finally:
        if pool:
            pool.close()
            pool.join()

    # Non-regression: keep the source's original tile wherever we produced none
    # (multiscale content that exists at a mid zoom but not at the source level).
    before = con.execute("SELECT COUNT(*) FROM tiles WHERE zoom_level>=? AND zoom_level<?",
                         (mzoom, szoom)).fetchone()[0]
    con.execute("INSERT OR IGNORE INTO tiles SELECT * FROM src.tiles "
                "WHERE zoom_level>=? AND zoom_level<?", (mzoom, szoom))
    filled = con.execute("SELECT COUNT(*) FROM tiles WHERE zoom_level>=? AND zoom_level<?",
                        (mzoom, szoom)).fetchone()[0] - before
    con.commit()
    con.execute("DETACH DATABASE src")
    if filled:
        print(f"  kept {filled} original tiles where no source existed to downscale")

    present = sorted(z for (z,) in con.execute("SELECT DISTINCT zoom_level FROM tiles"))
    meta = {"minzoom": str(min(present)), "maxzoom": str(max(present)),
            "downscaled": datetime.date.today().isoformat(),
            "downscale_source_zoom": str(szoom), "downscale_filter": "box-2x-premultiplied"}
    for k, v in meta.items():
        con.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (k, v))
    con.commit()
    con.execute("VACUUM")
    con.commit()
    con.close()
    os.replace(tmp, out)
    print(f"done: {out}")


def main():
    ap = argparse.ArgumentParser(description="Regenerate MBTiles lower zooms by anti-aliased downscaling.")
    ap.add_argument("input", type=Path)
    ap.add_argument("--out", type=Path, help="output mbtiles (default: <input>.downscaled.mbtiles)")
    ap.add_argument("--source-zoom", type=int, help="level to downscale from (default: max in file)")
    ap.add_argument("--min-zoom", type=int, help="lowest level to regenerate (default: min in file)")
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 1,
                    help="worker processes (default: all cores)")
    args = ap.parse_args()
    out = args.out or args.input.with_suffix(".downscaled.mbtiles")
    build(args.input, out, args.source_zoom, args.min_zoom, max(1, args.jobs))


if __name__ == "__main__":
    main()
