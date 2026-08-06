#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["requests", "pillow", "numpy"]
# ///
"""Download a Traficom raster chart layer to MBTiles, minimising empty requests.

Two sources, selected with --source:

  wmts  (default) the rasteripalvelu WMTS chart products (Merikarttasarjat,
        Rannikkokartat, Satamakartat, ...). Per-zoom tile rectangles come from
        the service's own TileMatrixSetLimits.
  wms   the S-57 ENC rendered server-side to raster (GetMap). A WMS has no
        declared tile rectangle, so each zoom's rectangle is derived from the
        source's geographic extent instead. No conditional-request support.

Two levers keep empty-tile requests down:

  1. TileMatrixSetLimits (from GetCapabilities) bound each zoom to the layer's
     declared col/row rectangle -> no out-of-range (HTTP 400) requests.
  2. Quadtree descent: at zoom z+1 only the four children of tiles
     that had data at zoom z are requested. Blank ocean/inland subtrees are
     pruned. This assumes "parent empty => all children empty" -- true for a
     base chart (Rannikkokartat), NOT for large-scale-only overlays. For
     Satamakartat / *erikoiskartat use --mode full (fetch the whole per-zoom
     limits rectangle, skip transparent tiles).

Standard web-mercator XYZ: WMTS TileMatrix=z, TileCol=x, TileRow=y (y down).
MBTiles stores TMS rows, so rows are flipped on write. A fully-transparent 200
PNG is empty on either source; 400/404 is empty only on WMTS, where it means
out-of-range -- a WMS answers those only for a malformed request, so there they
are errors. Resumable: an _fetched sidecar plus a completed_zoom marker let an
interrupted run continue.
"""

from __future__ import annotations

import argparse
import datetime
import email.utils
import io
import math
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode

import numpy as np
import requests
from PIL import Image
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

WMTS_CAPS = ("https://julkinen.traficom.fi/rasteripalvelu/wmts"
             "?service=WMTS&request=GetCapabilities")
WMTS_TILE = ("https://julkinen.traficom.fi/rasteripalvelu/wmts"
             "?service=WMTS&request=GetTile&version=1.0.0&style="
             "&tilematrixset=WGS84_Pseudo-Mercator&format=image/png"
             "&layer=Traficom:{layer}"
             "&tilematrix=WGS84_Pseudo-Mercator:{z}&tilecol={x}&tilerow={y}")
TILE_PX = 256                            # tile edge; alpha_mask reshapes on this
WMS = "https://julkinen.traficom.fi/s57/wms"
WMS_LAYER = "cells"                      # "S-57 ENC Layer"
WMS_STYLE = "style-id-202"               # "Full": soundings, contours, land detail
WMS_EXTENT = (18.5, 59.0, 32.0, 70.2)    # Finnish waters, incl. Saimaa and the Bothnian Bay
MERC_R = 20037508.342789244              # web-mercator half-circumference, metres
ATTRIBUTION = ("© Traficom. Not for navigation use. "
               "Does not meet official nautical chart requirements.")

_local = threading.local()


def session():
    s = getattr(_local, "s", None)
    if s is None:
        s = requests.Session()
        retry = Retry(total=4, backoff_factor=0.5,
                      status_forcelist=[429, 500, 502, 503, 504],
                      allowed_methods=["GET"])
        s.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=32))
        s.headers.update({"User-Agent": "fi-nautical-charts/traficom-dl"})
        _local.s = s
    return s


def parse_source(kind, layer):
    """Source descriptor: what to fetch, and how this service behaves.

    conditional  -- honours If-Modified-Since (304), so --refresh can delta-check
    empty_on_4xx -- 400/404 means "no tile here" rather than a broken request
    extent       -- geographic coverage, when the service declares no tile limits
    """
    if kind == "wmts":
        return {"kind": "wmts", "layer": layer, "conditional": True,
                "empty_on_4xx": True, "extent": None}
    if kind == "wms":
        return {"kind": "wms", "layer": layer or WMS_LAYER, "conditional": False,
                "empty_on_4xx": False, "extent": WMS_EXTENT}
    sys.exit(f"unknown source kind: {kind}")


