"""
utils/ticketmaster_client.py
────────────────────────────
Client for the Ticketmaster Discovery API.

Fetches upcoming music events by city so the concert alerts cog can match
them against a per-guild artist watchlist. We query at the city level rather
than artist-by-artist: two city queries retrieve all events in Toronto and
Montreal regardless of watchlist size, while a watchlist of 500 artists would
require 500+ individual API calls.

Public functions
----------------
get_upcoming_events(cities, api_key, days_ahead)  → list[dict]
    Return all upcoming music events in the given Canadian cities for the
    next days_ahead days, with full pagination.
"""

import logging
from datetime import datetime, timedelta, timezone

import requests

logger = logging.getLogger(__name__)

_BASE_URL = "https://app.ticketmaster.com/discovery/v2/events.json"
_REQUEST_TIMEOUT = 15

# Cap pagination per city to avoid runaway requests. At 200 events per page,
# 5 pages covers 1000 events — more than enough for any Canadian city.
_MAX_PAGES_PER_CITY = 5
_PAGE_SIZE = 200

_HEADERS = {
    "User-Agent": "DiscordBot/1.0 (concert alerts feature)",
}


class TicketmasterError(Exception):
    """Raised when the Ticketmaster API returns an error or cannot be reached."""
    pass


def get_upcoming_events(
    cities: list[str],
    api_key: str,
    days_ahead: int = 90,
) -> list[dict]:
    """
    Fetch all upcoming music events in the specified cities for the next
    days_ahead days.

    Parameters
    ----------
    cities     : List of city names to query, e.g. ["Toronto", "Montreal"].
                 Each city is queried separately with countryCode=CA.
    api_key    : Ticketmaster Discovery API key.
    days_ahead : How many calendar days ahead to look (default 90).

    Returns
    -------
    list[dict], each dict containing:
        event_id      – Ticketmaster event ID (str)
        event_name    – Full event name as listed on Ticketmaster (str)
        artist_names  – Attraction/artist names for this event (list[str])
        venue_name    – Venue name (str or None)
        city          – City where the event takes place (str)
        event_date    – Human-readable date, e.g. "Sat, Jul 12 2025" (str or None)
        event_url     – Ticketmaster purchase/info URL (str or None)

    Raises
    ------
    TicketmasterError
        If any HTTP request to the Ticketmaster API fails.
    """
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=days_ahead)

    # ISO 8601 format required by Ticketmaster.
    start_dt = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_dt = end.strftime("%Y-%m-%dT%H:%M:%SZ")

    all_events: list[dict] = []
    for city in cities:
        logger.info("Fetching Ticketmaster events for city: %s", city)
        city_events = _fetch_city_events(city, api_key, start_dt, end_dt)
        logger.info("Found %d events in %s", len(city_events), city)
        all_events.extend(city_events)

    return all_events


def _fetch_city_events(
    city: str,
    api_key: str,
    start_dt: str,
    end_dt: str,
) -> list[dict]:
    """
    Fetch all pages of music events for a single city, following pagination
    links up to _MAX_PAGES_PER_CITY times.

    Parameters
    ----------
    city     : City name to query.
    api_key  : Ticketmaster API key.
    start_dt : ISO 8601 start timestamp string.
    end_dt   : ISO 8601 end timestamp string.

    Returns
    -------
    list[dict] — all events parsed from all pages for this city.

    Raises
    ------
    TicketmasterError
        On any HTTP failure or non-2xx response.
    """
    # Base query parameters — countryCode=CA narrows to Canada so we don't get
    # events in a US city with the same name (e.g. Montreal, Indiana).
    params: dict = {
        "apikey": api_key,
        "city": city,
        "countryCode": "CA",
        "classificationName": "music",
        "size": _PAGE_SIZE,
        "startDateTime": start_dt,
        "endDateTime": end_dt,
        "sort": "date,asc",
    }

    events: list[dict] = []

    for page_num in range(_MAX_PAGES_PER_CITY):
        params["page"] = page_num

        try:
            response = requests.get(
                _BASE_URL, params=params, headers=_HEADERS, timeout=_REQUEST_TIMEOUT
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise TicketmasterError(
                f"Ticketmaster request failed for city {city!r}: {exc}"
            ) from exc

        data = response.json()

        # _embedded is absent when there are zero results.
        raw_events = data.get("_embedded", {}).get("events", [])
        for raw in raw_events:
            parsed = _parse_event(raw, city)
            if parsed:
                events.append(parsed)

        # Stop if we've consumed all pages.
        page_info = data.get("page", {})
        total_pages = page_info.get("totalPages", 1)
        if page_num + 1 >= total_pages:
            break

        logger.debug("Fetching page %d/%d for %s", page_num + 2, total_pages, city)

    return events


def _parse_event(raw: dict, city: str) -> dict | None:
    """
    Extract the fields we need from a single Ticketmaster event JSON object.

    Parameters
    ----------
    raw  : One element from the Ticketmaster events array.
    city : The city this event was retrieved for (used as fallback city label).

    Returns
    -------
    dict or None — None if the event object lacks a usable ID.
    """
    event_id = raw.get("id")
    if not event_id:
        return None

    event_name = raw.get("name", "")

    # Attractions are the performing artists. Festivals may have several.
    embedded = raw.get("_embedded", {})
    attractions = embedded.get("attractions", [])
    artist_names = [a["name"] for a in attractions if a.get("name")]

    # Venue details live under _embedded.venues[0].
    venues = embedded.get("venues", [])
    venue_name: str | None = venues[0].get("name") if venues else None
    event_city: str = (
        (venues[0].get("city") or {}).get("name") if venues else None
    ) or city

    # Ticketmaster provides the local date in dates.start.localDate (YYYY-MM-DD).
    local_date: str | None = raw.get("dates", {}).get("start", {}).get("localDate")
    event_date: str | None = None
    if local_date:
        try:
            dt = datetime.strptime(local_date, "%Y-%m-%d")
            # "%-d" strips the leading zero on day — Linux/macOS only. Works fine
            # since the bot runs on a Linux Docker container in production.
            event_date = dt.strftime("%a, %b %-d %Y")
        except ValueError:
            event_date = local_date

    return {
        "event_id": event_id,
        "event_name": event_name,
        "artist_names": artist_names,
        "venue_name": venue_name,
        "city": event_city,
        "event_date": event_date,
        "event_url": raw.get("url"),
    }
