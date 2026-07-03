"""
utils/spotify_client.py
───────────────────────
Wrapper around the Spotipy library for fetching new music releases and
reading public playlist data.

Two Spotify auth flows are used:

  Client Credentials (_sp)
    No user login required. Used for track/album search (new releases, album
    URL lookup). Cannot access playlist endpoints on newer app registrations.

  OAuth / Authorization Code (_sp_user, lazy)
    A real Spotify user has authorized the app. Token is cached in
    SPOTIFY_CACHE_PATH (default: .spotify_cache) and auto-refreshed by
    Spotipy. Required for playlist_items. Run auth_spotify.py once locally
    to generate the cache file, then copy it to the VM.

Spotify API restrictions (as of 2026 for new app registrations)
---------------------------------------------------------------
The following endpoints return 403 Forbidden without extended access:
  - GET /v1/browse/new-releases
  - GET /v1/artists

The following endpoint returns 401 with Client Credentials and requires
user OAuth even for public playlists on newer app registrations:
  - GET /v1/playlists/{id}/items  ← uses _sp_user

What works with Client Credentials:
  - GET /v1/search with type=track, limit<=10
  - GET /v1/search with type=album, limit<=1

Public functions
----------------
get_new_releases(genres, exclude_artist_ids, max_popularity)  → list[dict]
find_album_url(artist, album)                                  → str | None
get_playlist_artists(playlist_url)                             → list[str]
"""

import logging
import os
import re
from datetime import datetime

import spotipy
from spotipy.oauth2 import SpotifyClientCredentials, SpotifyOAuth

import config

logger = logging.getLogger(__name__)

# Client Credentials client — used for search endpoints.
_sp = spotipy.Spotify(
    auth_manager=SpotifyClientCredentials(
        client_id=config.SPOTIFY_CLIENT_ID,
        client_secret=config.SPOTIFY_CLIENT_SECRET,
    )
)

# OAuth client — lazily initialised on first call to get_playlist_artists().
# None until _get_oauth_client() is called for the first time.
_sp_user: spotipy.Spotify | None = None


def _get_oauth_client() -> spotipy.Spotify:
    """
    Return the OAuth-authenticated Spotipy client, creating it on first call.

    Validates the cache file before passing it to Spotipy. Without this check,
    a missing or empty cache causes Spotipy to attempt interactive terminal auth,
    which hangs (EOFError) inside a headless Docker container.

    Raises
    ------
    ValueError
        If the cache file is missing, is a directory, contains invalid JSON,
        or is missing the refresh_token key.
    spotipy.oauth2.SpotifyOauthError
        If the cached refresh token has been revoked and cannot be refreshed.
    """
    global _sp_user
    if _sp_user is not None:
        return _sp_user

    cache_path = config.SPOTIFY_CACHE_PATH

    # A missing path OR a directory (Docker creates a dir when the host path
    # doesn't exist at container start) are both unusable as a cache file.
    if not os.path.exists(cache_path) or os.path.isdir(cache_path):
        raise ValueError(
            f"Spotify OAuth cache not found at {cache_path!r}. "
            "Run auth_spotify.py locally to generate it, then copy it to the "
            f"server at {cache_path} and restart the bot container."
        )

    # Validate the cache contains a refresh token before handing it to Spotipy.
    # An empty or malformed file causes the same headless EOFError as no file.
    import json as _json
    try:
        with open(cache_path) as f:
            cached = _json.load(f)
        if not cached.get("refresh_token"):
            raise ValueError("refresh_token key is missing from the cache.")
    except (_json.JSONDecodeError, ValueError) as exc:
        raise ValueError(
            f"Spotify OAuth cache at {cache_path!r} is invalid or missing a "
            "refresh_token. Re-run auth_spotify.py locally and copy the new "
            "cache file to the server."
        ) from exc

    _sp_user = spotipy.Spotify(
        auth_manager=SpotifyOAuth(
            client_id=config.SPOTIFY_CLIENT_ID,
            client_secret=config.SPOTIFY_CLIENT_SECRET,
            redirect_uri=config.SPOTIFY_REDIRECT_URI,
            scope="playlist-read-private",
            cache_path=cache_path,
            # Never open a browser — the bot runs headless in Docker.
            open_browser=False,
        )
    )
    logger.debug("Spotify OAuth client initialised from cache %r", cache_path)
    return _sp_user

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


