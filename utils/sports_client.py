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

get_live_nfl_games()                → list[dict]
    NFL-specific variant that also covers the regular season: returns every
    live/just-finished playoff game (same as get_live_playoff_games("nfl"))
    plus, during the regular season, the single game selected for "today"
    (Eastern time) — the day's only game, or the latest-kickoff ("primetime")
    game when several are on. Fetches the NFL scoreboard once rather than
    querying it twice, since it's polled every 15 seconds.

get_all_live_playoff_games(sports)  → dict[str, list[dict]]
    Call get_live_playoff_games for each sport, suppressing per-sport
    errors so one failing endpoint doesn't block the others.
"""

import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger(__name__)

_EASTERN = ZoneInfo("America/New_York")

_BASE_URL = "https://site.api.espn.com/apis/site/v2/sports"
_REQUEST_TIMEOUT = 15

# Per-game play-by-play/boxscore endpoint, used only for NFL — see
# get_nfl_scoring_plays() for why.
_NFL_SUMMARY_URL = f"{_BASE_URL}/football/nfl/summary"

# Every NFL scoring play's `text` field observed in practice (rushing,
# passing, field goal, interception/fumble return TDs) starts with the
# scorer's full name followed by " {yards} Yd ", e.g. "Kyren Williams 5 Yd
# Rush (...)" or "Demarcus Robinson 39 Yd pass from Brock Purdy (...)" (the
# receiver, not the passer, since they're the one who scored). Matched
# non-greedily so a name containing a number-like token doesn't overrun.
# The yardage is captured too so field goal distance can be shown.
_SCORER_NAME_RE = re.compile(r"^(?P<name>.+?) (?P<yards>\d+) Yd ")

# ESPN bundles the PAT/2-point-conversion attempt into the TOUCHDOWN play's
# own text as a trailing parenthetical rather than exposing it as its own
# scoringPlays entry, e.g. "...(Harrison Butker Kick)" or "...(Carson Wentz
# Pass to Justin Jefferson for Two-Point Conversion)". This regex pulls out
# that trailing "(...)" (tolerating a stray "." after the closing paren, seen
# in at least one real play) so it can be classified separately below.
_TRAILING_PAREN_RE = re.compile(r"\(([^()]+)\)\.?\s*$")

# Made PAT: "{Kicker} Kick". Deliberately does NOT match "{Kicker} PAT Failed"
# or "{Kicker} PAT blocked" — those are missed attempts, worth zero fantasy
# points, and are skipped entirely rather than announced (see
# get_nfl_scoring_plays()).
_PAT_MADE_RE = re.compile(r"^(?P<kicker>.+?) Kick$")

# Made 2-point conversion via a pass: "{Passer} Pass to {Receiver} for
# Two-Point Conversion". Fantasy scoring credits the receiver, not the
# passer, same convention as a passing touchdown.
_TWO_POINT_PASS_RE = re.compile(r"^.+? Pass to (?P<receiver>.+?) for Two-Point Conversion$")

# Made 2-point conversion via a rush: "{Runner} Run for Two-Point
# Conversion". Not observed in the data used to build this parser (only pass
# conversions occurred), but included on the same convention as the pass
# case; failing to match just means the conversion isn't separately
# announced, not that anything crashes.
_TWO_POINT_RUSH_RE = re.compile(r"^(?P<runner>.+?) (?:Run|Rush) for Two-Point Conversion$")

# ESPN season type IDs for NFL, NHL, and MLB: "2" is the regular season,
# "3" is postseason / playoffs.
_REGULAR_SEASON_TYPE_ID = "2"
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
#
# These names are used for cosmetic / secondary decisions only (which embed
# title to use, whether to announce "Game Starting", whether to run the
# penalty-tally reconstruction) — NOT to decide whether a game is trackable.
# That decision used to be based on membership in this list, and on
# 2026-07-11 that caused a real bug: this set listed "STATUS_EXTRA_TIME" for
# soccer extra time, but ESPN's actual status name is "STATUS_OVERTIME".
# The wrong name meant a game entering extra time fell out of every status
# set below, get_live_playoff_games() silently dropped it from the feed, and
# the poller's "disappeared from the feed" fallback posted a final with the
# frozen regulation score and stopped tracking — mid-match. See
# get_live_playoff_games() for the fix: trackability is now based on ESPN's
# status.type.state field ("pre"/"in"/"post"), a much smaller and more
# stable surface than enumerating every status name ESPN might use.
#
# STATUS_FULL_TIME is intentionally in ACTIVE_STATUSES rather than
# FINAL_STATUSES. ESPN sets it at the end of 90 minutes as a transitional
# state before extra time begins — treating it as final would cause the bot
# to post a premature "draw" embed and delete the row, then re-announce the
# game as starting when STATUS_OVERTIME appears. By keeping it active, the
# bot holds off until ESPN either moves to STATUS_OVERTIME (game continues),
# STATUS_FINAL (true regulation end), or the completed flag confirms the
# match is truly over (see cogs/sports_scores.py's full-time grace period).
ACTIVE_STATUSES: frozenset[str] = frozenset({
    "STATUS_IN_PROGRESS",
    "STATUS_FIRST_HALF",
    "STATUS_SECOND_HALF",
    "STATUS_HALFTIME",
    "STATUS_END_PERIOD",
    "STATUS_FULL_TIME",    # end of 90 min — may still go to extra time
    "STATUS_OVERTIME",     # extra time in progress (confirmed via live API response)
    "STATUS_SHOOTOUT",     # penalty shootout in progress — unconfirmed name, see below
})

# ESPN status type names for games that have truly concluded.
FINAL_STATUSES: frozenset[str] = frozenset({
    "STATUS_FINAL",
    "STATUS_FINAL_OT",
    "STATUS_FINAL_AET",    # after extra time (soccer)
    "STATUS_FINAL_PEN",    # after penalties (soccer)
})


class SportsAPIError(Exception):
    """Raised when a request to the ESPN scoreboard API fails."""
    pass


def _fetch_scoreboard(url: str, params: dict | None = None) -> dict:
    """
    Fetch a single ESPN endpoint and return the parsed JSON body.

    Despite the name (most callers hit a scoreboard endpoint), this is also
    used by get_nfl_scoring_plays() to fetch ESPN's per-game summary endpoint,
    which takes an `event` query param — hence the optional params argument.

    Parameters
    ----------
    url    : Full endpoint URL.
    params : Optional query parameters.

    Raises
    ------
    SportsAPIError
        If the HTTP request fails or returns a non-2xx status code.
    """
    try:
        response = requests.get(url, params=params, timeout=_REQUEST_TIMEOUT)
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

    # ESPN represents season.type as a bare int (2 = regular season,
    # 3 = postseason, etc.), NOT as a nested {"id": ...} object — that's
    # status.type further down in the payload, which does use nested dicts,
    # making it easy to assume season.type follows the same shape. It
    # doesn't. Calling .get("id", ...) on an int raised AttributeError on
    # every call for NFL/NHL/MLB (soccer never hits this code path — it
    # returns True above), which cogs/sports_scores.py's broad
    # "except Exception" around the ESPN fetch swallowed into a WARNING log
    # line every poll, with no other visible symptom.
    season = event.get("season", {})
    return str(season.get("type", "")) == _POSTSEASON_TYPE_ID


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
        game_id            – ESPN event ID (str)
        sport              – sport name (str)
        home_team          – home team display name (str)
        away_team          – away team display name (str)
        home_score         – current home score (int, default 0)
        away_score         – current away score (int, default 0)
        home_penalty_score – penalty shootout score for home team (int | None)
        away_penalty_score – penalty shootout score for away team (int | None)
        status_name        – ESPN status type name, e.g. "STATUS_IN_PROGRESS" (str)
        status_detail      – short human-readable status, e.g. "Q2 5:30" (str)
        state              – ESPN's coarse lifecycle field: "pre", "in", or
                              "post" (str). Used by get_live_playoff_games()
                              to decide trackability instead of matching
                              status_name against an enumerated list — see
                              the comment above ACTIVE_STATUSES for why.
        completed          – True when ESPN marks the event as finished (bool)
        display_clock      – human-readable game clock, e.g. "74:52" (str)
        period             – period/half/quarter number (int)
        home_logo          – URL to the home team's logo image, or "" (str)
        away_logo          – URL to the away team's logo image, or "" (str)
        scoring_plays      – list of scoring play dicts (list[dict])
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
    home_logo = ""
    away_logo = ""
    # Penalty shootout scores — only present when ESPN reports a shootout.
    # None means no shootout data available (regulation or AET finish).
    home_penalty_score: int | None = None
    away_penalty_score: int | None = None

    for comp in competitors:
        team = comp.get("team", {})
        name = team.get("displayName", "Unknown")
        logo = team.get("logo", "")
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
            home_logo = logo
        elif comp.get("homeAway") == "away":
            away_team = name
            away_score = score
            away_penalty_score = pen_score
            away_logo = logo

    status_obj = event.get("status", {})
    status_type = status_obj.get("type", {})
    status_name = status_type.get("name", "")
    status_detail = status_type.get("shortDetail", "")
    state = status_type.get("state", "")

    # completed is ESPN's authoritative flag indicating the game is fully over.
    # It can be True even when status_name is still a transitional active value
    # (e.g. STATUS_FULL_TIME right after regulation before ESPN pushes FINAL).
    # We surface it so the poller can skip re-tracking genuinely finished games
    # whose tracking rows were already deleted.
    completed: bool = bool(status_type.get("completed", False))

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
        "state": state,
        "completed": completed,
        "display_clock": display_clock,
        "period": period,
        "home_logo": home_logo,
        "away_logo": away_logo,
        "scoring_plays": scoring_plays,
    }


def get_live_playoff_games(sport: str) -> list[dict]:
    """
    Return all currently in-progress or just-finished playoff games for a sport.

    Queries every endpoint associated with the sport (soccer has four), filters
    to playoff events only, and returns only events that have started (ESPN's
    status.type.state is "in" or "post" — i.e. not "pre"). De-duplicates
    across endpoints by game ID.

    Trackability is intentionally based on the `state` field rather than
    matching status_name against ACTIVE_STATUSES/FINAL_STATUSES. Those lists
    have to enumerate every status name ESPN might use, and getting one wrong
    silently drops the game from the feed — which is exactly what happened on
    2026-07-11 ("STATUS_EXTRA_TIME" vs the real "STATUS_OVERTIME"): the game
    disappeared from the feed the moment extra time began, and the poller's
    disappeared-from-feed fallback posted a final with the frozen regulation
    score. `state` only has three possible values, so it can't go stale the
    same way.

    Parameters
    ----------
    sport : One of "nfl", "nhl", "mlb", "soccer".

    Returns
    -------
    list[dict]
        Game dicts as returned by _parse_event(), filtered to games that have
        started. Empty list when no playoff games are live.

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

            # "pre" means the game hasn't kicked off yet — not trackable.
            # Anything else ("in" or "post") is live or just concluded.
            if parsed["state"] == "pre":
                continue

            # De-duplicate in case the same match appears on multiple endpoints.
            if parsed["game_id"] in seen_ids:
                continue

            # Every game returned by this function passed the _is_playoff
            # check above, so it's always a playoff/tournament game. Recorded
            # on the dict so embed builders can label it correctly instead of
            # assuming every tracked game is a playoff game (NFL regular
            # season games flow through get_live_nfl_games() instead, and are
            # marked is_playoff=False there).
            parsed["is_playoff"] = True

            seen_ids.add(parsed["game_id"])
            games.append(parsed)

    return games


