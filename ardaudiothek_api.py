from __future__ import annotations

import json
import time
from http.client import HTTPException
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen


GRAPHQL_URL = "https://api.ardaudiothek.de/graphql"
API_TIMEOUT_SECONDS = 30
HEAD_TIMEOUT_SECONDS = 20
# Pauses before each retry of a GraphQL request that failed temporarily (e.g. 502).
GRAPHQL_RETRY_DELAYS_SECONDS = (2, 10)
# Status codes meaning the audio file is gone for good, so the episode is unplayable.
MISSING_AUDIO_STATUSES = {404, 410}


class ShowNotFoundError(ValueError):
    """Raised when ARD does not return a show for the provided identifier."""


class AudioMissingError(Exception):
    """Raised when an episode's audio file no longer exists (HTTP 404/410)."""


PROGRAMSET_QUERY = """
query ProgramSet($id: ID!, $first: Int) {
  programSet(id: $id) {
    title
    path
    synopsis
    sharingUrl
    feedUrl
    publicationService { title }
    image { url url1X1 }
    items(
      orderBy: PUBLISH_DATE_DESC
      filter: { isPublished: { equalTo: true } }
      first: $first
    ) {
      nodes {
        title
        summary
        synopsis
        sharingUrl
        publicationStartDateAndTime: publishDate
        url
        episodeNumber
        duration
        image { url url1X1 }
        isPublished
        audios { url downloadUrl size mimeType }
      }
    }
  }
}
"""

# programSets.id is an internal number; coreId is the "urn:ard:show:<token>" used everywhere else.
DISCOVER_QUERY = """
query DiscoverShows($after: Cursor) {
  programSets(
    filter: { numberOfElements: { greaterThan: 0 } }
    orderBy: TITLE_ASC
    first: 500
    after: $after
  ) {
    pageInfo { hasNextPage endCursor }
    nodes { coreId title }
  }
}
"""


def get_file_length(url: str) -> int | None:
    """Fetch Content-Length for a remote resource.

    Raises AudioMissingError if the file is gone; returns None if the size is just unknown.
    """
    try:
        req = Request(url, method="HEAD")
        with urlopen(req, timeout=HEAD_TIMEOUT_SECONDS) as response:
            content_length = response.headers.get("Content-Length")
    except HTTPError as exc:
        if exc.code in MISSING_AUDIO_STATUSES:
            raise AudioMissingError(f"HTTP {exc.code} for {url}") from exc
        return None
    except (OSError, HTTPException, ValueError):
        # OSError covers HTTPError/URLError plus timeouts and connection resets
        # raised while reading the response; a missing length is not fatal.
        return None

    try:
        return int(content_length) if content_length is not None else None
    except ValueError:
        return None


def _is_temporary(exc: Exception) -> bool:
    if isinstance(exc, HTTPError):
        return exc.code in (429, 500, 502, 503, 504)
    # Timeouts, connection resets and DNS hiccups.
    return isinstance(exc, (OSError, HTTPException))


def _post_graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = Request(
        GRAPHQL_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    for delay in (*GRAPHQL_RETRY_DELAYS_SECONDS, None):
        try:
            with urlopen(req, timeout=API_TIMEOUT_SECONDS) as response:
                payload = json.loads(response.read().decode("utf-8"))
            break
        except Exception as exc:
            if delay is None or not _is_temporary(exc):
                raise
            time.sleep(delay)

    if payload.get("errors"):
        raise ValueError(f"GraphQL error: {payload['errors']}")

    return payload.get("data") or {}


def discover_shows() -> list[dict[str, Any]]:
    """All shows that have at least one episode, as {"coreId", "title"} dicts."""
    shows: list[dict[str, Any]] = []
    after = None
    while True:
        page = _post_graphql(DISCOVER_QUERY, {"after": after})["programSets"]
        shows.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            return shows
        after = page["pageInfo"]["endCursor"]


def get_show_json_graphql(show_id: str, latest: int | None) -> dict[str, Any]:
    data = _post_graphql(PROGRAMSET_QUERY, {"id": show_id, "first": latest})
    program_set = data.get("programSet")
    if program_set is None:
        raise ShowNotFoundError(f'No show found for "show"={show_id!r}')

    return program_set