def url_for(src, z, x, y):
    if src["kind"] == "wmts":
        return WMTS_TILE.format(layer=requests.utils.quote(src["layer"]), z=z, x=x, y=y)
    span = 2 * MERC_R / (1 << z)
    bbox = (-MERC_R + x * span, MERC_R - (y + 1) * span,
            -MERC_R + (x + 1) * span, MERC_R - y * span)
    return WMS + "?" + urlencode({
        "SERVICE": "WMS", "REQUEST": "GetMap", "VERSION": "1.1.1",
        "LAYERS": src["layer"], "STYLES": WMS_STYLE, "SRS": "EPSG:3857",
        "BBOX": ",".join(f"{v:.6f}" for v in bbox),
        "WIDTH": TILE_PX, "HEIGHT": TILE_PX, "FORMAT": "image/png",
        "TRANSPARENT": "true",
        # without this a server configured for INIMAGE renders the error as an
        # opaque PNG, which would classify as chart data and seed the fill
        "EXCEPTIONS": "application/vnd.ogc.se_xml"})


def bbox_limits(extent, minzoom, maxzoom):
    """Per-zoom tile rectangles derived from a geographic extent -- the stand-in
    for TileMatrixSetLimits on a service that declares none. Every zoom in range
    gets a key: run() takes its zoom list from these keys, so a missing one is a
    silently skipped zoom."""
    minlon, minlat, maxlon, maxlat = extent
    limits = {}
    for z in range(minzoom, maxzoom + 1):
        last = (1 << z) - 1
        limits[z] = (max(0, min(last, lon2x(minlon, z))),
                     max(0, min(last, lon2x(maxlon, z))),
                     max(0, min(last, lat2y(maxlat, z))),
                     max(0, min(last, lat2y(minlat, z))))
    return limits


def parse_limits(layer):
    import re
    xml = session().get(WMTS_CAPS, timeout=90).text
    block = next((b for b in xml.split("<Layer>")
                  if f"<ows:Identifier>Traficom:{layer}</ows:Identifier>" in b), None)
    if block is None:
        sys.exit(f"layer not found in capabilities: {layer}")
    m = re.search(r"<TileMatrixSet>WGS84_Pseudo-Mercator</TileMatrixSet>(.*?)"
                  r"</TileMatrixSetLink>", block, re.S)
    section = m.group(1) if m else ""
    limits = {}
    for lm in re.finditer(
            r"<TileMatrixLimits>\s*<TileMatrix>WGS84_Pseudo-Mercator:(\d+)</TileMatrix>"
            r"\s*<MinTileRow>(\d+)</MinTileRow>\s*<MaxTileRow>(\d+)</MaxTileRow>"
            r"\s*<MinTileCol>(\d+)</MinTileCol>\s*<MaxTileCol>(\d+)</MaxTileCol>", section):
        z, rmin, rmax, cmin, cmax = map(int, lm.groups())
        limits[z] = (cmin, cmax, rmin, rmax)
    if not limits:
        sys.exit(f"no WGS84_Pseudo-Mercator TileMatrixSetLimits for {layer}")
    return limits


def lon2x(lon, z):
    return int((lon + 180.0) / 360.0 * (1 << z))


def lat2y(lat, z):
    return int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * (1 << z))


def x2lon(x, z):
    return x / (1 << z) * 360.0 - 180.0


def y2lat(y, z):
    return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / (1 << z)))))


def _get_tile(src, z, x, y, ims=None):
    """The one HTTP call site. Classifies more finely than callers need:

      ok / blank (transparent 200) / outside (no tile) / notmodified / err

    fetch() narrows blank and outside to "empty"; fetch_masked() needs them
    apart, since a blank tile is tunnelled through and an outside tile is pruned.
    """
    if ims and not src["conditional"]:
        raise ValueError(f"{src['kind']} source does not support If-Modified-Since")
    headers = {"If-Modified-Since": ims} if ims else None
    try:
        r = session().get(url_for(src, z, x, y), timeout=30, headers=headers)
        if r.status_code == 304:
            return "notmodified", None
        if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
            img = Image.open(io.BytesIO(r.content)).convert("RGBA")
            if img.getchannel("A").getextrema()[1] == 0:
                return "blank", None
            return "ok", r.content
    except (requests.RequestException, OSError, ValueError):
        # an undecodable body is an err like any other: it belongs in _errors,
        # not in a traceback that kills a multi-hour run mid-batch
        return "err", None
    if r.status_code in (400, 404) and src["empty_on_4xx"]:
        return "outside", None
    return "err", None


