#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["requests", "pillow", "numpy", "scipy"]
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

from strip_nodata import (MIN_FILL as NODATA_MIN_FILL, RADIUS as NODATA_RADIUS,
                          nodata_mask, wholly_offsheet)

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
# Wider than the declared coverage on every side -- rendering the coverage layer
# over a deliberately oversized box puts it at lon 19.05..30.37, lat 58.84..66.50.
# build_coverage asserts the footprint does not touch the edge, so a service that
# grows past this fails loudly instead of being silently cropped.
WMS_EXTENT = (18.0, 58.0, 32.0, 70.2)
# The service publishes its own footprint as the S-57 usage bands. The parent
# layer renders exactly the union of coverage.1..coverage.9 (verified pixel-for-
# pixel), so one request replaces nine. Overview alone is NOT enough: Traficom's
# bands are not nested -- Saimaa has Coastal cover with no General above it.
WMS_COVERAGE_LAYER = "coverage"
WMS_MAX_PX = 2048                        # per-request GetMap dimension ceiling
MASK_VERSION = 2                         # bump when coverage eligibility changes
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
        # a bulk client should be reachable: a full run is ~1.6M requests, and
        # without a contact the only way to ask it to stop is to block it
        s.headers.update({"User-Agent": "fi-nautical-charts/traficom-dl "
                          "(+https://github.com/mairas/fi-nautical-charts)"})
        _local.s = s
    return s


def parse_source(kind, layer):
    """Source descriptor: what to fetch, and how this service behaves.

    conditional  -- honours If-Modified-Since (304), so --refresh can delta-check
    empty_on_4xx -- 400/404 means "no tile here" rather than a broken request
    extent       -- geographic coverage, when the service declares no tile limits
    nodata_fill  -- renders off-sheet as opaque black instead of transparency,
                    so it needs stripping on the way in (see strip_nodata)
    """
    if kind == "wmts":
        return {"kind": "wmts", "layer": layer, "conditional": True,
                "empty_on_4xx": True, "extent": None, "nodata_fill": True}
    if kind == "wms":
        # the ENC answers out-of-coverage with a genuinely transparent PNG
        return {"kind": "wms", "layer": layer or WMS_LAYER, "conditional": False,
                "empty_on_4xx": False, "extent": WMS_EXTENT, "nodata_fill": False}
    sys.exit(f"unknown source kind: {kind}")


def url_for(src, z, x, y):
    if src["kind"] == "wmts":
        return WMTS_TILE.format(layer=requests.utils.quote(src["layer"]), z=z, x=x, y=y)
    return wms_url(src["layer"], tile_bbox(z, x, y, 1, 1), TILE_PX, TILE_PX, WMS_STYLE)


def tile_bbox(z, x, y, nx, ny):
    """EPSG:3857 bounds of an nx x ny block of tiles with (x, y) at its top-left.
    XYZ rows run south, so the block's top edge comes from y and its bottom from
    y + ny."""
    span = 2 * MERC_R / (1 << z)
    return (-MERC_R + x * span, MERC_R - (y + ny) * span,
            -MERC_R + (x + nx) * span, MERC_R - y * span)


def wms_url(layers, bbox, w, h, style=""):
    return WMS + "?" + urlencode({
        "SERVICE": "WMS", "REQUEST": "GetMap", "VERSION": "1.1.1",
        "LAYERS": layers, "STYLES": style, "SRS": "EPSG:3857",
        "BBOX": ",".join(f"{v:.6f}" for v in bbox),
        "WIDTH": w, "HEIGHT": h, "FORMAT": "image/png", "TRANSPARENT": "true",
        # without this a server configured for INIMAGE renders the error as an
        # opaque PNG, which would classify as chart data and seed the fill
        "EXCEPTIONS": "application/vnd.ogc.se_xml"})


def bbox_limits(extent, minzoom, maxzoom):
    """Per-zoom tile rectangles derived from a geographic extent -- the stand-in
    for TileMatrixSetLimits on a service that declares none. Every zoom in range
    gets a key: run() takes its zoom list from these keys, so a missing one is a
    silently skipped zoom."""
    return {z: extent_rect(extent, z) for z in range(minzoom, maxzoom + 1)}


