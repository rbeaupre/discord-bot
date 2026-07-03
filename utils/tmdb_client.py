"""
utils/tmdb_client.py
────────────────────
Client for The Movie Database (TMDB) API v3.

Used by the movie night cog to fetch and cache the Criterion Collection film
catalog. The Criterion Collection company ID is discovered dynamically via a
company search so we don't hardcode a value that could change.

Director credits require a separate /credits call per film. To keep setup
fast on refreshes, credits are only fetched for films not already in the
database (controlled by the caller passing existing_ids).

Public functions
----------------
get_criterion_films(api_key, existing_ids)  → list[dict]
    Fetch all Criterion Collection films from TMDB, including director credits
    for any film not in existing_ids.
"""

import logging
import time

import requests

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.themoviedb.org/3"

# w500 gives a good balance between image quality and file size for Discord embeds.
_IMAGE_BASE_URL = "https://image.tmdb.org/t/p/w500"

_REQUEST_TIMEOUT = 15

# TMDB's free tier allows 40 requests per 10 seconds. A 0.26 s delay keeps us
# comfortably under the limit even during the initial catalog load that fetches
# one /credits call per film.
_CALL_DELAY = 0.26


class TMDBError(Exception):
    """Raised when the TMDB API returns an error or cannot be reached."""
    pass


def _get(path: str, api_key: str, **params) -> dict:
    """
    Make a GET request to the TMDB API and return the parsed JSON body.

    TMDB supports two authentication formats:
      - v3 API key (short alphanumeric string): passed as ?api_key= query param.
      - API Read Access Token (JWT starting with "eyJ"): passed as an
        Authorization: Bearer header. This is what the TMDB dashboard calls
        "API Read Access Token" and is the default for new accounts.

    Both token types work with the v3 API endpoints — only the auth method differs.

    Parameters
    ----------
    path    : API path relative to _BASE_URL, e.g. "/search/company".
    api_key : Either a v3 API key or a JWT Bearer token from the TMDB dashboard.
    **params: Additional query parameters.

    Raises
    ------
    TMDBError
        On any network error or non-2xx HTTP status.
    """
    url = _BASE_URL + path

    # JWTs start with "eyJ" (base64-encoded '{"'). Detect and use Bearer auth.
    if api_key.startswith("eyJ"):
        headers = {"Authorization": f"Bearer {api_key}"}
        query_params = params
    else:
        headers = {}
        query_params = {"api_key": api_key, **params}

    try:
        response = requests.get(
            url,
            params=query_params,
            headers=headers,
            timeout=_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise TMDBError(f"TMDB request failed for {path!r}: {exc}") from exc
    return response.json()


def _find_criterion_company_id(api_key: str) -> int:
    """
    Search TMDB for the Criterion Collection production company and return its ID.

    TMDB company IDs can theoretically change or a new Criterion entity could be
    added, so we discover the ID at runtime rather than hardcoding it.

    Raises
    ------
    TMDBError
        If no company containing "criterion" is found in the search results.
    """
    data = _get("/search/company", api_key, query="Criterion Collection")
    for company in data.get("results", []):
        if "criterion" in company.get("name", "").lower():
            logger.info(
                "Found Criterion Collection company on TMDB: %r (id=%d)",
                company["name"], company["id"],
            )
            return company["id"]
    raise TMDBError(
        "Could not find 'Criterion Collection' in TMDB company search. "
        "The company name may have changed on TMDB."
    )


def _get_director(tmdb_id: int, api_key: str) -> str | None:
    """
    Fetch the credits for a film and return the first credited director's name.

    Parameters
    ----------
    tmdb_id : TMDB numeric film ID.
    api_key : TMDB API key.

    Returns
    -------
    str or None — director name, or None if credits are unavailable or the
                  director field is absent.
    """
    try:
        time.sleep(_CALL_DELAY)
        data = _get(f"/movie/{tmdb_id}/credits", api_key)
        for person in data.get("crew", []):
            if person.get("job") == "Director":
                return person.get("name")
    except TMDBError as exc:
        # Non-fatal: we'll store the film without a director name.
        logger.debug("Could not fetch credits for tmdb_id=%d: %s", tmdb_id, exc)
    return None


def get_criterion_films(
    api_key: str,
    existing_ids: set[int] | None = None,
) -> list[dict]:
    """
    Fetch all Criterion Collection films from TMDB.

    First resolves The Criterion Collection's production company ID, then
    paginates through /discover/movie filtered to that company. For each film
    not already in existing_ids, an additional /credits call is made to get
    the director.

    Parameters
    ----------
    api_key      : TMDB v3 API key.
    existing_ids : Set of tmdb_ids already stored in the database. Director
                   credits are skipped for these to speed up catalog refreshes.
                   Pass None (the default) to fetch director for every film.

    Returns
    -------
    list[dict], each containing:
        tmdb_id    – TMDB film ID (int, used as PK in criterion_films table)
        title      – Film title (str)
        year       – Four-digit release year as int, or None
        director   – Director name (str), or None if not in existing_ids and
                     credits were unavailable
        overview   – Plot summary truncated to 1000 chars (str or None)
        poster_url – Full URL to the w500 poster image (str or None)

    Raises
    ------
    TMDBError
        If the company search or any discover page request fails.
    """
    if existing_ids is None:
        existing_ids = set()

    company_id = _find_criterion_company_id(api_key)

    films: list[dict] = []
    page = 1

    while True:
        time.sleep(_CALL_DELAY)
        logger.info("Fetching TMDB Criterion films — page %d", page)

        data = _get(
            "/discover/movie",
            api_key,
            with_companies=company_id,
            page=page,
            sort_by="release_date.asc",
        )

        results = data.get("results", [])
        total_pages = data.get("total_pages", 1)

        for movie in results:
            tmdb_id = movie.get("id")
            if not tmdb_id:
                continue

            title = movie.get("title") or "Unknown Title"

            # release_date is "YYYY-MM-DD" or an empty string.
            year: int | None = None
            release_date = movie.get("release_date", "")
            if release_date and len(release_date) >= 4:
                try:
                    year = int(release_date[:4])
                except ValueError:
                    pass

            overview = (movie.get("overview") or "")[:1000] or None

            poster_path = movie.get("poster_path")
            poster_url = (_IMAGE_BASE_URL + poster_path) if poster_path else None

            # Only call /credits for films that aren't already in the DB, since
            # existing films already have their director stored.
            director: str | None = None
            if tmdb_id not in existing_ids:
                director = _get_director(tmdb_id, api_key)

            films.append({
                "tmdb_id": tmdb_id,
                "title": title,
                "year": year,
                "director": director,
                "overview": overview,
                "poster_url": poster_url,
            })

        if page >= total_pages:
            break
        page += 1

    logger.info(
        "Fetched %d Criterion Collection films from TMDB (company_id=%d)",
        len(films), company_id,
    )
    return films
