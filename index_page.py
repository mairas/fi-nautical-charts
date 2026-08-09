"""Render the index page served beside the published charts.

The page is written from the same data as the manifest, at the same moment, so
the list a reader sees and the digests they verify against cannot disagree.

Everything is inline: no fonts, stylesheets or scripts are fetched. These charts
are downloaded onto boats, and the page has to render on a cabin laptop with no
connection to anything but the server it came from.
"""

from __future__ import annotations

import html

from currency import HUMAN

# What each set is for. The choice between them is the one thing a reader
# actually needs help with, and it is not derivable from the metadata.
NOTES = {
    "merikarttasarjat": ("Koko rannikko saaristoineen. Kattavin peruskartta.",
                         "The whole coast including the archipelago. The most complete base."),
    "merikarttasarja": ("Koko rannikko saaristoineen. Kattavin peruskartta.",
                        "The whole coast including the archipelago. The most complete base."),
    "rannikkokartat": ("Rannikkoalue. Sisäsaaristossa on kattavuusaukkoja.",
                       "Coastal waters. Has coverage gaps in the inner archipelago."),
    "satamakartat": ("Satamien yksityiskohtaiset kartat.",
                     "Detailed charts of harbours."),
    "veneilykartat": ("Veneilyreitit ja -alueet.",
                      "Boating routes and areas."),
    "yleiskartat250k": ("Yleiskuva mittakaavassa 1:250 000.",
                        "Overview at 1:250,000."),
    "yleiskartat": ("Pienimittakaavainen yleiskuva.",
                    "Small-scale overview."),
}

LINKS = [
    ("Signal K", "https://signalk.org/",
     "Veneen dataverkko, jolle nämä kartat on paketoitu.",
     "The boat data platform these charts are packaged for."),
    ("Freeboard-SK", "https://github.com/SignalK/freeboard-sk",
     "Signal K:n karttaplotteri, joka näyttää nämä kartat.",
     "The Signal K chart plotter that displays them."),
    ("HaLOS", "https://halos.fi",
     "Merikäyttöön rakennettu Raspberry Pi -käyttöjärjestelmä.",
     "A Raspberry Pi operating system built for marine use."),
    ("HALPI2", "https://shop.hatlabs.fi/products/halpi2-computer",
     "Veneen tietokone, jolla HaLOS ajetaan.",
     "The boat computer HaLOS runs on."),
    ("fi-nautical-charts", "https://github.com/mairas/fi-nautical-charts",
     "Työkalut, joilla nämä paketit rakennetaan.",
     "The tooling that builds these packages."),
]


