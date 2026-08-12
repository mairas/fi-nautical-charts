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

import sqlite3
import sys
from pathlib import Path

import pytest

import pipeline
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
            attempted.append(cmd[3])
            if "yleiskartat" in cmd[3]:
                raise Failed("strip-nodata exited 1")
            make_mbtiles(Path(cmd[cmd.index("--out") + 1]), PUBLISHED_META)
        if what == "downscale":
            # Satamakartat is not stripped, so downscale reads the archive and
            # the result carries no strip stamp
            stripped = ".stripped." in cmd[3]
            if not stripped:
                attempted.append(cmd[3])
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
