#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["requests", "pillow", "numpy"]
# ///
"""Check `--mode coverage` against the `full` oracle, and the footprint against itself.

Coverage mode can be wrong in two unrelated ways, and its price is a third
question, so there are three subcommands. The first two assert and can fail; the
last only measures, so its exit code says nothing about correctness.

  oracle  Download the same area twice, once with `full` (brute-force enumeration
          of the zoom's rectangle) and once with `coverage`, and diff the content
          tiles. A tile `full` stored and `coverage` did not is a hole in the
          footprint.

  mask    Two properties the oracle cannot see. Chunking: a footprint assembled
          from several column chunks must equal one assembled from a single
          chunk, or the row/column indices are transposed somewhere. Containment:
          the footprint built at a deeper zoom -- a finer pixel grid, different
          chunk boundaries, 16x the samples per cell -- must project up into the
          shipped one, or the rasterisation is dropping cells the dilation cannot
          restore.

  cost    What a full run would fetch and store, sampled over the footprint.

Boxes are named so a result stays re-runnable. Their value depends on where they
sit relative to the footprint: a box wholly inside tests that coverage keeps what
full finds, a straddling box tests that pruning does not cut real chart area, and
a box wholly outside tests that pruning happens at all. A box can therefore pass
while testing nothing, so each is tagged with the relation it is meant to have
and the run fails if it does not hold.
"""

from __future__ import annotations

import argparse
import random
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

import traficom_dl as T

HERE = Path(__file__).resolve().parent

# taken from the downloader so the harness cannot drift into verifying a mask,
# or a zoom range, that the downloader no longer produces
_DL_DEFAULTS = {a.dest: a.default for a in T.build_parser()._actions}
CZOOM, COVER = _DL_DEFAULTS["coverage_zoom"], _DL_DEFAULTS["coverage_oversample"]
DLZOOM = _DL_DEFAULTS["maxzoom"]

# bbox strings, minlon,minlat,maxlon,maxlat. The tags record each box's measured
# relation to the footprint at the default z11/16x parameters -- `mask` reports it,
# so a source change that moves a box between classes shows up rather than quietly
# weakening the check.
BOXES = {
    # inside: no pruning happens, so these test that coverage keeps what full
    # finds -- containment, not the mask boundary
    "helsinki":  ("24.90,60.09,25.06,60.17",   "inside",    "dense urban water"),
    "vano":      ("21.85,59.78,22.10,59.92",   "inside",    "archipelago, content resumes below a placeholder band"),
    "saimaa":    ("28.10,61.00,28.45,61.20",   "inside",    "inland waterway, Coastal band with no General above"),
    "bogskar":   ("20.20,59.42,20.50,59.58",   "inside",    "isolated outer skerry"),
    # straddling: these reach from charted cells, through the dilation ring, to
    # cells outside the mask, so full finds content on one side while coverage
    # prunes the other. That pairing is the only one that can expose a bad prune.
    # A narrower window sitting on the boundary also prunes, but everything it
    # keeps is dilation ring, which is empty by construction -- it shows only
    # that pruning loses nothing where there was nothing to lose. One per edge,
    # since each borders a different neighbour and the coverage may end
    # differently against each.
    "span-kemi":   ("23.5547,65.8028,24.2578,65.9465", "straddle", "Bothnian Bay coast, north edge"),
    "span-border": ("29.8828,65.8028,30.5859,65.9465", "straddle", "eastern border, toward Russia"),
    "span-coast":  ("21.7969,64.3209,22.5000,64.4728", "straddle", "Ostrobothnian coast, west edge"),
    "span-aland":  ("18.6328,60.5870,19.3359,60.7592", "straddle", "outer Aland, toward Sweden"),
    "span-south":  ("27.0703,59.9770,27.4219,60.3269", "straddle", "Gulf of Finland, south edge"),
    # outside: pruning must reduce these to zero requests
    "lapland":   ("25.50,67.50,25.80,67.65",   "outside",   "inland, no navigable water"),
    "baltic":    ("20.00,58.30,20.40,58.60",   "outside",   "open sea inside the extent, south of the footprint"),
}


def tiles_at(con, z):
    """Content tiles stored for a zoom, as XYZ (x, y). MBTiles rows are TMS."""
    return {(c, (1 << z) - 1 - r)
            for c, r in con.execute(
                "SELECT tile_column, tile_row FROM tiles WHERE zoom_level=?", (z,))}