def fetch(src, z, x, y, ims=None):
    status, data = _get_tile(src, z, x, y, ims)
    return x, y, ("empty" if status in ("blank", "outside") else status), data


def init_db(con, src, mode):
    layer = src["layer"]
    has_meta = con.execute("SELECT name FROM sqlite_master WHERE type='table' "
                           "AND name='metadata'").fetchone()
    prior = dict(con.execute("SELECT name, value FROM metadata "
                             "WHERE name IN ('source', 'source_layer')")) if has_meta else {}
    if prior and (prior.get("source"), prior.get("source_layer")) != (src["kind"], layer):
        sys.exit(f"existing archive is {prior.get('source')}:{prior.get('source_layer')}; "
                 f"refusing to write {src['kind']}:{layer} into it. Mixing sources in one "
                 f"file interleaves two chart products with nothing to tell them apart.")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("CREATE TABLE IF NOT EXISTS tiles "
                "(zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB)")
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS tile_index "
                "ON tiles (zoom_level, tile_column, tile_row)")
    con.execute("CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS _fetched "
                "(z INTEGER, x INTEGER, y INTEGER, PRIMARY KEY (z, x, y))")
    con.execute("CREATE TABLE IF NOT EXISTS _errors "
                "(z INTEGER, x INTEGER, y INTEGER, PRIMARY KEY (z, x, y))")
    for k, v in {"name": layer, "format": "png", "version": "1.0",
                 "type": "overlay" if mode == "full" else "baselayer",
                 "attribution": ATTRIBUTION,
                 "description": f"Traficom {layer} ({src['kind'].upper()}, mode={mode})"}.items():
        con.execute("INSERT OR IGNORE INTO metadata (name, value) VALUES (?, ?)", (k, v))
    # provenance must be written on every run, not INSERT OR IGNORE'd: it is what
    # the guard above and the currency/refresh tooling dispatch on, and a key that
    # only ever lands on a fresh file can never identify one already created
    for k, v in {"source": src["kind"], "source_layer": layer}.items():
        con.execute("INSERT OR REPLACE INTO metadata (name, value) VALUES (?, ?)", (k, v))
    con.commit()


def get_meta(con, k):
    row = con.execute("SELECT value FROM metadata WHERE name=?", (k,)).fetchone()
    return row[0] if row else None


def set_meta(con, k, v):
    con.execute("INSERT OR REPLACE INTO metadata (name, value) VALUES (?, ?)", (k, str(v)))


def data_tiles_at(con, z):
    return {(c, (1 << z) - 1 - r)
            for c, r in con.execute(
                "SELECT tile_column, tile_row FROM tiles WHERE zoom_level=?", (z,))}


def fetched_at(con, z):
    return {(x, y) for x, y in con.execute("SELECT x, y FROM _fetched WHERE z=?", (z,))}


def clamped_bounds(lim, bbox, z):
    cmin, cmax, rmin, rmax = lim
    if bbox:
        minlon, minlat, maxlon, maxlat = bbox
        cmin, cmax = max(cmin, lon2x(minlon, z)), min(cmax, lon2x(maxlon, z))
        rmin, rmax = max(rmin, lat2y(maxlat, z)), min(rmax, lat2y(minlat, z))
    return cmin, cmax, rmin, rmax


def compute_bounds(con):
    """Data extent as the union of leaf-tile footprints (the finest coverage at
    each location). Avoids both the deepest-zoom-only understatement for sparse
    layers and the coarse-tile overstatement of a naive all-zoom union."""
    zs = [z for (z,) in con.execute("SELECT DISTINCT zoom_level FROM tiles ORDER BY zoom_level")]
    if not zs:
        return None
    maxz = zs[-1]
    sets = {z: data_tiles_at(con, z) for z in zs}
    minlon = minlat = 1e9
    maxlon = maxlat = -1e9
    for z in zs:
        kids = sets.get(z + 1, set())
        for (x, y) in sets[z]:
            if z < maxz and ({(2 * x, 2 * y), (2 * x + 1, 2 * y),
                              (2 * x, 2 * y + 1), (2 * x + 1, 2 * y + 1)} & kids):
                continue  # has a deeper child -> not a leaf
            minlon = min(minlon, x2lon(x, z)); maxlon = max(maxlon, x2lon(x + 1, z))
            maxlat = max(maxlat, y2lat(y, z)); minlat = min(minlat, y2lat(y + 1, z))
    return minlon, minlat, maxlon, maxlat