def extent_rect(extent, z):
    """Tile rectangle (cmin, cmax, rmin, rmax) covering a geographic extent."""
    minlon, minlat, maxlon, maxlat = extent
    last = (1 << z) - 1
    clamp = lambda v: max(0, min(last, v))
    return (clamp(lon2x(minlon, z)), clamp(lon2x(maxlon, z)),
            clamp(lat2y(maxlat, z)), clamp(lat2y(minlat, z)))


def coverage_chunk(url, nc, nr, over):
    """One coverage render, as an nr x nc boolean block. A hole here would prune
    real chart area with no other symptom, so every failure is fatal rather than
    retried into an error table."""
    for _ in range(3):
        try:
            r = session().get(url, timeout=180)
            if not r.headers.get("content-type", "").startswith("image"):
                raise ValueError(f"HTTP {r.status_code}: {r.content[:200]!r}")
            img = Image.open(io.BytesIO(r.content)).convert("RGBA")
            if img.size != (nc * over, nr * over):
                raise ValueError(f"server returned {img.size}, requested "
                                 f"{(nc * over, nr * over)}; lower --coverage-oversample")
            a = np.asarray(img.getchannel("A")) > 0
            return a.reshape(nr, over, nc, over).max(axis=(1, 3))
        except (requests.RequestException, OSError, ValueError) as e:
            last = e
    # falling out with a return would hand build_coverage None, which numpy
    # assigns as False -- a rectangular hole in the footprint, silently
    sys.exit(f"coverage layer request failed after 3 attempts: {last}")


