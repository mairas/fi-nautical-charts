#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pillow"]
# ///
"""Publish processed chart sets, and describe what was published.

Publishing is a copy into a staging directory beside the target, verified by
reading back what was written, and then a rename -- never a transfer into the
served directory. The rename is atomic because staging shares the destination's
filesystem, so a client gets the whole old file or the whole new one.

Everything that can refuse the run happens before the first rename: sources are
checked for truncation, destination names are validated, every byte is staged
and verified, and the whole manifest is built. Past that point the only work
left is renaming, retiring superseded editions, and writing a manifest that was
already computed, so a failure there is reported as PartiallyPublished rather
than being confused with a run that changed nothing.

Retirement goes by identity, not by filename: a candidate is removed only if its
own metadata records the same layer and an older edition. A filename that merely
looks similar is left alone.

The manifest exists because a filename records the *source* edition, not what we
did to the tiles: reprocessing changes the bytes without changing any name. It
states size, digest and processing for every file present, which is also how a
downloader tells a truncated archive from a good one.
"""

from __future__ import annotations

import argparse
import datetime
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import index_page
import preview
from currency import slug

MANIFEST = "charts.json"
INDEX = "index.html"
PREVIEWS = "previews"
# Fields may be added at any time without a bump; an existing field never
# changes meaning or type without one. A consumer meeting a higher number
# should refuse rather than guess.
SCHEMA = 1
STAGING = ".staging"
LOCK = ".publish.lock"
CHUNK = 4 << 20

# The only shape this tool publishes. Metadata reaches the filesystem through
# these names, so anything else -- separators, glob metacharacters, an empty
# slug -- is refused rather than escaped.
NAME = re.compile(r"^fi-[a-z0-9]+-\d{4}-\d{2}-\d{2}\.mbtiles$")

SQLITE_MAGIC = b"SQLite format 3\x00"

REPO = Path(__file__).resolve().parent


class Unpublishable(RuntimeError):
    """The set was not published, and nothing in the destination was touched."""


class PartiallyPublished(RuntimeError):
    """Some files were renamed into place before the run failed."""


def read_meta(path: Path, immutable: bool = False) -> dict:
    """Read an MBTiles' metadata table.

    `immutable` suppresses the -wal/-shm files SQLite otherwise creates beside a
    WAL-mode database even when opening it read-only. Use it only for files
    nothing is writing: the destination is a directory a web server exposes, and
    it must not accumulate sidecars just because we looked at a chart.
    """
    query = "mode=ro&immutable=1" if immutable else "mode=ro"
    uri = f"file:{urlquote(path)}?{query}"
    con = sqlite3.connect(uri, uri=True)
    try:
        return dict(con.execute("SELECT name, value FROM metadata"))
    finally:
        con.close()


def urlquote(path: Path) -> str:
    """Percent-encode a path for a SQLite file: URI.

    SQLite splits the URI at the first `?` and percent-decodes what precedes it,
    so an unencoded path can inject query parameters -- `vfs=`, or a `mode=` that
    overrides the read-only intent -- or redirect the open to another file.
    """
    from urllib.parse import quote

    return quote(str(path))


def layer_prefix(meta: dict) -> str:
    layer = meta.get("wmts_layer") or meta.get("source_layer") or meta.get("name")
    if not layer or not meta.get("source_updated"):
        raise Unpublishable(
            "no layer and edition recorded, so there is no name to publish under; "
            "only the run that fetched the tiles could record that")
    return f"fi-{slug(layer)}"


def published_name(meta: dict) -> str:
    """The name currency.py --rename would give this file: layer and edition."""
    name = f"{layer_prefix(meta)}-{meta['source_updated']}.mbtiles"
    if not NAME.fullmatch(name):
        raise Unpublishable(
            f"metadata produces {name!r}, which is not a name this publishes: "
            f"layer {meta.get('wmts_layer')!r}, edition "
            f"{meta.get('source_updated')!r}")
    return name


def check_intact(path: Path) -> None:
    """Refuse a source that is shorter than its own header says it is.

    Both producers run with `PRAGMA journal_mode=OFF`, so a killed writer leaves
    a partial database rather than rolling back. Copying it would verify
    perfectly -- the read-back compares the copy against the source, and a
    faithful copy of a truncated file is faithful.
    """
    with open(path, "rb") as fh:
        header = fh.read(100)
    if header[:16] != SQLITE_MAGIC:
        raise Unpublishable(f"{path.name}: not an SQLite database")
    page_size = int.from_bytes(header[16:18], "big")
    page_size = 65536 if page_size == 1 else page_size
    pages = int.from_bytes(header[28:32], "big")
    change_counter = header[24:28]
    valid_for = header[92:96]
    if pages and change_counter == valid_for:
        expected = pages * page_size
        actual = path.stat().st_size
        if actual < expected:
            raise Unpublishable(
                f"{path.name}: header describes {pages} pages ({expected} bytes) "
                f"but the file is {actual}; it is truncated")

    wal = path.with_name(path.name + "-wal")
    if wal.exists() and wal.stat().st_size:
        raise Unpublishable(
            f"{path.name}: has an uncheckpointed -wal sidecar, and publishing "
            f"copies the database file alone, which would drop it")