def store_tile(con, z, x, y, data):
    con.execute("INSERT OR IGNORE INTO tiles (zoom_level, tile_column, tile_row, tile_data) "
                "VALUES (?,?,?,?)", (z, x, (1 << z) - 1 - y, sqlite3.Binary(data)))


def gen_candidates(z, limits, bbox, frontier, mode, full_until):
    """Candidate (x, y) tiles for a zoom, plus the full-rectangle size."""
    cmin, cmax, rmin, rmax = clamped_bounds(limits[z], bbox, z)
    if cmin > cmax or rmin > rmax:
        return [], 0
    rect = (cmax - cmin + 1) * (rmax - rmin + 1)
    seed = (mode == "full" or frontier is None
            or (full_until is not None and z <= full_until))
    if seed:
        cands = [(x, y) for y in range(rmin, rmax + 1) for x in range(cmin, cmax + 1)]
    else:
        kids = set()
        for (x, y) in frontier:
            kids.update({(2 * x, 2 * y), (2 * x + 1, 2 * y),
                         (2 * x, 2 * y + 1), (2 * x + 1, 2 * y + 1)})
        cands = [c for c in kids if cmin <= c[0] <= cmax and rmin <= c[1] <= rmax]
    return cands, rect


def retry_errors(con, src, concurrency, rounds=3):
    """Re-fetch tiles recorded in _errors; store recoveries, drop resolved ones."""
    recovered = 0
    for _ in range(rounds):
        errs = con.execute("SELECT z, x, y FROM _errors").fetchall()
        if not errs:
            break
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            results = list(pool.map(
                lambda t: (t[0],) + fetch(src, t[0], t[1], t[2]), errs))
        for z, x, y, status, data in results:
            if status == "err":
                continue
            con.execute("DELETE FROM _errors WHERE z=? AND x=? AND y=?", (z, x, y))
            con.execute("INSERT OR IGNORE INTO _fetched (z, x, y) VALUES (?,?,?)", (z, x, y))
            if status == "ok":
                store_tile(con, z, x, y, data)
                recovered += 1
        con.commit()
    return recovered


def repair(con, args, src, limits, bbox, zooms):
    """Re-derive the candidate set from stored data and re-fetch any tile absent
    from the archive. Recovers tiles that failed on the original run (which may
    predate error tracking), while re-confirming empties harmlessly."""
    print(f"repair: re-deriving missing tiles for {src['layer']} ...")
    frontier = None
    total = 0
    for z in zooms:
        cands, _ = gen_candidates(z, limits, bbox, frontier, args.mode, args.full_until)
        stored = data_tiles_at(con, z)
        absent = [(z, x, y) for (x, y) in cands if (x, y) not in stored]
        if absent:
            con.executemany("INSERT OR IGNORE INTO _errors (z, x, y) VALUES (?,?,?)", absent)
            con.commit()
            rec = retry_errors(con, src, args.concurrency)
            total += rec
            print(f"  z{z}: {len(absent):,} absent -> recovered {rec}")
        frontier = data_tiles_at(con, z)
    remaining = con.execute("SELECT count(*) FROM _errors").fetchone()[0]
    if remaining == 0:
        con.execute("DROP TABLE IF EXISTS _fetched")
        con.execute("DROP TABLE IF EXISTS _errors")
        con.commit(); con.execute("VACUUM")
    stored = con.execute("SELECT count(*) FROM tiles").fetchone()[0]
    con.commit()
    con.close()
    print(f"repair done: recovered {total}, {stored:,} stored, {remaining} still failing")


