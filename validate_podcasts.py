#!/usr/bin/env python3
"""Validate podcasts.json: JSON syntax plus the settings fetch_feeds.py relies on."""

from __future__ import annotations

import json
import sys

from feed_files import PODCASTS_FILE, ROOT, show_token
from feed_service import parse_and_validate


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _check_show_id(show_id: object, where: str, errors: list[str]) -> str | None:
    """Return the show's token if show_id is valid, else record an error."""
    if not isinstance(show_id, str):
        errors.append(f"{where}: 'show_id' must be a string")
        return None
    try:
        parse_and_validate(show_id, None)
    except ValueError as exc:
        errors.append(f"{where}: {exc}")
        return None
    return show_token(show_id)


def _check_keys(obj: dict[str, object], allowed: set[str], where: str, errors: list[str]) -> None:
    for key in sorted(obj.keys() - allowed):
        errors.append(f"{where}: unknown key {key!r} (allowed: {', '.join(sorted(allowed))})")


def validate(text: str) -> list[str]:
    try:
        config = json.loads(text)
    except json.JSONDecodeError as exc:
        return [f"invalid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})"]

    if not isinstance(config, dict):
        return ["top level must be an object with 'default_latest', 'exclude' and 'shows'"]

    errors: list[str] = []
    _check_keys(config, {"default_latest", "exclude", "shows"}, "top level", errors)
    if not _is_positive_int(config.get("default_latest")):
        errors.append("'default_latest' must be a positive integer")

    excluded: dict[str, str] = {}
    exclude = config.get("exclude", [])
    if not isinstance(exclude, list):
        errors.append("'exclude' must be a list of show IDs")
        exclude = []
    for index, show_id in enumerate(exclude):
        where = f"exclude[{index}]"
        token = _check_show_id(show_id, where, errors)
        if token is not None:
            if token in excluded:
                errors.append(f"{where}: already excluded in {excluded[token]}")
            else:
                excluded[token] = where

    shows = config.get("shows", [])
    if not isinstance(shows, list):
        errors.append("'shows' must be a list of show entries")
        shows = []
    # Show token -> entry that owns it, to catch the same show listed twice.
    seen: dict[str, str] = {}
    for index, entry in enumerate(shows):
        where = f"shows[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{where}: must be an object")
            continue
        _check_keys(entry, {"show_id", "name", "latest"}, where, errors)

        name = entry.get("name")
        if name is not None:
            if not isinstance(name, str) or not name.strip():
                errors.append(f"{where}: 'name' must be a non-empty string if set")
            else:
                where = f"{where} ({name!r})"

        # "latest": null is allowed and means "all episodes".
        if entry.get("latest") is not None and not _is_positive_int(entry["latest"]):
            errors.append(f"{where}: 'latest' must be a positive integer or null")

        token = _check_show_id(entry.get("show_id"), where, errors)
        if token is None:
            continue
        if token in seen:
            errors.append(f"{where}: same show as {seen[token]}")
        else:
            seen[token] = where
        if token in excluded:
            errors.append(f"{where}: also listed in {excluded[token]}")

    return errors


def main() -> int:
    errors = validate(PODCASTS_FILE.read_text(encoding="utf-8"))
    name = PODCASTS_FILE.relative_to(ROOT)
    if errors:
        for error in errors:
            print(f"{name}: {error}", file=sys.stderr)
        return 1
    print(f"{name}: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
