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

# Build order for a raster layer. strip-nodata must come first: downscaling
# averages the source's off-sheet fill into the levels below, and a grey average
# cannot be told from chart content afterwards.
./run dl --layer "Rannikkokartat public" --out mbtiles/rk.mbtiles
./run strip-nodata mbtiles/rk.mbtiles --out mbtiles/rk.stripped.mbtiles
./run downscale mbtiles/rk.stripped.mbtiles --out mbtiles/rk.final.mbtiles
./run currency mbtiles/rk.final.mbtiles      # relabel; publish names the file itself
./run publish mbtiles/rk.final.mbtiles --dest /srv/charts

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

## Stripping the off-sheet fill

Some layers render the area outside their sheets as **opaque black** rather than
leaving it transparent, so a viewer draws black wedges along every sheet edge.
Whole tiles are affected, and so are the tiles the sheet boundary crosses, which
come back part chart and part black.

Colour cannot separate the fill from chart ink — ink is pure `(0,0,0)` too, and
fills up to 5% of an ordinary tile. **Neither can shape.** Eroding the black mask
does leave seeds inside a fill, but Traficom sets place names in heavy serif
capitals whose strokes survive the same erosion, so a local test reads `HELSINKI`
as a solid region and deletes it, leaving the hollow anti-aliased outline behind.

**Position separates them.** The fill is not a shape inside a tile but a region
of the tile grid: it lies beyond the last sheet and runs to the edge of the data,
so the walk that finds the water outside the EEZ finds it too. Chart tiles wall
that walk. Only tiles it reaches, and the chart tiles they touch, are examined at
all — the interior is never a candidate, whatever its ink looks like.

Inside an examined tile three things narrow it further. Fractions are measured
over *opaque* pixels, since fill often arrives with a transparent margin where
the fetch ran past the served extent. The seeds grow back through the solid black
only, because at a sheet edge the chart's own ink abuts the fill and connectivity
would otherwise run down it. And on a chart tile the result keeps only black that
reaches the border facing the off-sheet region: fill continues into the tile the
walk came from, a place name set clear of the edge does not.

A tile that is 95% solid black and was *not* examined stops the run and is named.
Selecting by position is only as good as the walk, and a walk that stops short
would otherwise leave black in the output while every counter reported success.

The downloader strips too, but only tiles that are nothing but fill — decidable
from one tile, and what reports them blank so the crawl does not descend into the
sea beyond the last sheet. Fill *within* a chart tile is left for `strip-nodata`,
which has the grid to tell it from a place name.

```bash
./run strip-nodata mbtiles/fi-yleiskartat250k-2026-06-02.mbtiles --scan   # report only
./run strip-nodata mbtiles/fi-yleiskartat250k-2026-06-02.mbtiles
```

**Run it before `downscale`.** Averaging black into a parent turns it grey, and
grey is indistinguishable from chart content — no later pass can find it.

### The white beyond the chart limits

The same layers also render the area beyond the Finnish EEZ as **opaque white**,
which occludes whatever basemap sits under the chart. This half of the problem
needs a different method, because white is not a colour the chart reserves for
off-sheet: open sea is white too, and locally the two are identical — same
colour, same solidity. Only what encloses them differs.

So the second pass works on the **tile grid**, not on pixels. A tile is *marked*
if any opaque pixel of it is non-white, *featureless* otherwise. Flood the grid
inward from beyond the data, crossing only featureless tiles; a marked tile is a
wall. Featureless tiles the flood reaches are outside and get deleted; those it
cannot reach are enclosed by chart content — open water between soundings — and
stay. Marked tiles are never modified.

Tile granularity is what makes this safe rather than a limitation. The EEZ line
is dashed, and at pixel scale a flood slips between the dashes and empties water
that is well inside coverage; at tile scale every tile the line crosses holds
some of it, so the fence is unbroken. And since the only action is deleting whole
tiles that carry nothing, no chart pixel can be lost — there is no radius or
threshold to mistune.

