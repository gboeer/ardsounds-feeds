#!/usr/bin/env python3
"""Generate the GitHub Pages landing page and OPML files for the published podcast feeds."""

from __future__ import annotations

import html
import json
import shutil
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from email.utils import format_datetime
from xml.etree import ElementTree as ET

from feed_files import FEEDS_DIR, MANIFEST_FILE, ROOT, feed_base_url


PUBLIC_DIR = ROOT / "public"
INDEX_FILE = PUBLIC_DIR / "index.html"
OPML_DIR = PUBLIC_DIR / "opml"
TEMPLATE_FILE = ROOT / "templates" / "index.html"
TEMPLATE_CSS_FILE = ROOT / "templates" / "index.css"
OUTPUT_CSS_FILE = PUBLIC_DIR / "index.css"

# Section for shows ARD lists without a station.
OTHER_STATION = "Sonstige"


def _station_slug(station: str) -> str:
    """ASCII filename stem for a station, e.g. "Bayern 2" -> "bayern-2"."""
    ascii_name = unicodedata.normalize("NFKD", station).encode("ascii", "ignore").decode()
    words = "".join(c if c.isalnum() else " " for c in ascii_name.lower()).split()
    return "-".join(words) or "station"


def _group_by_station(shows: list[dict[str, object]]) -> dict[str, list[dict[str, object]]]:
    groups: dict[str, list[dict[str, object]]] = defaultdict(list)
    for show in shows:
        groups[str(show.get("station") or OTHER_STATION)].append(show)
    return dict(sorted(groups.items(), key=lambda item: item[0].casefold()))


def _opml_filenames(stations: list[str]) -> dict[str, str]:
    """Unique OPML filename per station; "all" and "featured" are reserved."""
    taken = {"all.opml", "featured.opml"}
    filenames: dict[str, str] = {}
    for station in stations:
        stem = _station_slug(station)
        filename, n = f"{stem}.opml", 2
        while filename in taken:
            filename, n = f"{stem}-{n}.opml", n + 1
        taken.add(filename)
        filenames[station] = filename
    return filenames


def _build_opml(title: str, shows: list[dict[str, object]], base_url: str) -> str:
    opml = ET.Element("opml", {"version": "2.0"})
    head = ET.SubElement(opml, "head")
    ET.SubElement(head, "title").text = title
    ET.SubElement(head, "dateCreated").text = format_datetime(datetime.now(timezone.utc))
    body = ET.SubElement(opml, "body")
    for show in shows:
        attrs = {
            "type": "rss",
            "text": str(show["name"]),
            "title": str(show["name"]),
            "xmlUrl": f"{base_url}/{show['feed']}",
        }
        if show.get("source_url"):
            attrs["htmlUrl"] = str(show["source_url"])
        ET.SubElement(body, "outline", attrs)
    ET.indent(opml)
    return ET.tostring(opml, encoding="unicode", xml_declaration=True) + "\n"


def _show_item(show: dict[str, object], base_url: str) -> str:
    name = str(show["name"])
    station = str(show.get("station") or "")
    feed_url = f"{base_url}/{show['feed']}"
    search_text = f"{name} {station}".lower()

    links = []
    if show.get("source_url"):
        links.append(
            f'<a href="{html.escape(str(show["source_url"]))}" target="_blank" rel="noopener">ARDSounds</a>'
        )
    if show.get("official_feed"):
        links.append(
            f'<a href="{html.escape(str(show["official_feed"]))}" target="_blank" rel="noopener"'
            ' title="RSS-Feed des Senders">Offizieller Feed</a>'
        )
    return (
        f'<li data-search="{html.escape(search_text)}">'
        f'<div class="show-head"><strong>{html.escape(name)}</strong>'
        f'<span class="show-links">{" · ".join(links)}</span></div>'
        '<div class="feed-link-row">'
        f'<a href="{html.escape(feed_url)}">{html.escape(feed_url)}</a>'
        f'<button class="copy-btn" type="button" data-copy="{html.escape(feed_url)}"'
        ' aria-label="Feed-URL kopieren" title="Feed-URL kopieren">'
        '<svg aria-hidden="true" focusable="false"><use href="#icon-copy"></use></svg>'
        "</button>"
        "</div>"
        "</li>"
    )


def _section(
    title: str, shows: list[dict[str, object]], opml: str, base_url: str, *, open_: bool
) -> str:
    items = "\n".join(_show_item(show, base_url) for show in shows)
    return (
        f'<details class="station"{" open" if open_ else ""}>'
        f'<summary><span class="station-name">{html.escape(title)}</span>'
        f'<span class="station-count">{len(shows)}</span></summary>'
        f'<p class="station-opml"><a href="./opml/{html.escape(opml)}" download>OPML dieser Gruppe</a></p>'
        f"<ul>\n{items}\n</ul>"
        "</details>"
    )


def main() -> int:
    # Written by fetch_feeds.py: only shows whose feed was actually generated.
    shows = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
    # Never link to a missing feed, even if the manifest is out of date.
    for show in [s for s in shows if not (FEEDS_DIR / str(s["feed"])).is_file()]:
        print(f"Skipping {show['name']}: no feed file {show['feed']}")
        shows.remove(show)
    base_url = feed_base_url()

    featured = [show for show in shows if show.get("listed")]
    stations = _group_by_station(shows)
    opml_files = _opml_filenames(list(stations))

    sections = []
    if featured:
        sections.append(_section("Ausgewählt", featured, "featured.opml", base_url, open_=True))
    sections += [
        _section(station, station_shows, opml_files[station], base_url, open_=False)
        for station, station_shows in stations.items()
    ]

    template = TEMPLATE_FILE.read_text(encoding="utf-8")
    page = (
        template.replace("__FEED_SECTIONS__", "\n".join(sections))
        .replace("__SHOW_COUNT__", str(len(shows)))
        .replace("__STATION_COUNT__", str(len(stations)))
        .replace("__GENERATED_AT__", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    )

    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_FILE.write_text(page, encoding="utf-8")
    shutil.copyfile(TEMPLATE_CSS_FILE, OUTPUT_CSS_FILE)

    # Rebuilt from scratch so stations that disappeared don't leave stale files behind.
    shutil.rmtree(OPML_DIR, ignore_errors=True)
    OPML_DIR.mkdir()
    opml_outputs = {"all.opml": ("ARDSounds – alle Podcasts", shows)}
    if featured:
        opml_outputs["featured.opml"] = ("ARDSounds – ausgewählte Podcasts", featured)
    for station, filename in opml_files.items():
        opml_outputs[filename] = (f"ARDSounds – {station}", stations[station])
    for filename, (title, opml_shows) in opml_outputs.items():
        (OPML_DIR / filename).write_text(_build_opml(title, opml_shows, base_url), encoding="utf-8")

    print(f"Saved -> {INDEX_FILE.relative_to(ROOT)} ({len(shows)} shows, {len(stations)} stations)")
    print(f"Saved -> {OUTPUT_CSS_FILE.relative_to(ROOT)}")
    print(f"Saved -> {OPML_DIR.relative_to(ROOT)}/ ({len(opml_outputs)} OPML files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
