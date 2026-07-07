"""
utils/sports_client.py
──────────────────────
Client for ESPN's public scoreboard API.

No API key is required — ESPN's public scoreboard endpoints are openly
accessible and widely used by third-party apps. We query per-sport
scoreboard URLs and filter to playoff or major-tournament games only.

For NFL, NHL, and MLB, season type "3" in the event payload indicates
postseason/playoffs. For soccer, we query the FIFA Men's World Cup endpoint
only — every game in that tournament counts regardless of season type.

Public constants
----------------
ACTIVE_STATUSES   — ESPN status names indicating a game is live.
FINAL_STATUSES    — ESPN status names indicating a game has ended.

Public functions
----------------
get_live_playoff_games(sport)       → list[dict]
    Return all in-progress or just-finished playoff games for one sport.

get_all_live_playoff_games(sports)  → dict[str, list[dict]]
    Call get_live_playoff_games for each sport, suppressing per-sport
    errors so one failing endpoint doesn't block the others.
"""

import logging

import requests

logger = logging.getLogger(__name__)

_BASE_URL = "https://site.api.espn.com/apis/site/v2/sports"
_REQUEST_TIMEOUT = 15

# ESPN season type "3" identifies postseason / playoffs for NFL, NHL, and MLB.
_POSTSEASON_TYPE_ID = "3"

# Scoreboard URL(s) for each supported sport. Soccer has multiple tournament
# endpoints; all others have a single endpoint.
_SPORT_ENDPOINTS: dict[str, list[str]] = {
    "nfl": [f"{_BASE_URL}/football/nfl/scoreboard"],
    "nhl": [f"{_BASE_URL}/hockey/nhl/scoreboard"],
    "mlb": [f"{_BASE_URL}/baseball/mlb/scoreboard"],
    "soccer": [
        f"{_BASE_URL}/soccer/fifa.world/scoreboard",    # FIFA Men's World Cup
    ],
}

# ESPN status type names for games that are currently being played.
# Soccer uses half-specific names rather than a single "in progress" value,
# and adds extra-time and shootout phases that must be tracked so scoring
# plays during those phases are not silently dropped.
ACTIVE_STATUSES: frozenset[str] = frozenset({
    "STATUS_IN_PROGRESS",
    "STATUS_FIRST_HALF",
    "STATUS_SECOND_HALF",
    "STATUS_HALFTIME",
    "STATUS_END_PERIOD",
    "STATUS_EXTRA_TIME",   # soccer: extra time (overtime) in progress
    "STATUS_SHOOTOUT",     # soccer: penalty shootout in progress
})

# ESPN status type names for games that have concluded.
FINAL_STATUSES: frozenset[str] = frozenset({
    "STATUS_FINAL",
    "STATUS_FULL_TIME",
    "STATUS_FINAL_OT",
    "STATUS_FINAL_AET",    # after extra time (soccer)
    "STATUS_FINAL_PEN",    # after penalties (soccer)
})

# All statuses worth tracking (active + final).
_TRACKABLE_STATUSES: frozenset[str] = ACTIVE_STATUSES | FINAL_STATUSES


class SportsAPIError(Exception):
    """Raised when a request to the ESPN scoreboard API fails."""
    pass


def _fetch_scoreboard(url: str) -> dict:
    """
    Fetch a single ESPN scoreboard endpoint and return the parsed JSON body.

    Parameters
    ----------
    url : Full scoreboard URL.

    Raises
    ------
    SportsAPIError
        If the HTTP request fails or returns a non-2xx status code.
    """
    try:
        response = requests.get(url, timeout=_REQUEST_TIMEOUT)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise SportsAPIError(f"ESPN request failed for {url!r}: {exc}") from exc
    return response.json()


def _is_playoff(event: dict, sport: str) -> bool:
    """
    Determine whether an ESPN event qualifies as a playoff or major tournament game.

    For NFL, NHL, and MLB, we check the season type ID embedded in the event
    payload — "3" means postseason. For soccer, all games from our chosen
    endpoints are major competitions so we always return True.

    Parameters
    ----------
    event : Raw ESPN event dict from the scoreboard response.
    sport : Sport name — one of "nfl", "nhl", "mlb", "soccer".

    Returns
    -------
    bool — True if the event should be tracked.
    """
    if sport == "soccer":
        # All queried soccer URLs cover major knockout/group-stage tournaments.
        return True

    season = event.get("season", {})
    season_type = season.get("type", {})
    return str(season_type.get("id", "")) == _POSTSEASON_TYPE_ID


