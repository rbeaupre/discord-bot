"""
utils/spotify_client.py
───────────────────────
Wrapper around the Spotipy library for fetching new music releases.

Uses Spotify's Client Credentials flow — no user login or OAuth callback
required — which is appropriate for reading public catalog data like albums.

Strategy
--------
Several Spotify approaches are broken for new app registrations:
  - genre: search filter — returns no results.
  - /v1/browse/new-releases endpoint — returns 403 Forbidden.
  - tag:new search filter — returns 400 Invalid limit.

Current working approach:

  1. sp.search(q='year:YYYY', type='track', limit=20)
       type='album' searches return 400 for newer app tiers. Searching for
       tracks by year works fine; each track object contains a nested album
       object with all the fields we need. Results are deduplicated by album ID.

  2. sp.artists([id, id, ...])
       Batch-fetches genre tags for up to 50 artists in a single API call.
       We use this to match each new release to one of our target genres.

For each target genre we pick the first album whose primary artist has a
matching Spotify genre tag. If no genre match is found we fall back to the
next unused album so the post always has content.

Public functions
----------------
get_new_releases(genres)  → list[dict]
    Fetch one recent release per genre from Spotify's new releases feed.
"""

import logging
from datetime import datetime

import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

import config

logger = logging.getLogger(__name__)

# Authenticate using client credentials. Spotipy handles token caching and
# refresh automatically, so we only need to create this object once.
_sp = spotipy.Spotify(
    auth_manager=SpotifyClientCredentials(
        client_id=config.SPOTIFY_CLIENT_ID,
        client_secret=config.SPOTIFY_CLIENT_SECRET,
    )
)


def _album_to_release_dict(album: dict, genre: str) -> dict:
    """
    Convert a raw Spotify album object into our standard release dict.

    Parameters
    ----------
    album : dict   Raw album object from the Spotify API.
    genre : str    The genre label we're associating this release with.

    Returns
    -------
    dict with keys: artist, title, release_date, spotify_url, image_url, genre
    """
    # Use the primary (first) artist name; compilations may have several.
    artist_name = album["artists"][0]["name"] if album["artists"] else "Unknown Artist"

    # Spotify returns cover art in descending size order — index 0 is largest (640x640).
    image_url = album["images"][0]["url"] if album.get("images") else None

    return {
        "artist": artist_name,
        "title": album["name"],
        "release_date": album.get("release_date", "Unknown"),
        "spotify_url": album["external_urls"].get("spotify", ""),
        "image_url": image_url,
        "genre": genre,
    }


def get_new_releases(genres: list[str]) -> list[dict]:
    """
    Fetch one new release per genre from Spotify's official new-releases feed.

    Approach
    --------
    1. Search Spotify with tag:new to get up to 50 recently released albums.
    2. Batch-fetch genre tags for all their primary artists in one API call.
    3. For each target genre, find the first album whose artist has a matching
       genre tag. If no match is found, fall back to the next unused album so
       the post always has content.

    Parameters
    ----------
    genres : list[str]
        Target genre labels, e.g. ["rock", "indie", "electronic"].

    Returns
    -------
    list[dict]
        One release dict per genre (see _album_to_release_dict for keys).
        May be shorter than the input list only if Spotify returns no albums
        at all (very unlikely).
    """
    # ── Step 1: fetch recent tracks and extract their album info ─────────────
    # Spotify restricts type="album" searches (400 Invalid limit) for newer
    # app tiers. type="track" works fine and each track object contains a
    # nested album object with all the fields we need. We deduplicate by
    # album ID so we don't show the same release twice.
    current_year = datetime.now().year
    try:
        result = _sp.search(q=f"year:{current_year}", type="track", limit=20)
        tracks = result["tracks"]["items"]
    except spotipy.SpotifyException as exc:
        logger.error("Failed to fetch new releases from Spotify: %s", exc)
        return []

    if not tracks:
        logger.warning("Spotify year:%d track search returned no results", current_year)
        return []

    # Deduplicate albums — multiple tracks may come from the same release.
    seen_album_ids: set[str] = set()
    albums = []
    for track in tracks:
        album = track.get("album", {})
        album_id = album.get("id")
        if album_id and album_id not in seen_album_ids:
            # The album object from a track search may omit the artists field;
            # copy it from the track so the rest of the code works as normal.
            if not album.get("artists"):
                album["artists"] = track.get("artists", [])
            seen_album_ids.add(album_id)
            albums.append(album)

    logger.info("Fetched %d unique albums from %d tracks", len(albums), len(tracks))

    # ── Step 2: batch-fetch artist genre tags ─────────────────────────────────
    # Extract the primary artist ID from each album (deduplicated).
    artist_ids = list({
        album["artists"][0]["id"]
        for album in albums
        if album.get("artists")
    })

    # sp.artists() accepts up to 50 IDs per call — we have at most 50 albums
    # so one call is always sufficient.
    artist_genre_map: dict[str, list[str]] = {}
    try:
        artists_result = _sp.artists(artist_ids[:50])
        for artist in artists_result.get("artists", []):
            if artist:
                artist_genre_map[artist["id"]] = artist.get("genres", [])
        logger.debug("Fetched genre tags for %d artists", len(artist_genre_map))
    except spotipy.SpotifyException as exc:
        # Non-fatal — we'll fall back to unfiltered selection below.
        logger.warning(
            "Could not fetch artist genres (%s) — genre matching disabled for this run", exc
        )

    # ── Step 3: match albums to target genres ─────────────────────────────────
    releases = []
    used_album_ids: set[str] = set()

    for genre in genres:
        matched_album = None

        # First pass: find an album whose artist has a matching genre tag.
        # Spotify genre tags are multi-word strings like "alternative rock" or
        # "indie pop", so we check if our keyword appears anywhere in any tag.
        for album in albums:
            if album["id"] in used_album_ids:
                continue
            artist_id = album["artists"][0]["id"] if album.get("artists") else None
            artist_genres = artist_genre_map.get(artist_id, [])
            if any(genre.lower() in ag.lower() for ag in artist_genres):
                matched_album = album
                logger.debug(
                    "Genre match for '%s': %s by %s",
                    genre, album["name"], album["artists"][0]["name"],
                )
                break

        # Second pass: no genre match found — just take the next unused album.
        # This ensures we always post something even if genre tags are sparse.
        if not matched_album:
            logger.warning(
                "No genre match for '%s' — using next available new release", genre
            )
            for album in albums:
                if album["id"] not in used_album_ids:
                    matched_album = album
                    break

        if matched_album:
            used_album_ids.add(matched_album["id"])
            releases.append(_album_to_release_dict(matched_album, genre))

    return releases
