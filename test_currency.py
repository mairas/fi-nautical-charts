"""Tests for the labelling and renaming step.

`currency` runs inside the monthly sequence, once per layer, straight after the
refresh. It is the only step that changes an archive's *filename*, and three
other modules have opinions about that name: `pipeline` looks the archive up
again afterwards, `publish` refuses a name it cannot parse, and the served
manifest is keyed on the prefix. None of those opinions is expressed in one
place, so what is pinned here is that they agree.
"""

import sqlite3
import subprocess
import sys

import pytest

import currency
import pipeline
import publish

ARCHIVE_META = {
    "wmts_layer": "Yleiskartat 250k public",
    "source_updated": "2026-08-14",
    "source_updated_oldest": "2024-11-16",
    "downloaded": "2026-08-14",
}


def make_archive(path, meta):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE metadata (name TEXT PRIMARY KEY, value TEXT)")
    con.execute("CREATE TABLE tiles (zoom_level INT, tile_column INT, "
                "tile_row INT, tile_data BLOB)")
    con.executemany("INSERT INTO metadata VALUES (?,?)", meta.items())
    con.execute("INSERT INTO tiles VALUES (13,1,1,?)", (b"tile",))
    con.commit()
    con.close()
    return path


def run_currency(path, *args):
    """As the pipeline invokes it: a subprocess, under this interpreter."""
    return subprocess.run([sys.executable, str(pipeline.REPO / "currency.py"),
                           str(path), *args],
                          capture_output=True, text=True)


@pytest.mark.parametrize("layer", [l.wmts for l in pipeline.LAYERS])
def test_every_layer_renames_to_a_name_publish_will_accept(tmp_path, layer):
    """The rename happens in the fifth minute of a run and the name is not read
    again until the tenth hour, by a step that refuses what it cannot parse. A
    layer whose slug produced anything else would fail the month at publish,
    having done all the work."""
    src = make_archive(tmp_path / "whatever-name.mbtiles",
                       ARCHIVE_META | {"wmts_layer": layer})
    done = run_currency(src, "--rename")
    assert done.returncode == 0, done.stderr

    renamed = [f for f in tmp_path.iterdir() if f.suffix == ".mbtiles"]
    assert len(renamed) == 1
    assert publish.NAME.fullmatch(renamed[0].name), (
        f"{renamed[0].name} is a name publish would refuse")


@pytest.mark.parametrize("layer", [l.wmts for l in pipeline.LAYERS])
def test_the_renamed_file_is_still_the_archive_the_pipeline_looks_for(
        tmp_path, layer):
    """`pipeline` re-reads the directory after this step, because the file it
    refreshed may no longer be called what it was. It matches on recorded layer,
    not on filename -- so the rename has to leave something `find_archives`
    still claims."""
    make_archive(tmp_path / "whatever-name.mbtiles",
                 ARCHIVE_META | {"wmts_layer": layer})
    run_currency(tmp_path / "whatever-name.mbtiles", "--rename")

    assert layer in pipeline.find_archives(tmp_path)


# What the published sets are called today. Spelled out rather than derived
# from slug(): the served directory holds files under exactly these names, and a
# set published under any other prefix is published *beside* the old edition
# instead of retiring it. A test that recomputes the prefix from the same
# function cannot see that -- both sides move together and agree on the way.
PUBLISHED_PREFIX = {
    "Yleiskartat 250k public": "fi-yleiskartat250k",
    "Rannikkokartat public": "fi-rannikkokartat",
    "Merikarttasarjat public": "fi-merikarttasarjat",
    "Satamakartat": "fi-satamakartat",
}


@pytest.mark.parametrize("layer", [l.wmts for l in pipeline.LAYERS])
def test_the_rename_keeps_the_prefix_the_served_directory_already_uses(
        tmp_path, layer):
    """Retirement is by prefix: a new edition replaces the old one only if the
    two share it. Rename a layer's slug and the month publishes a second copy,
    the manifest lists both, and nothing fails."""
    src = make_archive(tmp_path / "x.mbtiles",
                       ARCHIVE_META | {"wmts_layer": layer})
    run_currency(src, "--rename")

    renamed = next(f for f in tmp_path.iterdir() if f.suffix == ".mbtiles")
    assert renamed.name == f"{PUBLISHED_PREFIX[layer]}-2026-08-14.mbtiles"


