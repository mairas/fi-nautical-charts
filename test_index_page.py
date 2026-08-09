"""Tests for the index page written beside the published charts."""

import html
import re

import index_page

ENTRY = {
    "filename": "fi-veneilykartat-2026-06-21.mbtiles",
    "layer": "fi-veneilykartat",
    "bytes": 249417728,
    "sha256": "d0fe1c15a850d68df2fb163344e5400e6fc4b4b27b2948dd191c97ebcb3f08f5",
    "source_edition": "2026-06-21",
    "source_edition_oldest": "2025-01-20",
    "processing": "opaque-black-r4+offeez-tilelevel",
    "name": "Veneilykartat 2026-06-21",
}


def page(charts=None, generated="2026-08-09T09:53:27+00:00"):
    return index_page.render([ENTRY] if charts is None else charts, generated)


def test_every_chart_is_listed_with_a_working_download_link():
    out = page()
    assert f'href="{ENTRY["filename"]}"' in out
    assert "Veneilykartat" in out


def test_the_digest_is_shown_in_full_so_a_download_can_be_checked():
    assert ENTRY["sha256"] in page()


def test_the_edition_and_size_are_shown_in_units_a_reader_can_use():
    out = page()
    assert "2026-06-21" in out
    assert "237.9 MiB" in out, "249417728 bytes"


def test_the_not_for_navigation_warning_appears_in_both_languages():
    out = page()
    assert "Ei navigointikäyttöön" in out
    assert "Not for navigation" in out


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


def test_the_page_fetches_nothing_from_anywhere(tmp_path):
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


def test_an_unreadable_file_is_not_offered_for_download():
    """It is in the manifest for whoever operates the server; it is not
    something to hand a sailor."""
    junk = {"filename": "fi-enc-2026-01-01.mbtiles", "layer": None,
            "bytes": 14, "sha256": "ab" * 32, "readable": False}
    out = page([ENTRY, junk])
    assert "fi-enc-2026-01-01" not in out
    assert ENTRY["filename"] in out


def test_metadata_is_escaped_rather_than_injected():
    hostile = dict(ENTRY, filename='x"><script>alert(1)</script>.mbtiles')
    out = page([hostile])
    assert "<script>alert(1)</script>" not in out
    assert html.escape('x"><script>alert(1)</script>.mbtiles', quote=True) in out


def test_a_layer_with_no_label_still_renders():
    unknown = dict(ENTRY, layer="fi-tuntematon", name=None)
    out = page([unknown])
    assert "tuntematon" in out


def test_the_generated_instant_is_shown_so_staleness_is_visible():
    assert "2026-08-09T09:53:27+00:00" in page()


def test_human_size_rounds_the_way_a_download_page_should():
    assert index_page.human_size(249417728) == "237.9 MiB"
    assert index_page.human_size(4426489856) == "4.1 GiB"
    assert index_page.human_size(512) == "512 B"


def test_the_yleiskartat_products_get_a_label_of_their_own():
    """Their scale is part of the product name, so it survives into the slug and
    each needs its own entry; without one the page showed a bare slug."""
    finnish, english = index_page.labels("fi-yleiskartat250k")
    assert finnish == "Yleiskartat 250k"
    assert english == "General charts 1:250 000"


def test_the_source_chart_scale_is_shown_where_it_is_known():
    out = page([dict(ENTRY, layer="fi-yleiskartat250k")])
    assert "1:250 000" in out
    assert "Mittakaava" in out


def test_no_scale_is_invented_for_a_layer_that_does_not_state_one():
    """A wrong scale on a chart page is worse than a missing one."""
    assert "Mittakaava" not in page([dict(ENTRY, layer="fi-rannikkokartat")])


def test_a_sample_image_is_shown_when_one_was_rendered():
    out = index_page.render(
        [ENTRY], "2026-08-09T09:53:27+00:00",
        {"fi-veneilykartat": ("previews/fi-veneilykartat.png", "Hirvensalmi", 13)})
    assert 'src="previews/fi-veneilykartat.png"' in out
    assert "Hirvensalmi, z13" in out
    assert 'loading="lazy"' in out
    assert 'width="760" height="420"' in out, "reserve the box so the page does not jump"
    assert "Veneilykartat at Hirvensalmi" in out


def test_a_chart_with_no_sample_renders_without_a_figure():
    assert "<figure" not in page()


def test_every_layer_that_gets_a_sample_is_one_we_publish():
    import preview
    known = {"fi-merikarttasarjat", "fi-rannikkokartat", "fi-satamakartat",
             "fi-veneilykartat", "fi-yleiskartat250k"}
    assert set(preview.SPOTS) == known, set(preview.SPOTS) ^ known


def test_the_inland_set_is_sampled_somewhere_inland():
    """Hanko would show veneilykartat empty, which would read as broken."""
    import preview
    place, lat, lon, z = preview.SPOTS["fi-veneilykartat"]
    assert place == "Hirvensalmi"
    assert lat > 61 and lon > 26
