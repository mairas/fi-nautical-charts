#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Browse local MBTiles in a browser, several at once.

The charts are mostly judged by what they *removed*, and a removed pixel is
transparent -- which on a white page looks like nothing at all. So the map sits
on a coloured backdrop by default, the same one the boat's basemap shows
through: anything the strip took reads as that colour rather than as paper.

Every file given becomes a switchable layer, so a processed set and the archive
it came from can be flipped between at the same place and zoom.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
         "webp": "image/webp", "pbf": "application/x-protobuf"}


class Chart:
    def __init__(self, path: Path):
        self.path = path
        self.key = path.stem
        self.local = threading.local()
        m = dict(self.con().execute("SELECT name, value FROM metadata"))
        self.fmt = m.get("format", "png")
        self.label = m.get("name") or path.stem
        self.minzoom = int(m.get("minzoom", 0))
        self.maxzoom = int(m.get("maxzoom", 20))
        self.bounds = [float(v) for v in m["bounds"].split(",")] if "bounds" in m else None
        levels = self.con().execute(
            "SELECT COUNT(DISTINCT zoom_level), COUNT(*) FROM tiles").fetchone()
        self.centre = self._centre()
        self.note = (f"{levels[0]} levels, {levels[1]:,} tiles, "
                     f"z{self.minzoom}-{self.maxzoom}, "
                     f"{m.get('nodata_stripped', 'unstripped')}")

    def _centre(self) -> list[float] | None:
        """Where this chart actually has tiles, in lat/lon.

        Not the declared bounds: those describe the extent that was asked for,
        and for a coastal chart their centre is inland, where the file holds
        nothing at any zoom. Opening there shows an empty map and a screenful of
        404s that look like a fault.
        """
        levels = self.con().execute(
            "SELECT zoom_level, COUNT(*) FROM tiles GROUP BY zoom_level "
            "ORDER BY zoom_level").fetchall()
        pick = next((z for z, n in levels if n >= 16), levels[-1][0] if levels else None)
        if pick is None:
            return None
        tiles = self.con().execute(
            "SELECT tile_column, tile_row FROM tiles WHERE zoom_level=?",
            (pick,)).fetchall()
        if not tiles:
            return None
        # the tile nearest the middle of them, not the middle itself: this
        # coastline is an L around the Gulfs, and its mean is inland
        mx = sum(t[0] for t in tiles) / len(tiles)
        my = sum(t[1] for t in tiles) / len(tiles)
        col, row_tms = min(tiles, key=lambda t: (t[0] - mx) ** 2 + (t[1] - my) ** 2)
        n = 1 << pick
        lon = (col + 0.5) / n * 360 - 180
        y = n - row_tms - 0.5          # TMS rows count from the bottom
        lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
        return [lat, lon]

    def con(self) -> sqlite3.Connection:
        # one connection per thread: sqlite objects are not shared across them,
        # and the server is threaded so a slow tile does not block the page
        if not hasattr(self.local, "con"):
            self.local.con = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        return self.local.con

    def tile(self, z: int, x: int, y: int) -> bytes | None:
        row = self.con().execute(
            "SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? "
            "AND tile_row=?", (z, x, (1 << z) - 1 - y)).fetchone()
        return row[0] if row else None


PAGE = """<!doctype html><meta charset=utf-8><title>fi-nautical-charts</title>
<link rel=stylesheet href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
 html,body,#map{height:100%;margin:0}
 #map{background:__BACKDROP__}
 .leaflet-control-layers-list{font:13px/1.5 system-ui,sans-serif}
 .note{color:#666;font-size:11px}
</style><div id=map></div><script>
const charts = __CHARTS__;
// The floor is the shallowest level any of these files actually holds. Below
// it Leaflet would keep asking for that level and scaling it down -- at z0 that
// is the whole 32x32 grid at once, which queues six at a time and leaves the
// map blank for minutes. There is nothing to see out there anyway.
const floor = Math.min(...charts.map(c => c.minzoom));
const map = L.map('map', {center: __CENTER__, zoom: __ZOOM__,
                          minZoom: floor, maxZoom: 20});
const overlays = {};
charts.forEach((c, i) => {
  const opts = {
    maxNativeZoom: c.maxzoom, minNativeZoom: c.minzoom, tileSize: 256,
    keepBuffer: 2, updateWhenZooming: false,
    attribution: '&copy; Traficom. Not for navigation use.'
  };
  if (c.bounds) {
    opts.bounds = [[c.bounds[1], c.bounds[0]], [c.bounds[3], c.bounds[2]]];
  }
  const layer = L.tileLayer('/tiles/' + c.key + '/{z}/{x}/{y}', opts);
  layer.on('tileerror', e => console.warn('tile failed', c.key, e.coords));
  overlays[c.label + ' <span class=note>' + c.note + '</span>'] = layer;
  if (i === 0) layer.addTo(map);
});
L.control.layers(null, overlays, {collapsed: false, sortLayers: false}).addTo(map);
L.control.scale({imperial: false}).addTo(map);
const readout = L.control({position: 'bottomleft'});
readout.onAdd = function () {
  this._d = L.DomUtil.create('div', 'leaflet-bar');
  this._d.style.cssText = 'background:#fff;padding:2px 6px;font:12px system-ui';
  return this._d;
};
readout.addTo(map);
function show(e) {
  readout._d.textContent = e.latlng.lat.toFixed(5) + ', ' + e.latlng.lng.toFixed(5)
    + '  z' + map.getZoom().toFixed(1);
}
map.on('mousemove', show);
// reachable from the console and from a driver: the map is script-scoped,
// so without this there is no handle to check a zoom problem against
window.__map = map; window.__overlays = overlays;
</script>"""