The deepest zoom decides the boundary, since it resolves it most finely. Coarser
zooms cannot refine it: a coarse tile is marked when *any* part of its ground has
content, so it can say "something here" but never where. Ancestors then follow
their descendants — a tile whose deeper detail survives is kept whatever its own
rendering shows, or the pyramid gains holes that appear and vanish as you zoom.

Verified per chart by comparing against a `--skip-offeez` build: chart markings
are identical at every zoom, to the pixel, while off-sheet tiles go (9,254 on
Yleiskartat, 2,901 on Rannikkokartat). A one-tile white margin survives where a
tile straddles the limit, since such a tile is marked and therefore untouched.

Verified both directions on Yleiskartat, the layer with the most fill. Tiles that
are essentially nothing but fill go from 4,127 at z13 to 1, and every tile that
carries a place name — the ones a shape-only test hollowed out — comes through
byte-identical to the source. The two halves check each other: the off-EEZ pass
that follows classifies unstripped fill as a *marking*, so black left behind
walls its flood, and its counts moving (9,294 tiles dropped, none kept at any
zoom) is how an under-strip shows up even when the pixels are not inspected.

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
edition dates, carried by each tile's `Last-Modified`. The downloader reads them
as it fetches and stamps `source_updated` (newest) / `source_updated_oldest`
into the MBTiles metadata. `currency.py` turns those into the text a chart
client shows, and renames the file. It contacts nothing:

```bash
./run currency mbtiles/rannikkokartat.mbtiles --rename   # -> fi-rannikkokartat-2026-06-29.mbtiles
```

Fetch time is the only moment an edition date can be read at all. `Last-Modified`
is not one for every tile: GeoWebCache renders a tile it does not hold on demand
and stamps it with the moment it stored it, so our own requests manufacture dates
that read exactly like a reseed. Sampling a set later cannot separate them — in a
401-tile sample of veneilykartat the newest real edition and our own footprint
were three tiles each.

During the request that caused it, though, the two are unmistakable. The
response's own `Date` header says when the server answered, and a tile it made
for us is stamped inside that request: measured against Traficom, **0 s** for a
tile rendered on demand versus 71 and 1740 days for cached ones. Both timestamps
come from the server's clock, so neither our clock nor our timezone enters it.

The consequence is that currency is recorded, never re-derived. A set that never
recorded it has none, and `currency.py` says so rather than inventing one — as it
does for WMS-sourced sets, which have no edition date at any time. Relabelling
after a naming change is free and safe, though, since it reads only what is
already stored: worth knowing because `strip-nodata` and `downscale` copy
metadata verbatim, so rebuilding a chart carries its old name forward rather than
refreshing it.

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

## Publishing

`publish` puts finished sets into the directory a web server exposes, and writes
`charts.json` and `index.html` beside them describing every set present:

```bash
./run publish mbtiles/*.final.mbtiles --dest /srv/charts
```

Each file is copied into `<dest>/.staging/`, read back and compared against what
was read from the source, and only then renamed into place. Both halves matter.
Staging inside the destination keeps the rename on one filesystem, where it is
atomic — a client gets the whole old file or the whole new one, never a partial
write. Reading back is what an upload cannot do: a transfer that silently
truncated three of five files went unnoticed for six days, because size and exit
status both looked plausible.

**The staging directory and `.publish.lock` are dotfiles inside the served
directory**, and a partially written chart lives there for as long as a multi-GB
copy takes. nginx serves dotfiles unless told not to, so deny them:

```nginx
location ~ /\. { deny all; }
```

Everything that can refuse a run happens before the first rename — truncation
checks, name validation, staging, verification, and building the whole manifest
— so a refusal leaves the previous set exactly as it was and a failed refresh
degrades to stale charts, never to missing or half-written ones. Past that point
only the renames, the retirements and one small manifest write remain; a failure
there raises `PartiallyPublished` rather than `Unpublishable`, so a caller can
tell "nothing happened" from "the destination changed and the run stopped".