def get_live_nfl_games() -> list[dict]:
    """
    Return the NFL games that should currently be tracked: every live or
    just-finished playoff game, plus — during the regular season — the one
    game selected for "today" (Eastern time).

    Regular season selection: unlike playoffs (where every concurrent game is
    tracked), only one regular-season game is tracked per day. If today has
    exactly one NFL regular-season game, that's the target. If it has more
    than one (Sunday's slate, a late-season Saturday tripleheader, or the
    rare international Wed/Thu/Mon doubleheader), the target is whichever has
    the latest scheduled kickoff — the "primetime" game.

    The target is chosen from the full day's schedule, including games that
    haven't kicked off yet, so an early game going live first is never
    mistaken for the target just because it started sooner. Once chosen, it's
    only included in the result once it has actually started (state != "pre")
    — same contract as get_live_playoff_games().

    Fetches the NFL scoreboard exactly once — playoff and regular-season
    events share the same endpoint response, so this avoids the extra ESPN
    request that calling get_live_playoff_games("nfl") and a separate
    regular-season lookup back to back would cost every 15-second poll.

    Returns
    -------
    list[dict]
        Game dicts as returned by _parse_event(), each with an added
        "is_playoff" bool. Includes every live/finished playoff game plus,
        at most, one regular-season game.

    Raises
    ------
    SportsAPIError
        If the request to the ESPN API fails.
    """
    data = _fetch_scoreboard(_SPORT_ENDPOINTS["nfl"][0])
    today = datetime.now(_EASTERN).date()

    games: list[dict] = []
    seen_ids: set[str] = set()
    regular_season_candidates: list[tuple[datetime, dict]] = []

    for event in data.get("events", []):
        # season.type is a bare int (2 = regular season, 3 = postseason), not
        # a nested {"id": ...} object — see the comment in _is_playoff().
        type_id = str(event.get("season", {}).get("type", ""))

        if type_id == _POSTSEASON_TYPE_ID:
            parsed = _parse_event(event, "nfl")
            if parsed["state"] == "pre" or parsed["game_id"] in seen_ids:
                continue
            parsed["is_playoff"] = True
            seen_ids.add(parsed["game_id"])
            games.append(parsed)

        elif type_id == _REGULAR_SEASON_TYPE_ID:
            # Collect every regular-season game scheduled for today (any
            # state) so the primetime pick considers games that haven't
            # kicked off yet, not just ones already live.
            raw_date = event.get("date", "")
            try:
                start_time = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
            except ValueError:
                continue
            if start_time.astimezone(_EASTERN).date() == today:
                regular_season_candidates.append((start_time, event))

    if regular_season_candidates:
        # Latest kickoff wins — the only candidate when there's just one
        # game today, the primetime game when there are several.
        _, target_event = max(regular_season_candidates, key=lambda pair: pair[0])
        parsed = _parse_event(target_event, "nfl")
        if parsed["state"] != "pre" and parsed["game_id"] not in seen_ids:
            parsed["is_playoff"] = False
            games.append(parsed)

    # NFL's scoreboard endpoint never populates competition.details (verified
    # empirically against live and finished games — it's always an empty
    # list for this sport, unlike soccer/hockey), so _parse_event() above
    # always leaves scoring_plays as []. Fetch the richer, player-attributed
    # version from the summary endpoint for each game we're actually
    # tracking (at most one regular-season game plus whatever's in the
    # playoffs, so this is a small, bounded number of extra requests).
    for game in games:
        try:
            game["scoring_plays"] = get_nfl_scoring_plays(game["game_id"])
        except SportsAPIError as exc:
            logger.warning(
                "Failed to fetch NFL play-by-play for game %s: %s",
                game["game_id"], exc,
            )

    return games


