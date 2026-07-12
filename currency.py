#!/usr/bin/env python3
"""Stamp source-currency metadata into a Traficom MBTiles chart set, sampled
from the WMTS tiles' Last-Modified headers, and optionally rename the file to
fi-<layer>-<newest>.mbtiles.

Traficom reseeds its tile cache region by region, so a set spans a range of
dates; we record the newest (headline currency) and the oldest sampled.
"""

import argparse
import concurrent.futures
import datetime
import email.utils
import os
import sqlite3
import ssl
import urllib.parse
import urllib.request

BASE = "https://julkinen.traficom.fi/rasteripalvelu/wmts"
CTX = ssl.create_default_context()

HUMAN = {  # slug -> (english label, Finnish product name)
    "rannikkokartat": ("Coastal charts", "Rannikkokartat"),
    "satamakartat": ("Harbour charts", "Satamakartat"),
    "veneilykartat": ("Boating charts", "Veneilykartat"),
    "merikarttasarja": ("Nautical chart series", "Merikarttasarja"),
    "merikarttasarjat": ("Nautical chart series", "Merikarttasarjat"),
    "yleiskartat": ("General charts", "Yleiskartat"),
}


def slug(layer):
    return layer.lower().replace(" public", "").replace(" ", "").split("erikois")[0]


def last_modified(layer, z, x, y):
    q = {"service": "WMTS", "request": "GetTile", "version": "1.0.0", "style": "",
         "tilematrixset": "WGS84_Pseudo-Mercator", "format": "image/png",
         "layer": f"Traficom:{layer}", "tilematrix": f"WGS84_Pseudo-Mercator:{z}",
         "tilecol": str(x), "tilerow": str(y)}
    req = urllib.request.Request(BASE + "?" + urllib.parse.urlencode(q), method="HEAD")
    try:
        r = urllib.request.urlopen(req, context=CTX, timeout=30)
        lm = r.headers.get("Last-Modified")
    except Exception:
        return None
    return email.utils.parsedate_to_datetime(lm).date() if lm else None


def sample_dates(con, layer, per_zoom=20, concurrency=8):
    """Newest and oldest real edition dates. GeoWebCache stamps on-demand tiles
    (ones our own download forced it to generate) with today's date, so those
    are excluded from 'newest' -- they reflect our request, not a chart update."""
    tiles = []
    for (z,) in con.execute("SELECT DISTINCT zoom_level FROM tiles ORDER BY zoom_level"):
        rows = con.execute("SELECT tile_column, tile_row FROM tiles WHERE zoom_level=? "
                           "ORDER BY tile_column * 7919 % 104729 LIMIT ?", (z, per_zoom)).fetchall()
        tiles += [(z, c, (1 << z) - 1 - r) for c, r in rows]
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        dates = [d for d in pool.map(lambda t: last_modified(layer, *t), tiles) if d]
    if not dates:
        return None, None, 0
    today = datetime.date.today()
    editions = [d for d in dates if d < today] or dates
    return max(editions), min(dates), len(dates)


def main():
    p = argparse.ArgumentParser(description="Stamp source-currency metadata + rename")
    p.add_argument("mbtiles")
    p.add_argument("--country", default="fi")
    p.add_argument("--rename", action="store_true")
    args = p.parse_args()

    con = sqlite3.connect(args.mbtiles)
    meta = dict(con.execute("SELECT name, value FROM metadata").fetchall())
    layer = meta.get("wmts_layer") or meta.get("name")
    newest, oldest, n = sample_dates(con, layer)
    if not newest:
        raise SystemExit("no Last-Modified sampled; is the layer name correct?")

    sl = slug(layer)
    label, fin = HUMAN.get(sl, ("Nautical charts", layer))
    today = datetime.date.today().isoformat()
    stamp = {
        "wmts_layer": layer,
        "name": f"{args.country.upper()} {label} ({fin}) {newest}",
        "source_updated": newest.isoformat(),
        "source_updated_oldest": oldest.isoformat(),
        "downloaded": today,
        "description": (f"Finnish {label.lower()} (Traficom {fin}, WMTS, CC BY 4.0). "
                        f"Source updated {newest} (oldest sampled region {oldest}); "
                        f"downloaded {today}. Not for navigation use; does not meet "
                        f"official nautical chart requirements."),
    }
    con.executemany("INSERT OR REPLACE INTO metadata (name, value) VALUES (?, ?)",
                    list(stamp.items()))
    con.commit()
    con.close()
    print(f"{os.path.basename(args.mbtiles)}: sampled {n} tiles -> "
          f"newest {newest}, oldest {oldest}")

    if args.rename:
        newname = f"{args.country}-{sl}-{newest.isoformat()}.mbtiles"
        newpath = os.path.join(os.path.dirname(args.mbtiles) or ".", newname)
        os.rename(args.mbtiles, newpath)
        print(f"  renamed -> {newname}")


if __name__ == "__main__":
    main()