A run is refused if a source is shorter than its own SQLite header says it is
(`strip-nodata` and `downscale` both run with `journal_mode=OFF`, so a killed
writer leaves a partial file rather than rolling back, and a faithful copy of a
truncated file verifies perfectly); if a source has an uncheckpointed `-wal`,
whose contents copying the database file alone would drop; if two sources are
different editions of one layer, which would each retire the other; if the
metadata produces anything but a `fi-<layer>-<date>.mbtiles` name, since that
name reaches `open`, `glob` and `unlink`; or if another publish holds the
destination lock.

Naming and retirement come from metadata, not filenames. The destination name is
rebuilt from the layer and `source_updated` the file carries, so the working
filename does not matter and `currency --rename` is not a prerequisite. A file
already in the destination is retired only when its *own* metadata records the
same layer and an older edition — matching on the filename would also hit build
variants, hand-placed files, and a newer edition being republished over.

The manifest closes a gap the filenames cannot. A name records the *source*
edition, so the fill-strip and off-EEZ work changed the tiles without changing
any name, and anything caching by URL kept serving the old content.

| Field | Meaning |
|---|---|
| `schema` | Format version. Fields may be added without a bump; an existing field never changes meaning or type without one. Refuse a number you do not know rather than guessing |
| `generated` | RFC 3339 UTC instant the manifest was written, not when the charts were built |
| `pipeline` | `git describe` of the code that ran, or `unknown` outside a checkout. An opaque token: not orderable, not comparable across repos |
| `filename` | Resolves relative to the manifest's own URL. Changes with every edition, so track a chart by `layer` instead |
| `layer` | The chart this file is an edition of, and the prefix of its filename. Stable across editions. Null for a file that records no layer |
| `bytes`, `sha256` | Size and lowercase-hex digest of the complete file as served |
| `source_edition`, `source_edition_oldest` | Newest and oldest tile edition dates the download recorded. Null for a source that publishes no edition date |
| `processing` | The stamps `strip-nodata` and `downscale` leave in the file, or `none` |
| `name` | The label a chart client shows |
| `readable` | Present and `false` only for a file in the destination this tool cannot open as MBTiles. Such a file still gets a size and a digest |

```json
{
  "schema": 1,
  "generated": "2026-08-08T11:36:40+00:00",
  "pipeline": "37dab52",
  "charts": [
    {
      "filename": "fi-veneilykartat-2026-06-21.mbtiles",
      "layer": "fi-veneilykartat",
      "bytes": 245039104,
      "sha256": "ad697da38f74…",
      "source_edition": "2026-06-21",
      "source_edition_oldest": "2025-01-20",
      "processing": "opaque-black-r4-edge+offeez-tilelevel; box-2x-premultiplied from z15 on 2026-08-09",
      "name": "Veneilykartat 2026-06-21"
    }
  ]
}
```

`index.html` is the same facts for a reader: the chart list with sizes, editions
and digests, what each set covers, the licence and Traficom's attribution, and
the not-for-navigation warning. It is written from the manifest data in the same
call, so the page and the digests cannot come to disagree, and it fetches
nothing — no fonts, stylesheets or scripts — because these charts get downloaded
onto boats with no connection to anything but the server they came from. Chart
descriptions live in `index_page.py`; everything else comes from the metadata.

A retired edition disappears as soon as its replacement is in place, so a cached
manifest can name a file that now 404s; re-fetch `charts.json` rather than
treating it as an error. Run `currency` before publishing when the naming rules
have changed since the set was downloaded: `strip-nodata` and `downscale` copy
metadata verbatim, so a rebuilt chart carries its old label forward until
relabelled.

Python tools run via [uv](https://docs.astral.sh/uv/) with PEP 723 inline
dependencies — no manual environment setup. `./run test` runs the suite.