def _parse_event(event: dict, sport: str) -> dict:
    """
    Extract the fields we need from a single ESPN event object.

    Parameters
    ----------
    event : One element from the ESPN events array.
    sport : The sport this event belongs to ("nfl", "nhl", "mlb", "soccer").

    Returns
    -------
    dict with keys:
        game_id        – ESPN event ID (str)
        sport          – sport name (str)
        home_team      – home team display name (str)
        away_team      – away team display name (str)
        home_score     – current home score (int, default 0)
        away_score     – current away score (int, default 0)
        status_name    – ESPN status type name, e.g. "STATUS_IN_PROGRESS" (str)
        status_detail  – short human-readable status, e.g. "Q2 5:30" (str)
        scoring_plays  – list of scoring play dicts (list[dict])
    """
    game_id = event.get("id", "")

    # Competitors are nested under competitions[0].competitors.
    competitions = event.get("competitions", [{}])
    competition = competitions[0] if competitions else {}
    competitors = competition.get("competitors", [])

    home_team = "Home"
    away_team = "Away"
    home_score = 0
    away_score = 0
    # Penalty shootout scores — only present when ESPN reports a shootout.
    # None means no shootout data available (regulation or AET finish).
    home_penalty_score: int | None = None
    away_penalty_score: int | None = None

    for comp in competitors:
        name = comp.get("team", {}).get("displayName", "Unknown")
        try:
            score = int(comp.get("score") or 0)
        except (ValueError, TypeError):
            score = 0

        # ESPN reports the penalty shootout score separately as "shootoutScore".
        raw_pen = comp.get("shootoutScore")
        pen_score: int | None = None
        if raw_pen is not None:
            try:
                pen_score = int(raw_pen)
            except (ValueError, TypeError):
                pass

        if comp.get("homeAway") == "home":
            home_team = name
            home_score = score
            home_penalty_score = pen_score
        elif comp.get("homeAway") == "away":
            away_team = name
            away_score = score
            away_penalty_score = pen_score

    status_obj = event.get("status", {})
    status_type = status_obj.get("type", {})
    status_name = status_type.get("name", "")
    status_detail = status_type.get("shortDetail", "")

    # displayClock is the human-readable game clock at the top level of the
    # status object (not inside status.type). For soccer ESPN uses a count-up
    # clock so this shows elapsed minutes, e.g. "74:52" or "90:00" at full
    # time. For NFL/NHL/MLB it is a countdown to zero ("2:34" remaining in Q3).
    # period is the period/half/quarter/inning number.
    display_clock: str = status_obj.get("displayClock", "")
    period: int = status_obj.get("period", 0)

    # The `details` array contains ALL competition events — goals, yellow/red
    # cards, substitutions, etc. ESPN tags genuine scoring plays with
    # scoringPlay=True. We filter on that flag so cards and subs don't trigger
    # score alerts. The original enumeration index i is preserved (not reset
    # after filtering) so last_play_index in the DB remains valid across polls.
    raw_details = competition.get("details", [])
    scoring_plays: list[dict] = []
    for i, detail in enumerate(raw_details):
        # Skip non-scoring events. If the flag is absent (older API responses
        # or American sports where details may be scoring-only), include it.
        if not detail.get("scoringPlay", True):
            continue
        athletes = detail.get("athletesInvolved", [])
        scorer = athletes[0].get("displayName", "") if athletes else ""
        team = detail.get("team", {}).get("displayName", "")
        play_type = detail.get("type", {}).get("text", "")
        clock = detail.get("clock", {}).get("displayValue", "")
        scoring_plays.append({
            "index": i,
            "scorer": scorer,
            "team": team,
            "type": play_type,
            "clock": clock,
        })

    return {
        "game_id": game_id,
        "sport": sport,
        "home_team": home_team,
        "away_team": away_team,
        "home_score": home_score,
        "away_score": away_score,
        "home_penalty_score": home_penalty_score,
        "away_penalty_score": away_penalty_score,
        "status_name": status_name,
        "status_detail": status_detail,
        "display_clock": display_clock,
        "period": period,
        "scoring_plays": scoring_plays,
    }


def get_live_playoff_games(sport: str) -> list[dict]:
    """
    Return all currently in-progress or just-finished playoff games for a sport.

    Queries every endpoint associated with the sport (soccer has four), filters
    to playoff events only, and returns only events with a trackable status
    (active or final). De-duplicates across endpoints by game ID.

    Parameters
    ----------
    sport : One of "nfl", "nhl", "mlb", "soccer".

    Returns
    -------
    list[dict]
        Game dicts as returned by _parse_event(), filtered to trackable
        statuses only. Empty list when no playoff games are live.

    Raises
    ------
    SportsAPIError
        If any HTTP request to the ESPN API fails. The caller
        (get_all_live_playoff_games) catches this per-sport so other sports
        are unaffected.
    """
    endpoints = _SPORT_ENDPOINTS.get(sport, [])
    games: list[dict] = []
    seen_ids: set[str] = set()

    for url in endpoints:
        data = _fetch_scoreboard(url)
        for event in data.get("events", []):
            if not _is_playoff(event, sport):
                continue

            parsed = _parse_event(event, sport)

            if parsed["status_name"] not in _TRACKABLE_STATUSES:
                continue

            # De-duplicate in case the same match appears on multiple endpoints.
            if parsed["game_id"] in seen_ids:
                continue

            seen_ids.add(parsed["game_id"])
            games.append(parsed)

    return games


def get_all_live_playoff_games(sports: list[str]) -> dict[str, list[dict]]:
    """
    Fetch live playoff games for all specified sports in a single call.

    Errors from any single sport are caught and logged so a temporary ESPN
    outage for one sport does not prevent alerts for others.

    Parameters
    ----------
    sports : List of sport names to check, e.g. ["nhl", "soccer"].

    Returns
    -------
    dict mapping sport name → list of game dicts. A sport is omitted from
    the dict if it has no live games (or errored — check logs to distinguish).
    """
    results: dict[str, list[dict]] = {}

    for sport in sports:
        try:
            games = get_live_playoff_games(sport)
            if games:
                results[sport] = games
                logger.debug(
                    "Found %d live %s playoff game(s)", len(games), sport
                )
        except SportsAPIError as exc:
            # Log and continue — one sport failing doesn't block the others.
            logger.warning("ESPN API error for sport %r: %s", sport, exc)

    return results
