#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["requests", "pillow", "numpy"]
# ///
"""Check `--mode coverage` against the `full` oracle, and the footprint against itself.

Two jobs, because coverage mode can be wrong in two unrelated ways.

  oracle  Download the same area twice, once with `full` (brute-force enumeration
          of the zoom's rectangle) and once with `coverage`, and diff the content
          tiles. A tile `full` stored and `coverage` did not is a hole in the
          footprint. This is the discipline that caught `descent` losing every
          z15 tile at Vanoe and `mask` losing 29 at Helsinki.

  mask    Two properties the oracle cannot see. Chunking: a footprint assembled
          from several column chunks must equal one assembled from a single
          chunk, or the row/column indices are transposed somewhere. Containment:
          the footprint built at a deeper zoom -- a finer pixel grid, different
          chunk boundaries, 16x the samples per cell -- must project up into the
          shipped one, or the rasterisation is dropping cells the dilation cannot
          restore.

Boxes are named so a result stays re-runnable. Their value depends on where they
sit relative to the footprint: a box wholly inside tests that coverage keeps what
full finds, a straddling box tests that pruning does not cut real chart area, and
a box wholly outside tests that pruning happens at all.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

import traficom_dl as T

HERE = Path(__file__).resolve().parent

# bbox strings, minlon,minlat,maxlon,maxlat. The tags record each box's measured
# relation to the footprint at the default z11/16x parameters -- `mask` reports it,
# so a source change that moves a box between classes shows up rather than quietly
# weakening the check.
BOXES = {
    # inside: coverage must keep everything full finds
    "helsinki":  ("24.90,60.09,25.06,60.17",   "inside",    "dense urban water; the box mask mode failed"),
    "vano":      ("21.85,59.78,22.10,59.92",   "inside",    "archipelago; the box descent failed on raster"),
    "saimaa":    ("28.10,61.00,28.45,61.20",   "inside",    "inland waterway, Coastal band with no General above"),
    "bogskar":   ("20.20,59.42,20.50,59.58",   "inside",    "isolated outer skerry"),
    # straddling: pruning must not cut real chart area
    "diagonal":  ("22.6758,64.6991,23.3789,64.9979", "straddle", "Bothnian Bay, boundary crosses cells diagonally"),
    "diagonal2": ("29.5312,66.0894,30.2344,66.3728", "straddle", "eastern border, boundary crosses cells diagonally"),
    # 2x2 z11 windows cut from the two boundary regions above, small enough that
    # brute-force enumeration to z16 stays affordable
    "diagonal-deep":  ("22.8516,64.7741,23.2031,64.9235", "straddle", "Bothnian Bay boundary, deep-zoom window"),
    "diagonal2-deep": ("29.5312,66.2315,29.8828,66.3728", "straddle", "eastern border boundary, deep-zoom window"),
    # outside: pruning must reduce these to zero requests
    "lapland":   ("25.50,67.50,25.80,67.65",   "outside",   "inland, no navigable water"),
    "baltic":    ("20.00,58.30,20.40,58.60",   "outside",   "open sea inside the extent, south of the footprint"),
}


def tiles_at(con, z):
    """Content tiles stored for a zoom, as XYZ (x, y). MBTiles rows are TMS."""
    return {(c, (1 << z) - 1 - r)
            for c, r in con.execute(
                "SELECT tile_column, tile_row FROM tiles WHERE zoom_level=?", (z,))}


def run_dl(mode, out, bbox, minzoom, maxzoom):
    cmd = ["uv", "run", str(HERE / "traficom_dl.py"), "--source", "wms",
           "--mode", mode, "--out", str(out),
           "--minzoom", str(minzoom), "--maxzoom", str(maxzoom)]
    if bbox:
        cmd += ["--bbox", bbox]
    t0 = time.monotonic()
    # streamed rather than captured: an oracle run over a deep box is tens of
    # thousands of requests, and the child's progress line is the only sign it
    # is alive. It rewrites one line with \r, so read chunks, not lines.
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)
    out = []
    while chunk := proc.stdout.read(256):
        out.append(chunk)
        sys.stdout.write(chunk)
        sys.stdout.flush()
    if proc.wait() != 0:
        sys.exit(f"\n{mode} run failed ({proc.returncode})")
    text = "".join(out)
    num = lambda pat: (int(m.group(1).replace(",", ""))
                       if (m := re.search(pat, text)) else 0)
    # a run that gave up on tiles has holes, and a hole in the oracle reads as
    # agreement -- the comparison is only meaningful if both sides are complete
    return {"requested": num(r"requested ([\d,]+)"),
            "errors": num(r"unrecovered errors ([\d,]+)"),
            "secs": time.monotonic() - t0}


def db_stats(path):
    con = sqlite3.connect(path)
    zs = [z for (z,) in con.execute(
        "SELECT DISTINCT zoom_level FROM tiles ORDER BY zoom_level")]
    per_zoom = {z: tiles_at(con, z) for z in zs}
    nbytes = con.execute("SELECT coalesce(sum(length(tile_data)), 0) FROM tiles").fetchone()[0]
    con.close()
    return per_zoom, nbytes


def oracle(args):
    if args.box:
        if args.box not in BOXES:
            sys.exit(f"unknown box {args.box}; known: {', '.join(BOXES)}")
        bbox, klass, note = BOXES[args.box]
        label = f"{args.box} ({klass}: {note})"
    else:
        bbox, klass = args.bbox, "custom"
        label = bbox or "whole extent"

    print(f"oracle: {label}")
    print(f"  bbox {bbox or '(none -- whole WMS_EXTENT)'}  z{args.minzoom}-{args.maxzoom}")

    with tempfile.TemporaryDirectory() as td:
        full_db, cov_db = Path(td) / "full.mbtiles", Path(td) / "cov.mbtiles"
        print("  running full (oracle) ...")
        fstat = run_dl("full", full_db, bbox, args.minzoom, args.maxzoom)
        print("  running coverage ...")
        cstat = run_dl("coverage", cov_db, bbox, args.minzoom, args.maxzoom)
        fz, fbytes = db_stats(full_db)
        cz, cbytes = db_stats(cov_db)

    zooms = range(args.minzoom, args.maxzoom + 1)
    missed_total = extra_total = 0
    print(f"\n  {'zoom':>5} {'full':>8} {'coverage':>9} {'missed':>8} {'extra':>7}")
    for z in zooms:
        f, c = fz.get(z, set()), cz.get(z, set())
        missed, extra = f - c, c - f
        missed_total += len(missed)
        extra_total += len(extra)
        if f or c:
            flag = "  <-- MISSED" if missed else ""
            print(f"  {z:>5} {len(f):>8,} {len(c):>9,} {len(missed):>8,} {len(extra):>7,}{flag}")
        if missed and args.show:
            for x, y in sorted(missed)[:args.show]:
                print(f"        z{z} ({x},{y})  "
                      f"{T.x2lon(x, z):.5f},{T.y2lat(y + 1, z):.5f}")

    saved = (1 - cstat["requested"] / fstat["requested"]) * 100 if fstat["requested"] else 0
    print(f"\n  requests: full {fstat['requested']:,} ({fstat['secs']:.0f}s), "
          f"coverage {cstat['requested']:,} ({cstat['secs']:.0f}s) -> {saved:.1f}% fewer")
    print(f"  content:  full {sum(len(s) for s in fz.values()):,} tiles / {fbytes/1e6:.1f} MB, "
          f"coverage {sum(len(s) for s in cz.values()):,} tiles / {cbytes/1e6:.1f} MB")

    if fstat["errors"] or cstat["errors"]:
        print(f"\n  FAIL: unrecovered errors (full {fstat['errors']:,}, "
              f"coverage {cstat['errors']:,}); both sides must be complete for "
              f"the diff to mean anything")
        return 1
    if klass == "outside" and cstat["requested"] != 0:
        print(f"\n  FAIL: an outside box must prune to zero requests, "
              f"got {cstat['requested']:,}")
        return 1
    if extra_total:
        print(f"\n  FAIL: coverage stored {extra_total:,} tiles full did not; "
              f"coverage candidates must be a subset of full's")
        return 1
    if missed_total:
        print(f"\n  FAIL: coverage missed {missed_total:,} content tile(s)")
        return 1
    print(f"\n  PASS: 0 content tiles missed")
    return 0


def raw_grid(zoom, over, max_px):
    """The footprint before dilation, as a (rows, cols) bool array over the extent
    rectangle. Rendered here rather than taken from build_coverage so the chunk
    geometry can be varied independently of the shipped constant."""
    cmin, cmax, rmin, rmax = T.extent_rect(T.WMS_EXTENT, zoom)
    cols, rows = cmax - cmin + 1, rmax - rmin + 1
    grid = np.zeros((rows, cols), bool)
    step = max(1, max_px // over)
    chunks = [(c0, r0) for r0 in range(0, rows, step) for c0 in range(0, cols, step)]
    for i, (c0, r0) in enumerate(chunks, 1):
        nc, nr = min(step, cols - c0), min(step, rows - r0)
        url = T.wms_url(T.WMS_COVERAGE_LAYER,
                        T.tile_bbox(zoom, cmin + c0, rmin + r0, nc, nr),
                        nc * over, nr * over)
        grid[r0:r0 + nr, c0:c0 + nc] = T.coverage_chunk(url, nc, nr, over)
        print(f"\r    chunk {i}/{len(chunks)}", end="", flush=True)
    print(f"   ({len(chunks)} chunks, {grid.sum():,} cells)")
    return grid, (cmin, rmin)


def mask(args):
    fails = 0
    z, over = args.coverage_zoom, args.coverage_oversample

    print(f"chunking: same parameters (z{z}, {over}x), different chunk geometry")
    cols = T.extent_rect(T.WMS_EXTENT, z)[1] - T.extent_rect(T.WMS_EXTENT, z)[0] + 1
    wide, narrow = T.WMS_MAX_PX, 512
    print(f"  extent is {cols} columns; step {wide // over} spans it in one chunk, "
          f"step {narrow // over} needs several")
    a, _ = raw_grid(z, over, wide)
    b, _ = raw_grid(z, over, narrow)
    if np.array_equal(a, b):
        print(f"  PASS: identical, {a.sum():,} cells either way")
    else:
        d = int((a ^ b).sum())
        print(f"  FAIL: {d:,} cells differ between chunk geometries")
        fails += 1

    print(f"\ncontainment: footprint at z{args.fine_zoom} projected up into the "
          f"shipped z{z} mask")
    shipped = T.build_coverage(T.WMS_EXTENT, z, over)
    fine = T.build_coverage(T.WMS_EXTENT, args.fine_zoom, over)
    s = args.fine_zoom - z
    projected = {(x >> s, y >> s) for (x, y) in fine}
    outside = projected - shipped
    per_cell = (1 << s) ** 2 * over * over
    print(f"  shipped {len(shipped):,} cells, fine {len(fine):,} cells -> "
          f"{len(projected):,} projected ({per_cell:,} samples per z{z} cell "
          f"vs {over * over:,})")
    if outside:
        print(f"  FAIL: {len(outside):,} projected cell(s) outside the shipped mask")
        for x, y in sorted(outside)[:20]:
            print(f"    z{z} ({x},{y})  {T.x2lon(x, z):.5f},{T.y2lat(y + 1, z):.5f}")
        fails += 1
    else:
        print(f"  PASS: every projected cell is inside the shipped mask")

    print("\nboxes, against the shipped mask:")
    for name, (bbox, klass, _) in BOXES.items():
        minlon, minlat, maxlon, maxlat = (float(v) for v in bbox.split(","))
        cells = [(x, y)
                 for y in range(T.lat2y(maxlat, z), T.lat2y(minlat, z) + 1)
                 for x in range(T.lon2x(minlon, z), T.lon2x(maxlon, z) + 1)]
        n = sum(1 for c in cells if c in shipped)
        actual = "inside" if n == len(cells) else "outside" if n == 0 else "straddle"
        ok = "ok" if actual == klass else f"CHANGED, tagged {klass}"
        print(f"  {name:11} {n:>3}/{len(cells):<3} in mask  {actual:8} {ok}")
        if actual != klass:
            fails += 1
    return 1 if fails else 0


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    o = sub.add_parser("oracle", help="diff coverage against the full oracle")
    g = o.add_mutually_exclusive_group(required=True)
    g.add_argument("--box", help=f"named box: {', '.join(BOXES)}")
    g.add_argument("--bbox", help="minlon,minlat,maxlon,maxlat")
    g.add_argument("--whole-extent", action="store_true",
                   help="every tile in WMS_EXTENT -- only sane at a shallow zoom")
    o.add_argument("--minzoom", type=int, default=0)
    o.add_argument("--maxzoom", type=int, default=16)
    o.add_argument("--show", type=int, default=10,
                   help="list up to this many missed tiles per zoom")

    m = sub.add_parser("mask", help="footprint self-checks")
    m.add_argument("--coverage-zoom", type=int, default=11)
    m.add_argument("--coverage-oversample", type=int, default=16)
    m.add_argument("--fine-zoom", type=int, default=13,
                   help="zoom for the independent, finer-geometry build")

    args = p.parse_args()
    if args.cmd == "oracle":
        if getattr(args, "whole_extent", False):
            args.bbox, args.box = None, None
        sys.exit(oracle(args))
    sys.exit(mask(args))


if __name__ == "__main__":
    main()