def refresh(con, args, src, limits, bbox, zooms):
    """Re-check the existing coverage with If-Modified-Since = the day after the
    last download, so only tiles reseded since (new editions) transfer data;
    unchanged tiles return 304. Existing coverage only -- new chart areas need a
    fresh download."""
    since = get_meta(con, "downloaded")
    if not since:
        sys.exit("no 'downloaded' date in metadata; run currency.py first")
    base = datetime.date.fromisoformat(since) + datetime.timedelta(days=1)
    ims = email.utils.format_datetime(
        datetime.datetime(base.year, base.month, base.day, tzinfo=datetime.timezone.utc))
    print(f"refresh: checking for editions newer than {since} ...")
    frontier = None
    updated = removed = checked = 0
    for z in zooms:
        cands, _ = gen_candidates(z, limits, bbox, frontier, args.mode, args.full_until)
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            results = list(pool.map(
                lambda c: fetch(src, z, c[0], c[1], ims), cands))
        for x, y, status, data in results:
            checked += 1
            row = (1 << z) - 1 - y
            if status == "ok":
                con.execute("INSERT OR REPLACE INTO tiles "
                            "(zoom_level, tile_column, tile_row, tile_data) "
                            "VALUES (?,?,?,?)", (z, x, row, sqlite3.Binary(data)))
                updated += 1
            elif status == "empty":
                cur = con.execute("DELETE FROM tiles WHERE zoom_level=? AND "
                                  "tile_column=? AND tile_row=?", (z, x, row))
                removed += cur.rowcount
        con.commit()
        frontier = data_tiles_at(con, z)
        print(f"\r  z{z:<2} checked {checked:,}  updated {updated:,}  removed {removed:,}",
              end="", flush=True)
    print()
    set_meta(con, "downloaded", datetime.date.today().isoformat())
    con.commit()
    con.close()
    print(f"refresh done: {updated:,} tiles updated, {removed:,} removed. "
          f"Re-run currency.py to update the stamped dates.")


def alpha_mask(img, res=32):
    """res×res boolean coverage mask: a cell is True if any of its pixels are
    opaque (charted). Max-pooled so boundaries over-include -> no gaps."""
    a = np.asarray(img.getchannel("A"))
    b = TILE_PX // res
    return a.reshape(res, b, res, b).max(axis=(1, 3)) > 0


def _quadrant(mask, qx, qy):
    """The (qx, qy) quarter of a mask, upscaled back to full res (each cell
    -> 2×2). Resolution halves per level; once exhausted it degrades to
    'fully covered' -> over-fetch, never a gap."""
    h = mask.shape[0] // 2
    sub = mask[qy * h:(qy + 1) * h, qx * h:(qx + 1) * h]
    return np.repeat(np.repeat(sub, 2, 0), 2, 1)


def fetch_masked(src, z, x, y, res=32):
    status, data = _get_tile(src, z, x, y)
    if status == "ok":
        return "content", data, alpha_mask(Image.open(io.BytesIO(data)).convert("RGBA"), res)
    if status == "blank":
        return "placeholder", None, None       # transparent -> tunnel through
    if status == "outside":
        return "outside", None, None           # off-sheet / no tile -> prune
    return "err", None, None