def find_album_url(artist: str, album: str) -> str | None:
    """
    Search Spotify for an album by artist and title and return its URL.

    Used by the monthly album review feature to add a "Listen on Spotify"
    link to the Pitchfork Best New Album embed.

    We use `artist:` and `album:` field filters in the query so Spotify
    matches on metadata rather than full-text search — this produces much
    more accurate results for exact album lookups.

    Parameters
    ----------
    artist : str   Artist name as it appears on Pitchfork.
    album  : str   Album title as it appears on Pitchfork.

    Returns
    -------
    str or None
        The Spotify album page URL, or None if no match is found or the
        search fails (e.g. 403 on restricted app tier).
    """
    # Try with field filters first (most precise), then fall back to a plain
    # keyword search if the restricted app tier blocks the filtered query.
    queries = [
        f"artist:{artist} album:{album}",
        f"{artist} {album}",
    ]
    for query in queries:
        try:
            result = _sp.search(q=query, type="album", limit=1)
            items = result.get("albums", {}).get("items", [])
            if items:
                url = items[0].get("external_urls", {}).get("spotify")
                if url:
                    logger.debug(
                        "Spotify album match for '%s — %s' (query: %r): %s",
                        artist, album, query, url,
                    )
                return url
        except spotipy.SpotifyException as exc:
            logger.warning(
                "Spotify album search failed for query %r: %s — trying next", query, exc
            )
            continue

    return None


def get_playlist_artists(playlist_url: str) -> list[str]:
    """
    Return a deduplicated list of unique artist names from a public Spotify playlist.

    Paginates through all tracks in the playlist (100 per page) and collects
    every credited artist. Deduplication is case-insensitive but the first-seen
    capitalisation is preserved in the returned list.

    Parameters
    ----------
    playlist_url : Full Spotify playlist URL
                   (e.g. https://open.spotify.com/playlist/37i9dQZF1DX...)
                   or a Spotify URI (spotify:playlist:37i9dQZF1DX...).

    Returns
    -------
    list[str]
        Unique artist names in the order they first appear in the playlist.

    Raises
    ------
    ValueError
        If no playlist ID can be extracted from playlist_url.
    spotipy.SpotifyException
        If the playlist is private, doesn't exist, or the API request fails.
    """
    # Extract the 22-character playlist ID from a URL or URI.
    # URL: https://open.spotify.com/playlist/<id>?si=...
    # URI: spotify:playlist:<id>
    match = re.search(r"playlist[/:]([A-Za-z0-9]+)", playlist_url)
    if not match:
        raise ValueError(
            f"Could not extract a playlist ID from {playlist_url!r}. "
            "Use a link from the Spotify 'Share' menu."
        )

    playlist_id = match.group(1)
    logger.info("Fetching artists from Spotify playlist %s", playlist_id)

    # Use the OAuth client — Client Credentials returns 401 for this endpoint
    # on newer Spotify app registrations.
    sp = _get_oauth_client()

    # Only fetch the fields we need to minimise response size.
    fields = "items(track(artists(name))),next"

    seen_lower: set[str] = set()
    artists: list[str] = []
    offset = 0
    limit = 100

    while True:
        try:
            result = sp.playlist_items(
                playlist_id,
                fields=fields,
                limit=limit,
                offset=offset,
                additional_types=["track"],
            )
        except spotipy.SpotifyException:
            raise

        for item in result.get("items", []):
            track = item.get("track")
            if not track:
                # Local files or podcast episodes have no track object.
                continue
            for artist in track.get("artists", []):
                name = (artist.get("name") or "").strip()
                if name and name.lower() not in seen_lower:
                    seen_lower.add(name.lower())
                    artists.append(name)

        if not result.get("next"):
            break
        offset += limit

    logger.info(
        "Found %d unique artists in playlist %s", len(artists), playlist_id
    )
    return artists
