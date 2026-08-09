#!/usr/bin/env python3
"""Label a Traficom MBTiles chart set from the currency its download recorded,
and optionally rename it to fi-<layer>-<newest>.mbtiles.

Traficom reseeds its tile cache region by region, so a set spans a range of
edition dates, carried by each tile's Last-Modified header. traficom_dl.py reads
those as it fetches and stamps source_updated (newest) / source_updated_oldest,
because fetch time is the only moment a real edition can be told from a tile
GeoWebCache rendered on demand for us -- afterwards the two are the same date.

So nothing here contacts the network. This turns recorded facts into the text a
chart client shows.
"""

import argparse
import os
import sqlite3

HUMAN = {  # slug -> (english label, Finnish product name)
    "rannikkokartat": ("Coastal charts", "Rannikkokartat"),
    "satamakartat": ("Harbour charts", "Satamakartat"),
    "veneilykartat": ("Boating charts", "Veneilykartat"),
    "merikarttasarja": ("Nautical chart series", "Merikarttasarja"),
    "merikarttasarjat": ("Nautical chart series", "Merikarttasarjat"),
    "yleiskartat": ("General charts", "Yleiskartat"),
    # The scale is part of the product name here, so it survives into the slug
    # and each one needs its own entry.
    "yleiskartat100k": ("General charts 1:100 000", "Yleiskartat 100k"),
    "yleiskartat250k": ("General charts 1:250 000", "Yleiskartat 250k"),
}


def slug(layer):
    return layer.lower().replace(" public", "").replace(" ", "").split("erikois")[0]


def human_text(layer, newest, oldest, downloaded):
    """The reader-facing name and description.

    Freeboard truncates chart labels around 28 characters, so the name leads
    with the Finnish product name -- the part a Finnish sailor recognises -- and
    the English descriptor lives in the description instead."""
    label, fin = HUMAN.get(slug(layer), ("Nautical charts", layer.replace(" public", "")))
    return {
        "name": f"{fin} {newest}",
        "description": (f"Finnish {label.lower()} (Traficom {fin}, WMTS, CC BY 4.0). "
                        f"Source updated {newest} (oldest sampled region {oldest}); "
                        f"downloaded {downloaded}. Not for navigation use; does not meet "
                        f"official nautical chart requirements."),
    }


def main():
    p = argparse.ArgumentParser(description="Label a chart set from its recorded currency")
    p.add_argument("mbtiles")
    p.add_argument("--country", default="fi")
    p.add_argument("--rename", action="store_true")
    args = p.parse_args()

    con = sqlite3.connect(args.mbtiles)
    meta = dict(con.execute("SELECT name, value FROM metadata").fetchall())
    layer = meta.get("wmts_layer") or meta.get("source_layer") or meta.get("name")

    missing = [k for k in ("source_updated", "source_updated_oldest", "downloaded")
               if k not in meta]
    if missing and meta.get("source") == "wms":
        raise SystemExit(
            f"{os.path.basename(args.mbtiles)} came from the WMS, which serves no "
            f"Last-Modified and declares no edition date, so it has no currency to "
            f"label from and never will. Name that layer by hand.")
    if missing:
        raise SystemExit(
            f"{os.path.basename(args.mbtiles)} carries no {', '.join(missing)}. Only the "
            f"run that fetched the tiles could record that, so there is nothing here to "
            f"label it from: download or refresh it.")

    newest = meta["source_updated"]
    stamp = {"wmts_layer": layer,
             **human_text(layer, newest, meta["source_updated_oldest"], meta["downloaded"])}
    con.executemany("INSERT OR REPLACE INTO metadata (name, value) VALUES (?, ?)",
                    list(stamp.items()))
    con.commit()
    con.close()
    print(f"{os.path.basename(args.mbtiles)}: {stamp['name']}")

    if args.rename:
        newname = f"{args.country}-{slug(layer)}-{newest}.mbtiles"
        newpath = os.path.join(os.path.dirname(args.mbtiles) or ".", newname)
        os.rename(args.mbtiles, newpath)
        print(f"  renamed -> {newname}")


if __name__ == "__main__":
    main()