def mask_descent(con, args, src, limits, bbox, zooms):
    """Descend guided by the alpha footprint. Fetch a child only where an
    ancestor content tile is opaque; tunnel through transparent placeholder
    tiles (carrying the ancestor's mask) to reach deep content behind them.
    Prunes off-sheet/open-sea that full mode still fetches.

    FAST BUT NOT COMPLETE. Verified to prune huge off-sheet areas correctly
    (inland: 12 vs full's 1054 requests), but these charts also place real
    content at the deepest zoom in spots a coarser tile leaves transparent
    (Helsinki: 29 z15 tiles missed vs full). Mask prunes those. Use for quick
    previews / coverage estimates only; use --mode full for an authoritative,
    gap-free chart."""
    st = {"content": 0, "placeholder": 0, "outside": 0, "err": 0, "req": 0}

    z0 = zooms[0]
    cmin, cmax, rmin, rmax = clamped_bounds(limits[z0], bbox, z0)
    seed = [(x, y) for y in range(rmin, rmax + 1) for x in range(cmin, cmax + 1)]
    frontier = {}
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        for (x, y), (status, data, mask) in zip(
                seed, pool.map(lambda c: fetch_masked(src, z0, c[0], c[1]), seed)):
            st[status] += 1; st["req"] += 1
            if status == "content":
                store_tile(con, z0, x, y, data); frontier[(x, y)] = mask
    con.commit()
    print(f"  z{z0} seed: {len(frontier)} content tiles")

    for z in zooms[1:]:
        cmin, cmax, rmin, rmax = clamped_bounds(limits[z], bbox, z)
        cand = {}
        for (x, y), mask in frontier.items():
            for qx in (0, 1):
                for qy in (0, 1):
                    cx, cy = 2 * x + qx, 2 * y + qy
                    if cmin <= cx <= cmax and rmin <= cy <= rmax:
                        q = _quadrant(mask, qx, qy)
                        if q.any():
                            cand[(cx, cy)] = q
        items = list(cand.items())
        new_frontier = {}
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            for i in range(0, len(items), args.batch):
                chunk = items[i:i + args.batch]
                for ((cx, cy), inh), (status, data, mask) in zip(
                        chunk, pool.map(lambda kv: fetch_masked(src, z, kv[0][0], kv[0][1]),
                                        chunk)):
                    st[status] += 1; st["req"] += 1
                    if status == "content":
                        store_tile(con, z, cx, cy, data); new_frontier[(cx, cy)] = mask
                    elif status == "placeholder":
                        new_frontier[(cx, cy)] = inh
                con.commit()
        frontier = new_frontier
        print(f"\r  z{z:<2} req {st['req']:>8}  content {st['content']:>7}  "
              f"tunnel {st['placeholder']:>7}  frontier {len(frontier):>7}", end="", flush=True)
    print()
    b = compute_bounds(con)
    if b:
        set_meta(con, "bounds", "{:.5f},{:.5f},{:.5f},{:.5f}".format(*b))
    set_meta(con, "minzoom", str(zooms[0])); set_meta(con, "maxzoom", str(zooms[-1]))
    con.commit()
    stored = con.execute("SELECT count(*) FROM tiles").fetchone()[0]
    con.close()
    print(f"\n{src['layer']} [mask]: requested {st['req']:,}, stored {stored:,}, "
          f"placeholder-tunnelled {st['placeholder']:,}, err {st['err']:,}")


def fill_descent(con, args, src, limits, bbox, zooms):
    """Footprint-seeded flood fill (no full grid). Full-fetch the solid low zoom
    (--solid-zoom, the last zoom before the placeholder band) to get the complete
    chart footprint; then for each deeper zoom flood-fill the content outward from
    seeds, bounded to that footprint, stopping at empties.

    Seeds per zoom = the centre tile of every footprint cell (reaches content
    stranded behind the placeholder band, e.g. the archipelago) + the children of
    the previous zoom's content (reaches content chains). Off-footprint (inland,
    open Baltic beyond the charts) is never touched. Verify vs --mode full."""
    from collections import deque
    Zs = args.solid_zoom
    st = {"req": 0, "content": 0, "empty": 0, "err": 0}
    content = {z: set() for z in zooms}

    def fetch_batch(z, batch):
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            return list(pool.map(lambda c: fetch(src, z, c[0], c[1]), batch))

    def record(z, x, y, status, data):
        st["req"] += 1
        if status == "ok":
            store_tile(con, z, x, y, data); content[z].add((x, y)); st["content"] += 1
            return True
        st["err" if status == "err" else "empty"] += 1
        return False

    for z in [zz for zz in zooms if zz <= Zs]:
        cmin, cmax, rmin, rmax = clamped_bounds(limits[z], bbox, z)
        cands = [(x, y) for y in range(rmin, rmax + 1) for x in range(cmin, cmax + 1)]
        for i in range(0, len(cands), args.batch):
            for x, y, status, data in fetch_batch(z, cands[i:i + args.batch]):
                record(z, x, y, status, data)
            con.commit()
        print(f"  z{z} footprint: {len(content[z])} content")
    foot = content.get(Zs, set())

    for z in [zz for zz in zooms if zz > Zs]:
        cmin, cmax, rmin, rmax = clamped_bounds(limits[z], bbox, z)
        s = z - Zs

        def in_foot(x, y):
            return cmin <= x <= cmax and rmin <= y <= rmax and (x >> s, y >> s) in foot

        seen = set(); q = deque()

        def push(c):
            if c not in seen and in_foot(*c):
                seen.add(c); q.append(c)

        half = 1 << (s - 1)
        for fx, fy in foot:
            push(((fx << s) + half, (fy << s) + half))
        for px, py in content.get(z - 1, set()):
            for dx in (0, 1):
                for dy in (0, 1):
                    push((2 * px + dx, 2 * py + dy))

        while q:
            batch = [q.popleft() for _ in range(min(args.batch, len(q)))]
            for x, y, status, data in fetch_batch(z, batch):
                if record(z, x, y, status, data):
                    push((x + 1, y)); push((x - 1, y)); push((x, y + 1)); push((x, y - 1))
                    push((x + 1, y + 1)); push((x - 1, y - 1)); push((x + 1, y - 1)); push((x - 1, y + 1))
            con.commit()
            print(f"\r  z{z:<2} flood: content {len(content[z]):>7}  queued {len(q):>7}  "
                  f"req {st['req']:>8}", end="", flush=True)
        print()

    b = compute_bounds(con)
    if b:
        set_meta(con, "bounds", "{:.5f},{:.5f},{:.5f},{:.5f}".format(*b))
    set_meta(con, "minzoom", str(zooms[0])); set_meta(con, "maxzoom", str(zooms[-1]))
    con.commit()
    stored = con.execute("SELECT count(*) FROM tiles").fetchone()[0]
    con.close()
    print(f"\n{src['layer']} [fill]: requested {st['req']:,}, stored {stored:,}, "
          f"empty {st['empty']:,}, err {st['err']:,}")


