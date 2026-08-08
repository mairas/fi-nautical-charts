#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Publish processed chart sets, and describe what was published.

Two things go wrong when charts are shipped by hand, and both have happened.

An upload truncated three of five files and nothing noticed for six days, so
publishing here is a copy beside the target on the same filesystem, verified by
reading back what was written, and then a rename -- never a transfer into the
served directory. A run that fails leaves the previous set exactly as it was.

And a filename records the *source* edition, not what we did to the tiles. The
fill-strip and off-EEZ work changed the bytes without changing any name, so
anything caching by URL kept serving the old content. The manifest states the
size, the digest and the processing of every file actually present, which is
also the only way a downloader can tell a truncated archive from a good one.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import sqlite3
import subprocess
from pathlib import Path

from currency import slug

MANIFEST = "charts.json"
CHUNK = 4 << 20

REPO = Path(__file__).resolve().parent


class Unpublishable(RuntimeError):
    """The set was not published, and nothing in the destination was touched."""


def read_meta(path: Path, immutable: bool = False) -> dict:
    """Read an MBTiles' metadata table.

    `immutable` suppresses the -wal/-shm files SQLite otherwise creates beside a
    WAL-mode database even when opening it read-only. Use it only for files
    nothing is writing: the destination is a directory a web server exposes, and
    it must not accumulate sidecars just because we looked at a chart.
    """
    uri = f"file:{path}?mode=ro" + ("&immutable=1" if immutable else "")
    con = sqlite3.connect(uri, uri=True)
    try:
        return dict(con.execute("SELECT name, value FROM metadata"))
    finally:
        con.close()


def layer_prefix(meta: dict) -> str:
    layer = meta.get("wmts_layer") or meta.get("source_layer") or meta.get("name")
    if not layer or not meta.get("source_updated"):
        raise Unpublishable(
            "no layer and edition recorded, so there is no name to publish under; "
            "only the run that fetched the tiles could record that")
    return f"fi-{slug(layer)}"


def published_name(meta: dict) -> str:
    """The name currency.py --rename would give this file: layer and edition."""
    return f"{layer_prefix(meta)}-{meta['source_updated']}.mbtiles"


def digest_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def stage(source: Path, staged: Path) -> str:
    """Copy into place beside the target, returning the digest of what was read.

    Copying rather than linking keeps the served file a distinct inode, so a
    later run rewriting its working file cannot reach through and change what
    is being served.
    """
    h = hashlib.sha256()
    with open(source, "rb") as src, open(staged, "wb") as dst:
        while chunk := src.read(CHUNK):
            h.update(chunk)
            dst.write(chunk)
        dst.flush()
        os.fsync(dst.fileno())
    return h.hexdigest()


def processing(meta: dict) -> str:
    steps = []
    if stripped := meta.get("nodata_stripped"):
        steps.append(stripped)
    if filt := meta.get("downscale_filter"):
        steps.append(f"{filt} from z{meta.get('downscale_source_zoom', '?')} "
                     f"on {meta.get('downscaled', '?')}")
    return "; ".join(steps) if steps else "none"


def pipeline_version() -> str:
    try:
        rev = subprocess.run(["git", "-C", REPO, "describe", "--always", "--dirty"],
                             capture_output=True, text=True, check=True)
        return rev.stdout.strip()
    except (subprocess.CalledProcessError, OSError):
        return "unknown"


def entry(path: Path, sha: str) -> dict:
    meta = read_meta(path, immutable=True)
    return {"filename": path.name,
            "bytes": path.stat().st_size,
            "sha256": sha,
            "source_edition": meta.get("source_updated"),
            "source_edition_oldest": meta.get("source_updated_oldest"),
            "processing": processing(meta),
            "name": meta.get("name")}


def write_manifest(dest: Path, known: dict[str, str]) -> Path:
    charts = [entry(p, known.get(p.name) or digest_of(p))
              for p in sorted(dest.glob("fi-*.mbtiles"))]
    body = {"generated": datetime.date.today().isoformat(),
            "pipeline": pipeline_version(),
            "charts": charts}
    incoming = dest / f".{MANIFEST}.incoming"
    incoming.write_text(json.dumps(body, indent=2) + "\n")
    target = dest / MANIFEST
    os.replace(incoming, target)
    return target


def publish(sources, dest: Path, verbose: bool = False) -> list[Path]:
    """Publish every source or none of it.

    A manifest describing a half-updated directory would be worse than no new
    manifest, so naming and staging are both completed for the whole set before
    anything is renamed into place.
    """
    dest = Path(dest)
    sources = [Path(s) for s in sources]

    plan = []
    for src in sources:
        if not src.exists():
            raise Unpublishable(f"{src} does not exist")
        try:
            meta = read_meta(src)
        except sqlite3.DatabaseError as exc:
            raise Unpublishable(f"{src.name}: not readable as MBTiles ({exc})") from exc
        plan.append((src, published_name(meta), layer_prefix(meta)))

    seen = {}
    for src, name, _ in plan:
        if name in seen:
            raise Unpublishable(
                f"{seen[name].name} and {src.name} both publish as {name}; "
                f"which one won would depend on argument order")
        seen[name] = src

    staged_paths = []
    digests = {}
    try:
        for src, name, _ in plan:
            staged = dest / f".{name}.incoming"
            staged_paths.append(staged)
            written = stage(src, staged)
            found = digest_of(staged)
            if found != written:
                raise Unpublishable(
                    f"{name}: staged copy does not match its source "
                    f"({staged.stat().st_size} bytes written, digest {found[:12]} "
                    f"vs {written[:12]}); nothing was published")
            digests[name] = found
            if verbose:
                print(f"  staged {name}  {staged.stat().st_size} bytes  {found[:12]}")
    except BaseException:
        for staged in staged_paths:
            staged.unlink(missing_ok=True)
        raise

    published = []
    for (src, name, _), staged in zip(plan, staged_paths):
        target = dest / name
        os.replace(staged, target)
        published.append(target)
        if verbose:
            print(f"  published {name}")

    for _, name, layer in plan:
        for old in dest.glob(f"{layer}-*.mbtiles"):
            if old.name != name:
                old.unlink()
                if verbose:
                    print(f"  removed superseded {old.name}")

    write_manifest(dest, digests)
    return published


def main():
    p = argparse.ArgumentParser(description="Publish processed chart sets with a manifest")
    p.add_argument("sources", nargs="+", type=Path)
    p.add_argument("--dest", required=True, type=Path, help="the served chart directory")
    args = p.parse_args()

    published = publish(args.sources, args.dest, verbose=True)
    print(f"published {len(published)} set(s) and {MANIFEST} in {args.dest}")


if __name__ == "__main__":
    main()
