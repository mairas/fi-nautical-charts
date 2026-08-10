"""Tests for the index page written beside the published charts."""

import html
import re

import pytest

import index_page
import preview

ENTRY = {
    "filename": "fi-satamakartat-2026-06-29.mbtiles",
    "layer": "fi-satamakartat",
    "bytes": 249417728,
    "sha256": "d0fe1c15a850d68df2fb163344e5400e6fc4b4b27b2948dd191c97ebcb3f08f5",
    "source_edition": "2026-06-29",
    "source_edition_oldest": "2024-11-16",
    "processing": "opaque-black-disk64-b2+offeez-pixel",
    "name": "Satamakartat 2026-06-29",
}


def page(charts=None, generated="2026-08-09T09:53:27+00:00", previews=None):
    return index_page.render([ENTRY] if charts is None else charts, generated, previews)


def entry(layer, **over):
    return dict(ENTRY, layer=layer, filename=f"{layer}-2026-06-29.mbtiles", **over)


def test_every_chart_is_listed_with_a_working_download_link():
    out = page()
    assert f'href="{ENTRY["filename"]}"' in out
    assert "Satamakartat" in out


def test_the_digest_is_shown_in_full_so_a_download_can_be_checked():
    assert ENTRY["sha256"] in page()


def test_the_edition_and_size_are_shown_in_units_a_reader_can_use():
    out = page()
    assert "2026-06-29" in out
    assert "237.9 MiB" in out, "249417728 bytes"


def test_the_not_for_navigation_warning_appears_in_both_languages():
    out = page()
    assert "Ei navigointikäyttöön" in out
    assert "Not for navigation" in out


def test_the_uneven_rasterisation_of_older_material_is_disclosed():
    out = page()
    assert "Vanhemman materiaalin rasterointi on osin heikkolaatuista." in out
    assert "older material" in out


def test_the_licence_and_the_attribution_traficom_asks_for_are_present():
    out = page()
    assert "creativecommons.org/licenses/by/4.0" in out
    assert "Lähde: Traficom" in out
    assert "Source: Traficom" in out
    assert "© Traficom" not in out, "the licence asks for 'Source:', not a copyright mark"


def test_the_page_links_to_each_related_project():
    out = page()
    for url in ["https://signalk.org/",
                "https://github.com/SignalK/freeboard-sk",
                "https://halos.fi",
                "https://shop.hatlabs.fi/products/halpi2-computer",
                "https://github.com/mairas/fi-nautical-charts"]:
        assert url in out, url


def test_the_page_fetches_nothing_from_anywhere():
    """These charts get downloaded onto boats. The page has to render with no
    connection to anything but the server it came from."""
    out = page()
    external = re.findall(r'(?:src|href)\s*=\s*"(https?://[^"]+)"', out)
    assert all(u.startswith(("https://signalk.org", "https://github.com",
                             "https://halos.fi", "https://shop.hatlabs.fi",
                             "https://creativecommons.org"))
               for u in external), external
    assert "<script" not in out
    assert "@import" not in out
    assert "fonts.googleapis" not in out


def test_both_languages_are_marked_up_for_screen_readers_and_hyphenation():
    out = page()
    assert 'lang="fi"' in out and 'lang="en"' in out
    assert '<html lang="fi">' in out


def test_metadata_is_escaped_rather_than_injected():
    hostile = dict(ENTRY, filename='x"><script>alert(1)</script>.mbtiles')
    out = page([hostile])
    assert "<script>alert(1)</script>" not in out
    assert html.escape('x"><script>alert(1)</script>.mbtiles', quote=True) in out


def test_the_generated_instant_is_shown_so_staleness_is_visible():
    assert "2026-08-09T09:53:27+00:00" in page()


def test_human_size_rounds_the_way_a_download_page_should():
    assert index_page.human_size(249417728) == "237.9 MiB"
    assert index_page.human_size(4426489856) == "4.1 GiB"
    assert index_page.human_size(512) == "512 B"


# -- what is listed, and in what order ---------------------------------------