def run(args):
    src = parse_source(args.source, args.layer)

    if src["extent"] is None:
        limits = parse_limits(src["layer"])
    else:
        limits = bbox_limits(src["extent"], args.minzoom, args.maxzoom)
    zooms = sorted(z for z in limits if args.minzoom <= z <= args.maxzoom)
    if not zooms:
        sys.exit("no zoom levels in range")
    bbox = tuple(float(v) for v in args.bbox.split(",")) if args.bbox else None

    # mask and descent both prune on an out-of-band "no tile here" signal. A WMS
    # answers out-of-coverage with a transparent 200, indistinguishable from the
    # placeholder band they are built to tunnel through, so neither ever prunes.
    if args.mode in ("mask", "descent") and not src["empty_on_4xx"]:
        sys.exit(f"--mode {args.mode} prunes on 400/404, which the {src['kind']} source "
                 f"never returns; every empty tile would be tunnelled instead. "
                 f"Use --mode full (bounded by --bbox) until the coverage mode lands.")
    if args.repair and args.refresh:
        sys.exit("--repair and --refresh do different things; pass one")
    if args.refresh and not src["conditional"]:
        sys.exit(f"--refresh needs If-Modified-Since, which the {src['kind']} source "
                 f"does not support; re-download instead")

    con = sqlite3.connect(args.out)
    init_db(con, src, args.mode)

    if args.repair:
        repair(con, args, src, limits, bbox, zooms)
        return

    if args.refresh:
        refresh(con, args, src, limits, bbox, zooms)
        return

    if args.mode == "mask":
        mask_descent(con, args, src, limits, bbox, zooms)
        return

    if args.mode == "fill":
        fill_descent(con, args, src, limits, bbox, zooms)
        return

    startup = retry_errors(con, src, args.concurrency)
    if startup:
        print(f"startup: recovered {startup} previously-failed tile(s)")

    completed = get_meta(con, "completed_zoom")
    completed = int(completed) if completed is not None else None
    start_idx = zooms.index(completed) + 1 if completed in zooms else 0
    if start_idx >= len(zooms):
        print("already complete"); return

    # frontier = data tiles of the previous processed zoom (None => seed from limits)
    frontier = data_tiles_at(con, zooms[start_idx - 1]) if start_idx > 0 else None

    totals = {"ok": 0, "empty": 0, "err": 0, "req": 0}
    rect_total = 0  # tiles a full-limits (brute) run would request, for savings report

    for idx in range(start_idx, len(zooms)):
        z = zooms[idx]
        cands, rect = gen_candidates(z, limits, bbox, frontier, args.mode, args.full_until)
        rect_total += rect
        already = fetched_at(con, z) | data_tiles_at(con, z)
        todo = [c for c in cands if c not in already]

        zst = {"ok": 0, "empty": 0, "err": 0}
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            for i in range(0, len(todo), args.batch):
                chunk = todo[i:i + args.batch]
                for x, y, status, data in pool.map(lambda c: fetch(src, z, *c), chunk):
                    zst[status] += 1
                    if status == "err":
                        con.execute("INSERT OR IGNORE INTO _errors (z, x, y) VALUES (?,?,?)",
                                    (z, x, y))
                        continue
                    con.execute("INSERT OR IGNORE INTO _fetched (z, x, y) VALUES (?,?,?)",
                                (z, x, y))
                    if status == "ok":
                        row = (1 << z) - 1 - y
                        con.execute("INSERT OR IGNORE INTO tiles "
                                    "(zoom_level, tile_column, tile_row, tile_data) "
                                    "VALUES (?,?,?,?)", (z, x, row, sqlite3.Binary(data)))
                con.commit()
                print(f"\r  z{z:<2} req {zst['ok']+zst['empty']+zst['err']:>7}/"
                      f"{len(todo):<7} data={zst['ok']:>6} empty={zst['empty']:>7} "
                      f"err={zst['err']}", end="", flush=True)
        print()
        for k in zst:
            totals[k] += zst[k]
        totals["req"] += zst["ok"] + zst["empty"] + zst["err"]

        if zst["err"]:
            rec = retry_errors(con, src, args.concurrency, rounds=2)
            print(f"  z{z}: retried errors, recovered {rec}")

        set_meta(con, "completed_zoom", z)
        con.execute("DELETE FROM _fetched WHERE z=?", (z,))
        con.commit()
        frontier = data_tiles_at(con, z)
        past_seed = args.full_until is None or z >= args.full_until
        if not frontier and args.mode == "descent" and past_seed:
            print(f"  no data at z{z}; descent cannot continue"); break

    b = compute_bounds(con)
    if b:
        set_meta(con, "bounds", "{:.5f},{:.5f},{:.5f},{:.5f}".format(*b))
    set_meta(con, "minzoom", str(zooms[0]))
    set_meta(con, "maxzoom", str(zooms[-1]))
    con.commit()

    remaining = con.execute("SELECT count(*) FROM _errors").fetchone()[0]
    if remaining == 0 and start_idx == 0:
        con.execute("DROP TABLE IF EXISTS _fetched")
        con.execute("DROP TABLE IF EXISTS _errors")
        con.commit(); con.execute("VACUUM")

    stored = con.execute("SELECT count(*) FROM tiles").fetchone()[0]
    con.close()
    saved = (1 - totals["req"] / rect_total) * 100 if rect_total else 0
    print(f"\n{src['layer']} [{args.mode}]: requested {totals['req']:,}, "
          f"stored {stored:,}, empty {totals['empty']:,}, unrecovered errors {remaining:,}")
    print(f"  full-limits rectangle would be {rect_total:,} requests "
          f"-> descent saved {saved:.1f}%")
    print(f"  wrote {args.out}")


