"""
utils/espn_fantasy_client.py
─────────────────────────────
Client for ESPN's Fantasy Football API (v3).

This API is undocumented — its shape is inferred from what fantasy.espn.com's
own web app calls, and is widely reverse-engineered by the fantasy football
community. It can change without notice; get_league_rosters() raises
FantasyAPIError with enough detail (status code, response snippet, or which
field was missing) to diagnose a schema change if one happens.

Private leagues (the common case for home/friend leagues) require two session
cookies from a logged-in fantasy.espn.com browser session: espn_s2 and SWID.
These are opaque, ESPN-issued tokens with no officially published expiry —
observed behavior suggests they last roughly 1-2 months, but that's not an
ESPN-documented figure and could shift. When they do stop working, every
request returns 401 AUTH_LEAGUE_NOT_VISIBLE regardless of the league's
actual privacy setting. See cogs/fantasy.py for how the bot detects and
surfaces that.

Public function
---------------
get_league_rosters(league_id, season, espn_s2, swid) -> list[dict]
    Return one entry per rostered player: espn_player_id, player_name,
    manager_name.
"""

import logging

import requests

logger = logging.getLogger(__name__)

_BASE_URL = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons"
_REQUEST_TIMEOUT = 15

# ESPN's edge blocks requests with no browser-like User-Agent (seen on the
# site API too — see utils/sports_client.py's history), so we send one.
_USER_AGENT = "Mozilla/5.0 (compatible; discord-bot-fantasy-sync/1.0)"


class FantasyAPIError(Exception):
    """Raised when the ESPN Fantasy API request fails or returns unexpected data."""
    pass


class FantasyAuthError(FantasyAPIError):
    """
    Raised specifically on a 401 response — almost always means the stored
    espn_s2/SWID cookies have expired and need to be replaced via
    /fantasy config cookies. Split out from the base FantasyAPIError so the
    caller can post a "your cookies expired" alert instead of a generic
    failure warning.
    """
    pass


def get_league_rosters(league_id: int, season: int, espn_s2: str, swid: str) -> list[dict]:
    """
    Fetch every rostered player in a private ESPN fantasy football league,
    along with which manager's team owns them.

    Parameters
    ----------
    league_id : The numeric leagueId from the league's fantasy.espn.com URL.
    season    : NFL season year (e.g. 2026 for the 2026-27 season).
    espn_s2   : Session cookie value from a logged-in fantasy.espn.com browser.
    swid      : Session cookie value (including the surrounding curly braces).

    Returns
    -------
    list[dict] — one entry per rostered player:
        espn_player_id : int — ESPN's universal athlete ID, shared with the
                                site API used elsewhere in this bot (see
                                utils.sports_client.get_nfl_scoring_plays).
        player_name    : str — player's full name, as ESPN's fantasy API
                                reports it.
        manager_name   : str — display name of the team's first listed
                                owner. Co-owned teams only surface the first.
        team_name      : str — the fantasy team's own (manager-chosen) name,
                                e.g. "The Gridiron Gang". This is what
                                cogs/sports_scores.py calls out in scoring
                                alerts, not manager_name.

    Raises
    ------
    FantasyAuthError
        On a 401 response — the stored cookies have expired.
    FantasyAPIError
        On any other request failure, or if the response is missing fields
        this function depends on (schema drift in ESPN's undocumented API).
    """
    url = f"{_BASE_URL}/{season}/segments/0/leagues/{league_id}"
    try:
        response = requests.get(
            url,
            params=[("view", "mRoster"), ("view", "mTeam")],
            cookies={"espn_s2": espn_s2, "SWID": swid},
            headers={"User-Agent": _USER_AGENT},
            timeout=_REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise FantasyAPIError(f"ESPN Fantasy request failed: {exc}") from exc

    if response.status_code == 401:
        raise FantasyAuthError(
            "ESPN rejected the stored espn_s2/SWID cookies (401) — they've "
            "likely expired and need to be replaced via /fantasy config cookies."
        )
    if not response.ok:
        raise FantasyAPIError(
            f"ESPN Fantasy API returned {response.status_code}: {response.text[:300]}"
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise FantasyAPIError(f"ESPN Fantasy API returned non-JSON response: {exc}") from exc

    # members[] holds manager identities, keyed by a GUID that matches the
    # GUID(s) in each team's "owners" list below.
    manager_by_guid: dict[str, str] = {}
    for member in data.get("members", []):
        guid = member.get("id", "")
        if guid:
            manager_by_guid[guid] = member.get("displayName", "Unknown Manager")

    teams = data.get("teams", [])
    if not teams:
        raise FantasyAPIError(
            "ESPN Fantasy API response had no 'teams' — league_id/season may "
            "be wrong, or ESPN changed their response shape."
        )

    rosters: list[dict] = []
    for team in teams:
        owners = team.get("owners", [])
        manager_name = (
            manager_by_guid.get(owners[0], "Unknown Manager") if owners else "Unknown Manager"
        )

        # ESPN has represented the team's own (manager-chosen) name a couple
        # of different ways across API versions: a single "name" field in
        # newer responses, or split "location" + "nickname" fields in older
        # ones. Try both — this hasn't been verified against a live league
        # yet (needs real cookies to test), so if this comes back wrong or
        # empty, check what the actual team object looks like and adjust.
        team_name = team.get("name")
        if not team_name:
            team_name = f"{team.get('location', '')} {team.get('nickname', '')}".strip()
        if not team_name:
            team_name = "Unknown Team"

        for entry in team.get("roster", {}).get("entries", []):
            player = entry.get("playerPoolEntry", {}).get("player", {})
            player_id = player.get("id")
            player_name = player.get("fullName")
            if player_id is None or not player_name:
                continue
            rosters.append({
                "espn_player_id": int(player_id),
                "player_name": player_name,
                "manager_name": manager_name,
                "team_name": team_name,
            })

    return rosters