def human_size(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def labels(layer: str | None) -> tuple[str, str]:
    key = (layer or "")[3:]
    english, finnish = HUMAN.get(key, (None, None))
    if finnish:
        return finnish, english
    return key or "—", ""


def chart_row(entry: dict, index: int) -> str:
    finnish, english = labels(entry.get("layer"))
    note_fi, note_en = NOTES.get((entry.get("layer") or "")[3:], ("", ""))
    e = html.escape
    return f"""      <li class="chart" style="--i:{index}">
        <div class="chart-head">
          <h3><a href="{e(entry['filename'])}">{e(finnish)}</a></h3>
          <p class="en-label">{e(english)}</p>
          <a class="dl" href="{e(entry['filename'])}" download>Lataa · Download <span aria-hidden="true">↓</span></a>
        </div>
        <p class="note" lang="fi">{e(note_fi)}</p>
        <p class="note" lang="en">{e(note_en)}</p>
        <dl class="facts">
          <div><dt>Laitos · Edition</dt><dd>{e(entry.get('source_edition') or '—')}</dd></div>
          <div><dt>Koko · Size</dt><dd>{e(human_size(entry['bytes']))}</dd></div>
        </dl>
        <p class="digest"><span>sha256</span> <code>{e(entry['sha256'])}</code></p>
      </li>"""


def render(charts: list[dict], generated: str) -> str:
    rows = "\n".join(chart_row(c, i) for i, c in enumerate(
        c for c in charts if c.get("readable") is not False))
    links = "\n".join(
        f"""        <li><a href="{html.escape(url)}">{html.escape(name)}</a>
          <span lang="fi">{html.escape(fi)}</span>
          <span lang="en">{html.escape(en)}</span></li>"""
        for name, url, fi, en in LINKS)
    return f"""<!doctype html>
<html lang="fi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Suomalaiset merikartat MBTiles-paketteina — Finnish nautical charts</title>
<meta name="description" content="Traficomin avoimet rasterimerikartat MBTiles-paketteina Signal K:lle ja Freeboard-SK:lle. CC BY 4.0. Ei navigointikäyttöön.">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' fill='%2308161e'/%3E%3Cpath d='M4 21h24M4 25h24' stroke='%23f2679a' stroke-width='1.5' opacity='.55'/%3E%3Cpath d='M16 5l6 12H10z' fill='%23ece4d1'/%3E%3C/svg%3E">
<style>
:root {{
  --sea: #08161e;
  --sea-deep: #050f15;
  --sea-rise: #0d2029;
  --paper: #ece4d1;
  --paper-dim: #9fa39c;
  --rule: rgba(236,228,209,.19);
  --graticule: rgba(236,228,209,.055);
  --mark: #f2679a;
  --mark-deep: #d9346f;
  --display: 'Iowan Old Style','Palatino Linotype',Palatino,'Book Antiqua',Georgia,serif;
  --body: ui-sans-serif,-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;
  --mono: ui-monospace,SFMono-Regular,'SF Mono',Menlo,Consolas,monospace;
}}
* {{ box-sizing: border-box; }}
html {{ -webkit-text-size-adjust: 100%; }}
body {{
  margin: 0;
  background: var(--sea);
  color: var(--paper);
  font: 400 clamp(15px,.4vw + 14px,17px)/1.65 var(--body);
  /* The faint graticule a chart is drawn on, not a decorative gradient. */
  background-image:
    linear-gradient(var(--graticule) 1px, transparent 1px),
    linear-gradient(90deg, var(--graticule) 1px, transparent 1px);
  background-size: 128px 128px, 128px 128px;
  background-position: -1px -1px;
}}
.wrap {{ max-width: 62rem; margin: 0 auto; padding: 0 1.5rem; }}
a {{ color: var(--mark); text-decoration-thickness: 1px; text-underline-offset: .18em; }}
a:hover {{ color: var(--paper); }}
:focus-visible {{ outline: 2px solid var(--mark); outline-offset: 3px; }}

header {{ padding: clamp(3.5rem,9vw,7rem) 0 0; }}
h1 {{
  font: 400 clamp(2.1rem,6vw,4rem)/1.05 var(--display);
  margin: 0 0 .6rem;
  letter-spacing: -.015em;
}}
h1 em {{ display: block; font-style: normal; color: var(--paper-dim);
  font-size: clamp(1rem,2.2vw,1.5rem); letter-spacing: 0; margin-top: .5rem; }}

.bilingual {{ display: grid; gap: 1.5rem 3rem; margin: 2.5rem 0 0; }}
@media (min-width: 46rem) {{ .bilingual {{ grid-template-columns: 1fr 1fr; }} }}
.bilingual p {{ margin: 0; max-width: 34rem; }}
.bilingual [lang="en"] {{ color: var(--paper-dim); }}
.lang {{
  display: block; font: 500 .68rem/1 var(--body); letter-spacing: .16em;
  text-transform: uppercase; color: var(--paper-dim); margin-bottom: .7rem;
}}

.warning {{
  margin: 3rem 0 0; padding: 1.15rem 1.35rem;
  border-left: 3px solid var(--mark-deep);
  background: var(--sea-rise);
}}
.warning p {{ margin: 0; }}
.warning [lang="fi"] {{ font-weight: 600; }}
.warning [lang="en"] {{ color: var(--paper-dim); margin-top: .3rem; }}

h2 {{
  font: 500 .72rem/1 var(--body); letter-spacing: .18em; text-transform: uppercase;
  color: var(--paper-dim); margin: clamp(3.5rem,7vw,5.5rem) 0 0;
  padding-bottom: .9rem; border-bottom: 1px solid var(--rule);
}}

ul.charts {{ list-style: none; margin: 0; padding: 0; }}
.chart {{
  padding: 1.75rem 0 1.6rem 1.1rem;
  border-bottom: 1px solid var(--rule);
  border-left: 2px solid transparent;
  transition: border-color .18s ease, background-color .18s ease;
  animation: rise .5s cubic-bezier(.2,.7,.3,1) backwards;
  animation-delay: calc(var(--i) * 70ms + 120ms);
}}
.chart:hover {{ border-left-color: var(--mark-deep); background: var(--sea-deep); }}
@keyframes rise {{ from {{ opacity: 0; transform: translateY(9px); }} }}

.chart-head {{ display: flex; flex-wrap: wrap; align-items: baseline; gap: .3rem .9rem; }}
.chart h3 {{ margin: 0; font: 400 clamp(1.3rem,3vw,1.8rem)/1.2 var(--display); }}
.chart h3 a {{ color: var(--paper); text-decoration: none; }}
.chart h3 a:hover {{ color: var(--mark); }}
.dl {{
  margin-left: auto; white-space: nowrap; text-decoration: none;
  font: 500 .7rem/1 var(--body); letter-spacing: .11em; text-transform: uppercase;
  color: var(--mark-deep);
}}
.dl span {{ display: inline-block; transition: transform .18s ease; }}
.chart:hover .dl {{ color: var(--mark); }}
.chart:hover .dl span {{ transform: translateY(.2em); }}
.en-label {{ margin: 0; color: var(--paper-dim); font-size: .9rem; }}
.note {{ margin: .7rem 0 0; max-width: 40rem; }}
.note[lang="en"] {{ color: var(--paper-dim); margin-top: .15rem; }}

.facts {{ display: flex; flex-wrap: wrap; gap: .4rem 2.5rem; margin: 1.1rem 0 0; }}
.facts div {{ display: flex; align-items: baseline; gap: .6rem; }}
.facts dt {{
  font: 500 .66rem/1 var(--body); letter-spacing: .13em; text-transform: uppercase;
  color: var(--paper-dim);
}}
.facts dd {{ margin: 0; font-family: var(--mono); font-size: .92rem; }}
.digest {{ margin: .55rem 0 0; font-size: .74rem; color: var(--paper-dim); }}
.digest span {{ letter-spacing: .1em; text-transform: uppercase; }}
.digest code {{ font-family: var(--mono); word-break: break-all; }}

.licence p {{ max-width: 42rem; }}
.licence .attrib {{ font-family: var(--mono); font-size: .9rem; }}

ul.links {{ list-style: none; margin: 1.5rem 0 0; padding: 0; display: grid;
  gap: 1.4rem 3rem; }}
@media (min-width: 46rem) {{ ul.links {{ grid-template-columns: 1fr 1fr; }} }}
ul.links li {{ max-width: 26rem; }}
ul.links a {{ font: 400 1.15rem/1.3 var(--display); }}
ul.links span {{ display: block; font-size: .85rem; color: var(--paper-dim); }}
ul.links [lang="en"] {{ opacity: .72; }}

footer {{ margin: clamp(4rem,8vw,6rem) 0 0; padding: 1.4rem 0 3.5rem;
  border-top: 1px solid var(--rule); color: var(--paper-dim); font-size: .82rem; }}
footer code {{ font-family: var(--mono); }}

@media (prefers-reduced-motion: reduce) {{
  * {{ animation: none !important; transition: none !important; }}
}}
</style>
</head>
<body>
<div class="wrap">

<header>
  <h1>Suomalaiset merikartat<em>Finnish nautical charts, packaged for Signal K</em></h1>
</header>

<main>
  <section class="bilingual">
    <p lang="fi"><span class="lang">Suomeksi</span>Traficomin avoimet
    rasterimerikartat haettuna WMTS-rajapinnasta ja koottuna MBTiles-paketeiksi
    Signal K:ta ja Freeboard-SK:ta varten. Aineisto on Traficomin tuottamaa.
    Paketointi ei muuta karttojen sisältöä; se karsii lehtien ulkopuolisen
    täytön ja rakentaa pienemmät mittakaavatasot uudelleen.</p>
    <p lang="en"><span class="lang">In English</span>Traficom's open raster
    nautical charts, fetched from their WMTS service and packaged as MBTiles for
    Signal K and Freeboard-SK. The data is produced by Traficom. Packaging does
    not alter chart content; it strips the off-sheet fill and rebuilds the
    smaller scales.</p>
  </section>

  <section class="warning">
    <p lang="fi">Ei navigointikäyttöön. Ei täytä asianmukaisen merikartan
    vaatimuksia.</p>
    <p lang="en">Not for navigation. Does not meet the requirements of an
    official nautical chart.</p>
  </section>

  <h2>Kartat · Charts</h2>
  <ul class="charts">
{rows}
  </ul>

  <h2>Lisenssi · Licence</h2>
  <section class="licence">
    <p lang="fi">Aineisto on lisensoitu <a href="https://creativecommons.org/licenses/by/4.0/deed.fi">Creative
    Commons Nimeä 4.0 -lisenssillä</a>. Käyttäessäsi karttoja mainitse lähde.</p>
    <p lang="en">The data is licensed under <a href="https://creativecommons.org/licenses/by/4.0/">Creative
    Commons Attribution 4.0</a>. Credit the source when you use it.</p>
    <p class="attrib">Lähde: Traficom · Source: Traficom</p>
  </section>

  <h2>Liittyvät · Related</h2>
  <ul class="links">
{links}
  </ul>
</main>

<footer>
  <p>Luettelo koneluettavassa muodossa: <a href="charts.json">charts.json</a> —
  sisältää jokaisen tiedoston koon ja sha256-tiivisteen. ·
  Machine-readable index: <a href="charts.json">charts.json</a>, carrying every
  file's size and sha256 digest.</p>
  <p>Päivitetty · Updated <code>{html.escape(generated)}</code></p>
</footer>

</div>
</body>
</html>
"""
