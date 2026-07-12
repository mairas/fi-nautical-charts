#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["requests", "pillow"]
# ///
"""Render side-by-side A/B comparisons of Traficom nautical raster chart layers.

Fetches the same geographic area (bbox + zoom) from two or more Traficom WMTS
layers, stitches each into a panel, and composes the panels horizontally with
labels so the cartography can be compared directly.

Sources are given as tokens:
  traficom:<Layer>             Traficom WMTS layer, e.g. "Rannikkokartat public"

Traficom returns transparent PNGs where a layer has no data and HTTP 400
(TileOutOfRange) outside a layer's declared extent; both render as white, so a
blank panel means "this layer does not cover this area".
"""

from __future__ import annotations

import argparse
import io
import math
import sys
from concurrent.futures import ThreadPoolExecutor

import requests
from PIL import Image, ImageDraw, ImageFont

TILE = 256
TRAFICOM = ("https://julkinen.traficom.fi/rasteripalvelu/wmts"
            "?service=WMTS&request=GetTile&version=1.0.0&style="
            "&tilematrixset=WGS84_Pseudo-Mercator&format=image/png"
            "&layer=Traficom:{layer}"
            "&tilematrix=WGS84_Pseudo-Mercator:{z}&tilecol={x}&tilerow={y}")


def lon2x(lon, z):
    return int((lon + 180.0) / 360.0 * (1 << z))


def lat2y(lat, z):
    r = math.radians(lat)
    return int((1.0 - math.asinh(math.tan(r)) / math.pi) / 2.0 * (1 << z))


def parse_source(tok):
    if tok.startswith("traficom:"):
        layer = tok.split(":", 1)[1]
        return {"label": f"Traficom {layer}", "kind": "traficom", "layer": layer}
    sys.exit(f"unknown source token: {tok}")


def url_for(src, z, x, y):
    return TRAFICOM.format(layer=requests.utils.quote(src["layer"]), z=z, x=x, y=y)


def fetch_tile(session, src, z, x, y):
    try:
        r = session.get(url_for(src, z, x, y), timeout=30)
    except requests.RequestException:
        return None
    if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
        img = Image.open(io.BytesIO(r.content)).convert("RGBA")
        if img.getchannel("A").getextrema()[1] == 0:
            return None  # fully transparent 200 tile (overlay with no data here)
        return img
    return None  # 404 (sea) / 400 (out of range) -> treated as no data


def render_panel(src, z, x0, x1, y0, y1):
    session = requests.Session()
    session.headers.update({"User-Agent": "fi-nautical-charts/ab"})
    cols, rows = x1 - x0 + 1, y1 - y0 + 1
    panel = Image.new("RGBA", (cols * TILE, rows * TILE), (255, 255, 255, 255))
    got = 0
    coords = [(x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        tiles = pool.map(lambda c: (c, fetch_tile(session, src, z, c[0], c[1])), coords)
        for (x, y), img in tiles:
            if img is None:
                continue
            got += 1
            panel.alpha_composite(img, ((x - x0) * TILE, (y - y0) * TILE))
    return panel, got, cols * rows


def label_bar(width, text, height=30):
    bar = Image.new("RGBA", (width, height), (24, 40, 60, 255))
    d = ImageDraw.Draw(bar)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 18)
    except OSError:
        font = ImageFont.load_default()
    d.text((10, height // 2), text, fill=(255, 255, 255, 255), font=font, anchor="lm")
    return bar


def main():
    p = argparse.ArgumentParser(description="A/B render Traficom chart layers")
    p.add_argument("--bbox", default="24.90,60.09,25.06,60.17",
                   help="minlon,minlat,maxlon,maxlat (default: Helsinki front)")
    p.add_argument("--zoom", type=int, default=14)
    p.add_argument("--sources",
                   default="traficom:Rannikkokartat public,traficom:Merikarttasarjat public",
                   help="comma-separated source tokens")
    p.add_argument("--out", default="out/ab.png")
    args = p.parse_args()

    minlon, minlat, maxlon, maxlat = (float(v) for v in args.bbox.split(","))
    z = args.zoom
    x0, x1 = lon2x(minlon, z), lon2x(maxlon, z)
    y0, y1 = lat2y(maxlat, z), lat2y(minlat, z)
    srcs = [parse_source(t.strip()) for t in args.sources.split(",")]
    print(f"bbox={args.bbox} z={z}  tiles: x {x0}-{x1} ({x1-x0+1}) y {y0}-{y1} ({y1-y0+1})")

    panels = []
    for src in srcs:
        panel, got, total = render_panel(src, z, x0, x1, y0, y1)
        print(f"  {src['label']:38} {got}/{total} tiles with data")
        header = label_bar(panel.width, f"{src['label']}  (z{z})")
        stacked = Image.new("RGBA", (panel.width, panel.height + header.height),
                            (255, 255, 255, 255))
        stacked.alpha_composite(header, (0, 0))
        stacked.alpha_composite(panel, (0, header.height))
        panels.append(stacked)

    gap = 12
    W = sum(pn.width for pn in panels) + gap * (len(panels) - 1)
    H = max(pn.height for pn in panels)
    canvas = Image.new("RGBA", (W, H), (230, 230, 230, 255))
    x = 0
    for pn in panels:
        canvas.alpha_composite(pn, (x, 0))
        x += pn.width + gap

    import os
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    canvas.convert("RGB").save(args.out)
    print(f"wrote {args.out}  ({canvas.width}x{canvas.height})")


if __name__ == "__main__":
    main()
