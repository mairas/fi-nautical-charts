"""Tests for the monthly pipeline.

Two kinds of thing are pinned here. The decision functions -- which archive
belongs to which layer, whether a layer needs rebuilding, whether what came out
is still a chart -- are tested directly. The orchestration around them is tested
through a stubbed `run_step`, because the properties that make this safe to run
unattended (a failing layer does not stop the rest, --dry-run contacts nobody,
scratch does not survive a failure) live in the sequencing, not in any one step.

The steps themselves -- refresh, strip, downscale, publish -- have their own
tests. Here they are subprocesses and stay that way.
"""

import ast
import errno
import os
import re
import shlex
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import pipeline
import publish
import strip_nodata
from pipeline import Failed, Layer

# The literal string the current recipe writes. Spelled out rather than derived
# from processing_stamp: the stamp is what tells a published chart apart from
# one built by an older recipe, so a test that recomputes it would follow the
# code and never notice the change that makes every published set look stale.
LIVE_STAMP = ("nodata-r128-w254-n2:"
              "black-tiles+black-pixels+white-tiles+white-pixels")

ARCHIVE_META = {
    "wmts_layer": "Yleiskartat 250k public",
    "source_updated": "2026-06-02",
    "downloaded": "2026-07-12",
    "minzoom": "3",
    "maxzoom": "13",
}

# what the archive above becomes once this build processes and publishes it
PUBLISHED_META = ARCHIVE_META | {
    "nodata_stripped": LIVE_STAMP,
    "downscale_source_zoom": "13",
    "downscale_filter": "box-2x-premultiplied",
    "downscaled": "2026-08-09",
}

YLEIS = Layer("Yleiskartat 250k public", pipeline.DRAWN_LIMIT)
DEEPEST = Layer("Rannikkokartat public", pipeline.SHEETS_END)
ARCHIVE_NAME = "fi-yleiskartat250k-2026-06-02.mbtiles"


def make_mbtiles(path: Path, meta: dict[str, str], tiles: int = 100,
                 zooms: tuple[int, ...] = (3, 13)) -> Path:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE metadata (name TEXT PRIMARY KEY, value TEXT)")
    con.execute("CREATE TABLE tiles (zoom_level INT, tile_column INT, tile_row INT, tile_data BLOB)")
    con.executemany("INSERT INTO metadata VALUES (?,?)", meta.items())
    con.executemany("INSERT INTO tiles VALUES (?,?,1,?)",
                    [(zooms[i % len(zooms)], i, b"tile") for i in range(tiles)])
    con.commit()
    con.close()
    return path


@pytest.fixture
def archive(tmp_path):
    d = tmp_path / "archive"
    d.mkdir()
    make_mbtiles(d / ARCHIVE_NAME, ARCHIVE_META)
    return d


@pytest.fixture
def dest(tmp_path):
    d = tmp_path / "charts"
    d.mkdir()
    make_mbtiles(d / ARCHIVE_NAME, PUBLISHED_META)
    return d


@pytest.fixture
def work(tmp_path):
    d = tmp_path / "work"
    d.mkdir()
    return d


# --- which file is which layer ------------------------------------------------

def test_archives_are_matched_by_recorded_layer_not_by_filename(tmp_path):
    d = tmp_path / "a"
    d.mkdir()
    make_mbtiles(d / "fi-somethingelse-2020-01-01.mbtiles", ARCHIVE_META)
    assert pipeline.find_archives(d) == {
        "Yleiskartat 250k public": d / "fi-somethingelse-2020-01-01.mbtiles"}


def test_a_derived_sibling_is_not_mistaken_for_an_archive(tmp_path):
    """strip and downscale copy metadata verbatim and write beside their input,
    so their output claims the same layer as the file it came from."""
    d = tmp_path / "a"
    d.mkdir()
    make_mbtiles(d / ARCHIVE_NAME, ARCHIVE_META)
    for derived in ("fi-yleiskartat250k-2026-06-02.downscaled.mbtiles",
                    "fi-yleiskartat250k-2026-06-02.stripped.mbtiles",
                    "fi-yleiskartat250k-2026-06-02.processed.mbtiles"):
        make_mbtiles(d / derived, ARCHIVE_META)
    assert pipeline.find_archives(d) == {
        "Yleiskartat 250k public": d / ARCHIVE_NAME}


def test_two_archives_claiming_one_layer_is_refused(tmp_path):
    d = tmp_path / "a"
    d.mkdir()
    make_mbtiles(d / ARCHIVE_NAME, ARCHIVE_META)
    make_mbtiles(d / "fi-yleiskartat250k-2026-07-01.mbtiles", ARCHIVE_META)
    with pytest.raises(Failed, match="both claim layer"):
        pipeline.find_archives(d)


def test_a_file_recording_no_layer_is_ignored(tmp_path):
    d = tmp_path / "a"
    d.mkdir()
    make_mbtiles(d / "fi-mystery-2026-01-01.mbtiles", {"name": "no layer here"})
    assert pipeline.find_archives(d) == {}


def test_a_file_that_is_not_a_database_is_skipped_not_fatal(tmp_path):
    """The archive is a shared directory; a half-written download must cost one
    file, not the whole month's run."""
    d = tmp_path / "a"
    d.mkdir()
    (d / "fi-broken-2026-01-01.mbtiles").write_bytes(b"not sqlite at all")
    make_mbtiles(d / ARCHIVE_NAME, ARCHIVE_META)
    assert pipeline.find_archives(d) == {
        "Yleiskartat 250k public": d / ARCHIVE_NAME}


# --- deciding whether to reprocess -------------------------------------------

def test_matching_edition_and_recipe_means_no_work(archive, dest):
    assert pipeline.why_run(YLEIS, archive / ARCHIVE_NAME, dest, force=False) is None


def test_a_newer_archive_edition_triggers_a_rebuild(archive, dest):
    f = archive / "fi-yleiskartat250k-2026-07-05.mbtiles"
    make_mbtiles(f, ARCHIVE_META | {"source_updated": "2026-07-05"})
    why = pipeline.why_run(YLEIS, f, dest, force=False)
    assert "2026-06-02" in why and "2026-07-05" in why


def test_a_changed_strip_recipe_triggers_a_rebuild(archive, dest):
    """A published set whose strip stamp predates the current recipe must be
    rebuilt even though the source edition has not moved."""
    live = dest / ARCHIVE_NAME
    live.unlink()
    make_mbtiles(live, PUBLISHED_META | {"nodata_stripped": "opaque-black-r4"})
    assert LIVE_STAMP in pipeline.why_run(
        YLEIS, archive / ARCHIVE_NAME, dest, force=False)


def test_a_changed_source_zoom_triggers_a_rebuild(archive, dest):
    live = dest / ARCHIVE_NAME
    live.unlink()
    make_mbtiles(live, PUBLISHED_META | {"downscale_source_zoom": "12"})
    why = pipeline.why_run(YLEIS, archive / ARCHIVE_NAME, dest, force=False)
    assert "z12" in why and "z13" in why


def test_a_set_that_was_never_downscaled_says_so(archive, dest):
    live = dest / ARCHIVE_NAME
    live.unlink()
    make_mbtiles(live, {k: v for k, v in PUBLISHED_META.items()
                        if k != "downscale_source_zoom"})
    assert "never downscaled" in pipeline.why_run(
        YLEIS, archive / ARCHIVE_NAME, dest, force=False)