def digest_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def stage(source: Path, staged: Path) -> str:
    """Copy into staging, returning the digest of what was read.

    Copying rather than linking keeps the served file a distinct inode, so a
    later run rewriting its working file cannot reach through and change what
    is being served.
    """
    h = hashlib.sha256()
    fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with open(source, "rb") as src, open(fd, "wb") as dst:
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
                             capture_output=True, text=True, check=True, timeout=10)
        return rev.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return "unknown"


def layer_of(meta: dict) -> str | None:
    """The layer a file belongs to, or None if it does not record one.

    Unlike layer_prefix this never refuses: the manifest describes whatever is
    in the destination, including files this tool did not put there.
    """
    try:
        return layer_prefix(meta)
    except Unpublishable:
        return None


def manifest_entry(name: str, size: int, sha: str, meta: dict | None) -> dict:
    if meta is None:
        return {"filename": name, "layer": None, "bytes": size, "sha256": sha,
                "readable": False}
    return {"filename": name,
            "layer": layer_of(meta),
            "bytes": size,
            "sha256": sha,
            "source_edition": meta.get("source_updated"),
            "source_edition_oldest": meta.get("source_updated_oldest"),
            "processing": processing(meta),
            "name": meta.get("name")}


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def replace_text(dest: Path, name: str, text: str) -> Path:
    incoming = dest / f".{name}.incoming"
    with open(incoming, "w") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    target = dest / name
    os.replace(incoming, target)
    return target


def write_previews(dest: Path, samples: dict[str, bytes]) -> dict[str, tuple[str, str, int]]:
    """Put the sample images beside the page, and say where each one landed."""
    if not samples:
        return {}
    (dest / PREVIEWS).mkdir(exist_ok=True)
    placed = {}
    for layer, (data, place, zoom) in samples.items():
        name = f"{PREVIEWS}/{layer}.png"
        incoming = dest / f"{PREVIEWS}/.{layer}.png.incoming"
        with open(incoming, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(incoming, dest / name)
        placed[layer] = (name, place, zoom)
    fsync_dir(dest / PREVIEWS)
    return placed


def write_manifest(dest: Path, charts: list[dict], pipeline: str,
                   samples: dict | None = None) -> Path:
    """Write the manifest and the index page from one set of facts.

    The page a reader sees and the digests they verify against are written in
    the same breath, so they cannot come to disagree.
    """
    generated = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    ordered = sorted(charts, key=lambda e: e["filename"])
    body = {"schema": SCHEMA, "generated": generated, "pipeline": pipeline,
            "charts": ordered}

    placed = write_previews(dest, samples or {})
    target = replace_text(dest, MANIFEST, json.dumps(body, indent=2) + "\n")
    replace_text(dest, INDEX, index_page.render(ordered, generated, placed))
    fsync_dir(dest)
    return target


def retire(dest: Path, layer: str, edition: str, keep: set[str]) -> list[Path]:
    """Files this run supersedes: same layer by their own metadata, older edition.

    Matching on the filename alone would also hit build variants, hand-placed
    files, and a newer edition being republished over -- all of which are
    someone else's, and none of which this run is entitled to delete.
    """
    doomed = []
    for old in dest.glob("fi-*.mbtiles"):
        if old.name in keep or not NAME.fullmatch(old.name):
            continue
        try:
            meta = read_meta(old, immutable=True)
            if layer_prefix(meta) != layer:
                continue
        except (sqlite3.DatabaseError, Unpublishable):
            continue
        if meta["source_updated"] < edition:
            doomed.append(old)
    return doomed


def publish(sources, dest: Path, verbose: bool = False) -> dict:
    """Publish every source or none of it.

    Naming, truncation checks, staging, verification and the whole manifest are
    completed before anything is renamed, so any refusal leaves the previous set
    exactly as it was.
    """
    dest = Path(dest)
    sources = [Path(s) for s in sources]
    if not dest.is_dir():
        raise Unpublishable(f"{dest} is not a directory")
    if not os.access(dest, os.W_OK | os.X_OK):
        raise Unpublishable(f"{dest} is not writable")

    with exclusive(dest):
        return _publish(sources, dest, verbose)


class exclusive:
    """Hold the destination for one run.

    Two runs sharing a destination would stage over each other's files, and the
    manifest would then record a digest for bytes the other run wrote.
    """

    def __init__(self, dest: Path):
        self.path = dest / LOCK

    def __enter__(self):
        self.fd = os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o644)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.fd)
            raise Unpublishable(
                f"another publish holds {self.path}; refusing to run concurrently")
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)


