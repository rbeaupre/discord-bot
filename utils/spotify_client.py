"""
utils/spotify_client.py
───────────────────────
Wrapper around the Spotipy library for fetching new music releases.

Uses Spotify's Client Credentials flow — no user login or OAuth callback
required — which is appropriate for reading public catalog data like albums.

Spotify API restrictions (as of 2026 for new app registrations)
---------------------------------------------------------------
The following endpoints return 403 Forbidden for apps without extended access:
  - GET /v1/browse/new-releases
  - GET /v1/artists (batch artist lookup — no genre tags or artist popularity)

What DOES work:
  - GET /v1/search with type=track, limit<=10

Strategy
--------
For each target genre we run one track search using the genre name as a keyword
combined with a year filter: e.g. `rock year:2026`. This is a keyword search
(not the broken genre: field filter), so it matches tracks whose title, artist
name, or album name contains the genre word — good enough for our purposes.

Each track object from the search result contains:
  - track.popularity     (0–100) — used to filter out mainstream artists
  - track.artists[0].id          — used to exclude previously posted artists
  - track.album                  — album name, cover art, Spotify URL, release date

No secondary API calls are needed, so we stay within the restricted tier.

Public functions
----------------
get_new_releases(genres, exclude_artist_ids, max_popularity)  → list[dict]
"""

import logging
from datetime import datetime

import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

import config

logger = logging.getLogger(__name__)

_sp = spotipy.Spotify(
    auth_manager=SpotifyClientCredentials(
        client_id=config.SPOTIFY_CLIENT_ID,
        client_secret=config.SPOTIFY_CLIENT_SECRET,
    )
)

# Tracks with a Spotify popularity score above this threshold are considered
# mainstream and skipped in the first pass. Scale is 0–100.
# 75 allows moderately known indie/rock/electronic acts through while still
# filtering out top-40 chart staples. The keyword search for genre year:YYYY
# tends to surface tracks in the 60–75 range, so 60 was too strict — passes
# 1 and 2 both failed and pass 3 (no filter) fired unconditionally every time.
_DEFAULT_MAX_POPULARITY = 75


def _track_to_release_dict(track: dict, genre: str) -> dict:
    """
    Convert a Spotify track search result into our standard release dict.

    We use the album name and URL as the release rather than the individual
    track, since we're surfacing new albums/EPs to the server.

    Parameters
    ----------
    track : dict   Raw track object from the Spotify search API.
    genre : str    The genre label this track was found under.

    Returns
    -------
    dict with keys: artist, artist_id, title, release_date, spotify_url, image_url, genre
    """
    primary_artist = track["artists"][0] if track.get("artists") else {}
    album = track.get("album", {})

    # Link to the album page, not the individual track.
    spotify_url = album.get("external_urls", {}).get("spotify", "")

    # Spotify returns cover art in descending size order — index 0 is largest.
    images = album.get("images", [])
    image_url = images[0]["url"] if images else None

    return {
        "artist": primary_artist.get("name", "Unknown Artist"),
        "artist_id": primary_artist.get("id"),   # stored in DB to prevent repeats
        "title": album.get("name", track.get("name", "Unknown")),
        "release_date": album.get("release_date", "Unknown"),
        "spotify_url": spotify_url,
        "image_url": image_url,
        "genre": genre,
    }


def get_new_releases(
    genres: list[str],
    exclude_artist_ids: list[str] | None = None,
    max_popularity: int = _DEFAULT_MAX_POPULARITY,
) -> list[dict]:
    """
    Fetch one new release per genre, filtering for less well-known artists and
    skipping artists that have been posted in previous weeks.

    Parameters
    ----------
    genres             : Genre labels to search for, e.g. ["rock", "indie", "electronic"].
    exclude_artist_ids : Spotify artist IDs to skip (previously posted artists).
    max_popularity     : Maximum track popularity score (0–100). Tracks above this
                         are skipped in the first pass. Default 60.

    Returns
    -------
    list[dict]
        One release dict per genre. May be shorter than genres if a search fails.
    """
    if exclude_artist_ids is None:
        exclude_artist_ids = []

    current_year = datetime.now().year
    releases: list[dict] = []
    used_album_ids: set[str] = set()

    for genre in genres:
        # Search for tracks matching this genre keyword + current year.
        # We use keyword search (not the broken genre: field filter) — this
        # matches the genre word against track/artist/album metadata.
        try:
            result = _sp.search(
                q=f"{genre} year:{current_year}",
                type="track",
                limit=10,
            )
            tracks = result["tracks"]["items"]
        except spotipy.SpotifyException as exc:
            logger.error("Spotify search failed for genre '%s': %s", genre, exc)
            continue

        if not tracks:
            logger.warning("No Spotify results for genre '%s'", genre)
            continue

        matched = None

        # Pass 1: ideal match — not too popular, not a repeat, unused album.
        for track in tracks:
            album_id = track.get("album", {}).get("id")
            if album_id in used_album_ids:
                continue
            artist_id = track["artists"][0]["id"] if track.get("artists") else None
            if artist_id in exclude_artist_ids:
                continue
            if track.get("popularity", 100) > max_popularity:
                continue
            matched = track
            logger.debug(
                "Genre '%s' match: %s by %s (popularity %d)",
                genre,
                track.get("album", {}).get("name"),
                track["artists"][0]["name"] if track.get("artists") else "?",
                track.get("popularity", 0),
            )
            break

        # Pass 2: relax the popularity cap but keep the exclusion filter.
        # This is the more important fallback — a fresh (not yet posted) artist
        # is more valuable than a lower popularity score. When all 10 returned
        # tracks exceed max_popularity, this pass finds a new artist instead of
        # falling all the way through to pass 3 and repeating the same track.
        if not matched:
            logger.warning(
                "No low-popularity fresh match for '%s' — relaxing popularity cap", genre
            )
            for track in tracks:
                album_id = track.get("album", {}).get("id")
                if album_id in used_album_ids:
                    continue
                artist_id = track["artists"][0]["id"] if track.get("artists") else None
                if artist_id in exclude_artist_ids:
                    continue
                matched = track
                logger.debug(
                    "Genre '%s' pass-2 match: %s by %s (popularity %d — above cap)",
                    genre,
                    track.get("album", {}).get("name"),
                    track["artists"][0]["name"] if track.get("artists") else "?",
                    track.get("popularity", 0),
                )
                break

        # Pass 3: absolute last resort — all non-excluded artists have been
        # exhausted. Take the first unused track regardless of repeat history.
        if not matched:
            logger.warning(
                "No unposted match for '%s' — all available artists already posted, "
                "using first available track", genre
            )
            for track in tracks:
                album_id = track.get("album", {}).get("id")
                if album_id not in used_album_ids:
                    matched = track
                    break

        if matched:
            album_id = matched.get("album", {}).get("id")
            if album_id:
                used_album_ids.add(album_id)
            releases.append(_track_to_release_dict(matched, genre))

    return releases