def test_publish_retires_on_the_prefix_the_rename_produced(tmp_path):
    """The two are written in different modules and only meet in a directory
    listing ten hours apart."""
    src = make_archive(tmp_path / "x.mbtiles", ARCHIVE_META)
    run_currency(src, "--rename")

    renamed = next(f for f in tmp_path.iterdir() if f.suffix == ".mbtiles")
    assert renamed.name.startswith(publish.layer_prefix(ARCHIVE_META) + "-")


def test_a_set_with_no_recorded_currency_is_refused_by_name(tmp_path):
    """Only the run that fetched the tiles could record this, so there is
    nothing to fall back to. The message has to say which field is missing --
    the operator's next move differs for each."""
    src = make_archive(tmp_path / "x.mbtiles",
                       {"wmts_layer": "Satamakartat", "downloaded": "2026-08-14"})
    done = run_currency(src)

    assert done.returncode != 0
    assert "source_updated" in done.stderr and "source_updated_oldest" in done.stderr


def test_a_wms_set_is_told_it_never_will_have_currency(tmp_path):
    """A different refusal from the one above, because the remedy is different:
    the WMS serves no Last-Modified at all, so re-downloading will not help and
    the operator has to name that layer by hand."""
    src = make_archive(tmp_path / "x.mbtiles",
                       {"wmts_layer": "cells", "source": "wms"})
    done = run_currency(src)

    assert done.returncode != 0
    assert "never will" in done.stderr


def test_the_label_leads_with_the_name_a_finnish_sailor_recognises(tmp_path):
    """Freeboard truncates a chart label around 28 characters. Leading with the
    English descriptor would spend those characters on the part that is the same
    for every set."""
    src = make_archive(tmp_path / "x.mbtiles", ARCHIVE_META)
    run_currency(src)

    con = sqlite3.connect(next(tmp_path.iterdir()))
    name = dict(con.execute("SELECT name, value FROM metadata"))["name"]
    assert name.startswith("Yleiskartat 250k")
    assert len(name) <= 28


def test_labelling_twice_changes_nothing(tmp_path):
    """The pipeline relabels on every run, including runs that refreshed
    nothing, so this has to be a function of the recorded facts rather than of
    how many times it has been applied.

    The first snapshot is checked for having grown before the two are compared:
    a labelling step that wrote nothing at all would leave both reads showing
    the fixture, and two equal nothings satisfy an equality test.
    """
    src = make_archive(tmp_path / "x.mbtiles", ARCHIVE_META)
    first = run_currency(src)
    con = sqlite3.connect(src)
    once = dict(con.execute("SELECT name, value FROM metadata"))
    con.close()
    assert set(once) > set(ARCHIVE_META), (
        f"labelling added nothing to the metadata: {first.stderr}")

    run_currency(src)
    con = sqlite3.connect(src)
    assert dict(con.execute("SELECT name, value FROM metadata")) == once


@pytest.mark.parametrize("layer", [
    "Rannikkokartat public", "Rannikkokarttojen erikoiskartat",
    "Veneilykartat public", "Veneilykarttojen erikoiskartat",
    "Satamakartat", "Satamakarttojen erikoiskartat",
    "Merikarttasarjat public", "Yleiskartat 100k", "Yleiskartat 250k public",
])
def test_every_layer_in_the_table_slugs_into_a_publishable_filename(layer):
    """Not only the four the pipeline runs: `./run dl` takes any of these by
    hand, and a set downloaded that way is published by the same code. A slug
    keeping a space or a case would build a filename `publish` refuses, which
    is not discovered until something tries to publish it."""
    name = f"fi-{currency.slug(layer)}-2026-08-14.mbtiles"
    assert publish.NAME.fullmatch(name), f"{name} is a name publish would refuse"