def build_page(charts: list[Chart], backdrop: str) -> bytes:
    meta = [{"key": c.key, "label": c.label, "note": c.note,
             "minzoom": c.minzoom, "maxzoom": c.maxzoom, "bounds": c.bounds}
            for c in charts]
    located = [c for c in charts if c.centre]
    centre = located[0].centre if located else [60.2, 24.9]
    page = (PAGE.replace("__CHARTS__", json.dumps(meta))
                .replace("__CENTER__", json.dumps(centre))
                .replace("__ZOOM__", str(max(charts[0].minzoom, 8)))
                .replace("__BACKDROP__", backdrop))
    return page.encode()


def serve(paths: list[Path], port: int, backdrop: str, open_browser: bool) -> None:
    charts = []
    for p in paths:
        try:
            charts.append(Chart(p))
        except sqlite3.Error as exc:
            print(f"  skipping {p.name}: {exc}", file=sys.stderr)
    if not charts:
        sys.exit("no readable MBTiles given")
    # The whole point is comparing a processed set with the archive it came
    # from, and those are the same filename in two directories -- so the stem
    # alone collides, and the second would silently answer for both.
    stems = [c.key for c in charts]
    for c in charts:
        if stems.count(c.key) > 1:
            c.label = f"{c.label} [{c.path.parent.name}]"
            c.key = f"{c.path.parent.name}-{c.key}"
    seen: dict[str, int] = {}
    for c in charts:
        if c.key in seen:
            seen[c.key] += 1
            c.key = f"{c.key}-{seen[c.key]}"
            c.label = f"{c.label} ({seen[c.key]})"
        else:
            seen[c.key] = 1
    by_key = {c.key: c for c in charts}
    page = build_page(charts, backdrop)
    for c in charts:
        print(f"  {c.label}  ({c.note})")

    class Handler(BaseHTTPRequestHandler):
        # Keep-alive. The default is HTTP/1.0, which closes the socket after
        # every response, while the browser holds six open and reuses them --
        # so under a fast zoom it puts the next tile request on a socket the
        # server is already closing and the tile dies with ERR_CONNECTION_RESET
        # or ERR_SOCKET_NOT_CONNECTED. Every response here carries a
        # Content-Length, which is what 1.1 requires to reuse the connection.
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def send(self, code, body=b"", ctype="text/plain"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_GET(self):
            parts = self.path.split("?")[0].strip("/").split("/")
            if parts == [""]:
                return self.send(200, page, "text/html; charset=utf-8")
            if parts == ["favicon.ico"]:
                return self.send(204)
            if len(parts) == 5 and parts[0] == "tiles":
                _, key, z, x, y = parts
                chart = by_key.get(key)
                if chart is None:
                    return self.send(404)
                try:
                    blob = chart.tile(int(z), int(x), int(y.split(".")[0]))
                except (ValueError, sqlite3.Error):
                    return self.send(404)
                if blob is None:
                    return self.send(404)
                return self.send(200, blob, TYPES.get(chart.fmt, "image/png"))
            self.send(404)

    url = f"http://localhost:{port}/"
    print(f"\nserving {len(charts)} chart(s) at {url}   (ctrl-c to stop)")
    if open_browser:
        threading.Timer(0.5, webbrowser.open, (url,)).start()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


def main():
    p = argparse.ArgumentParser(description="Browse local MBTiles in a browser")
    p.add_argument("files", nargs="+", type=Path)
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--backdrop", default="#a7d0da",
                   help="what shows through where a chart is transparent "
                        "(default: the basemap blue the boat draws under it)")
    p.add_argument("--no-open", action="store_true", help="do not open a browser")
    args = p.parse_args()
    serve(args.files, args.port, args.backdrop, not args.no_open)


if __name__ == "__main__":
    main()
