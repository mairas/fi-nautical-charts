#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pillow", "numpy"]
# ///
"""Estimate each layer's *native* max zoom -- the deepest level whose tiles carry
genuine new detail rather than an upscale of the level above it.

Traficom renders charts at a handful of native scale bands; above a layer's
native resolution the WMTS just upscales (often nearest-neighbour), so those
deep tiles look sharp-but-jagged while adding no information. Downscaling our own
pyramid only helps where the source level is genuine, so we need to know, per
layer and per region, where genuine detail actually stops.

Method: for a zoom transition z -> z+1, take each sampled child tile and compare
it against a nearest-neighbour and a bilinear upscale of its parent's quadrant. A
child pixel that matches *either* upscale (within tolerance) is "explained by the
parent" -- no new detail. The fraction explained by *neither*, over opaque
pixels, is the novelty. High novelty => z+1 is genuine; ~0 => z+1 is an upscale.
The deepest z+1 that is still genuine is the native max.

Reads MBTiles read-only. Never writes to the source files.
"""

from __future__ import annotations

import argparse
import io
import math
import sqlite3
import statistics
from pathlib import Path

import numpy as np
from PIL import Image

TILE = 256
DIFF_TOL = 8          # per-pixel max-channel diff below which a pixel "matches" an upscale
ALPHA_MIN = 8         # alpha above which a pixel counts as opaque content
MIN_CONTENT = 0.02    # skip child tiles with less opaque content than this fraction
GENUINE = 0.03        # median novelty above this -> transition adds genuine detail
UPSCALED = 0.01       # median novelty below this -> transition is an upscale


def deg2num(lon: float, lat: float, z: int) -> tuple[int, int]:
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    lat_r = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(lat_r)) / math.pi) / 2.0 * n)
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


def tms_row(y_xyz: int, z: int) -> int:
    return (2 ** z - 1) - y_xyz


def load_rgba(blob: bytes) -> np.ndarray:
    img = Image.open(io.BytesIO(blob)).convert("RGBA")
    if img.size != (TILE, TILE):
        img = img.resize((TILE, TILE), Image.NEAREST)
    return np.asarray(img, dtype=np.uint8)


def upscale_quadrant(parent: np.ndarray, qx: int, qy: int, resample: int) -> np.ndarray:
    quad = parent[qy * 128:qy * 128 + 128, qx * 128:qx * 128 + 128]
    return np.asarray(
        Image.fromarray(quad, "RGBA").resize((TILE, TILE), resample), dtype=np.uint8
    )


def novelty(child: np.ndarray, parent: np.ndarray, qx: int, qy: int) -> float | None:
    """Fraction of opaque child pixels not reproduced by any simple upscale of the
    parent quadrant. None if the child has too little content to judge."""
    opaque = child[..., 3] > ALPHA_MIN
    n_opaque = int(opaque.sum())
    if n_opaque < MIN_CONTENT * TILE * TILE:
        return None
    nn = upscale_quadrant(parent, qx, qy, Image.NEAREST)
    bl = upscale_quadrant(parent, qx, qy, Image.BILINEAR)
    d_nn = np.abs(child.astype(np.int16) - nn.astype(np.int16)).max(axis=2)
    d_bl = np.abs(child.astype(np.int16) - bl.astype(np.int16)).max(axis=2)
    explained = (d_nn <= DIFF_TOL) | (d_bl <= DIFF_TOL)
    novel = opaque & ~explained
    return int(novel.sum()) / n_opaque


def exact_nn_frac(child: np.ndarray, parent: np.ndarray, qx: int, qy: int) -> float:
    nn = upscale_quadrant(parent, qx, qy, Image.NEAREST)
    return float((child == nn).all(axis=2).mean())


def fetch(con: sqlite3.Connection, z: int, x: int, y_xyz: int) -> bytes | None:
    row = con.execute(
        "SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
        (z, x, tms_row(y_xyz, z)),
    ).fetchone()
    return row[0] if row else None


def sample_children(con, z_child, n, bbox):
    """Return up to n (x, y_xyz) child coords with content, optionally within bbox."""
    where = "zoom_level=?"
    params: list = [z_child]
    if bbox:
        lon0, lat0, lon1, lat1 = bbox
        x0, y0 = deg2num(lon0, lat1, z_child)   # NW corner: min lon, max lat -> min y
        x1, y1 = deg2num(lon1, lat0, z_child)   # SE corner: max lon, min lat -> max y
        r0, r1 = sorted((tms_row(y0, z_child), tms_row(y1, z_child)))
        where += " AND tile_column BETWEEN ? AND ? AND tile_row BETWEEN ? AND ?"
        params += [min(x0, x1), max(x0, x1), r0, r1]
    rows = con.execute(
        f"SELECT tile_column, tile_row FROM tiles WHERE {where} ORDER BY RANDOM() LIMIT ?",
        params + [n],
    ).fetchall()
    return [(c, tms_row(r, z_child)) for c, r in rows]