def get_nfl_scoring_plays(game_id: str) -> list[dict]:
    """
    Fetch player-attributed scoring plays for one NFL game from ESPN's
    per-game summary endpoint.

    The main /scoreboard endpoint (used for every other sport, and for
    everything else about an NFL game — score, status, clock) never
    populates competition.details for NFL, so it can't tell us WHO scored.
    /summary?event={id} does: its top-level scoringPlays array has a
    consistently formatted `text` field across every scoring type observed
    (rushing/passing/return TDs, field goals) — always
    "{Scorer Full Name} {yards} Yd {action}...". We parse the scorer's name
    out of that prefix (see _SCORER_NAME_RE) since scoringPlays entries don't
    carry a structured athlete field of their own.

    Player IDs and headshot images come from a second part of the same
    response: boxscore.players lists every athlete who recorded a stat in
    the game (across both teams and all stat categories — passing, rushing,
    receiving, defensive, interceptions, kick/punt returns, kicking,
    punting), each with a display name, ESPN's universal athlete ID (the
    same ID space used elsewhere, e.g. Fantasy rosters), and a headshot CDN
    URL. We build a name -> (id, headshot) map from that once per call and
    match the parsed scorer name against it — no extra API call needed for
    the photo.

    Made PATs and made 2-point conversions are announced as their own
    entries, since fantasy leagues award points for both and ESPN doesn't
    expose them as their own scoringPlays entries — it bundles the attempt
    into a trailing parenthetical on the touchdown play's own text (e.g.
    "...(Harrison Butker Kick)" or "...(Carson Wentz Pass to Justin
    Jefferson for Two-Point Conversion)"). We parse that suffix and, only on
    a made attempt, synthesize a second play entry immediately after the
    touchdown's — see _PAT_MADE_RE / _TWO_POINT_PASS_RE / _TWO_POINT_RUSH_RE.
    Missed/blocked PATs and failed 2-point conversions score zero fantasy
    points, so they're deliberately NOT announced — the suffix simply fails
    to match any of those patterns and nothing extra is added. Field goals
    never carry this suffix (there's no PAT after a field goal) and are
    unaffected.

    A parse or match failure just leaves the affected field(s) empty/None
    rather than raising — callers already treat a missing scorer name as "no
    attribution available" (see cogs/sports_scores.py), so this degrades
    safely instead of breaking the whole poll over one oddly-worded play.

    Parameters
    ----------
    game_id : ESPN's event ID string, e.g. "401872657".

    Returns
    -------
    list[dict], one entry per scoring play (including synthesized PAT/2-point
    entries), in chronological order:
        index          : position in this list. Used the same way as the
                         raw details-array index elsewhere in this module —
                         to detect which plays are new since the last poll.
                         Recomputed fresh every call, but stable across polls
                         because the same historical plays always parse to
                         the same number of entries in the same order.
        scorer         : player's full name, or "" if the text didn't match
                         the expected pattern.
        espn_player_id : int | None — set only when the parsed name matched
                         an athlete in this game's boxscore.
        headshot_url   : str | None — set under the same condition.
        team           : scoring team's display name.
        type           : short play-type label, e.g. "Passing Touchdown",
                         "Field Goal Good", or (synthesized) "Point After
                         Touchdown" / "Two-Point Conversion".
        clock          : game clock at the time of the play, e.g. "4:28".
        period         : quarter number the play happened in (1-4, 5+ for
                         OT). Synthesized PAT/2-point entries inherit the
                         parent touchdown's period.
        yards          : int | None — yardage parsed from the play text
                         (e.g. field goal distance). None for synthesized
                         PAT/2-point entries, which have no yardage of
                         their own.

    Raises
    ------
    SportsAPIError
        If the request to ESPN fails.
    """
    data = _fetch_scoreboard(_NFL_SUMMARY_URL, params={"event": game_id})

    athlete_by_name: dict[str, tuple[int, str | None]] = {}
    for team_block in data.get("boxscore", {}).get("players", []):
        for category in team_block.get("statistics", []):
            for entry in category.get("athletes", []):
                athlete = entry.get("athlete", {})
                name = athlete.get("displayName") or athlete.get("fullName")
                athlete_id = athlete.get("id")
                if not name or athlete_id is None:
                    continue
                headshot_url = athlete.get("headshot", {}).get("href")
                athlete_by_name[name] = (int(athlete_id), headshot_url)

    def _make_play(scorer: str, play_type: str, team: str, clock: str, period: int, yards: int | None) -> dict:
        athlete_id, headshot_url = athlete_by_name.get(scorer, (None, None))
        return {
            "scorer": scorer,
            "espn_player_id": athlete_id,
            "headshot_url": headshot_url,
            "team": team,
            "type": play_type,
            "clock": clock,
            "period": period,
            "yards": yards,
        }

    plays: list[dict] = []
    for sp in data.get("scoringPlays", []):
        text = sp.get("text", "")
        team = sp.get("team", {}).get("displayName", "")
        clock = sp.get("clock", {}).get("displayValue", "")
        period = sp.get("period", {}).get("number", 0)

        match = _SCORER_NAME_RE.match(text)
        scorer = match.group("name") if match else ""
        yards = int(match.group("yards")) if match else None

        plays.append(_make_play(scorer, sp.get("type", {}).get("text", ""), team, clock, period, yards))

        # Check the trailing "(...)" for a made PAT or 2-point conversion to
        # announce as its own entry. Anything that doesn't match one of
        # these (missed/blocked kicks, failed conversions, or no suffix at
        # all, e.g. field goals) is silently skipped — see docstring.
        suffix_match = _TRAILING_PAREN_RE.search(text)
        if not suffix_match:
            continue
        suffix = suffix_match.group(1).strip()

        pat_match = _PAT_MADE_RE.match(suffix)
        two_point_pass_match = _TWO_POINT_PASS_RE.match(suffix)
        two_point_rush_match = _TWO_POINT_RUSH_RE.match(suffix)

        # PAT/2-point attempts happen on the very next snap after the
        # touchdown, so they share its period — there's no separate period
        # value for them in ESPN's data (they're not their own scoringPlays
        # entry at all, see docstring).
        if pat_match:
            plays.append(_make_play(pat_match.group("kicker"), "Point After Touchdown", team, clock, period, None))
        elif two_point_pass_match:
            plays.append(_make_play(two_point_pass_match.group("receiver"), "Two-Point Conversion", team, clock, period, None))
        elif two_point_rush_match:
            plays.append(_make_play(two_point_rush_match.group("runner"), "Two-Point Conversion", team, clock, period, None))

    for index, play in enumerate(plays):
        play["index"] = index

    return plays


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