def run_dl(mode, out, bbox, args):
    cmd = ["uv", "run", str(HERE / "traficom_dl.py"), "--source", "wms",
           "--mode", mode, "--out", str(out),
           "--minzoom", str(args.minzoom), "--maxzoom", str(args.maxzoom),
           # forwarded so the oracle can grade a mask other than the default one;
           # otherwise the only check with ground truth is stuck at 11/16x while
           # the CLI advertises 6..14
           "--coverage-zoom", str(args.coverage_zoom),
           "--coverage-oversample", str(args.coverage_oversample)]
    if bbox:
        cmd += ["--bbox", bbox]
    t0 = time.monotonic()
    # read1 returns whatever has arrived rather than blocking for a full buffer,
    # so the child's progress line surfaces as it is written. It rewrites one
    # line with \r, so read chunks, not lines.
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    chunks = []
    while raw := proc.stdout.read1(4096):
        text = raw.decode("utf-8", "replace")
        chunks.append(text)
        sys.stdout.write(text)
        sys.stdout.flush()
    if proc.wait() != 0:
        sys.exit(f"\n{mode} run failed ({proc.returncode})")
    text = "".join(chunks)

    def num(pat):
        # no default: a missing counter would read as zero, and zero is the
        # passing value for both the completeness gate and the outside-box
        # assertion, so an unreadable summary would grade itself clean
        m = re.search(pat, text)
        if not m:
            sys.exit(f"\ncould not read '{pat}' from the {mode} run's summary; "
                     f"refusing to grade output that cannot be parsed")
        return int(m.group(1).replace(",", ""))

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
        fstat = run_dl("full", full_db, bbox, args)
        print("  running coverage ...")
        cstat = run_dl("coverage", cov_db, bbox, args)
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
    # Agreeing on nothing is not agreement. A service rendering transparent
    # everywhere -- wrong style id, renamed layer, outage -- makes both sides
    # store zero tiles and every difference vanish.
    content = sum(len(s) for s in fz.values())
    if klass != "outside" and content == 0:
        print(f"\n  FAIL: the oracle found no content anywhere, so there was "
              f"nothing for coverage to miss; check the source is rendering")
        return 1
    # The class assertions describe the footprint at the coverage zoom. Below it
    # eligibility comes from the projected ancestors, which at the coarsest zooms
    # span the whole extent -- an outside box cannot prune to zero there, and a
    # straddling one cannot prune at all.
    if args.minzoom < args.coverage_zoom:
        print(f"\n  note: class assertions skipped, they hold only from "
              f"z{args.coverage_zoom} down (--minzoom is {args.minzoom})")
    elif klass == "outside" and cstat["requested"] != 0:
        print(f"\n  FAIL: an outside box must prune to zero requests, "
              f"got {cstat['requested']:,}")
        return 1
    # A straddling box that prunes nothing passes for the wrong reason: coverage
    # enumerated the same tiles full did, so the diff proves only that they agree
    # where nothing was at stake. Dilation absorbs a window drawn around the raw
    # footprint edge, which is exactly how such a box gets mistagged.
    elif klass == "straddle" and cstat["requested"] >= fstat["requested"]:
        print(f"\n  FAIL: {args.box} is tagged straddle but pruned nothing "
              f"({cstat['requested']:,} of full's {fstat['requested']:,}); it lies "
              f"inside the dilated mask, so this run tested no pruning")
        return 1
    elif klass == "inside" and cstat["requested"] != fstat["requested"]:
        print(f"\n  FAIL: {args.box} is tagged inside but pruned "
              f"{fstat['requested'] - cstat['requested']:,} tiles")
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


def mask(args):
    fails = 0
    z, over = args.coverage_zoom, args.coverage_oversample

    # build_coverage is called rather than reimplemented: a local copy of its
    # chunk loop would compare itself against itself, and the transposed index
    # this is here to catch is exactly what survives a copy.
    print(f"chunking: same parameters (z{z}, {over}x), different chunk geometry")
    cmin, cmax, _, _ = T.extent_rect(T.WMS_EXTENT, z)
    wide, narrow = T.WMS_MAX_PX, 512
    print(f"  extent is {cmax - cmin + 1} columns; step {wide // over} spans it in "
          f"one chunk, step {narrow // over} needs several")
    a = T.build_coverage(T.WMS_EXTENT, z, over, max_px=wide)
    b = T.build_coverage(T.WMS_EXTENT, z, over, max_px=narrow)
    if a == b:
        print(f"  PASS: identical, {len(a):,} cells either way")
    else:
        print(f"  FAIL: {len(a ^ b):,} cells differ between chunk geometries")
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
    # Containment alone is nearly free: the fine build is smaller than the mask it
    # is checked against, so a build that collapsed would pass by projecting
    # nothing. The gap is the dilation margin, and it is only meaningful while it
    # stays near what the dilation is documented to cost.
    slack = len(shipped - projected) / len(shipped)
    verdict = "FAIL" if slack > 0.25 else "ok"
    print(f"  margin: {len(shipped - projected):,} shipped cells the fine build "
          f"does not reach ({100 * slack:.1f}%)  {verdict}")
    if verdict == "FAIL":
        print(f"    a fine build that recovers this much less is evidence about "
              f"the service, not a passing containment check")
        fails += 1

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


