#!/usr/bin/env python3
"""Fetch RSS feeds for all ARD shows (minus exclusions in podcasts.json) and store them under feeds/.

Usage:
    python fetch_feeds.py                # every show ARD lists with at least one episode
    python fetch_feeds.py --listed-only  # only the shows listed in podcasts.json
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from xml.etree import ElementTree as ET

from ardaudiothek_api import (
    ShowNotFoundError,
    discover_shows,
    get_file_length,
    get_show_json_graphql,
)
from feed_files import (
    FEEDS_DIR,
    MANIFEST_FILE,
    PODCASTS_FILE,
    ROOT,
    feed_base_url,
    feed_filename,
    show_token,
)
from feed_service import parse_and_validate
from rss_xml import NoPlayableEpisodesError, build_rss_xml, read_enclosure_lengths

# Shows fetched at once; each also runs its own pool of enclosure-size lookups.
SHOW_WORKERS = 4


class NoEpisodesError(ValueError):
    """Raised when ARD lists a show but returns no published episodes for it."""


# Conditions that won't fix themselves by tomorrow: don't publish the show at all,
# rather than keeping its previous feed as for temporary failures.
UNAVAILABLE_ERRORS = (ShowNotFoundError, NoEpisodesError, NoPlayableEpisodesError)


def _escape_annotation(value: str, *, is_property: bool = False) -> str:
    """Escape text for a GitHub Actions workflow command (e.g. ::warning::)."""
    value = value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    if is_property:
        value = value.replace(":", "%3A").replace(",", "%2C")
    return value


def _select_shows(config: dict[str, Any], listed_only: bool) -> list[dict[str, Any]]:
    """Shows to publish as {"token", "name", "latest", "listed"}; podcasts.json entries override defaults."""
    default_latest = config["default_latest"]
    excluded = {show_token(show_id) for show_id in config.get("exclude", [])}
    overrides = {show_token(entry["show_id"]): entry for entry in config.get("shows", [])}

    if listed_only:
        candidates = {token: None for token in overrides}
    else:
        candidates = {show_token(show["coreId"]): show["title"] for show in discover_shows()}
        # Listed shows are always included, even if discovery missed them.
        candidates.update({token: None for token in overrides if token not in candidates})

    selected = []
    for token, title in candidates.items():
        if token in excluded:
            continue
        override = overrides.get(token, {})
        selected.append(
            {
                "token": token,
                "name": override.get("name") or title,
                # An explicit "latest": null in podcasts.json means "all episodes".
                "latest": override["latest"] if "latest" in override else default_latest,
                "listed": token in overrides,
            }
        )
    return selected


def _remove_unpublished_feeds(filenames: set[str]) -> None:
    """Delete feeds (e.g. restored from the CI cache) for shows that are no longer published."""
    for path in FEEDS_DIR.glob("*.xml"):
        if path.name not in filenames:
            path.unlink()
            print(f"Removed unpublished feed -> {path.relative_to(ROOT)}")


def _load_known_lengths() -> dict[str, int]:
    """Enclosure sizes from the feeds of the previous run; audio URLs don't change."""
    known: dict[str, int] = {}
    for path in FEEDS_DIR.glob("*.xml"):
        try:
            known.update(read_enclosure_lengths(path.read_text(encoding="utf-8")))
        except (OSError, ET.ParseError) as exc:
            print(f"Ignoring unreadable feed {path.name}: {exc}", file=sys.stderr)
    return known


def _load_previous_manifest() -> dict[str, dict[str, Any]]:
    """Manifest entries of the previous run by feed filename, used for shows kept as fallback."""
    try:
        entries = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {entry["feed"]: entry for entry in entries}


