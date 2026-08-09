"""Render a sample of each chart set for the index page.

A reader choosing between five sets of the same coast is really asking what
each one draws. The names do not answer that and the file sizes certainly do
not, so the page shows the same harbour from each chart at the zoom that set is
meant to be read at -- and the one inland set at an inland place, since Hanko
would show it empty.
"""

from __future__ import annotations

import io
import math
import sqlite3
from pathlib import Path

from PIL import Image

TILE = 256
SIZE = (760, 420)

HANKO = ("Hanko Itäsatama", 59.8225, 22.9750)
HIRVENSALMI = ("Hirvensalmi", 61.6339, 26.7861)

# layer -> (place, lat, lon, zoom)
SPOTS = {
    "fi-yleiskartat250k": (*HANKO, 10),
    "fi-merikarttasarjat": (*HANKO, 13),
    "fi-rannikkokartat": (*HANKO, 13),
    "fi-satamakartat": (*HANKO, 15),
    "fi-veneilykartat": (*HIRVENSALMI, 13),
}


def pixel_xy(lat: float, lon: float, z: int) -> tuple[float, float]:
    n = TILE * (1 << z)
    return ((lon + 180) / 360 * n,
            (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)


def render(db: Path, lat: float, lon: float, z: int,
           size: tuple[int, int] = SIZE) -> Image.Image:
    width, height = size
    cx, cy = pixel_xy(lat, lon, z)
    left, top = cx - width / 2, cy - height / 2
    out = Image.new("RGBA", size, (255, 255, 255, 255))
    con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
    try:
        for tx in range(int(left // TILE), int((left + width) // TILE) + 1):
            for ty in range(int(top // TILE), int((top + height) // TILE) + 1):
                row = con.execute(
                    "SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? "
                    "AND tile_row=?", (z, tx, (1 << z) - 1 - ty)).fetchone()
                if not row:
                    continue
                tile = Image.open(io.BytesIO(row[0])).convert("RGBA")
                out.paste(tile, (int(tx * TILE - left), int(ty * TILE - top)), tile)
    finally:
        con.close()
    return out


def encode(image: Image.Image) -> bytes:
    """PNG, palettised. Chart rasters carry few colours and hard edges, so a
    palette keeps every line crisp at a fraction of the bytes, where JPEG would
    ring around exactly the thin black work these charts are made of."""
    buf = io.BytesIO()
    image.convert("RGB").quantize(colors=192, method=Image.Quantize.MEDIANCUT).save(
        buf, format="PNG", optimize=True)
    return buf.getvalue()


def for_layer(db: Path, layer: str) -> tuple[bytes, str, int] | None:
    """The sample image for one layer, or None if no spot is defined for it."""
    spot = SPOTS.get(layer)
    if not spot:
        return None
    place, lat, lon, z = spot
    return encode(render(db, lat, lon, z)), place, z