def _publish(sources: list[Path], dest: Path, verbose: bool) -> dict:
    plan = []
    for src in sources:
        if not src.exists():
            raise Unpublishable(f"{src} does not exist")
        if src.resolve().parent == dest.resolve():
            raise Unpublishable(
                f"{src.name} is already in the destination; publishing it onto "
                f"itself would retire the file it was built from")
        check_intact(src)
        try:
            meta = read_meta(src)
        except sqlite3.DatabaseError as exc:
            raise Unpublishable(f"{src.name}: not readable as MBTiles ({exc})") from exc
        plan.append((src, published_name(meta), layer_prefix(meta), meta))

    by_layer = {}
    for src, name, layer, _ in plan:
        if layer in by_layer:
            raise Unpublishable(
                f"{by_layer[layer].name} and {src.name} are both {layer}; "
                f"a run publishes one edition of a layer, not several")
        by_layer[layer] = src

    staging = dest / STAGING
    staging.mkdir(exist_ok=True)
    swept = [debris for debris in staging.iterdir()]
    for debris in swept:
        debris.unlink()
        if verbose:
            print(f"  swept stale {debris.name}")

    staged_paths = []
    charts = []
    samples = {}
    try:
        for src, name, _, meta in plan:
            staged = staging / name
            staged_paths.append(staged)
            written = stage(src, staged)
            found = digest_of(staged)
            if found != written:
                raise Unpublishable(
                    f"{name}: staged copy does not match its source "
                    f"({staged.stat().st_size} bytes written, digest {found[:12]} "
                    f"vs {written[:12]}); nothing was published")
            charts.append(manifest_entry(name, staged.stat().st_size, found, meta))
            sample = preview.for_layer(staged, layer_prefix(meta))
            if sample:
                samples[layer_prefix(meta)] = sample
            if verbose:
                print(f"  staged {name}  {staged.stat().st_size} bytes  {found[:12]}"
                      + ("  +sample" if sample else ""))

        keep = {name for _, name, _, _ in plan}
        doomed = []
        for _, _, layer, meta in plan:
            doomed.extend(retire(dest, layer, meta["source_updated"], keep))
        for old in sorted(dest.glob("fi-*.mbtiles")):
            if old.name in keep or old in doomed:
                continue
            try:
                meta = read_meta(old, immutable=True)
            except sqlite3.DatabaseError:
                meta = None
            charts.append(manifest_entry(old.name, old.stat().st_size,
                                         digest_of(old), meta))
        pipeline = pipeline_version()
    except BaseException:
        for staged in staged_paths:
            staged.unlink(missing_ok=True)
        raise

    published = []
    try:
        for (_, name, _, _), staged in zip(plan, staged_paths):
            os.replace(staged, dest / name)
            published.append(dest / name)
            if verbose:
                print(f"  published {name}")
        fsync_dir(dest)

        for old in doomed:
            old.unlink()
            if verbose:
                print(f"  retired {old.name}")
        fsync_dir(dest)

        manifest = write_manifest(dest, charts, pipeline, samples)
    except BaseException as exc:
        raise PartiallyPublished(
            f"{len(published)} of {len(plan)} renamed into place before failing: "
            f"{exc}") from exc

    return {"published": published, "retired": doomed, "manifest": manifest,
            "swept": swept}


def main() -> None:
    p = argparse.ArgumentParser(description="Publish processed chart sets with a manifest")
    p.add_argument("sources", nargs="+", type=Path)
    p.add_argument("--dest", required=True, type=Path, help="the served chart directory")
    args = p.parse_args()

    try:
        result = publish(args.sources, args.dest, verbose=True)
    except Unpublishable as exc:
        sys.exit(f"refused, nothing published: {exc}")
    except PartiallyPublished as exc:
        sys.exit(f"DESTINATION CHANGED and the run did not finish: {exc}")
    print(f"published {len(result['published'])} set(s), retired "
          f"{len(result['retired'])}, wrote {result['manifest'].name}")


if __name__ == "__main__":
    main()
