# fi-nautical-charts

Tooling to build [Signal K](https://signalk.org/) / Freeboard-SK MBTiles chart
sets from Finnish open nautical charts.

## Sources

- **Traficom raster WMTS** — official Finnish nautical charts, open data under
  CC BY 4.0. Endpoint `https://julkinen.traficom.fi/rasteripalvelu/wmts`. The
  `WGS84_Pseudo-Mercator` tile matrix is standard web-mercator XYZ (z0–15 for
  the marine layers). This is the authoritative, license-clean source.

- **Traficom S-57 ENC WMS** (`--source wms`) — the electronic navigational chart
  rendered server-side to raster. Endpoint `https://julkinen.traficom.fi/s57/wms`,
  layer `cells`, style `style-id-202` ("Full"). A different product from the raster
  charts above, not a replacement for any of them.

The rendered raster charts are **only** available through the WMTS view service;
Traficom's bulk download service offers vector shapefiles (WFS) and depth GeoTIFFs
(WCS), not the chart images. So tile-by-tile WMTS fetching is the route to raster
MBTiles — legitimate here because the WMTS is the CC BY 4.0 distribution.

The same argument covers the ENC WMS independently: its own `GetCapabilities`
declares `AccessConstraints` of CC BY 4.0, *"Lähde: Traficom. Ei navigointikäyttöön.
Ei täytä asianmukaisen merikartan vaatimuksia."*

The WMS has no `TileMatrixSetLimits`, so its per-zoom tile rectangles are derived
from a configured extent instead. It also serves no `Last-Modified`, `ETag` or
edition date, so `refresh` and `currency` do not apply to WMS-sourced files —
keeping one current means re-downloading it. Because out-of-coverage arrives as a
transparent 200 rather than a 404, `--mode mask` and `--mode descent` are refused
on this source: both prune on 400/404 and would tunnel through every empty tile.
`coverage` is the mode for this source, and is its default.

**Mode: coverage (WMS only).** Every other mode decides a deeper zoom's candidates
from a shallower zoom's content. That holds for a pre-rendered pyramid but not for
a WMS rendering S-57 on demand: usage-band visibility is scale-gated, so a cell can
draw nothing at one scale and a full harbour chart at another, and following
content strands whatever sits behind a blank ancestor. So the footprint is read
from the service's own `coverage` layer instead, and the same footprint gates every
zoom.

The parent `coverage` layer renders exactly the union of `coverage.1`–`coverage.9`
(verified pixel-for-pixel), so one request replaces nine. `coverage.1` alone is not
enough — Traficom's usage bands are not nested, and Saimaa carries Coastal cover
with no General above it.

Footprint cells are rendered `--coverage-oversample` pixels across (default 16,
where the footprint stops changing) and max-pooled, then dilated by one cell.
Coarse rasterisation over-includes at polygon boundaries but drops the occasional
small cell, so the dilation is what makes the result independent of the sampling
resolution; it costs about 8% more cells. The build asserts the footprint does not
touch the edge of the configured extent, so an extent that crops real coverage
fails loudly rather than silently losing what lies beyond.

**Verification.** `./run verify-coverage oracle` runs coverage and `full` over the
same area and diffs the content tiles; `mask` checks two footprint properties the
diff cannot see. Every result below names its zoom range, because the pruning a
run exercises depends on it.

- **Whole extent, z0–10**: 1,556 content tiles, **0 missed**, at 55% fewer
  requests (2,042 against 4,586). Below z7 the projected footprint keeps the whole
  extent rectangle, so that band compares two identical enumerations and the saving
  is earned at z7–z10. At z11 the mask is tight — 4,667 tiles carry all 4,319
  content tiles that enumerating 13,203 finds.
- **Deep zoom, z12–16**: 0 missed on the five `span-*` boxes, which reach from
  charted cells through the dilation ring to cells outside the mask and so prune
  24–52% while still finding every content tile `full` does. One per coverage edge.
- **Containment only, z12–16**: 0 missed on Helsinki, Vänö, Bogskär and Saimaa
  (the band discontinuity). These lie wholly inside the mask, so coverage
  enumerates exactly what `full` does — they show it keeps what it should, and
  nothing about pruning.
- **Off the footprint, z12–14**: inland Lapland and the open Baltic prune to zero
  requests; `full` spends 389 and 701 confirming the same emptiness.
- **Chunking**: a footprint assembled from 18 chunks equals one assembled from 2,
  cell for cell. Both come from `build_coverage` itself, not a copy of its loop.
- **Containment**: built at z13 — a finer pixel grid, different chunk boundaries,
  4,096 samples per z11 cell against 256 — every projected cell falls inside the
  shipped mask, which is 4.9% larger than the fine build reaches.

Scope of the claim: the deep-zoom evidence is five windows on a footprint whose
perimeter is 340 cells, so it samples the boundary rather than covering it. A box
can also pass while testing nothing, so each carries the relation it is meant to
have — `inside`, `straddle` or `outside` — and the run fails if that does not
hold: an `inside` box that prunes, a `straddle` box that does not, an `outside`
box that requests anything, or an oracle side that finds no content at all.

**Cost** (`verify-coverage cost`, sampled over the footprint rather than over the
boxes, whose bytes per tile span 15×). Bytes per tile *fall* with depth, 5.6 kB at
z11 to 1.0 kB at z16, since the same ink spreads over four times the tiles, so a
level costs about 2.9× its parent rather than 4×:

| Cap | Requests | Storage | Time at `--concurrency 8` |
|-----|---------:|--------:|--------------------------:|
| z14 | 397k | 0.9 GB | ~5 h |
| z15 | 1.59M | 2.4 GB | ~15 h |
| z16 | 6.37M | 6.7 GB | ~49 h |

**z15 is the cap**, and the default. z16 does carry genuine new detail — `./run
native-zoom --zmin 14 --zmax 16` scores the z15→z16 transition at 0.073 median
novelty against a 0.03 threshold — but less than half of what z15 adds (0.160),
with 89% of its pixels exactly reproducible by upscaling its parent. Paying 64% of
the bytes and 70% of the time for that is the trade being declined, not an absence
of detail. Adding it later costs its own 34.5 h plus 1.5–2 h, since a re-run keeps
every stored tile and re-requests only the ~10% that were empty.

Cost figures come from one sampling burst on one network, so treat the hours as an
order of magnitude. Bytes per tile is heavy-tailed, so its error is cv/√n rather
than the 1/√n that would apply to the content rate.

Attribution required: *"Source: Traficom. Not for navigation use. Does not meet
official nautical chart requirements."*

## Layers (Traficom WMTS)

Base layers each pair with an `erikoiskartat` (special/large-scale) companion:

| Base | Special | Notes |
|------|---------|-------|
| `Rannikkokartat public` | `Rannikkokarttojen erikoiskartat` | Coastal charts; covers Helsinki |
| `Veneilykartat public` | `Veneilykarttojen erikoiskartat` | Boating charts; partial coverage |
| `Satamakartat` | `Satamakarttojen erikoiskartat` | Harbour charts |
| `Yleiskartat 100k` / `250k` | — | Small-scale overview |

The `public` suffix marks the openly-licensed subset of each product.

## Usage

```bash
./run help                 # list commands
./run ab                   # A/B render (default: Helsinki front, Rannikkokartat vs Merikarttasarjat)
./run ab --sources "traficom:Satamakartat,traficom:Veneilykartat public" --bbox 26.7,60.3,27.0,60.45

# Download a layer to MBTiles. Descent (default) prunes empty subtrees; sparse
# overlays seed with --full-until so no isolated feature is pruned.
./run dl --layer "Merikarttasarjat public" --out mbtiles/merikarttasarjat.mbtiles
./run dl --layer "Satamakartat" --full-until 11 --out mbtiles/satamakartat.mbtiles
./run dl --layer "Veneilykartat public" --full-until 10 --out mbtiles/veneilykartat.mbtiles

# Retry tiles that failed on an earlier run (e.g. transient network errors).
./run dl --layer "Rannikkokartat public" --out mbtiles/rannikkokartat.mbtiles --repair

# Download the ENC. WMS source defaults to --mode coverage and --maxzoom 15.
./run dl --source wms --out mbtiles/fi-enc.mbtiles

# Check coverage mode against brute-force enumeration before trusting a run.
# Class assertions hold from the coverage zoom (11) down, so keep --minzoom there.
./run verify-coverage oracle --box span-kemi --minzoom 12 --maxzoom 16
./run verify-coverage oracle --whole-extent          # z0-10; deeper is millions
./run verify-coverage mask          # chunking + containment + box classes
./run verify-coverage cost          # price a full run from a footprint sample

# Is the deepest level real detail, or an upscale of the one above it?
./run native-zoom mbtiles/fi-enc.mbtiles --zmin 14 --zmax 16
```

Failed tiles are recorded in an `_errors` table and retried automatically —
after each zoom during a download, and at the start of any resumed run. If a
run finished before error tracking existed, `--repair` re-derives the expected
tiles and re-fetches whatever is missing from the archive.

## Downscaling the pyramid

Traficom serves each layer's lower zoom levels as crude rescales of the deepest
native level, often close to nearest-neighbour, so lines look jagged. `./run
downscale` rebuilds every level below the deepest one by proper anti-aliased
reduction:

```bash
./run downscale mbtiles/fi-satamakartat-2026-06-29.mbtiles \
    --out mbtiles/fi-satamakartat-2026-06-29.downscaled.mbtiles
```

- Cascades one octave at a time (z15→z14→…), each level from the level above —
  a standard mip pyramid.
- Each output pixel is the box average of the 2×2 block beneath it. For an exact
  2× step that block lies entirely inside the tile's own children, so the result
  is **provably seam-free** with no gutter; box also avoids the ringing haloes a
  Lanczos kernel adds around hard chart edges. Alpha is premultiplied so
  transparent off-sheet pixels don't bleed dark haloes into coastlines.
- Sparse-aware (a parent is built only where a child exists) and non-destructive:
  the source opens read-only, the deepest level is copied verbatim, and only
  lower levels are regenerated. Idempotent input→output — drop it into a CI step.
- Never loses coverage: where a mid-zoom tile has content the source level lacks
  (Traficom sometimes renders a feature at a lower zoom only), the original tile
  is kept. Per-tile image work is fanned out across all cores.

`--source-zoom` overrides the level to downscale from (default: deepest present);
`--min-zoom` limits how far down to regenerate (default: the file's lowest zoom).

`./run native-zoom <file>` reports whether each level is genuine detail or an
upscale, confirming the deepest level is worth downscaling from.

## Currency and refresh

Traficom reseeds its tile cache region by region, so a set spans a range of
edition dates, readable from each tile's `Last-Modified` header. `currency.py`
samples these, stamps `source_updated` (newest) / `source_updated_oldest` /
`downloaded` into the MBTiles metadata, and renames the file:

```bash
./run currency mbtiles/rannikkokartat.mbtiles --rename   # -> fi-rannikkokartat-2026-06-29.mbtiles
```

On-demand tiles (ones our own download forced GeoWebCache to generate) carry
today's date, so they're excluded from `source_updated` — it reflects real
editions, not our footprint. That exclusion only holds on the day of the
download: months later those tiles read back as an ordinary edition of the day
we fetched them. So never re-sample a set to fix its labelling —

```bash
./run currency mbtiles/fi-rannikkokartat-2026-06-29.mbtiles --restamp
```

rebuilds `name` and `description` from the dates the file already carries,
touching no network and leaving `downloaded` alone. Reach for it when the
naming rules change: the name is written once, at download time, and
`strip-nodata` and `downscale` both copy metadata verbatim, so rebuilding a
chart carries its original name forward rather than refreshing it.

Keep a set current without re-downloading it:

```bash
./run refresh mbtiles/fi-rannikkokartat-2026-06-29.mbtiles
```

`refresh` walks the existing coverage with `If-Modified-Since` = the day after
the last download: unchanged tiles return 304 (cheap), only reseded tiles
transfer data. New chart *areas* (coverage that didn't exist before) need a
fresh `dl` run.

Files are named `<country>-<layer>-<newest-edition-date>.mbtiles`.

**Pick the right layer first.** `Merikarttasarjat public` (the combined official
chart series) is the complete coastal base — it charts the whole coast including
the archipelago. `Rannikkokartat public` looks identical around cities but has
real **coverage holes** over the inner archipelago (it serves transparent
placeholder tiles there), so it is not a safe base on its own.

**Mode: fill (default).** `fill` is the complete, no-full-grid method: it
full-fetches the solid low zoom (`--solid-zoom`, default 11) for the chart
footprint, then flood-fills each deeper zoom outward from seeds, bounded to that
footprint. Verified tile-for-tile against `full` on Vänö, Helsinki, inland and
open sea (0 content missed, up to 90% fewer requests). It reaches content behind
the placeholder band and in transparent quadrants of content tiles, while never
touching off-sheet land/sea. Caveat: `--solid-zoom` must be the layer's last
dense zoom before the placeholder band; verify a new layer against `full` on a
few small boxes before trusting it. `full` is retained only as that test oracle
and for special cases.

**(legacy) Mode: full vs descent.** Traficom serves transparent
placeholder tiles at mid zooms where a chart has no data at that scale, but then
serves **real content again at a deeper zoom** (e.g. Rannikkokartat near Vänö:
z8–11 content, z12–14 placeholder, z15 content). `descent` only follows children
of content tiles, so it prunes at the placeholder and **misses the deep content
behind it** — in the Vänö box it lost 100% of the z15 tiles. So `full` (fetch
every zoom's full `TileMatrixSetLimits` rectangle) is the safe default. `descent`
is a ~5× cheaper optimization that is only correct for a layer with no
placeholder gaps (verified dense, e.g. `Merikarttasarjat public`, which matches
full enumeration). A re-run reuses already-stored tiles, so `--mode full` over an
existing descent archive tops it up to complete coverage without re-fetching what
it already has.

`--mode mask` is a fast footprint-guided variant (tunnels through placeholders,
prunes off-sheet land/sea via each tile's alpha). It's much cheaper on inland
areas but **not complete** — content that appears only at the deepest zoom in a
spot the coarser tile leaves transparent gets pruned (verified: 29 z15 tiles
missed at Helsinki). Fast previews only; `full` for authoritative charts.

Python tools run via [uv](https://docs.astral.sh/uv/) with PEP 723 inline
dependencies — no manual environment setup.