def test_the_charts_are_listed_coarsest_first():
    """The order a reader picks in: start at the overview and work down to the
    harbour. Filename order would open with Merikarttasarjat."""
    out = page([entry(l) for l in ["fi-satamakartat", "fi-merikarttasarjat",
                                   "fi-rannikkokartat", "fi-yleiskartat250k"]])
    seen, order = set(), []
    for name in re.findall(r"Yleiskartat|Rannikkokartat|Merikarttasarjat|Satamakartat", out):
        if name not in seen:
            seen.add(name)
            order.append(name)
    assert order == ["Yleiskartat", "Rannikkokartat", "Merikarttasarjat", "Satamakartat"]


def test_veneilykartat_is_not_listed():
    """Dropped: its coverage is minimal and the name misleads."""
    out = page([entry("fi-veneilykartat"), entry("fi-satamakartat")])
    assert "Veneilykartat" not in out
    assert "Satamakartat" in out


def test_a_file_the_page_does_not_name_is_left_off_it():
    """The manifest is the inventory; the page is a shortlist for a reader."""
    out = page([entry("fi-satamakartat"), entry("fi-tuntematon")])
    assert "tuntematon" not in out
    assert "Satamakartat" in out


def test_an_unreadable_file_is_not_offered_for_download():
    junk = {"filename": "fi-enc-2026-01-01.mbtiles", "layer": None,
            "bytes": 14, "sha256": "ab" * 32, "readable": False}
    out = page([ENTRY, junk])
    assert "fi-enc-2026-01-01" not in out
    assert ENTRY["filename"] in out


# -- the copy a reader actually reads ----------------------------------------

def test_the_yleiskartat_product_is_named_rather_than_slugged():
    """Its scale is part of the product name, so it survives into the slug and
    the old lookup missed, leaving a bare `yleiskartat250k` on the page."""
    finnish, english = index_page.labels("fi-yleiskartat250k")
    assert finnish == "Yleiskartat"
    assert english == "General charts"


@pytest.mark.parametrize("layer,scale", [
    ("fi-yleiskartat250k", "1:250 000"),
    ("fi-rannikkokartat", "1:50 000"),
    ("fi-merikarttasarjat", "1:50 000"),
    ("fi-satamakartat", "1:20 000"),
])
def test_each_set_states_the_scale_its_source_chart_is_drawn_at(layer, scale):
    assert scale in page([entry(layer)])


def test_the_series_keeps_the_name_traficom_gives_it():
    """Merikarttasarjat, not Merikartat: it is a series of chart sheets, and the
    shorter name claims something the product is not."""
    finnish, english = index_page.labels("fi-merikarttasarjat")
    assert finnish == "Merikarttasarjat"
    assert english == "Nautical chart series"


# -- samples -----------------------------------------------------------------

def test_a_sample_image_is_shown_when_one_was_rendered():
    out = page(previews={"fi-satamakartat":
                         ("previews/fi-satamakartat.png", "Hanko Itäsatama", 15)})
    assert 'src="previews/fi-satamakartat.png"' in out
    assert "Hanko Itäsatama, z15" in out
    assert 'loading="lazy"' in out
    assert 'width="760" height="420"' in out, "reserve the box so the page does not jump"
    assert "Satamakartat at Hanko" in out


def test_a_chart_with_no_sample_renders_without_a_figure():
    assert "<figure" not in page()


def test_every_listed_chart_has_a_sample_and_every_sample_a_listing():
    assert set(preview.SPOTS) == set(index_page.ORDER), \
        set(preview.SPOTS) ^ set(index_page.ORDER)


def test_the_samples_all_show_the_same_place_so_the_sets_can_be_compared():
    places = {spot[0] for spot in preview.SPOTS.values()}
    assert places == {"Hanko Itäsatama"}


def test_each_set_is_sampled_at_the_zoom_it_is_meant_to_be_read_at():
    zooms = {layer: spot[3] for layer, spot in preview.SPOTS.items()}
    assert zooms["fi-yleiskartat250k"] < zooms["fi-rannikkokartat"]
    assert zooms["fi-rannikkokartat"] == zooms["fi-merikarttasarjat"]
    assert zooms["fi-satamakartat"] > zooms["fi-merikarttasarjat"]