def verdict(median: float) -> str:
    if median >= GENUINE:
        return "genuine"
    if median < UPSCALED:
        return "upscaled"
    return "marginal"


def analyse(path: Path, zmin: int, zmax: int, sample: int, bbox, examples_dir: Path | None):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    have = {z for (z,) in con.execute("SELECT DISTINCT zoom_level FROM tiles")}
    lo, hi = max(zmin, min(have)), min(zmax, max(have))
    print(f"\n=== {path.name} ===  zooms {min(have)}..{max(have)}"
          + (f"  bbox={bbox}" if bbox else ""))
    print(f"{'z->z+1':>8} {'pairs':>6} {'novel_med':>10} {'novel_mean':>10} "
          f"{'novel_p90':>10} {'nn_exact':>9}  verdict")
    native = min(have)
    for z in range(lo, hi):
        zc = z + 1
        coords = sample_children(con, zc, sample, bbox)
        nov, exact = [], []
        examples: list = []
        for cx, cy in coords:
            cblob = fetch(con, zc, cx, cy)
            if not cblob:
                continue
            px, py, qx, qy = cx // 2, cy // 2, cx % 2, cy % 2
            pblob = fetch(con, z, px, py)
            if not pblob:
                continue
            child, parent = load_rgba(cblob), load_rgba(pblob)
            nv = novelty(child, parent, qx, qy)
            if nv is None:
                continue
            nov.append(nv)
            exact.append(exact_nn_frac(child, parent, qx, qy))
            if examples_dir is not None:
                examples.append((nv, child, parent, qx, qy, zc, cx, cy))
        if not nov:
            print(f"{z}->{zc:<5} {'0':>6}  (no scorable pairs)")
            continue
        med = statistics.median(nov)
        mean = statistics.fmean(nov)
        p90 = float(np.percentile(nov, 90))
        v = verdict(med)
        if v != "upscaled":
            native = zc
        print(f"{z}->{zc:<5} {len(nov):>6} {med:>10.3f} {mean:>10.3f} {p90:>10.3f} "
              f"{statistics.fmean(exact):>9.3f}  {v}")
        if examples_dir is not None and examples:
            dump_examples(examples_dir, path.stem, examples)
    print(f"  -> estimated native max zoom: {native}")
    con.close()
    return native


def dump_examples(out: Path, stem: str, examples: list):
    """Save the lowest- and highest-novelty pair per transition as a side-by-side
    strip: [nn-upscale(parent) | child | diff-heat]."""
    out.mkdir(parents=True, exist_ok=True)
    examples.sort(key=lambda e: e[0])
    for tag, (nv, child, parent, qx, qy, zc, cx, cy) in (("lo", examples[0]), ("hi", examples[-1])):
        nn = upscale_quadrant(parent, qx, qy, Image.NEAREST)
        diff = np.abs(child.astype(np.int16) - nn.astype(np.int16)).max(axis=2)
        heat = np.zeros((TILE, TILE, 4), np.uint8)
        heat[..., 0] = np.clip(diff * 4, 0, 255)
        heat[..., 3] = 255
        strip = np.concatenate([nn, child, heat], axis=1)
        name = f"{stem}_z{zc}_{tag}_nov{nv:.2f}_x{cx}_y{cy}.png"
        Image.fromarray(strip, "RGBA").save(out / name)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mbtiles", nargs="+", type=Path)
    ap.add_argument("--zmin", type=int, default=8)
    ap.add_argument("--zmax", type=int, default=15)
    ap.add_argument("--sample", type=int, default=300, help="child tiles per transition")
    ap.add_argument("--bbox", help="lon_min,lat_min,lon_max,lat_max region filter")
    ap.add_argument("--examples-dir", type=Path, help="dump lo/hi novelty example strips here")
    args = ap.parse_args()
    bbox = tuple(float(v) for v in args.bbox.split(",")) if args.bbox else None
    for p in args.mbtiles:
        analyse(p, args.zmin, args.zmax, args.sample, bbox, args.examples_dir)


if __name__ == "__main__":
    main()
