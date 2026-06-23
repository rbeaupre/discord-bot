"""
utils/spotify_client.py
───────────────────────
Wrapper around the Spotipy library for fetching new music releases.

Uses Spotify's Client Credentials flow — no user login or OAuth callback
required — which is appropriate for reading public catalog data like albums.

Public functions
----------------
get_new_releases(genres)  → list[dict]
    Fetch one recent release per genre from Spotify.
"""

import logging

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


def get_new_release_by_genre(genre: str) -> dict | None:
    """
    Search Spotify for the most recently released album or single in a genre.

    Strategy: Use Spotify's search API with a 'genre:"<name>"' filter token
    combined with 'year:<current_year>' to surface fresh releases. Results are
    ordered by Spotify's relevance score, so we take the first result — it is
    almost always a very recent release.

    Note: Spotify's genre taxonomy for search differs from audio feature genres.
    Common values that work well: "rock", "indie", "electronic", "pop", "metal".
    If a genre string returns no results, the caller receives None and the
    weekly post skips that genre gracefully.

    Parameters
    ----------
    genre : str
        Genre label to search for (e.g. "rock", "indie rock", "electronic").

    Returns
    -------
    dict with keys:
        artist       – primary artist name
        title        – album or single title
        release_date – release date string from Spotify (e.g. "2026-06-10")
        spotify_url  – link to the release on Spotify
        image_url    – URL of the album cover art (largest available)
        genre        – the genre string passed in (echoed back for labelling)

    Returns None if Spotify returns no results or raises an error.
    """
    # Strategy 1: genre filter + tag:new (Spotify's built-in new-releases flag).
    # This is more reliable than year: filtering, which Spotify's search index
    # handles inconsistently and often returns zero results.
    try:
        results = _sp.search(
            q=f'genre:"{genre}" tag:new',
            type="album",
            limit=10,
        )
        albums = results.get("albums", {}).get("items", [])
    except spotipy.SpotifyException as exc:
        logger.error("Spotify search failed for genre '%s': %s", genre, exc)
        return None

    # Strategy 2: if tag:new returns nothing, fall back to genre filter alone.
    # This broadens the search to all time but still targets the right genre.
    if not albums:
        logger.warning(
            "No results for genre '%s' with tag:new — retrying without tag filter", genre
        )
        try:
            results = _sp.search(
                q=f'genre:"{genre}"',
                type="album",
                limit=10,
            )
            albums = results.get("albums", {}).get("items", [])
        except spotipy.SpotifyException as exc:
            logger.error("Spotify fallback search failed for genre '%s': %s", genre, exc)
            return None

    if not albums:
        logger.warning("No Spotify results for genre '%s' with any search strategy", genre)
        return None

    # Take the first result — Spotify ranks by relevance, and with year filtering
    # this is reliably a recent release in the correct genre.
    album = albums[0]

    # The artists list can contain multiple names (for compilations, features, etc.).
    # We display just the primary (first) artist.
    artist_name = album["artists"][0]["name"] if album["artists"] else "Unknown Artist"

    # Spotify returns images in descending size order — index 0 is the largest
    # (usually 640x640), which looks best in a Discord embed.
    image_url = album["images"][0]["url"] if album["images"] else None

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
    Fetch one new release per genre from the provided list.

    Calls get_new_release_by_genre() for each genre and collects the results.
    Genres that return no results are silently skipped so the weekly post still
    goes out even if one genre search fails.

    Parameters
    ----------
    genres : list[str]
        Genre strings to search for, e.g. ["rock", "indie rock", "electronic"].

    Returns
    -------
    list[dict]
        One release dict per successful genre lookup (see get_new_release_by_genre).
        May be shorter than the input list if some genres return no results.
    """
    releases = []
    for genre in genres:
        release = get_new_release_by_genre(genre)
        if release:
            releases.append(release)
        else:
            logger.warning("Skipping genre '%s' — no release found", genre)
    return releases
