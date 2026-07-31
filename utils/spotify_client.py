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
get_new_releases(genres, exclude_artist_ids, exclude_release_keys, max_popularity)  → list[dict]
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

# Spotify caps search results at 10 per page for this app's restricted tier
# (see the module docstring — limit>10 fails). We page through results with
# `offset` instead of raising the limit.
_SEARCH_PAGE_LIMIT = 10

# How far to page within a single year before giving up on it. 4 pages of 10
# (offsets 0/10/20/30) covers the 40 most relevant results for the query —
# beyond that, relevance has usually dropped off enough that paging further
# rarely surfaces anything new.
_MAX_SEARCH_OFFSET = 30

# How many years to step backward (current_year, current_year-1, ...) before
# giving up on a genre entirely. The keyword+year search tends to return
# nearly the same handful of relevant tracks every week, so once every result
# for the current year has already been posted, older years are the only
# way to find genuinely fresh artists.
_MAX_YEARS_BACK = 3


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
    exclude_release_keys: list[tuple[str, str]] | None = None,
    max_popularity: int = _DEFAULT_MAX_POPULARITY,
) -> list[dict]:
    """
    Fetch one new release per genre, filtering for less well-known artists and
    skipping artists/releases that have ever been posted before.

    The keyword+year search Spotify's restricted app tier allows us to use
    (see module docstring) tends to return nearly the same handful of
    relevant tracks for a given query every week, so a single 10-result page
    for the current year is often entirely artists we've already posted. To
    find something genuinely fresh, this pages through additional result
    pages (via `offset`) and, if still nothing turns up, steps back through
    previous years (`year:YYYY`) — see _MAX_SEARCH_OFFSET / _MAX_YEARS_BACK.

    Parameters
    ----------
    genres               : Genre labels to search for, e.g. ["rock", "indie", "electronic"].
    exclude_artist_ids   : Spotify artist IDs to skip — should be every artist
                           ever posted for this guild (see database.models.MusicPost),
                           not just a recent window, so the same act never repeats.
    exclude_release_keys : (artist_name, album_title) pairs to skip, both
                           lowercased/stripped, matching _release_key() below.
                           Same all-time scope as exclude_artist_ids — an extra
                           guard against the same album resurfacing under a
                           different Spotify artist ID.
    max_popularity        : Maximum track popularity score (0–100). Tracks above this
                            are skipped in the first pass. Default 75.

    Returns
    -------
    list[dict]
        One release dict per genre. May be shorter than genres if a search
        fails or no fresh artist can be found within the search budget.
    """
    exclude_artist_ids = set(exclude_artist_ids or [])
    exclude_release_keys = set(exclude_release_keys or [])

    current_year = datetime.now().year
    releases: list[dict] = []
    used_album_ids: set[str] = set()
    # Spotify sometimes assigns a different album ID to the same release when
    # it's returned by a different keyword search (e.g. market/edition variants),
    # so album_id alone isn't a reliable dedup key across genres. Tracking
    # (artist, normalized title) as well catches the same album showing up
    # under two different genre fields in one post.
    used_release_keys: set[tuple[str, str]] = set()

    def _release_key(track: dict) -> tuple[str, str]:
        artist_name = (
            track["artists"][0].get("name", "") if track.get("artists") else ""
        )
        album_title = track.get("album", {}).get("name", "")
        return (artist_name.strip().lower(), album_title.strip().lower())

    def _is_fresh(track: dict) -> bool:
        album_id = track.get("album", {}).get("id")
        if album_id in used_album_ids:
            return False
        key = _release_key(track)
        if key in used_release_keys or key in exclude_release_keys:
            return False
        artist_id = track["artists"][0]["id"] if track.get("artists") else None
        if artist_id in exclude_artist_ids:
            return False
        return True

    for genre in genres:
        matched = None
        # Every fresh (not-yet-posted) track we see while paging, regardless
        # of popularity — reused for the relaxed-popularity fallback below so
        # we don't have to re-hit the API for it.
        fresh_candidates: list[dict] = []

        for year in range(current_year, current_year - _MAX_YEARS_BACK - 1, -1):
            for offset in range(0, _MAX_SEARCH_OFFSET + 1, _SEARCH_PAGE_LIMIT):
                # Search for tracks matching this genre keyword + year. We use
                # keyword search (not the broken genre: field filter) — this
                # matches the genre word against track/artist/album metadata.
                try:
                    result = _sp.search(
                        q=f"{genre} year:{year}",
                        type="track",
                        limit=_SEARCH_PAGE_LIMIT,
                        offset=offset,
                    )
                    tracks = result["tracks"]["items"]
                except spotipy.SpotifyException as exc:
                    logger.error(
                        "Spotify search failed for genre '%s' year %d offset %d: %s",
                        genre, year, offset, exc,
                    )
                    break

                if not tracks:
                    # No more results at this offset for this year — paging
                    # further won't help, move on to the previous year.
                    break

                for track in tracks:
                    if not _is_fresh(track):
                        continue
                    fresh_candidates.append(track)
                    if track.get("popularity", 100) <= max_popularity:
                        matched = track
                        break

                if matched:
                    break
            if matched:
                break

        # No ideal (low-popularity, fresh) match anywhere in the search
        # budget — fall back to the most popular fresh candidate we saw while
        # paging, rather than making more API calls. A fresh artist above the
        # popularity cap is still better than no release for this genre.
        if not matched and fresh_candidates:
            matched = fresh_candidates[0]
            logger.debug(
                "Genre '%s' — using above-popularity-cap fresh match: %s by %s",
                genre,
                matched.get("album", {}).get("name"),
                matched["artists"][0]["name"] if matched.get("artists") else "?",
            )

        if not matched:
            logger.warning(
                "No unposted match for '%s' after searching %d year(s) back — "
                "skipping this genre this week", genre, _MAX_YEARS_BACK,
            )
            continue

        album_id = matched.get("album", {}).get("id")
        if album_id:
            used_album_ids.add(album_id)
        used_release_keys.add(_release_key(matched))
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

    seen_lower: set[str] = set()
    artists: list[str] = []
    offset = 0
    limit = 100

    while True:
        try:
            # No fields filter — Spotify can return track=null for all items
            # when the filter interacts with certain playlist item types, even
            # when the playlist contains normal tracks. Fetch full objects instead.
            result = sp.playlist_items(
                playlist_id,
                limit=limit,
                offset=offset,
            )
        except spotipy.SpotifyException:
            raise

        for playlist_item in result.get("items", []):
            # Spotify changed the key from "track" to "item" in a newer API version.
            track = playlist_item.get("item") or playlist_item.get("track")
            if not track:
                # Local files or podcast episodes may have no track object.
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