def cost(args):
    """What a full run would fetch and store, from a random sample of the real
    footprint rather than from whichever boxes happened to get picked.

    Named boxes are chosen to stress correctness -- dense water, boundaries --
    so their byte averages are not the footprint's. Sampling uniformly over
    every tile the footprint implies at a zoom gives an estimate that is
    actually about the run being priced."""
    rng = random.Random(args.seed)
    fails = 0
    src = T.parse_source("wms", None)
    fp = sorted(T.build_coverage(T.WMS_EXTENT, args.coverage_zoom,
                                 args.coverage_oversample))
    print(f"footprint: {len(fp):,} cells at z{args.coverage_zoom}, "
          f"sampling {args.samples} tiles per zoom (seed {args.seed})\n")
    print(f"  {'zoom':>4} {'tiles':>11} {'content':>8} {'KB/tile':>8} "
          f"{'GB':>7} {'hours':>7}")
    tot_gb = tot_h = 0.0
    for z in args.zooms:
        s = z - args.coverage_zoom
        total = len(fp) * (1 << s) ** 2
        picks = [(( cx << s) + rng.randrange(1 << s),
                  ( cy << s) + rng.randrange(1 << s))
                 for cx, cy in (fp[rng.randrange(len(fp))]
                                for _ in range(args.samples))]
        t0 = time.monotonic()
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            res = list(pool.map(lambda c: T.fetch(src, z, c[0], c[1]), picks))
        secs = time.monotonic() - t0
        sizes = [len(d) for (_, _, st, d) in res if st == "ok"]
        errs = sum(1 for r in res if r[2] == "err")
        ok = len(res) - errs
        if not ok or errs / len(res) > 0.05:
            # an errored sample is not an empty tile, and counting it as one
            # deflates the very estimate this exists to produce
            print(f"  {z:>4} {total:>11,}   {errs} of {len(res)} samples failed "
                  f"-- no usable estimate")
            fails += 1
            continue
        rate = len(sizes) / ok
        mean = sum(sizes) / len(sizes) if sizes else 0.0
        # bytes per tile is heavy-tailed, so the error of its mean is cv/sqrt(n),
        # not the 1/sqrt(n) that would apply to the content rate
        sd = (sum((v - mean) ** 2 for v in sizes) / (len(sizes) - 1)) ** 0.5 \
            if len(sizes) > 1 else 0.0
        se = (sd / mean / len(sizes) ** 0.5 * 100) if mean else 0.0
        kb = mean / 1024
        gb = total * rate * kb * 1024 / 1e9
        hours = total / (ok / secs) / 3600
        tot_gb += gb
        tot_h += hours
        print(f"  {z:>4} {total:>11,} {rate:>7.1%} {kb:>8.1f} {gb:>7.1f} {hours:>7.1f}"
              f" {se:>6.0f}%")
    print(f"\n  cumulative: {tot_gb:.1f} GB, {tot_h:.0f} h at "
          f"concurrency {args.concurrency}")
    print(f"  kB/tile error is cv/sqrt(n) at n={args.samples}; hours come from one "
          f"burst on one network, so treat them as an order of magnitude")
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
    o.add_argument("--maxzoom", type=int, default=None,
                   help="default: the downloader's own maxzoom, or 10 for "
                        "--whole-extent, where a deep run is millions of requests")
    o.add_argument("--coverage-zoom", type=int, default=CZOOM)
    o.add_argument("--coverage-oversample", type=int, default=COVER)
    o.add_argument("--show", type=int, default=10,
                   help="list up to this many missed tiles per zoom")

    m = sub.add_parser("mask", help="footprint self-checks")
    m.add_argument("--coverage-zoom", type=int, default=CZOOM)
    m.add_argument("--coverage-oversample", type=int, default=COVER)
    m.add_argument("--fine-zoom", type=int, default=13,
                   help="zoom for the independent, finer-geometry build")

    c = sub.add_parser("cost", help="price a full run from a footprint sample")
    c.add_argument("--samples", type=int, default=300)
    c.add_argument("--zooms", default="11,12,13,14,15,16",
                   type=lambda v: [int(t) for t in v.split(",")])
    c.add_argument("--seed", type=int, default=1)
    c.add_argument("--concurrency", type=int, default=8)
    c.add_argument("--coverage-zoom", type=int, default=CZOOM)
    c.add_argument("--coverage-oversample", type=int, default=COVER)

    args = p.parse_args()
    if args.cmd == "oracle":
        if args.whole_extent:
            args.bbox, args.box = None, None
        if args.maxzoom is None:
            args.maxzoom = 10 if args.whole_extent else DLZOOM
        if args.maxzoom > args.coverage_zoom + 5:
            p.error(f"--maxzoom {args.maxzoom} enumerates "
                    f"4^{args.maxzoom - args.coverage_zoom} tiles per footprint "
                    f"cell on the full side; raise --coverage-zoom or lower it")
        sys.exit(oracle(args))
    if args.cmd == "mask" and args.fine_zoom <= args.coverage_zoom:
        p.error("--fine-zoom must be deeper than --coverage-zoom; it exists to "
                "sample the same footprint more finely")
    if args.cmd == "cost" and min(args.zooms) < args.coverage_zoom:
        p.error(f"--zooms below --coverage-zoom ({args.coverage_zoom}) have no "
                f"footprint cells to sample from")
    if args.cmd == "cost":
        sys.exit(cost(args))
    sys.exit(mask(args))


if __name__ == "__main__":
    main()