def _official_feed(show: dict[str, Any]) -> str | None:
    # feedUrl is often an internal ID such as "crid://swr.de/…"; only keep real URLs.
    url = show.get("feedUrl") or ""
    return url if url.startswith(("http://", "https://")) else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--listed-only", action="store_true", help="Only fetch the shows listed in podcasts.json"
    )
    args = parser.parse_args()

    config = json.loads(PODCASTS_FILE.read_text(encoding="utf-8"))
    base_url = feed_base_url()
    shows = _select_shows(config, args.listed_only)
    print(f"Publishing {len(shows)} show(s)")

    FEEDS_DIR.mkdir(exist_ok=True)
    _remove_unpublished_feeds({feed_filename(show["token"]) for show in shows})
    previous_manifest = _load_previous_manifest()
    known_lengths = _load_known_lengths()
    print(f"Reusing {len(known_lengths)} enclosure size(s) from previous feeds")
    new_lookups = itertools.count()

    def file_length(url: str) -> int | None:
        if url in known_lengths:
            return known_lengths[url]
        next(new_lookups)  # itertools.count is safe to advance from worker threads
        return get_file_length(url)

    def fetch(show: dict[str, Any]) -> dict[str, Any]:
        """Build and save one feed; returns its manifest entry, or raises."""
        latest = show["latest"]
        request = parse_and_validate(show["token"], str(latest) if latest is not None else None)
        data = get_show_json_graphql(request.show_id, request.latest)
        if not (data.get("items") or {}).get("nodes"):
            raise NoEpisodesError("ARD returned no published episodes")
        filename = feed_filename(show["token"])
        xml = build_rss_xml(data, f"{base_url}/{filename}", get_file_length=file_length)
        (FEEDS_DIR / filename).write_text(xml, encoding="utf-8")
        return {
            "show_id": show["token"],
            "name": show["name"] or data.get("title", ""),
            "feed": filename,
            "source_url": data.get("sharingUrl", ""),
            "official_feed": _official_feed(data),
            "station": (data.get("publicationService") or {}).get("title") or "",
            "listed": show["listed"],
        }

    manifest: list[dict[str, Any]] = []
    errors: list[str] = []
    unavailable = 0
    with ThreadPoolExecutor(max_workers=SHOW_WORKERS) as pool:
        futures = {pool.submit(fetch, show): show for show in shows}
        for future in as_completed(futures):
            show = futures[future]
            label = f"{show['name'] or show['token']} ({show['token']})"
            filename = feed_filename(show["token"])
            try:
                entry = future.result()
            except UNAVAILABLE_ERRORS as exc:
                unavailable += 1
                print(f"  Not publishing {label}: {exc}")
                (FEEDS_DIR / filename).unlink(missing_ok=True)
                continue
            except Exception as exc:
                msg = f"  ERROR fetching {label}: {exc}"
                print(msg, file=sys.stderr)
                if os.getenv("GITHUB_ACTIONS") == "true":
                    # Surface the failure as an annotation on the workflow run page.
                    title = _escape_annotation(f"Feed failed: {label}", is_property=True)
                    print(f"::warning title={title}::{_escape_annotation(str(exc))}")
                errors.append(msg)
                if (FEEDS_DIR / filename).is_file():
                    print(f"  Keeping feed from previous run -> {filename}")
                    previous = previous_manifest.get(filename) or {
                        "show_id": show["token"],
                        "name": show["name"] or show["token"],
                        "feed": filename,
                        "source_url": "",
                        "official_feed": None,
                        "station": "",
                    }
                    manifest.append({**previous, "listed": show["listed"]})
                continue
            manifest.append(entry)
            print(f"  Saved -> {FEEDS_DIR.name}/{entry['feed']}  {entry['name']}")

    manifest.sort(key=lambda entry: entry["name"].casefold())
    MANIFEST_FILE.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"Saved -> {MANIFEST_FILE.relative_to(ROOT)} ({len(manifest)} show(s))")
    print(f"Looked up {next(new_lookups)} new enclosure size(s)")
    print(f"Not published: {unavailable} show(s) without playable episodes")

    if errors:
        print(f"\n{len(errors)} of {len(shows)} feed(s) failed.", file=sys.stderr)
        # Only fail the run if nothing was generated, so one broken show
        # doesn't block publishing the others.
        if len(errors) == len(shows) - unavailable:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