def build_coverage(extent, zoom, over, max_px=WMS_MAX_PX):
    """The service's declared footprint, as the set of `zoom` tiles it touches.

    Each cell is rendered `over` pixels across and max-pooled, then the grid is
    dilated by one cell: coarse rasterisation over-includes at polygon boundaries
    but drops the occasional small cell, so the dilation is what makes the result
    independent of the sampling resolution. Costs ~8% more cells."""
    cmin, cmax, rmin, rmax = extent_rect(extent, zoom)
    cols, rows = cmax - cmin + 1, rmax - rmin + 1
    grid = np.zeros((rows, cols), bool)
    step = max(1, max_px // over)
    chunks = [(c0, r0) for r0 in range(0, rows, step) for c0 in range(0, cols, step)]
    for i, (c0, r0) in enumerate(chunks, 1):
        nc, nr = min(step, cols - c0), min(step, rows - r0)
        url = wms_url(WMS_COVERAGE_LAYER,
                      tile_bbox(zoom, cmin + c0, rmin + r0, nc, nr), nc * over, nr * over)
        grid[r0:r0 + nr, c0:c0 + nc] = coverage_chunk(url, nc, nr, over)
        print(f"\r  coverage: chunk {i}/{len(chunks)}", end="", flush=True)
    print()

    frac = grid.sum() / grid.size
    if not 0.02 < frac < 0.95:
        sys.exit(f"coverage layer rendered {100 * frac:.1f}% of the extent, which is not a "
                 f"plausible footprint. A blank render would prune everything and an opaque "
                 f"one would defeat the pruning, and neither reports an HTTP error.")
    if grid[0].any() or grid[-1].any() or grid[:, 0].any() or grid[:, -1].any():
        sys.exit(f"declared coverage reaches the edge of WMS_EXTENT {extent}, so the extent "
                 f"is cropping it. Widen the constant and re-run; a mask built from a cropped "
                 f"render silently loses whatever lies beyond.")

    dil = np.zeros_like(grid)
    pad = np.pad(grid, 1)
    for dy in (0, 1, 2):
        for dx in (0, 1, 2):
            dil |= pad[dy:dy + rows, dx:dx + cols]
    return {(cmin + int(c), rmin + int(r)) for r, c in zip(*np.nonzero(dil))}


def coverage_ancestors(footprint, zoom, minzoom):
    """Footprint cells projected up to each zoom above `zoom`, so eligibility at a
    coarse zoom is a set lookup rather than a walk over every descendant cell,
    then dilated by one cell.

    This dilation answers a different problem from build_coverage's. The chart
    layer paints ink outside the coverage layer's own polygons: ask both for the
    same z4 tile north of the declared limit and `cells` renders hundreds of
    opaque pixels where `coverage` renders none. It follows the request framing
    rather than the ground area -- the same z4 extent asked for as one 2x2 block
    shows ink in two tiles, asked for as four separate tiles shows it in all four
    -- so the cause is not established and the margin has to be empirical.

    Measured against the whole extent, the spill runs 24 px at z3-z4, 48-96 px at
    z5-z10, and 128 px at z11. One cell is 256 px at every zoom, so the margin
    holds throughout but is only 2x at its tightest, not the order of magnitude a
    single-tile measurement first suggested. A fixed ground margin cannot work
    here: it would be far too small at z4 and pointlessly large at z11.

    Consequently the dilation is load-bearing and nearly spent at z11. Raising
    --coverage-zoom shrinks the same margin in ground terms; re-measure before
    trusting it there."""
    out = {}
    for z in range(minzoom, zoom):
        s = zoom - z
        cells = {(x >> s, y >> s) for (x, y) in footprint}
        out[z] = {(x + dx, y + dy) for (x, y) in cells
                  for dx in (-1, 0, 1) for dy in (-1, 0, 1)}
    return out


def coverage_key(args, src):
    """Everything that determines the footprint's content. A cache hit on anything
    less silently reuses a mask built for a different question."""
    return "|".join(str(v) for v in (args.coverage_zoom, args.coverage_oversample,
                                     WMS_COVERAGE_LAYER, src["extent"]))


def load_coverage(con, key):
    if get_meta(con, "coverage_key") != key:
        return None
    return {(x, y) for x, y in con.execute("SELECT x, y FROM _coverage")} or None


def store_coverage(con, footprint, key, args):
    con.execute("DELETE FROM _coverage")
    con.executemany("INSERT INTO _coverage (x, y) VALUES (?,?)", sorted(footprint))
    set_meta(con, "coverage_key", key)
    set_meta(con, "coverage_zoom", args.coverage_zoom)
    set_meta(con, "coverage_oversample", args.coverage_oversample)
    set_meta(con, "coverage_layer", WMS_COVERAGE_LAYER)
    con.commit()


def run_shape(args, src):
    """What `completed_zoom` was measured against. The marker is a bare zoom
    number, so resuming under a different mode, bbox, zoom range or footprint
    would otherwise skip work the earlier run never did.

    MASK_VERSION covers the part no argument describes: which tiles a given
    footprint makes eligible. Widening the dilation changes that without changing
    any input, so without it a re-run finds the marker intact and reports
    'already complete' having fetched none of the tiles the change exists to
    add."""
    return "|".join(str(v) for v in (args.mode, args.bbox, args.minzoom, args.maxzoom,
                                     coverage_key(args, src), MASK_VERSION))


def resume_point(con, args, src, zooms):
    """Index into `zooms` to restart from, honouring completed_zoom only when the
    run is shaped the same way as the one that wrote it."""
    completed = get_meta(con, "completed_zoom")
    if completed is None:
        return 0
    if get_meta(con, "run_shape") != run_shape(args, src):
        print("  run differs from the one that wrote completed_zoom "
              "(mode, bbox, zoom range, footprint or eligibility rule); "
              "re-walking every zoom")
        return 0
    completed = int(completed)
    return zooms.index(completed) + 1 if completed in zooms else 0


def stamp_zoom_range(con):
    """minzoom/maxzoom from what the archive holds, not from what was asked for.
    A run that stopped early still has to describe itself honestly: the console
    warning scrolls away, the metadata is what a client reads."""
    lo, hi = con.execute("SELECT min(zoom_level), max(zoom_level) FROM tiles").fetchone()
    if lo is not None:
        set_meta(con, "minzoom", str(lo))
        set_meta(con, "maxzoom", str(hi))


def finish_run(con):
    """What only a finished run can state about the archive."""
    stamp_zoom_range(con)
    flush_editions(con)
    set_meta(con, "downloaded", datetime.date.today().isoformat())


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


def strip_fill(arr, original):
    """Drop tiles that are nothing but off-sheet fill, returning None for them.

    A tile emptied here reports blank, which is what keeps the crawl from
    descending into the sea beyond the last sheet -- so this is a pruning
    decision as much as a cleaning one, and it has to be made per tile as the
    tile arrives.

    Only wholly off-sheet tiles qualify. Fill *within* a chart tile is left for
    strip-nodata, which has the tile grid to tell it from a place name; deciding
    that here, from one tile in isolation, is what ate HELSINKI. Everything else
    is returned byte-for-byte as the server sent it."""
    if not wholly_offsheet(arr):
        return original
    m = nodata_mask(arr, NODATA_RADIUS, protect=False)
    if m.sum() < NODATA_MIN_FILL:
        return original
    a = arr.copy()
    a[m] = (255, 255, 255, 0)
    if a[..., 3].max() == 0:
        return None
    buf = io.BytesIO()
    Image.fromarray(a, "RGBA").save(buf, format="PNG")
    return buf.getvalue()


EDITION_LAG = 60    # seconds

_editions = {"newest": None, "oldest": None}
_editions_lock = threading.Lock()


def note_edition(headers):
    """Record a tile's edition date, unless the server just rendered it for us.

    Last-Modified is the only edition signal the WMTS offers, and it is not one
    for every tile: GeoWebCache renders a tile it does not hold on demand and
    stamps it with the moment it stored it, so our own requests manufacture
    dates indistinguishable from a reseed. Reading them back later as editions
    is what made a set report the day we downloaded it as its edition.

    The response's own Date header settles it, and only during the request that
    caused it. A tile made for us is stamped within that request; a cached one
    is days or years older -- measured against Traficom, 0 s versus 71 and 1740
    days, so the threshold is nowhere near anything. Both timestamps come from
    the server's clock, so neither our clock nor our timezone enters it."""
    lm, served = headers.get("Last-Modified"), headers.get("Date")
    if not lm or not served:
        return
    try:
        made_at = email.utils.parsedate_to_datetime(lm)
        served_at = email.utils.parsedate_to_datetime(served)
    except (TypeError, ValueError):
        return
    if (served_at - made_at).total_seconds() < EDITION_LAG:
        return
    day = made_at.date().isoformat()
    with _editions_lock:
        for key, pick in (("newest", max), ("oldest", min)):
            seen = _editions[key]
            _editions[key] = day if seen is None else pick(seen, day)


def flush_editions(con):
    """Fold the editions seen so far into the archive's own record.

    Merged, not overwritten: a resumed run sees only the tiles it still had to
    fetch, and a refresh only the ones that changed. ISO dates sort by date."""
    with _editions_lock:
        newest, oldest = _editions["newest"], _editions["oldest"]
    if newest is None:
        return
    prior_new = get_meta(con, "source_updated")
    prior_old = get_meta(con, "source_updated_oldest")
    set_meta(con, "source_updated", max(newest, prior_new) if prior_new else newest)
    set_meta(con, "source_updated_oldest", min(oldest, prior_old) if prior_old else oldest)


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
            data = r.content
            if src["nodata_fill"]:
                data = strip_fill(np.asarray(img), r.content)
                if data is None:
                    return "blank", None
            note_edition(r.headers)   # only tiles we keep speak for the set's currency
            return "ok", data
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
    con.execute("CREATE TABLE IF NOT EXISTS _coverage "
                "(x INTEGER, y INTEGER, PRIMARY KEY (x, y))")
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
    predate error tracking), while re-confirming empties harmlessly.

    A coverage archive derives its candidates from the footprint instead. It is
    the only way back for one: a completed archive skips every zoom on a re-run,
    so without this the two recovery paths would each defer to the other."""
    print(f"repair: re-deriving missing tiles for {src['layer']} ...")
    footprint = above = None
    if args.mode == "coverage":
        footprint = build_coverage(src["extent"], args.coverage_zoom,
                                   args.coverage_oversample)
        above = coverage_ancestors(footprint, args.coverage_zoom, zooms[0])
        print(f"  coverage: {len(footprint):,} cells at z{args.coverage_zoom}")
    frontier = None
    total = 0
    for z in zooms:
        if footprint is not None:
            cmin, cmax, rmin, rmax = clamped_bounds(limits[z], bbox, z)
            s = z - args.coverage_zoom
            cands = [(x, y) for y in range(rmin, rmax + 1)
                     for x in range(cmin, cmax + 1)
                     if ((x >> s, y >> s) in footprint if s >= 0
                         else (x, y) in above[z])]
        else:
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
        flush_editions(con)
        con.commit()
        frontier = data_tiles_at(con, z)
        print(f"\r  z{z:<2} checked {checked:,}  updated {updated:,}  removed {removed:,}",
              end="", flush=True)
    print()
    # only now: refresh reads this as its If-Modified-Since baseline, so advancing
    # it before every zoom is checked would make the next run skip what this one
    # never reached
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
                flush_editions(con)
                con.commit()
        frontier = new_frontier
        print(f"\r  z{z:<2} req {st['req']:>8}  content {st['content']:>7}  "
              f"tunnel {st['placeholder']:>7}  frontier {len(frontier):>7}", end="", flush=True)
    print()
    b = compute_bounds(con)
    if b:
        set_meta(con, "bounds", "{:.5f},{:.5f},{:.5f},{:.5f}".format(*b))
    finish_run(con)
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
            flush_editions(con)
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
            flush_editions(con)
            con.commit()
            print(f"\r  z{z:<2} flood: content {len(content[z]):>7}  queued {len(q):>7}  "
                  f"req {st['req']:>8}", end="", flush=True)
        print()

    b = compute_bounds(con)
    if b:
        set_meta(con, "bounds", "{:.5f},{:.5f},{:.5f},{:.5f}".format(*b))
    finish_run(con)
    con.commit()
    stored = con.execute("SELECT count(*) FROM tiles").fetchone()[0]
    con.close()
    print(f"\n{src['layer']} [fill]: requested {st['req']:,}, stored {stored:,}, "
          f"empty {st['empty']:,}, err {st['err']:,}")


def coverage_descent(con, args, src, limits, bbox, zooms):
    """Fetch every tile the service's declared coverage touches, at every zoom.

    Unlike descent/fill/mask this never infers a deeper zoom's candidates from a
    shallower zoom's content, so scale-dependent rendering cannot strand content
    behind a blank ancestor. The same footprint gates every zoom.

    Bounded to the footprint, not a full grid: over Finnish waters it keeps
    roughly a third of the extent rectangle, and off-footprint open sea and inland
    are never requested.

    Resumable and error-tracking like the descent path -- an error is recorded and
    retried, never counted as absence."""
    key = coverage_key(args, src)
    footprint = load_coverage(con, key)
    if footprint is None:
        print(f"  building coverage footprint at z{args.coverage_zoom} "
              f"({args.coverage_oversample}x oversample) ...")
        footprint = build_coverage(src["extent"], args.coverage_zoom,
                                   args.coverage_oversample)
        store_coverage(con, footprint, key, args)
    print(f"  coverage: {len(footprint):,} cells at z{args.coverage_zoom}")
    above = coverage_ancestors(footprint, args.coverage_zoom, zooms[0])

    def eligible(z, x, y):
        if z >= args.coverage_zoom:
            s = z - args.coverage_zoom
            return (x >> s, y >> s) in footprint
        return (x, y) in above[z]

    recovered = retry_errors(con, src, args.concurrency)
    if recovered:
        print(f"  startup: recovered {recovered} previously-failed tile(s)")
    start = resume_point(con, args, src, zooms)
    if start >= len(zooms) and not con.execute("SELECT count(*) FROM _errors").fetchone()[0]:
        print("already complete")
        con.close()
        return

    totals = {"ok": 0, "empty": 0, "err": 0}
    for z in zooms[start:]:
        cmin, cmax, rmin, rmax = clamped_bounds(limits[z], bbox, z)
        # run() established the bbox meets the source at the deepest zoom, and a
        # bbox that meets it there meets it everywhere, so this cannot happen for
        # an accepted bbox. Marking the zoom complete instead would leave an
        # archive that claims a range it never fetched.
        if cmin > cmax or rmin > rmax:
            sys.exit(f"z{z} has an empty tile rectangle for a bbox accepted at "
                     f"z{zooms[-1]}; nothing was marked complete.")
        cands = [(x, y) for y in range(rmin, rmax + 1) for x in range(cmin, cmax + 1)
                 if eligible(z, x, y)]
        rect = (cmax - cmin + 1) * (rmax - rmin + 1)
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
                        store_tile(con, z, x, y, data)
                flush_editions(con)
                con.commit()
                print(f"\r  z{z:<2} req {sum(zst.values()):>7}/{len(todo):<7} "
                      f"data={zst['ok']:>6} empty={zst['empty']:>7} err={zst['err']}",
                      end="", flush=True)
        print(f"   (coverage kept {len(cands):,} of {rect:,})")
        if zst["err"]:
            print(f"  z{z}: retried errors, recovered "
                  f"{retry_errors(con, src, args.concurrency, rounds=2)}")
        for k in zst:
            totals[k] += zst[k]
        # a zoom whose errors did not clear is not complete: leave the marker
        # where it is so the next run re-walks it rather than skipping past
        if con.execute("SELECT count(*) FROM _errors WHERE z=?", (z,)).fetchone()[0]:
            print(f"  z{z}: unrecovered errors, not marking complete")
            break
        set_meta(con, "completed_zoom", z)
        set_meta(con, "run_shape", run_shape(args, src))
        con.execute("DELETE FROM _fetched WHERE z=?", (z,))
        con.commit()

    b = compute_bounds(con)
    if b:
        set_meta(con, "bounds", "{:.5f},{:.5f},{:.5f},{:.5f}".format(*b))
    finish_run(con)
    con.commit()
    remaining = con.execute("SELECT count(*) FROM _errors").fetchone()[0]
    if remaining == 0:
        con.execute("DROP TABLE IF EXISTS _fetched")
        con.execute("DROP TABLE IF EXISTS _errors")
        con.execute("DROP TABLE IF EXISTS _coverage")
        con.commit()
        con.execute("VACUUM")
    stored = con.execute("SELECT count(*) FROM tiles").fetchone()[0]
    con.close()
    print(f"\n{src['layer']} [coverage]: requested {sum(totals.values()):,}, "
          f"stored {stored:,}, empty {totals['empty']:,}, unrecovered errors {remaining:,}")
    if remaining:
        print(f"  {remaining:,} tiles still failing -- the archive is INCOMPLETE; "
              f"re-run to retry them before treating it as finished")
    print(f"  wrote {args.out}")


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
    if bbox:
        minlon, minlat, maxlon, maxlat = bbox
        # Ordering is checked on the numbers, not inferred from how they quantise.
        # Every bbox lands inside the single tile a coarse zoom offers, transposed
        # or not, so a tile-rectangle test cannot see this at all.
        if minlon >= maxlon or minlat >= maxlat:
            sys.exit(f"--bbox {args.bbox} is minlon,minlat,maxlon,maxlat; "
                     f"this one runs backwards, so it selects nothing.")
        # Overlap is judged at the deepest zoom, where the tiles are smallest and
        # the answer is sharpest. A bbox that meets the source anywhere meets it
        # at every zoom, so one test settles it.
        deep = clamped_bounds(limits[zooms[-1]], bbox, zooms[-1])
        if deep[0] > deep[1] or deep[2] > deep[3]:
            sys.exit(f"--bbox {args.bbox} lies outside the area "
                     f"{src['layer']} covers.")

    # mask and descent both prune on an out-of-band "no tile here" signal. A WMS
    # answers out-of-coverage with a transparent 200, indistinguishable from the
    # placeholder band they are built to tunnel through, so neither ever prunes.
    if args.mode == "coverage" and src["extent"] is None:
        sys.exit(f"--mode coverage reads the service's declared coverage layer, which the "
                 f"{src['kind']} source does not publish; it declares per-zoom tile limits "
                 f"instead, so use --mode fill or full there")
    if args.mode in ("mask", "descent") and not src["empty_on_4xx"]:
        sys.exit(f"--mode {args.mode} prunes on 400/404, which the {src['kind']} source "
                 f"never returns; every empty tile would be tunnelled instead. "
                 f"Use --mode coverage, which bounds the run to the declared footprint.")
    if args.repair and args.refresh:
        sys.exit("--repair and --refresh do different things; pass one")
    if args.refresh and not src["conditional"]:
        sys.exit(f"--refresh needs If-Modified-Since, which the {src['kind']} source "
                 f"does not support; re-download instead")

    con = sqlite3.connect(args.out)
    init_db(con, src, args.mode)

    if args.repair:
        if get_meta(con, "coverage_key") and args.mode != "coverage":
            sys.exit(f"this archive was built with --mode coverage; repairing it as "
                     f"{args.mode} would re-derive candidates by the quadtree descent "
                     f"coverage mode exists to avoid. Pass --mode coverage.")
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

    if args.mode == "coverage":
        coverage_descent(con, args, src, limits, bbox, zooms)
        return

    startup = retry_errors(con, src, args.concurrency)
    if startup:
        print(f"startup: recovered {startup} previously-failed tile(s)")

    start_idx = resume_point(con, args, src, zooms)
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
                        store_tile(con, z, x, y, data)
                flush_editions(con)
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
        set_meta(con, "run_shape", run_shape(args, src))
        con.execute("DELETE FROM _fetched WHERE z=?", (z,))
        con.commit()
        frontier = data_tiles_at(con, z)
        past_seed = args.full_until is None or z >= args.full_until
        if not frontier and args.mode == "descent" and past_seed:
            print(f"  no data at z{z}; descent cannot continue"); break

    b = compute_bounds(con)
    if b:
        set_meta(con, "bounds", "{:.5f},{:.5f},{:.5f},{:.5f}".format(*b))
    finish_run(con)
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


def build_parser():
    """Separate from main() so the verification harness can read these defaults
    instead of restating them, and so cannot end up checking a mask the
    downloader no longer builds."""
    p = argparse.ArgumentParser(description="Download a Traficom chart layer to MBTiles")
    p.add_argument("--source", choices=["wmts", "wms"], default="wmts",
                   help="wmts: rasteripalvelu chart products (default). "
                        "wms: S-57 ENC rendered to raster")
    p.add_argument("--layer", default=None,
                   help='wmts: required, e.g. "Rannikkokartat public". '
                        f'wms: defaults to "{WMS_LAYER}"')
    p.add_argument("--out", required=True)
    p.add_argument("--mode", choices=["descent", "full", "mask", "fill", "coverage"],
                   default=None, help="default: coverage for wms, fill for wmts")
    p.add_argument("--coverage-zoom", type=int, default=11,
                   help="coverage mode: zoom whose tiles the footprint is built on. "
                        "Deeper means a tighter footprint but more coverage renders, "
                        "and shrinks the one-cell dilation margin")
    p.add_argument("--coverage-oversample", type=int, default=16,
                   help="coverage mode: pixels per footprint cell when rendering the "
                        "coverage layer; 16 is where the footprint stops changing")
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
    return p


def main():
    p = build_parser()
    args = p.parse_args()
    if args.source == "wmts" and not args.layer:
        p.error("--layer is required for --source wmts")   # exit 2, with usage
    if args.mode is None:
        args.mode = "coverage" if args.source == "wms" else "fill"
    if not 6 <= args.coverage_zoom <= 14:
        p.error("--coverage-zoom outside 6..14: shallower degenerates into the full "
                "grid, deeper makes the footprint build larger than the download")
    if not 1 <= args.coverage_oversample <= 64:
        p.error("--coverage-oversample outside 1..64")
    run(args)


if __name__ == "__main__":
    main()