def main():
    p = argparse.ArgumentParser(description="Download a Traficom chart layer to MBTiles")
    p.add_argument("--source", choices=["wmts", "wms"], default="wmts",
                   help="wmts: rasteripalvelu chart products (default). "
                        "wms: S-57 ENC rendered to raster")
    p.add_argument("--layer", default=None,
                   help='wmts: required, e.g. "Rannikkokartat public". '
                        f'wms: defaults to "{WMS_LAYER}"')
    p.add_argument("--out", required=True)
    p.add_argument("--mode", choices=["descent", "full", "mask", "fill"], default="fill")
    p.add_argument("--solid-zoom", type=int, default=11,
                   help="fill mode: last dense zoom before the placeholder band; "
                        "its content defines the footprint the fill is bounded to")
    p.add_argument("--full-until", type=int, default=None,
                   help="fetch the full limits rectangle for zooms <= this, then "
                        "descend (safe seeding for sparse overlays like Satamakartat)")
    p.add_argument("--minzoom", type=int, default=0)
    p.add_argument("--maxzoom", type=int, default=15)
    p.add_argument("--bbox", default=None, help="minlon,minlat,maxlon,maxlat (test subset)")
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--batch", type=int, default=2000)
    p.add_argument("--repair", action="store_true",
                   help="re-fetch any tile absent from an existing archive "
                        "(recovers failures from a run that predates error tracking)")
    p.add_argument("--refresh", action="store_true",
                   help="If-Modified-Since delta: re-fetch only tiles reseded "
                        "since the stamped download date (existing coverage only)")
    args = p.parse_args()
    if args.source == "wmts" and not args.layer:
        p.error("--layer is required for --source wmts")   # exit 2, with usage
    run(args)


if __name__ == "__main__":
    main()