def test_an_unpublished_layer_is_always_built(archive, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert "nothing published" in pipeline.why_run(
        YLEIS, archive / ARCHIVE_NAME, empty, force=False)


def test_force_overrides_an_up_to_date_layer(archive, dest):
    assert pipeline.why_run(YLEIS, archive / ARCHIVE_NAME, dest, force=True) == "forced"


def test_a_published_file_for_another_layer_does_not_count(archive, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    make_mbtiles(other / "fi-satamakartat-2026-06-29.mbtiles",
                 PUBLISHED_META | {"wmts_layer": "Satamakartat"})
    assert "nothing published" in pipeline.why_run(
        YLEIS, archive / ARCHIVE_NAME, other, force=False)


# --- the archive moved, and that has to survive a failed build ----------------

def test_an_archive_ahead_of_the_published_set_triggers_a_rebuild(archive, dest):
    """The counts a refresh returns are gone when the run ends, and neither a
    withdrawn tile nor an on-demand re-render moves source_updated. Only the
    revision the published file was built from can say the archive has moved."""
    f = archive / ARCHIVE_NAME
    pipeline.bump_revision(f)
    why = pipeline.why_run(YLEIS, f, dest, force=False)
    assert "revision 1" in why and "built from 0" in why


def test_the_rebuild_is_still_owed_after_a_failed_build(archive, dest):
    """Month 1 moved the archive and the build failed; month 2 refreshes with
    nothing new. The layer must still rebuild rather than read as up to date."""
    f = archive / ARCHIVE_NAME
    pipeline.bump_revision(f)                       # month 1: tiles moved
    assert pipeline.why_run(YLEIS, f, dest, force=False) is not None
    assert pipeline.why_run(YLEIS, f, dest, force=False) is not None   # month 2


def test_a_published_set_built_from_the_current_revision_is_up_to_date(archive, dest):
    f = archive / ARCHIVE_NAME
    pipeline.bump_revision(f)
    live = dest / ARCHIVE_NAME
    live.unlink()
    make_mbtiles(live, PUBLISHED_META | {"pipeline_revision": "1"})
    assert pipeline.why_run(YLEIS, f, dest, force=False) is None


def test_revision_survives_a_rename(archive):
    f = archive / ARCHIVE_NAME
    assert pipeline.bump_revision(f) == 1
    assert pipeline.bump_revision(f) == 2
    assert pipeline.revision(pipeline.meta(f)) == 2


def test_an_unreadable_revision_reads_as_never_built(archive, dest):
    live = dest / ARCHIVE_NAME
    live.unlink()
    make_mbtiles(live, PUBLISHED_META | {"pipeline_revision": "not a number"})
    assert pipeline.why_run(YLEIS, archive / ARCHIVE_NAME, dest, force=False) is None


# --- the recipe a layer would be built with ----------------------------------

def test_every_layer_downscales_from_the_deepest_level(archive):
    """There is no shallower source to choose: strip_nodata cleans the deepest
    level and deletes the rest, so downscaling from anything above it would find
    no tiles to build from."""
    for layer in (YLEIS, DEEPEST):
        assert pipeline.recipe(layer, archive / ARCHIVE_NAME)[1] == 13


def test_the_deepest_level_is_read_from_the_tiles_not_the_metadata(tmp_path):
    """A download that stopped short leaves a maxzoom claim its tiles do not
    back, and downscaling from a level with no tiles produces an empty pyramid."""
    f = make_mbtiles(tmp_path / "a.mbtiles", ARCHIVE_META | {"maxzoom": "15"},
                     zooms=(3, 13))
    assert pipeline.recipe(DEEPEST, f)[1] == 13


def test_an_archive_with_no_tiles_is_refused_rather_than_downscaled_from_nothing(tmp_path):
    f = make_mbtiles(tmp_path / "a.mbtiles", ARCHIVE_META, tiles=0)
    with pytest.raises(Failed, match="no tiles"):
        pipeline.recipe(DEEPEST, f)


def test_the_expected_strip_stamp_is_what_published_charts_carry(archive):
    assert pipeline.recipe(YLEIS, archive / ARCHIVE_NAME)[0] == LIVE_STAMP
    assert strip_nodata.processing_stamp(
        stages=pipeline.DRAWN_LIMIT,
        white=pipeline.WHITE_LEVEL) == LIVE_STAMP


# --- refusing a bad build ------------------------------------------------------

def test_a_normal_build_passes_verification(tmp_path, archive):
    out = make_mbtiles(tmp_path / "out.mbtiles", PUBLISHED_META, tiles=76)
    pipeline.verify(YLEIS, archive / ARCHIVE_NAME, out, baseline=100)


def test_a_build_that_lost_most_of_its_tiles_is_refused(tmp_path, archive):
    out = make_mbtiles(tmp_path / "out.mbtiles", PUBLISHED_META, tiles=20)
    with pytest.raises(Failed, match="too much loss"):
        pipeline.verify(YLEIS, archive / ARCHIVE_NAME, out, baseline=100)


def test_loss_is_measured_against_the_archive_before_the_run_touched_it(tmp_path, archive):
    """A refresh that answered 404 through a maintenance window deletes tiles
    wholesale. Measured against the gutted archive the ratio is ~1.0; measured
    against what the layer had before the run, it is the disaster it is."""
    out = make_mbtiles(tmp_path / "out.mbtiles", PUBLISHED_META, tiles=95)
    pipeline.verify(YLEIS, archive / ARCHIVE_NAME, out, baseline=100)
    with pytest.raises(Failed, match="too much loss"):
        pipeline.verify(YLEIS, archive / ARCHIVE_NAME, out, baseline=100_000)


@pytest.mark.parametrize("missing", ["source_updated", "wmts_layer",
                                     "nodata_stripped", "downscale_filter"])
def test_a_build_missing_the_metadata_publish_needs_is_refused(tmp_path, archive, missing):
    out = make_mbtiles(tmp_path / "out.mbtiles",
                       {k: v for k, v in PUBLISHED_META.items() if k != missing},
                       tiles=100)
    with pytest.raises(Failed, match=missing):
        pipeline.verify(YLEIS, archive / ARCHIVE_NAME, out, baseline=100)


def test_a_build_that_downscaled_from_the_wrong_level_is_refused(tmp_path, archive):
    """Publishing it would leave the published zoom disagreeing with the recipe,
    so the layer would rebuild every month and never converge."""
    out = make_mbtiles(tmp_path / "out.mbtiles",
                       PUBLISHED_META | {"downscale_source_zoom": "12"}, tiles=100)
    with pytest.raises(Failed, match="rebuild this layer again"):
        pipeline.verify(YLEIS, archive / ARCHIVE_NAME, out, baseline=100)


def test_a_build_stamped_with_the_wrong_strip_is_refused(tmp_path, archive):
    out = make_mbtiles(tmp_path / "out.mbtiles",
                       PUBLISHED_META | {"nodata_stripped": "opaque-black-r9-edge"},
                       tiles=100)
    with pytest.raises(Failed, match="but this build asked for"):
        pipeline.verify(YLEIS, archive / ARCHIVE_NAME, out, baseline=100)


# --- what the steps are actually told to do -----------------------------------

@pytest.fixture
def steps(monkeypatch):
    """Record every command the pipeline would run, and run none of them."""
    seen = []

    def fake(cmd, what):
        seen.append((what, cmd))

    monkeypatch.setattr(pipeline, "run_step", fake)
    return seen


def only(steps, what):
    return next(cmd for w, cmd in steps if w == what)


def step_input(cmd):
    """The file a step reads: the first argument after the script it runs.

    Found by shape rather than by index -- the uv invocation carries flags of
    its own, and a test that counts positions breaks when one is added.
    """
    script = next(i for i, a in enumerate(cmd) if a.endswith(".py"))
    return cmd[script + 1]


def test_downscale_is_always_told_which_level_to_start_from(archive, work, steps):
    """Derived from recipe, not spelled out: the zoom process passes and the zoom
    why_run compares against have to be one number."""
    src = archive / ARCHIVE_NAME
    steps.append(("stub-out", []))
    with pytest.raises(Failed):        # nothing wrote the output, so verify trips
        pipeline.process(YLEIS, src, work, jobs=1, baseline=100)
    cmd = only(steps, "downscale")
    assert "--source-zoom" in cmd
    assert cmd[cmd.index("--source-zoom") + 1] == str(pipeline.recipe(YLEIS, src)[1])


def test_an_unpinned_layer_is_told_the_deepest_level_too(archive, work, steps):
    src = archive / ARCHIVE_NAME
    with pytest.raises(Failed):
        pipeline.process(DEEPEST, src, work, jobs=1, baseline=100)
    cmd = only(steps, "downscale")
    assert cmd[cmd.index("--source-zoom") + 1] == "13"


def test_strip_is_told_the_settings_the_recipe_predicts(archive, work, steps):
    """Both are in the stamp, so a run that used different ones from those
    why_run compares against would rebuild this layer every month."""
    src = archive / ARCHIVE_NAME
    with pytest.raises(Failed):
        pipeline.process(YLEIS, src, work, jobs=1, baseline=100)
    cmd = only(steps, "strip-nodata")
    assert str(src) in cmd
    assert Path(cmd[cmd.index("--out") + 1]).parent == work
    stamp = pipeline.recipe(YLEIS, src)[0]
    assert cmd[cmd.index("--white-level") + 1] in stamp
    assert cmd[cmd.index("--radius") + 1] in stamp
    assert cmd[cmd.index("--stages") + 1].replace(",", "+") in stamp


def test_a_layer_with_nothing_to_strip_is_not_stripped(archive, work, steps):
    """Satamakartat carries no off-sheet fill at all -- not one wholly-fill tile
    in 3,000 sampled -- so the strip there can only take chart. Its archive goes
    straight to downscale, and the result carries no strip stamp for anything
    downstream to compare against."""
    src = archive / ARCHIVE_NAME
    plain = Layer("Satamakartat")
    with pytest.raises(Failed):
        pipeline.process(plain, src, work, jobs=1, baseline=100)
    assert not [c for c, what in steps if what == "strip-nodata"], \
        "a layer with nothing to remove was stripped anyway"
    assert str(src) in only(steps, "downscale"), \
        "downscale did not read the archive directly"
    assert pipeline.recipe(plain, src)[0] is None


def test_only_a_layer_that_draws_its_limit_gets_the_blank_trimmed(archive, work, steps):
    """white-pixels needs a drawn limit to stop at. Where a sheet simply ends
    the water inside is the same white as the blank outside and the trim takes
    both -- 2,365 tiles lost more than 20,000 px each on Rannikkokartat when
    this was measured. Only Yleiskartat draws one."""
    src = archive / ARCHIVE_NAME
    with pytest.raises(Failed):
        pipeline.process(DEEPEST, src, work, jobs=1, baseline=100)
    cmd = only(steps, "strip-nodata")
    assert "white-pixels" not in cmd[cmd.index("--stages") + 1]
    assert "white-pixels" not in pipeline.recipe(DEEPEST, src)[0]
    assert "white-pixels" in pipeline.recipe(YLEIS, src)[0], \
        "the one layer that does draw a limit lost the trim too"


def test_the_layer_table_matches_the_census_it_was_set_from():
    """The two tests above pin the mechanism; this pins the table, which is the
    part that decides what actually happens to a chart.

    From a 3,000-tile sample of each archive's deepest level, share of tiles
    that are nothing but off-sheet fill: Yleiskartat 8.90%, Rannikkokartat
    1.77%, Merikarttasarjat 0.27%, Satamakartat 0.00%. Only Yleiskartat draws a
    limit the trim can stop at."""
    by_name = {l.wmts: l for l in pipeline.LAYERS}
    assert not by_name["Satamakartat"].stages, \
        "Satamakartat has nothing to strip; stripping it can only take chart"
    trims = {n for n, l in by_name.items() if "white-pixels" in l.stages}
    assert trims == {"Yleiskartat 250k public"}, \
        f"the trim needs a drawn limit, and only Yleiskartat has one: {trims}"
    for name, l in by_name.items():
        assert set(l.stages) <= set(strip_nodata.STAGES), f"{name} names no such stage"
        assert list(l.stages) == [s for s in strip_nodata.STAGES if s in l.stages], \
            f"{name} lists its stages out of the order they run in"


def test_a_step_that_exits_non_zero_stops_the_layer():
    with pytest.raises(Failed, match="exited 3"):
        pipeline.run_step([sys.executable, "-c", "raise SystemExit(3)"], "downscale")


# --- refreshing ----------------------------------------------------------------

def test_a_refresh_that_wrote_no_report_is_a_failure_not_a_quiet_month(archive, work, steps):
    """A sweep that transferred tiles and died before reporting must not be read
    as 'nothing changed' -- that is the staleness this whole step exists to stop."""
    with pytest.raises(Failed, match="wrote no report"):
        pipeline.refresh(archive / ARCHIVE_NAME, work)


def test_a_malformed_report_fails_the_layer_not_the_run(archive, work, monkeypatch):
    def fake(cmd, what):
        if what == "refresh":
            Path(cmd[cmd.index("--report") + 1]).write_text("{torn by a kill")

    monkeypatch.setattr(pipeline, "run_step", fake)
    with pytest.raises(Failed, match="not readable JSON"):
        pipeline.refresh(archive / ARCHIVE_NAME, work)


def test_movement_bumps_the_archive_revision(archive, work, monkeypatch):
    def fake(cmd, what):
        if what == "refresh":
            Path(cmd[cmd.index("--report") + 1]).write_text(
                '{"checked": 9, "updated": 0, "removed": 3}')

    monkeypatch.setattr(pipeline, "run_step", fake)
    counts = pipeline.refresh(archive / ARCHIVE_NAME, work)
    assert counts["revision"] == 1
    assert pipeline.revision(pipeline.meta(archive / ARCHIVE_NAME)) == 1


def test_a_quiet_refresh_leaves_the_revision_alone(archive, work, monkeypatch):
    def fake(cmd, what):
        if what == "refresh":
            Path(cmd[cmd.index("--report") + 1]).write_text(
                '{"checked": 9, "updated": 0, "removed": 0}')

    monkeypatch.setattr(pipeline, "run_step", fake)
    pipeline.refresh(archive / ARCHIVE_NAME, work)
    assert pipeline.revision(pipeline.meta(archive / ARCHIVE_NAME)) == 0


def test_the_archive_is_relabelled_after_it_is_refreshed(archive, work, monkeypatch):
    order = []

    def fake(cmd, what):
        order.append(what)
        if what == "refresh":
            Path(cmd[cmd.index("--report") + 1]).write_text('{"updated": 0, "removed": 0}')

    monkeypatch.setattr(pipeline, "run_step", fake)
    pipeline.refresh(archive / ARCHIVE_NAME, work)
    assert order == ["refresh", "currency"]


def test_the_renamed_archive_is_looked_up_again_after_refresh(archive, work, dest, monkeypatch):
    """currency renames on every new edition, so the path the caller started
    with is stale exactly in the month where there is work to do."""
    renamed = archive / "fi-yleiskartat250k-2026-08-01.mbtiles"

    def fake(cmd, what):
        if what == "refresh":
            Path(cmd[cmd.index("--report") + 1]).write_text('{"updated": 5, "removed": 0}')
        if what == "currency":
            src = Path(cmd[-2])
            make_mbtiles(renamed, ARCHIVE_META | {"source_updated": "2026-08-01"})
            src.unlink()

    monkeypatch.setattr(pipeline, "run_step", fake)
    with pytest.raises(Failed):        # process runs no steps, so verify trips
        pipeline.build_layer(YLEIS, archive / ARCHIVE_NAME, work, dest,
                             jobs=1, force=False, skip_refresh=False)
    assert renamed.exists()
    assert not (work / "fi-yleiskartat250k-2026-06-02.processed.mbtiles").exists()


# --- scratch ------------------------------------------------------------------

def test_a_failed_build_leaves_no_scratch_behind(archive, work, dest, steps):
    """Including the finished file: downscale has already moved it into place by
    the time verify refuses it, and nothing else would ever collect it."""
    stem = "fi-yleiskartat250k-2026-06-02"
    for name in (f"{stem}.stripped.mbtiles", f"{stem}.processed.mbtiles",
                 f"{stem}.processed.mbtiles.partial"):
        (work / name).write_bytes(b"scratch")
    with pytest.raises(Failed):
        pipeline.build_layer(YLEIS, archive / ARCHIVE_NAME, work, dest,
                             jobs=1, force=True, skip_refresh=True)
    assert list(work.iterdir()) == []


def test_a_successful_build_keeps_the_file_it_built(work):
    stem = "fi-yleiskartat250k-2026-06-02"
    for name in (f"{stem}.stripped.mbtiles.partial", f"{stem}.stripped.mbtiles"):
        (work / name).write_bytes(b"scratch")
    keep = work / f"{stem}.processed.mbtiles"
    keep.write_bytes(b"the build")
    pipeline.sweep(work, stem, built=False)
    assert [p.name for p in work.iterdir()] == [keep.name]


def test_last_months_orphans_are_swept_whatever_they_were_called(work):
    """The per-layer sweep keys on the archive stem, which currency renames on
    every new edition, and it is a finally that a kill -9 skips entirely."""
    (work / "fi-yleiskartat250k-2026-01-01.processed.mbtiles").write_bytes(b"old")
    (work / "fi-satamakartat-2025-11-11.stripped.mbtiles.partial").write_bytes(b"older")
    (work / pipeline.LOCK).write_bytes(b"")
    pipeline.sweep_stale(work)
    assert [p.name for p in work.iterdir()] == [pipeline.LOCK]


def test_a_full_disk_is_refused_before_anything_is_built(work, archive, monkeypatch):
    import shutil as sh
    monkeypatch.setattr(pipeline.shutil, "disk_usage",
                        lambda p: sh._ntuple_diskusage(100, 100, 1))
    with pytest.raises(Failed, match="refusing rather than filling the disk"):
        pipeline.check_space(work, [archive / ARCHIVE_NAME])


# --- one run at a time ---------------------------------------------------------

def test_a_second_run_is_refused_while_the_first_holds_the_lock(work):
    with pipeline.exclusive(work):
        with pytest.raises(Failed, match="refusing to run concurrently"):
            with pipeline.exclusive(work):
                pass


def test_the_lock_is_released_when_the_run_ends(work):
    with pipeline.exclusive(work):
        pass
    with pipeline.exclusive(work):
        pass


# --- the directories a run is pointed at ---------------------------------------

def test_directories_are_resolved_so_every_step_sees_the_same_one(tmp_path, monkeypatch):
    import argparse
    (tmp_path / "a").mkdir(), (tmp_path / "w").mkdir(), (tmp_path / "d").mkdir()
    monkeypatch.chdir(tmp_path)
    args = argparse.Namespace(archive=Path("a"), work=Path("w"), dest=Path("d"))
    assert pipeline.resolve_dirs(args) is None
    assert all(p.is_absolute() for p in (args.archive, args.work, args.dest))


@pytest.mark.parametrize("alias", ["archive-is-dest", "archive-is-work", "work-is-dest"])
def test_pointing_two_of_the_directories_at_one_place_is_refused(tmp_path, alias):
    import argparse
    a, w, d = (tmp_path / n for n in "awd")
    for p in (a, w, d):
        p.mkdir()
    args = argparse.Namespace(archive=a, work=w, dest=d)
    if alias == "archive-is-dest":
        args.dest = a
    elif alias == "archive-is-work":
        args.work = a
    else:
        args.dest = w
    assert "same directory" in pipeline.resolve_dirs(args)


@pytest.mark.parametrize("which", ["--archive", "--work", "--dest"])
def test_an_empty_directory_argument_is_refused(cli, which):
    """A variable the unit's environment file does not set arrives as an empty
    argument, and Path("") is the cwd -- a real directory that passes every
    check in resolve_dirs. Blank --dest publishes into the clone; blank --work
    sweeps it."""
    with pytest.raises(SystemExit) as exit:
        cli(which, "")
    assert exit.value.code == 2


def test_a_terminated_run_keeps_the_layers_it_already_published(
        cli, archive, work, monkeypatch, capsys):
    """TimeoutStartSec makes SIGTERM a scheduled event rather than an accident,
    and Python's default handling skips every `finally` -- which is where the
    sweeps live. The layer the signal lands on leaves no scratch and publishes
    nothing; the layers behind it keep what they already put in place."""
    make_mbtiles(archive / "fi-satamakartat-2026-06-29.mbtiles",
                 ARCHIVE_META | {"wmts_layer": "Satamakartat",
                                 "source_updated": "2026-06-29"})
    published = []

    def fake(cmd, what):
        if what == "publish":
            published.append(step_input(cmd))
            return
        if what == "downscale" and "satamakartat" in step_input(cmd):
            raise pipeline.Terminated("stopped by signal 15")
        if what in ("strip-nodata", "downscale"):
            make_mbtiles(Path(cmd[cmd.index("--out") + 1]), PUBLISHED_META)

    monkeypatch.setattr(pipeline, "run_step", fake)
    assert cli("--force", "--skip-refresh", "--layer", "Yleiskartat 250k public",
               "--layer", "Satamakartat") == 143
    assert [Path(p).name.split("-")[1] for p in published] == ["yleiskartat250k"]
    assert [p.name for p in work.iterdir()] == [pipeline.LOCK]
    out = capsys.readouterr()
    assert "stopped by signal 15" in out.err
    # The summary is the only account of what a killed run did land, and a
    # killed run is now the ordinary way a long month ends.
    assert "1 published" in out.out


def test_a_signal_during_the_startup_sweep_is_still_accounted_for(
        cli, monkeypatch, capsys):
    """The sweep deletes a killed run's scratch and has taken minutes at 14 GB,
    so a signal lands in it often enough to matter. It runs before the layer
    loop, so a summary printed only around that loop leaves the run that did
    least the one run that says nothing about itself."""
    def swept(_):
        raise pipeline.Terminated("stopped by signal 15")

    monkeypatch.setattr(pipeline, "sweep_stale", swept)
    assert cli("--skip-refresh", "--layer", "Yleiskartat 250k public") == 143
    assert "0 published" in capsys.readouterr().out


def test_the_lock_covers_the_archive_as_well_as_the_work_directory(cli, archive):
    """Refresh rewrites the archive in place and currency renames it. Two runs
    with different --work would both acquire and race the same SQLite file."""
    with pipeline.exclusive(archive):
        assert cli("--skip-refresh", "--layer", "Yleiskartat 250k public") == 2


def test_a_second_run_reports_the_refusal_rather_than_a_traceback(cli, work, capsys):
    """Exit 2 is "did not run" everywhere else in main(). Letting Failed escape
    gave exit 1 -- what run() returns when a layer broke -- so a refused month
    and a broken one were one signal."""
    with pipeline.exclusive(work):
        assert cli("--skip-refresh", "--layer", "Yleiskartat 250k public") == 2
    assert "refusing to run concurrently" in capsys.readouterr().err


# --- publishing has to be able to rename, not copy -----------------------------

def test_a_layout_that_can_rename_is_accepted(work, dest):
    assert pipeline.can_rename(work, dest) is None


def test_the_probe_leaves_nothing_behind_in_either_directory(work, dest):
    before = sorted(p.name for p in dest.iterdir())
    pipeline.can_rename(work, dest)
    assert sorted(p.name for p in dest.iterdir()) == before
    assert list(work.iterdir()) == []


def test_a_layout_that_cannot_rename_is_named_not_discovered_at_hour_ten(
        work, dest, monkeypatch):
    """Two bind mounts of one host filesystem report the same st_dev and still
    fail with EXDEV, because rename(2) tests mount-point identity. Only trying
    it finds that out."""
    def cross_device(src, dst):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(pipeline.os, "replace", cross_device)
    complaint = pipeline.can_rename(work, dest)
    assert str(work) in complaint and str(dest) in complaint


def test_the_probe_cleans_up_after_a_failed_rename(work, dest, monkeypatch):
    monkeypatch.setattr(pipeline.os, "replace",
                        lambda s, d: (_ for _ in ()).throw(OSError(errno.EXDEV, "no")))
    pipeline.can_rename(work, dest)
    assert list(work.iterdir()) == []


def test_a_destination_that_cannot_be_written_says_so(work, dest, monkeypatch):
    """Denied by a mocked errno rather than by mode bits: root ignores mode bits,
    so a container running the suite as root would not exercise this at all."""
    monkeypatch.setattr(pipeline.os, "replace",
                        lambda s, d: (_ for _ in ()).throw(
                            PermissionError(errno.EACCES, "Permission denied")))
    complaint = pipeline.can_rename(work, dest)
    assert complaint and str(dest) in complaint


def test_the_probe_does_not_destroy_a_file_that_is_already_there(work, dest):
    """os.replace overwrites whatever it lands on and the cleanup then removes
    it, so a fixed probe name would delete a served chart that happened to
    share it."""
    for d in (work, dest):
        (d / ".rename-probe").write_text("someone else's file")
    assert pipeline.can_rename(work, dest) is None
    assert (work / ".rename-probe").read_text() == "someone else's file"
    assert (dest / ".rename-probe").read_text() == "someone else's file"


def test_two_probes_at_once_do_not_collide(work, dest):
    """can_rename runs before the lock is taken, so two runs can probe together."""
    names = set()
    real = pipeline.tempfile.mkstemp

    def remember(**kwargs):
        fd, path = real(**kwargs)
        names.add(Path(path).name)
        return fd, path

    pipeline.tempfile.mkstemp = remember
    try:
        for _ in range(5):
            assert pipeline.can_rename(work, dest) is None
    finally:
        pipeline.tempfile.mkstemp = real
    assert len(names) == 5


def test_a_run_is_refused_before_any_step_when_the_layout_cannot_rename(
        cli, monkeypatch):
    monkeypatch.setattr(pipeline, "run_step",
                        lambda *a: pytest.fail("a step ran despite a bad layout"))
    monkeypatch.setattr(pipeline, "can_rename",
                        lambda w, d: "work and dest cannot rename between them")
    assert cli("--skip-refresh", "--layer", "Yleiskartat 250k public") == 2


def test_a_missing_directory_is_named(tmp_path):
    import argparse
    (tmp_path / "a").mkdir()
    args = argparse.Namespace(archive=tmp_path / "a", work=tmp_path / "gone",
                              dest=tmp_path / "a")
    assert "no such directory" in pipeline.resolve_dirs(args)


# --- the run as a whole --------------------------------------------------------

@pytest.fixture
def cli(monkeypatch, archive, work, dest):
    def run(*extra):
        monkeypatch.setattr(sys, "argv", [
            "pipeline.py", "--archive", str(archive), "--work", str(work),
            "--dest", str(dest), *extra])
        return pipeline.main()
    return run


def test_dry_run_touches_nothing_and_runs_no_steps(cli, monkeypatch, work, archive):
    monkeypatch.setattr(pipeline, "run_step",
                        lambda *a: pytest.fail("--dry-run ran a step"))
    before = sorted(p.name for p in archive.iterdir())
    assert cli("--dry-run", "--layer", "Yleiskartat 250k public") == 0
    assert list(work.iterdir()) == []
    assert sorted(p.name for p in archive.iterdir()) == before


def test_dry_run_reports_a_missing_archive_in_its_exit_status(cli, monkeypatch):
    monkeypatch.setattr(pipeline, "run_step", lambda *a: None)
    assert cli("--dry-run", "--layer", "Satamakartat") == 1


def test_each_layer_publishes_as_it_finishes(cli, monkeypatch, archive):
    """One publish after the loop means a run that does not reach the end
    publishes nothing, however many layers it finished. The run is long enough
    that systemd killing it is a scheduled event, and the layer that is last is
    then the one that never lands -- the same one, every month."""
    make_mbtiles(archive / "fi-satamakartat-2026-06-29.mbtiles",
                 ARCHIVE_META | {"wmts_layer": "Satamakartat",
                                 "source_updated": "2026-06-29"})
    published, trace = [], []

    def fake(cmd, what):
        src = step_input(cmd)
        trace.append((what, "satamakartat" if "satamakartat" in src
                      else "yleiskartat250k"))
        if what == "publish":
            published.append([a for a in cmd if a.endswith(".mbtiles")])
            return
        if what == "strip-nodata":
            make_mbtiles(Path(cmd[cmd.index("--out") + 1]), PUBLISHED_META)
        if what == "downscale":
            # Satamakartat is not stripped, so downscale reads the archive and
            # the result carries no strip stamp
            stripped = ".stripped." in step_input(cmd)
            m = PUBLISHED_META | {
                "downscale_source_zoom": cmd[cmd.index("--source-zoom") + 1]}
            if not stripped:
                m = m | {"wmts_layer": "Satamakartat",
                         "source_updated": "2026-06-29"}
                m.pop("nodata_stripped", None)
            make_mbtiles(Path(cmd[cmd.index("--out") + 1]), m)

    monkeypatch.setattr(pipeline, "run_step", fake)
    assert cli("--force", "--skip-refresh", "--layer", "Yleiskartat 250k public",
               "--layer", "Satamakartat") == 0
    assert len(published) == 2, "the sets were held back for one publish at the end"
    assert all(len(call) == 1 for call in published), published
    # Interleaving, not just one call per set: a loop over the finished files
    # placed after the layer loop also publishes each of them separately, in
    # this order, and still loses every one of them to a signal.
    began_second = next(i for i, (_, layer) in enumerate(trace)
                        if layer == "satamakartat")
    assert ("publish", "yleiskartat250k") in trace[:began_second], trace


def test_one_failing_layer_does_not_stop_the_others(cli, monkeypatch, archive, dest):
    """The failing layer runs first, so a `break` where the loop means `continue`
    costs the layer behind it. Both are attempted, the good one still publishes,
    and the run reports failure."""
    make_mbtiles(archive / "fi-satamakartat-2026-06-29.mbtiles",
                 ARCHIVE_META | {"wmts_layer": "Satamakartat",
                                 "source_updated": "2026-06-29"})
    attempted, published_cmd = [], []

    def fake(cmd, what):
        if what == "strip-nodata":
            attempted.append(step_input(cmd))
            if "yleiskartat" in step_input(cmd):
                raise Failed("strip-nodata exited 1")
            make_mbtiles(Path(cmd[cmd.index("--out") + 1]), PUBLISHED_META)
        if what == "downscale":
            # Satamakartat is not stripped, so downscale reads the archive and
            # the result carries no strip stamp
            stripped = ".stripped." in step_input(cmd)
            if not stripped:
                attempted.append(step_input(cmd))
            zoom = cmd[cmd.index("--source-zoom") + 1]
            m = PUBLISHED_META | {"wmts_layer": "Satamakartat",
                                  "source_updated": "2026-06-29",
                                  "downscale_source_zoom": zoom}
            if not stripped:
                m.pop("nodata_stripped", None)
            make_mbtiles(Path(cmd[cmd.index("--out") + 1]), m)
        if what == "publish":
            published_cmd.append(cmd)

    monkeypatch.setattr(pipeline, "run_step", fake)
    rc = cli("--force", "--skip-refresh",
             "--layer", "Yleiskartat 250k public", "--layer", "Satamakartat")
    assert rc == 1
    assert [Path(a).name.split("-")[1] for a in attempted] == ["yleiskartat250k",
                                                              "satamakartat"]
    assert len(published_cmd) == 1
    assert any("satamakartat" in a for a in published_cmd[0])


def test_a_run_sweeps_last_months_orphans_before_it_builds(cli, monkeypatch, work):
    orphan = work / "fi-yleiskartat250k-2026-01-01.processed.mbtiles"
    orphan.write_bytes(b"left by a killed run three months ago")
    monkeypatch.setattr(pipeline, "run_step", lambda *a: None)
    assert cli("--skip-refresh", "--layer", "Yleiskartat 250k public") == 0
    assert not orphan.exists()


def test_a_run_with_nothing_to_do_publishes_nothing_and_succeeds(cli, monkeypatch):
    calls = []
    monkeypatch.setattr(pipeline, "run_step", lambda cmd, what: calls.append(what))
    assert cli("--skip-refresh", "--layer", "Yleiskartat 250k public") == 0
    assert "publish" not in calls


def test_the_work_directory_is_empty_whether_publish_succeeds_or_not(cli, monkeypatch, work):
    def fake(cmd, what):
        if what in ("strip-nodata", "downscale"):
            make_mbtiles(Path(cmd[cmd.index("--out") + 1]), PUBLISHED_META)
        if what == "publish":
            raise Failed("publish exited 1")

    monkeypatch.setattr(pipeline, "run_step", fake)
    assert cli("--force", "--skip-refresh", "--layer", "Yleiskartat 250k public") == 1
    assert [p.name for p in work.iterdir()] == [pipeline.LOCK]


def test_an_unknown_layer_is_refused(cli):
    assert cli("--layer", "Nonexistent charts") == 2


def test_an_archive_no_layer_claims_is_reported_not_silently_skipped(cli, monkeypatch,
                                                                    archive, capsys):
    make_mbtiles(archive / "fi-veneilykartat-2026-06-21.mbtiles",
                 ARCHIVE_META | {"wmts_layer": "Veneilykartat public"})
    monkeypatch.setattr(pipeline, "run_step", lambda *a: None)
    cli("--dry-run")
    assert "no layer claims it" in capsys.readouterr().out


# --- being a guest on a shared host ------------------------------------------

def test_every_step_is_niced(monkeypatch):
    monkeypatch.setattr(pipeline.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert pipeline.nicely(["uv", "run", "x.py"]) == [
        "nice", "-n", str(pipeline.NICE), "ionice", "-c", pipeline.IONICE_IDLE,
        "uv", "run", "x.py"]


def test_a_host_without_ionice_still_runs_and_says_so(monkeypatch, capsys):
    monkeypatch.setattr(pipeline, "_missing", set())
    monkeypatch.setattr(pipeline.shutil, "which",
                        lambda name: None if name == "ionice" else f"/usr/bin/{name}")
    assert pipeline.nicely(["x"]) == ["nice", "-n", str(pipeline.NICE), "x"]
    assert "no ionice" in capsys.readouterr().err


def test_the_missing_tool_warning_is_not_repeated_every_step(monkeypatch, capsys):
    monkeypatch.setattr(pipeline, "_missing", set())
    monkeypatch.setattr(pipeline.shutil, "which", lambda name: None)
    pipeline.nicely(["x"])
    capsys.readouterr()
    pipeline.nicely(["x"])
    assert capsys.readouterr().err == ""


# --- a run log someone can read a month later ---------------------------------

def test_a_long_run_reads_in_hours_rather_than_hundreds_of_minutes():
    assert pipeline.duration(10.5 * 3600) == "10 h 30 min"


def test_a_run_under_an_hour_stays_in_minutes():
    assert pipeline.duration(5 * 60) == "5 min"


def test_the_hour_boundary_does_not_read_as_zero_minutes():
    assert pipeline.duration(3600) == "1 h 0 min"


def test_a_run_that_rounds_up_to_the_hour_never_reads_as_sixty_minutes():
    """Branching on the seconds rather than on the rounded minutes puts the
    boundary in the wrong place, and 3599 s prints as `60 min`."""
    assert pipeline.duration(3599) == "1 h 0 min"


def test_each_layer_is_timed_from_its_own_start_not_the_run_s(
        cli, archive, dest, monkeypatch, capsys):
    """Timing every layer from the whole run's start would read as plausible
    increasing numbers -- 9 h, 9 h 30, 9 h 45 -- and hide which layer is slow,
    which is the only question a ten-hour log gets asked."""
    satama = ARCHIVE_META | {"wmts_layer": "Satamakartat",
                             "source_updated": "2026-06-29"}
    make_mbtiles(archive / "fi-satamakartat-2026-06-29.mbtiles", satama)
    # published too, so both layers skip and the run's exit code stays readable
    published = PUBLISHED_META | satama
    published.pop("nodata_stripped")
    make_mbtiles(dest / "fi-satamakartat-2026-06-29.mbtiles", published)
    ticks = iter([0,                                  # run start
                  100, 100 + 9 * 3600,                # Yleiskartat start, end
                  100 + 9 * 3600, 100 + 12 * 3600,    # Satamakartat start, end
                  100 + 12 * 3600])                   # run end
    monkeypatch.setattr(pipeline.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(pipeline, "run_step", lambda *a: None)
    assert cli("--skip-refresh", "--layer", "Yleiskartat 250k public",
               "--layer", "Satamakartat") == 0
    logged = {}
    for chunk in capsys.readouterr().out.split("\n=== ")[1:]:
        name, _, body = chunk.partition(" ===\n")
        logged[name] = body
    assert "took 9 h 0 min" in logged["Yleiskartat 250k public"]
    assert "took 3 h 0 min" in logged["Satamakartat"]


def test_a_failed_layer_is_still_timed(cli, monkeypatch, capsys):
    def fake(cmd, what):
        if what == "strip-nodata":
            raise Failed("strip-nodata exited 1")

    monkeypatch.setattr(pipeline, "run_step", fake)
    assert cli("--force", "--skip-refresh", "--layer", "Yleiskartat 250k public") == 1
    assert "took 0 min" in capsys.readouterr().out.split("=== Yleiskartat")[1]


def test_a_cgroup_reports_the_whole_subtree_and_says_so(tmp_path, monkeypatch):
    """ru_maxrss is the largest single process. Both heavy steps fan out across
    a worker pool, so the number that matters is the parent plus its workers
    resident together, which only the cgroup counter sees."""
    peak = tmp_path / "memory.peak"
    peak.write_text("224395264\n")
    monkeypatch.setattr(pipeline, "CGROUP_PEAK", peak)
    assert pipeline.peak_memory() == (224395264, "subtree")


def test_without_a_cgroup_the_process_figure_is_used_and_labelled(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "CGROUP_PEAK", tmp_path / "absent")
    assert pipeline.peak_memory()[1] == "one process"


@pytest.mark.parametrize("content", ["", "   ", "not a number", "1_0", "-5", "12.5"])
def test_an_unreadable_cgroup_value_falls_back_rather_than_failing_the_run(
        tmp_path, monkeypatch, content):
    """This runs in the last line of a ten-hour job. It must not be what fails
    it."""
    peak = tmp_path / "memory.peak"
    peak.write_text(content)
    monkeypatch.setattr(pipeline, "CGROUP_PEAK", peak)
    assert pipeline.peak_memory()[1] == "one process"


def test_a_cgroup_that_cannot_be_read_falls_back(monkeypatch):
    """Denied by a raised error rather than by mode bits, which root ignores."""
    class Denied:
        def read_text(self):
            raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(pipeline, "CGROUP_PEAK", Denied())
    assert pipeline.peak_memory()[1] == "one process"


def test_the_run_says_which_measurement_it_reported(cli, monkeypatch, capsys):
    monkeypatch.setattr(pipeline, "peak_memory", lambda: (412 * 1024 * 1024, "subtree"))
    monkeypatch.setattr(pipeline, "run_step", lambda *a: None)
    assert cli("--skip-refresh", "--layer", "Yleiskartat 250k public") == 0
    out = capsys.readouterr().out
    assert "412 MiB" in out and "subtree" in out


def test_peak_memory_measures_the_steps_and_not_the_pipeline_itself(monkeypatch):
    """RUSAGE_SELF would report this process -- tens of MiB, never the multi-GB
    peak of a strip -- and the whole point is to catch a step's regression."""
    seen = []

    class Usage:
        ru_maxrss = 4096

    monkeypatch.setattr(pipeline.resource, "getrusage",
                        lambda who: (seen.append(who), Usage())[1])
    pipeline.peak_process_memory()
    assert seen == [pipeline.resource.RUSAGE_CHILDREN]


def test_peak_memory_reads_the_units_of_the_host_it_runs_on(monkeypatch):
    """ru_maxrss is KiB on Linux and bytes on macOS. Production is Linux and the
    tests run on both, so the wrong assumption is off by 1024 where nobody looks."""
    class Usage:
        ru_maxrss = 4096

    monkeypatch.setattr(pipeline.resource, "getrusage", lambda who: Usage())
    monkeypatch.setattr(pipeline.sys, "platform", "linux")
    assert pipeline.peak_process_memory() == 4096 * 1024
    monkeypatch.setattr(pipeline.sys, "platform", "darwin")
    assert pipeline.peak_process_memory() == 4096


# --- how a step gets invoked ---------------------------------------------------

def test_a_step_runs_under_uv_against_the_lockfile_by_default(monkeypatch):
    monkeypatch.delenv(pipeline.INTERPRETER, raising=False)
    assert pipeline.uv("downscale.py", "--jobs", "1") == [
        "uv", "run", "--locked", str(pipeline.REPO / "downscale.py"), "--jobs", "1"]


def test_a_named_interpreter_replaces_uv_entirely(monkeypatch):
    """The image resolves its dependencies once, at build time. Re-resolving at
    01:00 is the thing --locked exists to prevent, and an interpreter that was
    given its environment at build time has nothing left to resolve."""
    monkeypatch.setenv(pipeline.INTERPRETER, "/usr/local/bin/python3")
    assert pipeline.uv("downscale.py", "--jobs", "1") == [
        "/usr/local/bin/python3", str(pipeline.REPO / "downscale.py"), "--jobs", "1"]


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_interpreter_reads_as_unset_not_as_an_empty_argument(monkeypatch, blank):
    """Same shape as the blank --dest that published into the clone: an unset
    variable arrives as an empty string, and honouring it would exec ''."""
    monkeypatch.setenv(pipeline.INTERPRETER, blank)
    assert pipeline.uv("downscale.py")[0] == "uv"


# --- the timer has to invoke a command this repo actually has ------------------

def unit_text(name: str) -> str:
    return (pipeline.REPO / "systemd" / name).read_text()


def test_the_service_passes_only_variables_the_example_env_file_sets():
    """The two files are coupled by hand across a directory boundary. Rename one
    and the timer's next fire passes an empty path, a month later."""
    exec_line = next(l for l in unit_text("fi-nautical-charts.service").splitlines()
                     if l.startswith("ExecStart="))
    used = set(re.findall(r'\$([A-Z_]+)', exec_line))
    defined = set(re.findall(r'^([A-Z_]+)=',
                             unit_text("fi-nautical-charts.env.example"), re.M))
    assert used == defined


def test_the_timer_names_a_service_file_that_exists():
    unit = re.search(r'^Unit=(\S+)$', unit_text("fi-nautical-charts.timer"),
                     re.M).group(1)
    assert (pipeline.REPO / "systemd" / unit).exists()


def exec_start() -> str:
    return next(l for l in unit_text("fi-nautical-charts.service").splitlines()
                if l.startswith("ExecStart="))


def test_the_service_runs_the_image_the_env_file_names():
    """The image is built here and pulled from nowhere, so the name in the
    example file and the tag the build writes are one string kept in two
    places."""
    tag = re.search(r'--tag (\S+)', (pipeline.REPO / "run").read_text()).group(1)
    assert re.search(rf'^CHARTS_IMAGE={tag}$',
                     unit_text("fi-nautical-charts.env.example"), re.M)


SPOOL = "/srv/charts"
SETTINGS = {"CHARTS_IMAGE": "an-image",
            "CHARTS_ARCHIVE": "/srv/archive",
            "CHARTS_WORK": f"{SPOOL}/work",
            "CHARTS_DEST": f"{SPOOL}/published"}


def run_exec_start(tmp_path, **overrides) -> subprocess.CompletedProcess:
    """Run the unit's ExecStart with a stub docker on PATH.

    The line is a bash program, and reading a program is not running it: the
    mount it assembles is `$(dirname …)` of something the file never spells out,
    and which of the quoting forms survives is bash's business, not a reader's.
    So this runs it, and the stub reports the argv the real client would get.

    Specifiers are systemd's and are substituted here the way systemd would.
    """
    stub = tmp_path / "docker"
    argv = tmp_path / "argv.txt"
    stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {shlex.quote(str(argv))}\n')
    stub.chmod(0o755)

    line = exec_start().removeprefix("ExecStart=")
    for specifier, value in (("%N", "fi-nautical-charts"),
                             ("%U", str(os.getuid())), ("%G", str(os.getgid()))):
        line = line.replace(specifier, value)
    done = subprocess.run(shlex.split(line), capture_output=True, text=True,
                          env={"PATH": f"{tmp_path}:/usr/bin:/bin",
                               **(SETTINGS | overrides)})
    done.argv = argv.read_text().split("\n") if argv.exists() else []
    return done


def test_the_unit_hands_docker_one_mount_holding_both_work_and_dest(tmp_path):
    """Two volumes fail `os.replace` with EXDEV even when both sides are the
    same host filesystem, because a rename tests mount-point identity. Publishing
    is a rename, so a second volume would fail at hour ten, on the step that has
    already cost the most -- and no volume at all would fail at the first."""
    mounts = [a for a in run_exec_start(tmp_path).argv if a.count(":") == 1
              and a.startswith("/")]
    assert f"{SPOOL}:{SPOOL}" in mounts, "the parent work and dest share is not mounted"
    for path in (SETTINGS["CHARTS_WORK"], SETTINGS["CHARTS_DEST"]):
        assert f"{path}:{path}" not in mounts, (
            f"{path} is mounted in its own right; publishing cannot rename across "
            f"two mounts")


def test_the_unit_passes_only_flags_the_pipeline_declares(tmp_path):
    """Taken from the parser rather than from the file's text: a flag that
    argparse dropped can survive for years in a docstring, and a substring
    search cannot tell the two apart."""
    declared = {a.value for node in ast.walk(ast.parse(
                    (pipeline.REPO / "pipeline.py").read_text()))
                if isinstance(node, ast.Call)
                and getattr(node.func, "attr", None) == "add_argument"
                for a in node.args if isinstance(a, ast.Constant)}
    argv = run_exec_start(tmp_path).argv
    passed = argv[argv.index(SETTINGS["CHARTS_IMAGE"]) + 1:]
    assert passed, "nothing follows the image name"
    for flag in (a for a in passed if a.startswith("--")):
        assert flag in declared, f"{flag} is not an argument pipeline.py declares"


def test_the_unit_refuses_a_layout_that_cannot_publish(tmp_path):
    """Work and dest in different parents cannot go in through one mount, and
    the container would simply not have the destination -- reporting a directory
    missing that plainly exists on the host."""
    done = run_exec_start(tmp_path, CHARTS_DEST="/var/www/charts")
    assert done.returncode == 78 and not done.argv
    assert "side by side" in done.stderr


def test_the_unit_refuses_an_empty_image_name(tmp_path):
    """Empty is the one value docker reads as "the next argument is the image",
    which makes it complain about a reference format and name nothing anyone
    set. It is also the one thing here the pipeline cannot check for itself."""
    done = run_exec_start(tmp_path, CHARTS_IMAGE="")
    assert done.returncode == 78 and not done.argv
    assert "CHARTS_IMAGE" in done.stderr


def test_the_unit_never_reaches_a_registry(tmp_path):
    """The name carries no registry and the image exists only where it was
    built, so the default policy would send an unattended run to Docker Hub for
    whatever answers to it, and run that as this user with the archive and the
    served directory mounted."""
    assert "--pull=never" in run_exec_start(tmp_path).argv


def test_the_service_uses_only_the_three_specifiers_it_means_to():
    """A `%` in an Exec line is systemd's, not the shell's. An unknown one fails
    the unit at load, which is loud; a known one substitutes silently, which is
    not. A `printf "%s\\n"` written into this line came back as `/bin/bash\\n`,
    because %s is the user's shell."""
    assert set(re.findall(r'%(.)', exec_start())) == {"N", "U", "G"}


def test_the_container_is_stoppable_by_the_name_it_runs_under():
    """--sig-proxy carries one signal and never escalates, so a client that dies
    first leaves the container running with the scratch it had built. Stopping it
    by name goes through the daemon instead, and only works if the two names
    agree."""
    started = re.search(r'--name (\S+)', exec_start()).group(1)
    stopped = re.search(r'^ExecStop=.*docker stop\b.*?(\S+)$',
                        unit_text("fi-nautical-charts.service"), re.M).group(1)
    assert started == stopped


# --- the image has to carry what the run reaches -------------------------------

def dockerfile() -> str:
    return (pipeline.REPO / "Dockerfile").read_text()


def runtime_stage() -> str:
    """The last stage, which is what the image ships. Everything before it
    builds the environment and is thrown away."""
    return "FROM " + dockerfile().split("\nFROM ")[-1]


def reachable_modules() -> set[str]:
    """Every file in this repo the monthly run reaches, by import or by exec.

    Followed rather than listed: a list is a second place to remember, and the
    thing it would be remembered for -- a new import -- is exactly what a list
    misses. Starts at pipeline.py, takes every sibling it imports and every
    script it names as a string, and repeats. Names that are not files here are
    the standard library and drop out.
    """
    found: set[str] = set()
    queue = ["pipeline.py"]
    while queue:
        name = queue.pop()
        if name in found or not (pipeline.REPO / name).is_file():
            continue
        found.add(name)
        source = (pipeline.REPO / name).read_text()
        queue += re.findall(r'"(\w+\.py)"', source)
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                queue += [f"{a.name}.py" for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                queue.append(f"{node.module}.py")
    return found


def test_the_image_carries_every_file_the_run_reaches():
    """publish.py imports preview.py, and nothing about the entry point says so.
    A file left out surfaces ten hours in, at the step that publishes."""
    copied: set[str] = set()
    for line in runtime_stage().splitlines():
        if line.startswith("COPY "):
            copied.update(re.findall(r'\w+\.py', line))
    assert reachable_modules() <= copied


def test_the_entrypoint_does_not_go_through_the_run_dispatcher():
    """Verified: the dispatcher's bash function behind the `time` builtin
    swallows SIGTERM. The container exits 137, the handler never runs, and
    multi-gigabyte scratch survives the month. A shell-form ENTRYPOINT fails the
    same way, because docker wraps it in `sh -c` with no exec."""
    entry = next(l for l in runtime_stage().splitlines()
                 if l.startswith("ENTRYPOINT"))
    assert entry.startswith("ENTRYPOINT [")
    assert "/run" not in entry


def test_every_base_image_is_pinned_by_digest():
    """A tag moves under the two hosts that build from it. The lockfiles hash-pin
    every wheel, so the base image is what is left to drift."""
    stages = set(re.findall(r'^FROM \S+ AS (\S+)', dockerfile(), re.M))
    refs = re.findall(r'^FROM (\S+)', dockerfile(), re.M)
    refs += re.findall(r'^COPY --from=(\S+)', dockerfile(), re.M)
    for ref in refs:
        if ref not in stages:
            assert "@sha256:" in ref, f"{ref} is a tag, and a tag moves"


def test_the_image_names_the_interpreter_it_built():
    """The image's own check is what proves the interpreter is there and works;
    this only catches the halves drifting apart, which it can do without a
    daemon and before a build. Both ends are read out of the Dockerfile rather
    than spelled here, so renaming the venv consistently stays a rename and not
    a test failure."""
    venv = re.search(r'uv venv (\S+)', dockerfile()).group(1)
    assert re.search(rf'\b{pipeline.INTERPRETER}={re.escape(venv)}/bin/python\b',
                     runtime_stage())


def test_the_commit_reaches_the_manifest():
    """Three files carry it: `run` reads it from git, the Dockerfile declares it
    as an argument, and the image exports it under the name publish.py looks up.
    A missing value stops the build, but a wrong one cannot be caught by anything
    that has not built the image -- so what is pinned here is the chain, which is
    what a rename breaks."""
    named = re.search(rf'\b{publish.VERSION}=\$(\w+)', runtime_stage())
    assert named, "the image pins a fixed version instead of the commit built"
    declared = set(re.findall(r'^ARG (\w+)', dockerfile(), re.M))
    passed = set(re.findall(r'--build-arg (\w+)=',
                            (pipeline.REPO / "run").read_text()))
    assert named.group(1) in declared and named.group(1) in passed
    assert passed <= declared, "the build passes an argument the image ignores"


def test_a_caller_cannot_talk_the_build_into_a_different_revision():
    """The target forwards what it is given, because --no-cache and a second
    --tag are reasonable things to want. Docker takes the last value of a
    repeated --build-arg, so where the forwarding sits decides whether a caller
    can put a commit in the manifest that built nothing."""
    build = re.search(r'docker build (.*?) \.$',
                      (pipeline.REPO / "run").read_text(), re.M).group(1)
    assert build.index('"$@"') < build.index('--build-arg')


def test_the_build_context_excludes_what_the_repo_already_ignores():
    """The archives beside the checkout are gigabytes. Every one of them would
    be packed up and handed to the daemon on each build."""
    ignored = (pipeline.REPO / ".dockerignore").read_text().split()
    for pattern in (pipeline.REPO / ".gitignore").read_text().split():
        assert pattern in ignored, f"{pattern} reaches the build context"
