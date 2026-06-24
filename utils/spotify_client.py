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
    dict with keys:
        artist, artist_id, title, release_date, spotify_url, image_url, genre
    """
    primary_artist = album["artists"][0] if album.get("artists") else {}
    artist_name = primary_artist.get("name", "Unknown Artist")
    # artist_id is stored in the DB after posting so we can skip this artist
    # in future weeks (repeat-prevention).
    artist_id = primary_artist.get("id")

    # Spotify returns cover art in descending size order — index 0 is largest (640x640).
    image_url = album["images"][0]["url"] if album.get("images") else None

    return {
        "artist": artist_name,
        "artist_id": artist_id,
        "title": album["name"],
        "release_date": album.get("release_date", "Unknown"),
        "spotify_url": album["external_urls"].get("spotify", ""),
        "image_url": image_url,
        "genre": genre,
    }


def get_new_releases(
    genres: list[str],
    exclude_artist_ids: list[str] | None = None,
    max_popularity: int = 60,
) -> list[dict]:
    """
    Fetch one new release per genre, filtered for less well-known artists and
    avoiding artists that have been posted recently.

    Approach
    --------
    1. Search Spotify by current year to get a batch of recent albums.
    2. Batch-fetch popularity scores and genre tags for all primary artists.
    3. For each target genre, find the first album whose artist:
         - Has a popularity score at or below max_popularity (avoids mainstream acts)
         - Is not in the exclude_artist_ids list (avoids weekly repeats)
         - Has a genre tag matching the target genre keyword
       Falls back to the next unused album if no genre match is found, and
       relaxes all filters as a last resort so the post always has content.

    Parameters
    ----------
    genres             : Target genre labels, e.g. ["rock", "indie", "electronic"].
    exclude_artist_ids : Artist IDs to skip (previously posted artists).
    max_popularity     : Maximum Spotify popularity score (0–100). Artists above
                         this threshold are skipped. Default 60.

    Returns
    -------
    list[dict]
        One release dict per genre (see _album_to_release_dict for keys).
    """
    if exclude_artist_ids is None:
        exclude_artist_ids = []
    # ── Step 1: fetch recent tracks and extract their album info ─────────────
    # Spotify restricts type="album" searches (400 Invalid limit) for newer
    # app tiers. type="track" works fine and each track object contains a
    # nested album object with all the fields we need. We deduplicate by
    # album ID so we don't show the same release twice.
    current_year = datetime.now().year
    try:
        result = _sp.search(q=f"year:{current_year}", type="track", limit=10)
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

    # sp.artists() accepts up to 50 IDs per call — we have at most 10 albums
    # so one call is always sufficient. We store the full artist object so we
    # can access both genres and popularity in the selection step below.
    artist_genre_map: dict[str, dict] = {}  # artist_id → {genres, popularity, ...}
    try:
        artists_result = _sp.artists(artist_ids[:50])
        for artist in artists_result.get("artists", []):
            if artist:
                artist_genre_map[artist["id"]] = {
                    "genres": artist.get("genres", []),
                    "popularity": artist.get("popularity", 0),
                }
        logger.debug("Fetched info for %d artists", len(artist_genre_map))
    except spotipy.SpotifyException as exc:
        # Non-fatal — selection will fall back to pass 3 (unfiltered) below.
        logger.warning(
            "Could not fetch artist info (%s) — filters disabled for this run", exc
        )

    # ── Step 3: match albums to target genres with popularity + repeat filters ──
    releases = []
    used_album_ids: set[str] = set()

    for genre in genres:
        matched_album = None

        # Pass 1: ideal match — correct genre, not too popular, not a recent repeat.
        for album in albums:
            if album["id"] in used_album_ids:
                continue
            artist_id = album["artists"][0]["id"] if album.get("artists") else None
            artist_info = artist_genre_map.get(artist_id, {})
            popularity = artist_info.get("popularity", 0)
            artist_genres = artist_info.get("genres", [])

            if popularity > max_popularity:
                continue
            if artist_id in exclude_artist_ids:
                continue
            if any(genre.lower() in ag.lower() for ag in artist_genres):
                matched_album = album
                logger.debug(
                    "Genre match for '%s': %s by %s (popularity %d)",
                    genre, album["name"], album["artists"][0]["name"], popularity,
                )
                break

        # Pass 2: relax the repeat filter — correct genre, not too popular,
        # but allow a previously posted artist if nothing fresh is available.
        if not matched_album:
            logger.warning("No fresh match for '%s' — relaxing repeat filter", genre)
            for album in albums:
                if album["id"] in used_album_ids:
                    continue
                artist_id = album["artists"][0]["id"] if album.get("artists") else None
                artist_info = artist_genre_map.get(artist_id, {})
                popularity = artist_info.get("popularity", 0)
                artist_genres = artist_info.get("genres", [])

                if popularity > max_popularity:
                    continue
                if any(genre.lower() in ag.lower() for ag in artist_genres):
                    matched_album = album
                    break

        # Pass 3: last resort — just take the next unused album regardless of
        # genre, popularity, or repeat history so the post always has content.
        if not matched_album:
            logger.warning(
                "No filtered match for '%s' — using next available release", genre
            )
            for album in albums:
                if album["id"] not in used_album_ids:
                    matched_album = album
                    break

        if matched_album:
            used_album_ids.add(matched_album["id"])
            releases.append(_album_to_release_dict(matched_album, genre))

    return releases
