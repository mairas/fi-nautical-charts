#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pillow", "numpy", "scipy"]
# ///
"""Refresh the Traficom raster sets, reprocess what moved, and publish it.

One layer at a time, start to finish, because the build host is shared and has
two cores: a layer that runs alone finishes late but leaves the neighbours
alone, and two that run together leave nothing for anyone.

Refresh runs against the raw archive and nothing else writes to it. `strip` and
`downscale` read it read-only and build fresh files in the work directory, so
the archive keeps the full tile set that next month's If-Modified-Since sweep
has to reason about, including the off-sheet tiles publishing removes.

A layer is reprocessed when the archive moved since the published set was built,
or when the recipe changed. Both comparisons are against durable state -- the
archive's revision counter and the published file's own stamps -- so a build
that fails is still owed a rebuild next month rather than being forgotten with
the counts that scheduled it.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import resource
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from publish import NAME, Unpublishable, layer_prefix
from strip_nodata import RADIUS, WHITE, processing_stamp

REPO = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Layer:
    wmts: str
    stages: tuple[str, ...] = ()   # empty: this layer is not stripped at all


# Which layer gets which pass, from a census of the deepest level of each
# archive (3,000 tiles sampled): share of tiles that are nothing but off-sheet
# fill, and share blank white throughout.
#
#   Yleiskartat     8.90%   17.00%
#   Rannikkokartat  1.77%    3.37%
#   Merikarttasarjat 0.27%    0.87%
#   Satamakartat    0.00%    0.20%
#
# Satamakartat has nothing to remove -- not one wholly-fill tile in 3,000 -- so
# the strip there is all risk and no gain, and running the blank pass on it took
# 5.2M pixels of surveyed water across 1,340 tiles.
#
# white-pixels trims the blank on the tiles the outer limit crosses, and needs
# that limit to be drawn to stop at. Only Yleiskartat draws one, as a dashed
# line the radius closes. Where a sheet simply ends the water inside is the same
# white as the blank outside and the trim takes both, so the coastal layers stop
# at white-tiles.
DRAWN_LIMIT = ("black-tiles", "black-pixels", "white-tiles", "white-pixels")
SHEETS_END = ("black-tiles", "black-pixels", "white-tiles")

LAYERS = [Layer("Yleiskartat 250k public", DRAWN_LIMIT),
          Layer("Rannikkokartat public", SHEETS_END),
          Layer("Merikarttasarjat public", SHEETS_END),
          Layer("Satamakartat")]

# Least of the archive's tiles a processed set may keep, measured against the
# archive as it stood before this run touched it. Off-EEZ removal is the heavy
# step and Yleiskartat, the layer it bites hardest, keeps 0.76; the other three
# keep within a percent of 1.0. Anything under this is a broken run, not a
# thorough one -- including a refresh that deleted tiles wholesale because the
# server answered 404 for a while.
MIN_KEPT = 0.5

# Traficom does not render blank the same everywhere: the south-eastern sheets
# draw it fefefe, corner fill included, so at the default 255 none of it is
# found. One step of slack is the difference between finding all of it and none.
WHITE_LEVEL = WHITE - 1

NICE = 19           # every step yields the CPU; the neighbours are production
IONICE_IDLE = "3"   # ionice class: disk only when nobody else wants it
LOCK = ".pipeline.lock"

# Names the interpreter each step runs under. Unset outside a container, where
# uv owns the environment; set by the image, which baked one at build time.
INTERPRETER = "CHARTS_PYTHON"

# strip and downscale hold a full copy each, and publish stages a third beside
# the destination. Two archive-sets of headroom is the rough peak.
HEADROOM = 2


class Failed(Exception):
    """A step failed. The layer stops; the run continues with the next one."""


class Terminated(BaseException):
    """The run was asked to stop.

    Not an Exception on purpose: the per-layer handler catches Exception and
    moves to the next layer, which is right for a step that failed and wrong
    for a signal. This unwinds past it, running the sweeps in the `finally`
    blocks on the way out.
    """


class exclusive:
    """Hold a directory for the length of a run.

    Taken on both the archive and the work directory, because they are separate
    resources and a run mutates both: refresh rewrites the archive in place and
    currency renames it, while scratch is built at paths derived from the layer
    alone. Locking only the work directory left two runs pointed at one archive
    and different scratch free to race the same SQLite file.

    publish takes its own lock on the destination, but only for the minutes it
    is renaming files.
    """

    def __init__(self, where: Path):
        self.path = where / LOCK

    def __enter__(self):
        self.fd = os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o644)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.fd)
            raise Failed(f"another run holds {self.path}; refusing to run concurrently")
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)


def readonly(path: Path) -> sqlite3.Connection:
    """Open a chart without writing to it -- including the -wal/-shm sidecars
    SQLite otherwise creates beside a WAL database just because it was read."""
    return sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)


def meta(path: Path) -> dict[str, str]:
    con = readonly(path)
    try:
        return dict(con.execute("SELECT name, value FROM metadata"))
    finally:
        con.close()


def tile_count(path: Path) -> int:
    con = readonly(path)
    try:
        return con.execute("SELECT count(*) FROM tiles").fetchone()[0]
    finally:
        con.close()


def max_zoom(path: Path) -> int | None:
    con = readonly(path)
    try:
        return con.execute("SELECT max(zoom_level) FROM tiles").fetchone()[0]
    finally:
        con.close()


def find_archives(archive: Path) -> dict[str, Path]:
    """Map each layer to its archive file by what the file says it is.

    Not by filename: currency.py renames on every new edition, so the name is
    the one thing about an archive that does not stay put. The name still
    decides what counts as an archive at all -- strip and downscale copy the
    source metadata verbatim, so a `.stripped`/`.downscaled` sibling claims the
    same layer as the file it came from, and both tools write beside their input
    by default.
    """
    found: dict[str, Path] = {}
    for f in sorted(archive.glob("*.mbtiles")):
        if not NAME.fullmatch(f.name):
            continue
        try:
            layer = meta(f).get("wmts_layer")
        except sqlite3.Error:
            continue
        if not layer:
            continue
        if layer in found:
            raise Failed(f"{found[layer].name} and {f.name} both claim layer "
                         f"{layer!r}; the archive holds one file per layer")
        found[layer] = f
    return found


_missing: set[str] = set()


def nicely(cmd: list[str]) -> list[str]:
    """Prefix a command so it yields CPU and disk to whatever else runs here.

    Says so when it cannot: yielding is the whole reason this job is allowed to
    run on a host with production neighbours, and silently not yielding is the
    one outcome nobody would notice in a twelve-hour log.
    """
    out = []
    for tool, args in (("nice", ["-n", str(NICE)]), ("ionice", ["-c", IONICE_IDLE])):
        if shutil.which(tool):
            out += [tool, *args]
        elif tool not in _missing:
            _missing.add(tool)
            print(f"  warning: no {tool} on this host; steps run at normal priority",
                  file=sys.stderr)
    return out + cmd


def terminate(signum, frame) -> None:
    raise Terminated(f"stopped by signal {signum}; scratch swept, nothing published")


def duration(seconds: float) -> str:
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60} min"


def peak_process_memory() -> int:
    """The largest resident size any one finished process reached, in bytes.

    Stands in for `/usr/bin/time`, which the build host does not have. Memory is
    a binding constraint on that host and `strip-nodata` has had to be bounded
    once already, so a regression should show up in the monthly log rather than
    need a ten-hour rerun to find.

    One process, not one step, and the distinction matters: strip and downscale
    both fan out across a worker pool, so a step's real footprint is its parent
    plus however many workers are resident at once. This number never sums them.
    """
    peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024   # KiB elsewhere


def run_step(cmd: list[str], what: str) -> None:
    full = nicely(cmd)
    print(f"  $ {' '.join(full)}", flush=True)
    proc = subprocess.run(full, cwd=REPO)
    if proc.returncode != 0:
        raise Failed(f"{what} exited {proc.returncode}")


def uv(script: str, *args: str) -> list[str]:
    """How a step is invoked.

    --locked: refuse to resolve. The inline dependency blocks carry no version
    bounds, so without a lockfile an unattended 01:00 run would silently take
    whatever pillow, numpy or scipy released since anyone last looked, and write
    the result into the served directory. A dependency change should be a commit
    someone read.

    An image resolves once, at build time, and bakes the result. Naming its
    interpreter here keeps that guarantee while dropping the machinery that
    enforces it per-run: there is nothing left to resolve, and uv's cache cannot
    be made read-only, so keeping uv in the container would mean a writable
    cache tied to whichever uid the run happens to use.
    """
    if interpreter := os.environ.get(INTERPRETER, "").strip():
        return [interpreter, str(REPO / script), *args]
    return ["uv", "run", "--locked", str(REPO / script), *args]


def revision(m: dict[str, str]) -> int:
    """How many times the archive has moved under the pipeline.

    A file built from the archive carries the revision it was built from,
    because strip and downscale copy metadata verbatim. Comparing the two is
    what makes 'the archive changed' survive a failed build: the counts a
    refresh returns are gone the moment the run ends, and neither a withdrawn
    tile nor a re-render advances `source_updated`, so an edition date cannot
    stand in for it.
    """
    try:
        return int(m.get("pipeline_revision", 0))
    except ValueError:
        return 0


def bump_revision(path: Path) -> int:
    con = sqlite3.connect(str(path))
    try:
        current = revision(dict(con.execute("SELECT name, value FROM metadata")))
        con.execute("INSERT OR REPLACE INTO metadata VALUES ('pipeline_revision', ?)",
                    (str(current + 1),))
        con.commit()
        return current + 1
    finally:
        con.close()


def refresh(path: Path, work: Path) -> dict[str, int]:
    """Delta-check the archive against the server, then relabel it.

    Bumps the archive's revision when tiles actually moved. Relabelling can
    rename the file, so the caller has to look the archive up again afterwards
    rather than reuse `path`.
    """
    layer = meta(path).get("wmts_layer")
    if not layer:
        raise Failed(f"{path.name} records no wmts_layer, so there is nothing to refresh")
    report = work / f".refresh-{path.stem}.json"
    report.unlink(missing_ok=True)
    run_step(uv("traficom_dl.py", "--layer", layer, "--out", str(path),
                "--refresh", "--report", str(report)), "refresh")
    if not report.exists():
        raise Failed("refresh wrote no report; treating the result as unknown "
                     "rather than as 'nothing changed'")
    try:
        counts = json.loads(report.read_text())
    except ValueError as exc:
        raise Failed(f"refresh report is not readable JSON: {exc}") from exc
    if not isinstance(counts, dict):
        raise Failed(f"refresh report is {type(counts).__name__}, not an object")
    report.unlink()

    if counts.get("errors"):
        print(f"  {counts['errors']:,} tiles failed to transfer; the archive keeps "
              f"its old date so the next run re-checks them", file=sys.stderr)
    if counts.get("updated", 0) + counts.get("removed", 0):
        counts["revision"] = bump_revision(path)
    # stdlib only, so it runs under this interpreter rather than a second uv env
    run_step([sys.executable, str(REPO / "currency.py"), str(path), "--rename"],
             "currency")
    return counts


def recipe(layer: Layer, archive: Path) -> tuple[str | None, int]:
    """The strip stamp and downscale source zoom this build would produce.

    One answer used twice: `process` passes the zoom to downscale and `why_run`
    compares both against what is published. Predicting one thing and doing
    another is how a layer ends up rebuilding every month without converging.

    A stamp of None means this layer is not stripped, and a published set for it
    should carry none either.
    """
    zoom = max_zoom(archive)
    if zoom is None:
        raise Failed(f"{archive.name} holds no tiles, so there is no level to "
                     f"downscale from")
    if not layer.stages:
        return None, zoom
    return processing_stamp(RADIUS, stages=layer.stages, white=WHITE_LEVEL), zoom


def published(dest: Path, prefix: str) -> Path | None:
    files = sorted(dest.glob(f"{prefix}-*.mbtiles"))
    return files[-1] if files else None


def why_run(layer: Layer, archive: Path, dest: Path, force: bool) -> str | None:
    """The reason to reprocess this layer, or None to skip it."""
    if force:
        return "forced"
    m = meta(archive)
    try:
        prefix = layer_prefix(m)
    except Unpublishable as exc:
        raise Failed(str(exc)) from exc
    live = published(dest, prefix)
    if live is None:
        return "nothing published for this layer yet"
    lm = meta(live)
    if lm.get("source_updated") != m.get("source_updated"):
        return (f"published edition {lm.get('source_updated')} but the archive is "
                f"now {m.get('source_updated')}")
    want_strip, want_zoom = recipe(layer, archive)
    if lm.get("nodata_stripped") != want_strip:
        return (f"published strip is {lm.get('nodata_stripped')!r}, this build "
                f"would write {want_strip!r}")
    if lm.get("downscale_source_zoom") != str(want_zoom):
        was = lm.get("downscale_source_zoom")
        return (f"published set was never downscaled, this build would use z{want_zoom}"
                if not was else
                f"published downscale came from z{was}, this build would use z{want_zoom}")
    if revision(m) != revision(lm):
        return (f"archive is at revision {revision(m)}, the published set was built "
                f"from {revision(lm)}")
    return None


def process(layer: Layer, archive: Path, work: Path, jobs: int,
            baseline: int) -> Path:
    """Strip the off-sheet fill, then rebuild the lower zooms. Returns the file
    to publish. Both steps read the archive read-only and write into `work`."""
    stripped = work / f"{archive.stem}.stripped.mbtiles"
    out = work / f"{archive.stem}.processed.mbtiles"
    stamp, zoom = recipe(layer, archive)

    source = archive
    if stamp is not None:
        strip = ["strip_nodata.py", str(archive), "--out", str(stripped),
                 "--radius", str(RADIUS), "--white-level", str(WHITE_LEVEL),
                 "--stages", ",".join(layer.stages), "--jobs", str(jobs)]
        run_step(uv(*strip), "strip-nodata")
        source = stripped
    run_step(uv("downscale.py", str(source), "--out", str(out),
                "--source-zoom", str(zoom), "--jobs", str(jobs)), "downscale")
    stripped.unlink(missing_ok=True)

    verify(layer, archive, out, baseline)
    return out


def verify(layer: Layer, archive: Path, out: Path, baseline: int) -> None:
    """Catch a processed file that came out obviously wrong before it is offered
    to publish.

    Publish checks that the file is intact; this checks that it is still the
    chart it was meant to be. `baseline` is the archive's tile count from before
    this run refreshed it, so a refresh that deleted half the coverage -- the
    server answering 404 through a maintenance window looks exactly like tiles
    being withdrawn -- is caught here rather than published over the good set.
    """
    if not out.exists():
        raise Failed("downscale exited cleanly but left no file to publish")
    try:
        after, m = tile_count(out), meta(out)
    except sqlite3.Error as exc:
        raise Failed(f"{out.name} is not a readable chart: {exc}") from exc
    if after < baseline * MIN_KEPT:
        raise Failed(f"processing kept {after:,} of the {baseline:,} tiles this "
                     f"layer had before the run; that is too much loss to publish "
                     f"without someone looking at it")
    want_strip, want_zoom = recipe(layer, archive)
    required = ["source_updated", "wmts_layer", "downscale_filter"]
    if want_strip is not None:
        required.append("nodata_stripped")
    for key in required:
        if not m.get(key):
            raise Failed(f"processed file records no {key}")
    if m.get("nodata_stripped") != want_strip:
        raise Failed(f"built file records strip {m.get('nodata_stripped')!r} but this "
                     f"build asked for {want_strip!r}")
    if m.get("downscale_source_zoom") != str(want_zoom):
        raise Failed(f"built file downscaled from z{m.get('downscale_source_zoom')} "
                     f"but this build asked for z{want_zoom}; publishing it would "
                     f"rebuild this layer again every month")


def build_layer(layer: Layer, archive: Path, work: Path, dest: Path,
                jobs: int, force: bool, skip_refresh: bool) -> Path | None:
    """Take one layer from archive to a file ready to publish, or None to skip."""
    baseline = tile_count(archive)
    if skip_refresh:
        print("  refresh skipped")
    else:
        refresh(archive, work)
        found = find_archives(archive.parent)
        if layer.wmts not in found:
            raise Failed(f"after refresh the archive no longer holds {layer.wmts}; "
                         f"currency may have renamed it to something unexpected")
        archive = found[layer.wmts]

    reason = why_run(layer, archive, dest, force)
    if reason is None:
        print(f"  up to date, nothing to do ({archive.name})")
        return None
    print(f"  reprocessing: {reason}")
    try:
        return process(layer, archive, work, jobs, baseline)
    except BaseException:
        sweep(work, archive.stem, built=True)
        raise
    finally:
        sweep(work, archive.stem, built=False)


def sweep(work: Path, stem: str, built: bool) -> None:
    """Drop this layer's scratch. `built` also drops the finished file, which is
    right only when the build failed -- on the way out it is what gets published.

    Multi-gigabyte files each, on the host that has already run out of disk."""
    names = [f"{stem}.*.partial", f"{stem}.stripped.mbtiles"]
    if built:
        names.append(f"{stem}.processed.mbtiles")
    for pattern in names:
        for f in work.glob(pattern):
            f.unlink(missing_ok=True)


def sweep_stale(work: Path) -> None:
    """Drop every chart file left in the work directory by an earlier run.

    The per-layer sweep runs in a `finally`, so a kill -9, an OOM or a reboot
    skips it -- and it keys on the archive stem, which currency renames on every
    new edition, so last month's orphan is invisible to next month's sweep. This
    runs before anything else and does not care whose it was: nothing in the
    work directory outlives the run that made it.
    """
    for f in sorted(work.glob("*.mbtiles*")):
        print(f"  dropping stale scratch {f.name} ({f.stat().st_size / 1e9:.1f} GB)")
        f.unlink(missing_ok=True)


def check_space(work: Path, archives: list[Path]) -> None:
    need = sum(f.stat().st_size for f in archives) * HEADROOM
    free = shutil.disk_usage(work).free
    if free < need:
        raise Failed(f"{free / 1e9:.0f} GB free on {work} but a full run needs about "
                     f"{need / 1e9:.0f} GB; refusing rather than filling the disk "
                     f"partway through a build")


def directory(value: str) -> Path:
    """Refuse an empty path before it silently becomes the current directory.

    A variable the unit's environment file does not set expands to an empty
    argument rather than being dropped, and `Path("")` resolves to the cwd --
    a real directory, distinct from the other two, so every check in
    `resolve_dirs` passes. A blank `--dest` publishes ten hours of work into
    the clone and a blank `--work` sweeps it.
    """
    if not value.strip():
        raise argparse.ArgumentTypeError(
            "empty path: check that the environment file sets every directory")
    return Path(value)


def resolve_dirs(args: argparse.Namespace) -> str | None:
    """Absolute, distinct, and all three present. Returns a complaint or None.

    Every step runs with cwd=REPO, so a relative path would name one directory
    to the pipeline and another to its children -- and the repo root has an
    `mbtiles/` and an `out/` for it to land in.
    """
    args.archive, args.work, args.dest = (p.resolve() for p in
                                          (args.archive, args.work, args.dest))
    for d in (args.archive, args.work, args.dest):
        if not d.is_dir():
            return f"no such directory: {d}"
    named = {"--archive": args.archive, "--work": args.work, "--dest": args.dest}
    for a, b in (("--archive", "--dest"), ("--archive", "--work"), ("--work", "--dest")):
        if named[a] == named[b]:
            return (f"{a} and {b} are the same directory ({named[a]}); the archive is "
                    f"refreshed and renamed in place and must not be what is served")
    return None


def main() -> int:
    p = argparse.ArgumentParser(
        description="Refresh, reprocess and publish the Traficom raster sets")
    p.add_argument("--archive", required=True, type=directory,
                   help="the raw archives; refreshed in place, never published")
    p.add_argument("--work", required=True, type=directory,
                   help="scratch for processed copies; same filesystem as --dest")
    p.add_argument("--dest", required=True, type=directory,
                   help="the served chart directory")
    p.add_argument("--layer", action="append", dest="only",
                   help="restrict the run to this WMTS layer (repeatable)")
    p.add_argument("--jobs", type=int, default=1,
                   help="workers for strip and downscale (default 1: the build host "
                        "has two cores and neighbours that want one of them)")
    p.add_argument("--force", action="store_true",
                   help="reprocess and publish even when nothing changed")
    p.add_argument("--skip-refresh", action="store_true",
                   help="do not contact the server; rebuild from the archive as it is, "
                        "which is what a recipe fix wants and a failed sweep does not")
    p.add_argument("--dry-run", action="store_true",
                   help="report what each layer would do, contact nobody, write nothing")
    args = p.parse_args()

    if complaint := resolve_dirs(args):
        print(complaint, file=sys.stderr)
        return 2

    try:
        found = find_archives(args.archive)
    except (Failed, OSError) as exc:
        print(f"cannot read the archive: {exc}", file=sys.stderr)
        return 2

    if args.only:
        unknown = set(args.only) - {l.wmts for l in LAYERS}
        if unknown:
            print(f"not layers this pipeline handles: {', '.join(sorted(unknown))}",
                  file=sys.stderr)
            return 2
    layers = [l for l in LAYERS if not args.only or l.wmts in args.only]

    for orphan in sorted(set(found) - {l.wmts for l in LAYERS}):
        print(f"note: {found[orphan].name} is in the archive but no layer claims it; "
              f"it will not be refreshed or published")

    if args.dry_run:
        return dry_run(layers, found, args)

    # The 24h TimeoutStartSec makes a SIGTERM mid-run a scheduled possibility
    # rather than an accident, and Python's default handling exits without
    # running a `finally` -- which is where every sweep of the multi-gigabyte
    # scratch lives. The host has run out of disk before.
    signal.signal(signal.SIGTERM, terminate)

    try:
        with exclusive(args.archive), exclusive(args.work):
            return run(layers, found, args)
    except Failed as exc:
        # 2 is "did not run", which every other refusal in main() already
        # returns. Letting this escape gave a traceback and exit 1 -- the code
        # run() returns when a layer failed, so a refused month and a broken
        # one were the same signal to whatever reads the exit status.
        print(exc, file=sys.stderr)
        return 2
    except Terminated as exc:
        print(exc, file=sys.stderr)
        return 128 + signal.SIGTERM


def dry_run(layers: list[Layer], found: dict[str, Path],
            args: argparse.Namespace) -> int:
    failures = 0
    for layer in layers:
        print(f"\n=== {layer.wmts} ===")
        archive = found.get(layer.wmts)
        if archive is None:
            print("  no archive file for this layer", file=sys.stderr)
            failures += 1
            continue
        try:
            reason = why_run(layer, archive, args.dest, args.force)
        except Exception as exc:
            print(f"  cannot decide: {exc}", file=sys.stderr)
            failures += 1
            continue
        print(f"  {reason or 'up to date (before any refresh)'}")
    return 1 if failures else 0


def run(layers: list[Layer], found: dict[str, Path],
        args: argparse.Namespace) -> int:
    ready: list[Path] = []
    failures: list[tuple[str, str]] = []
    started = time.monotonic()

    sweep_stale(args.work)
    try:
        check_space(args.work, [found[l.wmts] for l in layers if l.wmts in found])
    except Failed as exc:
        print(exc, file=sys.stderr)
        return 2

    try:
        for layer in layers:
            archive = found.get(layer.wmts)
            print(f"\n=== {layer.wmts} ===", flush=True)
            if archive is None:
                failures.append((layer.wmts, "no archive file for this layer"))
                print("  no archive file for this layer", file=sys.stderr)
                continue
            began = time.monotonic()
            try:
                out = build_layer(layer, archive, args.work, args.dest,
                                  max(1, args.jobs), args.force, args.skip_refresh)
            except Exception as exc:
                failures.append((layer.wmts, str(exc)))
                print(f"  FAILED: {exc}", file=sys.stderr)
                continue
            finally:
                # The sweep runs whether or not anything is rebuilt and is the
                # long step, so a quiet month spends nearly all its hours in
                # layers this line is the only account of.
                print(f"  took {duration(time.monotonic() - began)}", flush=True)
            if out is not None:
                ready.append(out)
    except Terminated:
        # build_layer's own finally has swept the layer it was on; these are the
        # finished sets waiting for publish, which nothing else will collect.
        for f in ready:
            f.unlink(missing_ok=True)
        raise

    if ready:
        print(f"\n=== publishing {len(ready)} set(s) ===", flush=True)
        try:
            run_step(uv("publish.py", *[str(f) for f in ready],
                        "--dest", str(args.dest)), "publish")
        except Failed as exc:
            failures.append(("publish", str(exc)))
            print(f"  FAILED: {exc}", file=sys.stderr)
        for f in ready:
            f.unlink(missing_ok=True)
    else:
        print("\nnothing to publish")

    print(f"\nrun finished in {duration(time.monotonic() - started)}: "
          f"{len(ready)} processed, {len(failures)} failed")
    print(f"peak memory in any one process: {peak_process_memory() / 2**20:.0f} MiB")
    for name, why in failures:
        print(f"  {name}: {why}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
