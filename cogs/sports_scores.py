"""
cogs/sports_scores.py
─────────────────────
Live sports score updates cog. Polls the ESPN public API on a per-sport
cadence and posts Discord embeds for game starts, scoring plays, and final
scores in the configured channel. NHL, MLB, and soccer are playoff/tournament
-only; NFL also covers the regular season — see get_live_nfl_games() in
utils/sports_client.py for the one-game-per-day selection rule.

How it works
────────────
1. On bot connect (on_ready), registers one APScheduler IntervalJob per sport
   per guild. NFL polls every 15 seconds (to catch TD + PAT as separate events);
   NHL, MLB, and soccer poll every 1 minute. All jobs are independent so one
   sport's cadence doesn't affect others.
2. Each poll is a no-op when the feature is disabled, the sport is toggled off,
   or no games are live — off-season overhead is negligible (one DB read).
3. The live_game_states table tracks per-game state (scores, last play reported,
   start-announced flag) so alerts are correct across bot restarts and polls.
4. Each sport (NFL, NHL, MLB, soccer) can be toggled independently.

Slash commands
──────────────
/scores status                        — Show current config (anyone)
/scores config channel <#ch>          — Set the alert channel (admin)
/scores config sports [nfl] [nhl] [mlb] [soccer]
                                      — Toggle sports on or off (admin)
/scores config enable                 — Enable the feature (anyone)
/scores config disable                — Disable the feature (anyone)

Default: enabled, all four sports, channel name "sports-updates".
"""

import logging
from datetime import datetime, timezone

import discord
from apscheduler.triggers.interval import IntervalTrigger
from discord import app_commands
from discord.ext import commands
from sqlalchemy.exc import IntegrityError

from database.db import SessionLocal
from database.models import FantasyRosterEntry, LiveGameState, ScheduleConfig
from utils.sports_client import (
    ACTIVE_STATUSES,
    FINAL_STATUSES,
    get_live_nfl_games,
    get_live_playoff_games,
)

logger = logging.getLogger(__name__)

# ── Defaults ──────────────────────────────────────────────────────────────────
_DEFAULT_CHANNEL_NAME = "sports-updates"
_ALL_SPORTS = ["nfl", "nhl", "mlb", "soccer"]

# Per-sport polling intervals in seconds. NFL gets a short interval so that a
# touchdown and the following PAT can land in separate embeds — ESPN sometimes
# batches them together in the details array within a 20-40 second window.
_POLL_INTERVALS: dict[str, int] = {
    "nfl": 15,
    "nhl": 60,
    "mlb": 60,
    "soccer": 60,
}

# Sport-appropriate emoji for use in embed title strings only.
_SPORT_EMOJI = {
    "nfl": "🏈",
    "nhl": "🏒",
    "mlb": "⚾",
    "soccer": "⚽",
}

# Human-readable league/competition labels for embed footers.
_SPORT_LABELS = {
    "nfl": "NFL",
    "nhl": "NHL",
    "mlb": "MLB",
    "soccer": "Soccer",
}

def _format_period_label(sport: str, period: int) -> str:
    """
    Return a short human-readable period label for the time field in the
    final score embed, e.g. "Q4", "P3", "Half 2", "ET".

    Returns an empty string when the period number is unavailable (0) or
    when no label convention is defined for the sport.

    Parameters
    ----------
    sport  : One of "nfl", "nhl", "mlb", "soccer".
    period : ESPN's period number (1-based). Conventions differ by sport:
             NFL: 1-4 = Q1-Q4, 5 = OT
             NHL: 1-3 = P1-P3, 4 = OT, 5 = SO
             MLB: inning number (no useful label needed here)
             Soccer: 1 = first half, 2 = second half, 3-4 = extra time halves
    """
    if not period:
        return ""

    if sport == "nfl":
        if period <= 4:
            return f"Q{period}"
        return "OT"

    if sport == "nhl":
        if period <= 3:
            return f"P{period}"
        if period == 4:
            return "OT"
        return "SO"

    if sport == "soccer":
        if period == 1:
            return "1st Half"
        if period == 2:
            return "2nd Half"
        if period in (3, 4):
            return "ET"
        if period >= 5:
            return "Penalties"

    # MLB innings don't benefit from a short label alongside the clock.
    return ""


# Sports where every scoring play is worth exactly +1 point/goal.
# For these sports we can reconstruct accurate intermediate scores when
# multiple goals land in the same poll. NFL and MLB are excluded because
# point values vary per play type (TD=6, PAT=1, FG=3, home run=1-4 runs, etc.)
# and we can't reliably infer the increment from the details array alone.
_UNIT_SCORE_SPORTS: frozenset[str] = frozenset({"soccer", "nhl"})

# ESPN sets status.type.completed=True right at the 90-minute whistle for
# soccer knockout matches that are about to go to extra time — the flag
# reflects "this phase of the match is over", not "the whole match is over".
# Because of this we can't trust completed=True as an immediate final signal
# while the status is still STATUS_FULL_TIME: LiveGameState.pending_final_since
# tracks when we first saw the game stalled there, and we hold off treating
# it as done until that timestamp is this many seconds in the past — giving
# ESPN a chance to either push STATUS_EXTRA_TIME (game continues, the timer
# resets) or a genuine terminal status. If neither happens in time we trust
# the completed flag and post the final anyway — that's the scenario the
# completed-flag fallback exists for in the first place (a game that truly
# stalls at STATUS_FULL_TIME, e.g. a group-stage draw ESPN never bothers to
# push STATUS_FINAL for).
_FULL_TIME_GRACE_SECONDS = 120

# Statuses that indicate a game has just kicked off. Only these trigger the
# "Game Starting" embed when we first see a game with no DB row. All other
# active statuses (halftime, second half, full time, extra time, shootout)
# mean the game has clearly been going for a while — in those cases we
# silently create a tracking row without posting a start announcement.
# This prevents spurious "Game Starting" embeds after a bot restart or
# redeploy that lands mid-game, or when ESPN keeps a completed game in the
# feed at STATUS_FULL_TIME long after it ended.
_ANNOUNCE_START_STATUSES: frozenset[str] = frozenset({
    "STATUS_IN_PROGRESS",   # generic in-progress (NFL, NHL, MLB, early soccer)
    "STATUS_FIRST_HALF",    # soccer: explicitly the first half
})


def _season_footer(sport: str, game: dict) -> str:
    """
    Build the embed footer text, labeling whether the game is a playoff game
    or a regular-season game.

    game["is_playoff"] is set by utils.sports_client for every game dict
    freshly fetched from ESPN (True from get_live_playoff_games(), True or
    False from get_live_nfl_games()). It's absent on the synthetic dict
    _poll_scores builds from stored LiveGameState columns when a game
    disappears from the feed mid-poll — that dict only carries the columns
    LiveGameState persists, which doesn't include is_playoff. In that case
    we fall back to the bare sport label rather than guessing.
    """
    label = _SPORT_LABELS.get(sport, sport.upper())
    is_playoff = game.get("is_playoff")
    if is_playoff is True:
        return f"{label} · Playoff"
    if is_playoff is False:
        return f"{label} · Regular Season"
    return label


def _apply_team_branding(
    embed: discord.Embed, game: dict, thumbnail_override: str | None = None, compact: bool = False
) -> None:
    """
    Attach both teams' logos to an embed: the home team's logo, and either
    thumbnail_override (e.g. a scoring play's player headshot) or the away
    team's logo as the thumbnail (top-right).

    compact controls where the home logo goes, since a Discord embed only
    has one large-image slot and one thumbnail slot:
      - False (default; game start, score update, final score — posted a
        handful of times per game): home logo in the large embed image
        (bottom). Gives both teams comparable visual weight for these
        lower-frequency, more "event"-like embeds.
      - True (scoring plays — posted every time anyone scores, so several
        times a game): home logo in the small author icon (top-left)
        instead. set_image() always renders as a large banner in Discord
        regardless of the source image's actual resolution — fine
        occasionally, but overwhelming when it repeats on every touchdown,
        field goal, and PAT (reported against a real game on 2026-09-14).

    game["home_logo"]/["away_logo"] are set by utils.sports_client for every
    game dict freshly fetched from ESPN. They're absent on the synthetic dict
    _poll_scores builds from stored LiveGameState columns when a game
    disappears from the feed mid-poll — LiveGameState doesn't persist logo
    URLs, so this silently no-ops for whichever side lacks one, the same
    degrade-gracefully handling already used for that dict's missing
    is_playoff/period/display_clock fields.
    """
    home_logo = game.get("home_logo")
    away_logo = game.get("away_logo")

    if home_logo:
        if compact:
            embed.set_author(
                name=f"{game.get('away_team', '')} @ {game.get('home_team', '')}",
                icon_url=home_logo,
            )
        else:
            embed.set_image(url=home_logo)

    thumbnail_url = thumbnail_override or away_logo
    if thumbnail_url:
        embed.set_thumbnail(url=thumbnail_url)


def _article(phrase: str) -> str:
    """Return "an" if phrase starts with a vowel sound, else "a"."""
    return "an" if phrase[:1].upper() in "AEIOU" else "a"


def _job_id(guild_id: int, sport: str) -> str:
    """
    Return the stable APScheduler job ID for a guild + sport polling job.

    One job is registered per sport per guild so each sport can have its own
    independent polling interval (e.g. 15 s for NFL, 60 s for NHL/MLB/soccer).
    """
    return f"sports_scores_{sport}_{guild_id}"


class SportsScoresCog(commands.Cog, name="SportsScores"):
    """
    Cog that polls ESPN for live game updates and posts them to Discord.

    NHL, MLB, and soccer are tracked for playoff/tournament games only. NFL
    also tracks the regular season, one game per day (see get_live_nfl_games()
    in utils/sports_client.py).

    One APScheduler IntervalTrigger job is registered per sport per guild, each
    on its own cadence: NFL at 15-second intervals (to catch TD + PAT as separate
    events) and NHL, MLB, and soccer at 1-minute intervals.
    """

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ──────────────────────────────────────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Register one interval polling job per sport per guild on bot connect."""
        for guild in self.bot.guilds:
            await self._schedule_for_guild(guild.id)
        logger.info(
            "SportsScoresCog ready — polling jobs registered for %d guild(s)",
            len(self.bot.guilds),
        )

    async def _schedule_for_guild(self, guild_id: int) -> None:
        """
        Load (or create) this guild's sports scores config and register
        (or replace) one APScheduler IntervalTrigger job per sport.

        Each sport gets its own independent job so polling cadences can differ:
        NFL fires every 15 seconds; NHL, MLB, and soccer fire every 1 minute.
        All jobs are named sports_scores_{sport}_{guild_id} so they're stable
        across reconnects and can be individually replaced on config changes.

        The hour/minute/day_of_week columns in ScheduleConfig are placeholder
        values and are not used for scheduling — only channel_id and
        content_options matter for this feature.
        """
        with SessionLocal() as session:
            cfg = (
                session.query(ScheduleConfig)
                .filter_by(guild_id=guild_id, feature="sports_scores")
                .first()
            )

            if cfg is None:
                # First setup: create a default config row with all sports enabled.
                cfg = ScheduleConfig(
                    guild_id=guild_id,
                    feature="sports_scores",
                    hour=0,            # placeholder — interval jobs ignore this
                    minute=0,          # placeholder
                    timezone="America/New_York",
                    day_of_week=None,  # not applicable for interval jobs
                )
                cfg.content_options = {
                    "enabled": True,
                    "enabled_sports": list(_ALL_SPORTS),
                }
                session.add(cfg)
                session.commit()
                logger.info(
                    "Created default sports scores config for guild %d", guild_id
                )

        # Register one job per sport. Each job is independent so NFL can poll
        # every 15 seconds while the others run on a 1-minute cadence.
        # replace_existing=True makes each add_job call idempotent on reconnects.
        for sport in _ALL_SPORTS:
            interval_seconds = _POLL_INTERVALS[sport]
            self.bot.scheduler.add_job(
                self._poll_scores,
                IntervalTrigger(seconds=interval_seconds),
                id=_job_id(guild_id, sport),
                args=[guild_id, sport],
                replace_existing=True,
            )
            logger.debug(
                "Sports score job registered for guild %d sport %s (every %ds)",
                guild_id,
                sport,
                interval_seconds,
            )

    # ──────────────────────────────────────────────────────────────────────────
    # Polling logic
    # ──────────────────────────────────────────────────────────────────────────

    async def _poll_scores(self, guild_id: int, sport: str) -> None:
        """
        Main polling callback — called by APScheduler on a per-sport cadence.

        Checks ESPN for live games for a single sport (playoff-only for NHL/
        MLB/soccer; playoffs plus the selected regular-season game for NFL —
        see get_live_nfl_games()), compares against the stored live_game_states
        rows for this guild + sport, and posts embeds:
          - A game starting (first time we detect it as in_progress)
          - Each new scoring play since the previous poll
          - A final score when the game ends (STATUS_FINAL or disappears from feed)

        This method exits early when the feature is disabled or the given sport
        is not in enabled_sports, so off-season and disabled-sport resource usage
        is minimal — just one DB read per poll.

        Parameters
        ----------
        guild_id : ID of the Discord guild to post updates for.
        sport    : Sport to check — one of "nfl", "nhl", "mlb", "soccer".
        """
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            logger.warning(
                "Score polling fired but guild %d is not in bot cache — skipping",
                guild_id,
            )
            return

        # ── Load config ───────────────────────────────────────────────────────
        with SessionLocal() as session:
            cfg = (
                session.query(ScheduleConfig)
                .filter_by(guild_id=guild_id, feature="sports_scores")
                .first()
            )
            if cfg is None:
                return

            options = cfg.content_options
            channel_id = cfg.channel_id

        # Fast exit when the feature is disabled — avoids ESPN API calls.
        if not options.get("enabled", True):
            return

        # Fast exit when this specific sport is toggled off.
        enabled_sports: list[str] = options.get("enabled_sports", _ALL_SPORTS)
        if sport not in enabled_sports:
            return

        # Resolve the target channel. Log at DEBUG only — this fires frequently
        # and we don't want to spam logs if the channel isn't configured yet.
        channel = self._resolve_channel(guild, channel_id, _DEFAULT_CHANNEL_NAME)
        if channel is None:
            logger.debug(
                "Score polling: no channel found for guild %d — "
                "set one with /scores config channel",
                guild_id,
            )
            return

        # ── Fetch live games from ESPN for this sport ─────────────────────────
        # NFL uses get_live_nfl_games() instead of get_live_playoff_games(),
        # since it also covers the regular season (one game per day — see
        # that function's docstring for the selection rule). NHL, MLB, and
        # soccer remain playoff/tournament-only.
        try:
            if sport == "nfl":
                live_games_raw = get_live_nfl_games()
            else:
                live_games_raw = get_live_playoff_games(sport)
        except Exception as exc:
            logger.warning("ESPN API error for sport %r (guild %d): %s", sport, guild_id, exc)
            return

        # Build a list of game dicts and a set of visible IDs for the disappear check.
        live_games: list[dict] = live_games_raw
        live_game_ids: set[str] = {game["game_id"] for game in live_games}

        # ── Load existing DB state for this guild + sport ─────────────────────
        # Filter by sport so each sport's job only reads its own rows — this also
        # means the "disappeared from feed" check below stays sport-scoped.
        with SessionLocal() as session:
            rows = (
                session.query(LiveGameState)
                .filter_by(guild_id=guild_id, sport=sport)
                .all()
            )
            # Expunge so objects are accessible after the session closes.
            for row in rows:
                session.expunge(row)

        # Index by game_id for O(1) lookup.
        existing: dict[str, LiveGameState] = {row.game_id: row for row in rows}

        # ── Process each game currently visible in the ESPN feed ──────────────
        for game in live_games:
            game_id = game["game_id"]
            status_name = game["status_name"]
            is_active = status_name in ACTIVE_STATUSES
            is_final = status_name in FINAL_STATUSES

            if game_id not in existing:
                # First time we're seeing this game (or the tracking row was deleted).
                #
                # Guard: if ESPN marks the game as completed but we have no row, it
                # either ended between polls without us catching it (safe to skip — we
                # already handled the "disappeared from feed" path) or it's a game whose
                # row was manually deleted while ESPN still had it at STATUS_FULL_TIME
                # before transitioning to the true final status. Either way, do not
                # re-create a tracking row for a completed game we've already processed.
                if game.get("completed", False):
                    logger.debug(
                        "Skipping completed game %s (%s vs %s) — no tracking row, "
                        "ESPN completed=True",
                        game_id, game["away_team"], game["home_team"],
                    )

                elif is_active:
                    # Only announce "Game Starting" when ESPN shows an early-game
                    # status. Mid-game statuses (halftime, second half, full time,
                    # extra time, shootout) mean the game is already well underway —
                    # silently create the tracking row without the embed so we can
                    # still catch scoring plays and the final without spamming the
                    # channel. This also handles post-restart / post-redeploy polls
                    # where the bot has no DB rows but ESPN still shows active games.
                    if status_name in _ANNOUNCE_START_STATUSES:
                        await self._post_game_start(channel, game)

                    # Set last_play_index to the raw details-array index of the
                    # last scoring play already in the ESPN feed. Future polls
                    # only pick up plays whose raw index is strictly greater than
                    # this value, so goals that existed before we started tracking
                    # (e.g. we caught the game in the 2nd half) are never replayed.
                    #
                    # NOTE: do NOT use len(scoring_plays)-1 here. last_play_index
                    # is compared against raw details-array indices, not positions
                    # within the filtered scoring_plays list. Non-scoring events
                    # (yellow cards, substitutions) appear between goals in the
                    # raw array, so the last scoring play's raw index is always
                    # >= len(scoring_plays)-1 and often significantly higher.
                    # Using the count would leave old plays eligible for re-posting.
                    initial_play_idx = (
                        game["scoring_plays"][-1]["index"]
                        if game["scoring_plays"] else -1
                    )

                    with SessionLocal() as session:
                        try:
                            session.add(LiveGameState(
                                guild_id=guild_id,
                                game_id=game_id,
                                sport=sport,
                                home_team=game["home_team"],
                                away_team=game["away_team"],
                                home_score=game["home_score"],
                                away_score=game["away_score"],
                                status="in_progress",
                                start_announced=True,
                                last_play_index=initial_play_idx,
                            ))
                            session.commit()
                        except IntegrityError:
                            # A concurrent poll already inserted this row — safe to ignore.
                            session.rollback()

                elif is_final:
                    # We never saw this game start, so skip the belated final.
                    # Posting a "Final: 3-1" for a game the channel never knew
                    # about would be confusing.
                    logger.debug(
                        "Skipping late final for unseen game %s (%s vs %s)",
                        game_id, game["away_team"], game["home_team"],
                    )

            else:
                # We already have state for this game.
                row = existing[game_id]

                if row.status == "in_progress":
                    # Collect scoring plays that appeared after the last poll.
                    new_plays = [
                        p for p in game["scoring_plays"]
                        if p["index"] > row.last_play_index
                    ]

                    is_shootout = (status_name == "STATUS_SHOOTOUT")

                    if new_plays and is_shootout:
                        # During a penalty shootout each kick scores +1 in the
                        # penalty tally. ESPN keeps the regulation score (tied) in
                        # home_score/away_score and exposes the running penalty total
                        # as home_penalty_score/away_penalty_score. We use those for
                        # backwards reconstruction so each embed shows the correct
                        # "Penalties: X — Y" tally rather than the frozen tied score.
                        current_home_pen = game.get("home_penalty_score") or 0
                        current_away_pen = game.get("away_penalty_score") or 0
                        home_new = sum(
                            1 for p in new_plays
                            if p["team"].lower().strip()
                            == game["home_team"].lower().strip()
                        )
                        away_new = len(new_plays) - home_new
                        running_home = current_home_pen - home_new
                        running_away = current_away_pen - away_new

                        for play in new_plays:
                            if (play["team"].lower().strip()
                                    == game["home_team"].lower().strip()):
                                running_home += 1
                            else:
                                running_away += 1
                            await self._post_scoring_play(
                                channel, game, play, running_home, running_away,
                                is_penalty=True,
                            )

                    elif new_plays and game["sport"] in _UNIT_SCORE_SPORTS:
                        # For soccer and hockey each scoring play is +1, so we can
                        # reconstruct accurate intermediate scores by working backwards
                        # from the current ESPN score. This ensures the first of two
                        # goals that land in the same poll shows "1-0" not "2-0".
                        #
                        # We count how many of the new plays belong to each side,
                        # then subtract from the current ESPN score to find the
                        # pre-batch starting point. This stays correct even when ESPN
                        # details lagged by a poll, because the backwards calculation
                        # is anchored to the authoritative current score.
                        home_new = sum(
                            1 for p in new_plays
                            if p["team"].lower().strip()
                            == game["home_team"].lower().strip()
                        )
                        away_new = len(new_plays) - home_new
                        running_home = game["home_score"] - home_new
                        running_away = game["away_score"] - away_new

                        for play in new_plays:
                            if (play["team"].lower().strip()
                                    == game["home_team"].lower().strip()):
                                running_home += 1
                            else:
                                running_away += 1
                            await self._post_scoring_play(
                                channel, game, play, running_home, running_away
                            )

                    else:
                        # For NFL/MLB, score increments vary per play type and
                        # can't be reliably inferred, so show the current ESPN score
                        # in every embed (original behaviour).
                        for play in new_plays:
                            await self._post_scoring_play(
                                channel, game, play,
                                game["home_score"], game["away_score"],
                            )

                    # If the score changed but ESPN's details array had nothing new,
                    # post a generic score update so the channel is never left showing
                    # a stale scoreline. This covers the lag window where ESPN updates
                    # the score field before populating the details array.
                    #
                    # Excluded for NFL: NFL doesn't use the details array at all (see
                    # get_nfl_scoring_plays()), and that function now accounts for
                    # every scoring event that changes the score — touchdowns, field
                    # goals, made PATs, and made 2-point conversions — so a genuine
                    # "score changed with nothing new in scoring_plays" gap shouldn't
                    # occur for NFL anymore. Posting this generic, unattributed
                    # fallback anyway was producing a duplicate-looking second embed
                    # for the same score change right after the attributed one.
                    # NHL/MLB/soccer are unaffected and keep this safety net, since
                    # their scoring_plays still comes from the details array, where
                    # this lag is a real, documented behavior (see _post_score_update).
                    score_changed = (
                        game["home_score"] != row.home_score
                        or game["away_score"] != row.away_score
                    )
                    if sport != "nfl" and not new_plays and score_changed:
                        await self._post_score_update(channel, game)

                    # Advance last_play_index to the most recent play we processed.
                    new_last_idx = (
                        new_plays[-1]["index"] if new_plays else row.last_play_index
                    )

                    completed_flag = game.get("completed", False)

                    # See _FULL_TIME_GRACE_SECONDS: completed=True at
                    # STATUS_FULL_TIME is ambiguous for soccer (regulation over,
                    # but extra time may follow), so it only counts as done once
                    # it has persisted across polls for the grace period. Any
                    # recognised final status, or completed=True at any other
                    # stalled status (e.g. STATUS_SHOOTOUT), is trusted immediately.
                    #
                    # NOTE: pending_final_since is a naive-UTC column (no
                    # timezone=True), so we compare against naive
                    # datetime.utcnow() here rather than the tz-aware
                    # datetime.now(timezone.utc) used elsewhere in this file —
                    # a value written as tz-aware comes back naive on the next
                    # poll's fresh DB read, and subtracting aware from naive
                    # raises TypeError.
                    now = datetime.utcnow()
                    new_pending_final_since = row.pending_final_since

                    if completed_flag and status_name == "STATUS_FULL_TIME" and not is_final:
                        if row.pending_final_since is None:
                            # First poll to see it stalled here — start the grace
                            # timer and keep treating the game as in progress.
                            new_pending_final_since = now
                            is_done = False
                        else:
                            elapsed = (now - row.pending_final_since).total_seconds()
                            is_done = elapsed >= _FULL_TIME_GRACE_SECONDS
                    else:
                        # Status moved on from STATUS_FULL_TIME (e.g. to
                        # STATUS_EXTRA_TIME — the game continued) or this poll
                        # resolved to a real final. Either way, no grace timer
                        # should still be running for this game.
                        new_pending_final_since = None
                        is_done = is_final or completed_flag

                    if is_done:
                        # Game just ended. This is triggered by a recognised final
                        # status (STATUS_FINAL, STATUS_FINAL_PEN, etc.), by
                        # completed=True at a non-STATUS_FULL_TIME stalled status,
                        # or by completed=True having persisted at STATUS_FULL_TIME
                        # past the grace period above.
                        await self._post_final_score(channel, game)
                        with SessionLocal() as session:
                            db_row = session.get(LiveGameState, row.id)
                            if db_row:
                                session.delete(db_row)
                                session.commit()
                    else:
                        # Game still in progress — persist the updated state.
                        with SessionLocal() as session:
                            db_row = session.get(LiveGameState, row.id)
                            if db_row:
                                db_row.home_score = game["home_score"]
                                db_row.away_score = game["away_score"]
                                db_row.last_play_index = new_last_idx
                                db_row.pending_final_since = new_pending_final_since
                                db_row.updated_at = datetime.now(timezone.utc)
                                session.commit()

        # ── Handle games that disappeared from the feed ───────────────────────
        # If a game is tracked in our DB but is no longer in the ESPN feed,
        # it ended between polls without us catching STATUS_FINAL. Post a final
        # embed with the last known scores and delete the tracking row.
        for game_id, row in existing.items():
            if row.status == "in_progress" and game_id not in live_game_ids:
                logger.info(
                    "Game %s (%s vs %s) disappeared from ESPN feed — posting final",
                    game_id, row.away_team, row.home_team,
                )
                # Build a minimal game dict from stored state so we can reuse
                # the same embed builder used for STATUS_FINAL events.
                synthetic_game = {
                    "sport": row.sport,
                    "home_team": row.home_team,
                    "away_team": row.away_team,
                    "home_score": row.home_score,
                    "away_score": row.away_score,
                }
                await self._post_final_score(channel, synthetic_game)

                # Delete the row rather than marking it "final" — same convention
                # as the normal final-score path above.
                with SessionLocal() as session:
                    db_row = session.get(LiveGameState, row.id)
                    if db_row:
                        session.delete(db_row)
                        session.commit()

    # ──────────────────────────────────────────────────────────────────────────
    # Embed builders
    # ──────────────────────────────────────────────────────────────────────────

    async def _post_game_start(
        self,
        channel: discord.TextChannel,
        game: dict,
    ) -> None:
        """
        Post a "game starting" embed when we first detect a game as in_progress.

        Parameters
        ----------
        channel : Discord channel to post in.
        game    : Game dict as returned by get_live_playoff_games().
        """
        sport = game["sport"]
        emoji = _SPORT_EMOJI.get(sport, "")

        embed = discord.Embed(
            title=f"{emoji} Game Starting",
            description=f"{game['away_team']} vs {game['home_team']}",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text=_season_footer(sport, game))
        _apply_team_branding(embed, game)
        await channel.send(embed=embed)

    async def _post_scoring_play(
        self,
        channel: discord.TextChannel,
        game: dict,
        play: dict,
        display_home_score: int,
        display_away_score: int,
        is_penalty: bool = False,
    ) -> None:
        """
        Post a scoring update embed for a single scoring play.

        Parameters
        ----------
        channel             : Discord channel to post in.
        game                : Current game state dict (used for team names, sport).
        play                : One entry from game["scoring_plays"].
        display_home_score  : The home score to show in this embed. For unit-score
                              sports (soccer, hockey) this is the reconstructed
                              per-play running total; for others it is the current
                              ESPN score passed through from the caller. During a
                              penalty shootout this is the running penalty tally.
        display_away_score  : Equivalent away score.
        is_penalty          : When True, the embed description is prefixed with
                              "Penalties:" so the channel clearly shows the shootout
                              tally rather than the frozen regulation scoreline.
        """
        sport = game["sport"]
        label = _SPORT_LABELS.get(sport, sport.upper())

        scorer = play.get("scorer") or ""
        team = play.get("team") or ""
        play_type = play.get("type") or "Score"
        clock = play.get("clock") or ""
        yards = play.get("yards")
        headshot_url = play.get("headshot_url")

        # NFL-only: espn_player_id (from utils.sports_client.get_nfl_scoring_plays)
        # is the same universal ESPN athlete ID used in fantasy rosters, so a
        # direct ID lookup avoids fragile name matching between the two APIs.
        fantasy_team_name = None
        espn_player_id = play.get("espn_player_id")
        if sport == "nfl" and espn_player_id is not None:
            fantasy_team_name = self._get_fantasy_team_name(channel.guild.id, espn_player_id)

        if sport == "nfl":
            # "Field Goal Good" is ESPN's literal type text (kept as-is in
            # utils.sports_client for data fidelity) but reads awkwardly in
            # a sentence — display it as plain "Field Goal" here instead.
            # "PAT" and "Two-Point Conversion" are this module's own
            # synthesized types (see get_nfl_scoring_plays) and already
            # display-ready as-is.
            play_type_display = "Field Goal" if play_type == "Field Goal Good" else play_type
            verb_phrase = f"scored {_article(play_type_display)} {play_type_display}"

            if fantasy_team_name and scorer:
                title = f"{fantasy_team_name}'s player, {scorer}, {verb_phrase}"
            elif scorer:
                title = f"{scorer} {verb_phrase}"
            else:
                title = f"{team} {verb_phrase}"
        elif scorer:
            title = f"{scorer} scores!"
        else:
            title = f"{team} scores!"

        # Show the score at the moment of this specific play. During a penalty
        # shootout the regulation score is frozen (a tie), so we prefix the line
        # with "Penalties:" and show the running penalty tally instead.
        if is_penalty:
            score_line = (
                f"Penalties: {game['away_team']} {display_away_score} "
                f"— {display_home_score} {game['home_team']}"
            )
        else:
            score_line = (
                f"{game['away_team']} {display_away_score} "
                f"— {display_home_score} {game['home_team']}"
            )

        embed = discord.Embed(
            title=title,
            description=score_line,
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        # Field goal distance, shown separately from the title per how the
        # rest of this embed already surfaces secondary detail (e.g. the
        # final-score embed's "Time" field) rather than crowding the title.
        if sport == "nfl" and play_type == "Field Goal Good" and yards is not None:
            embed.add_field(name="Distance", value=f"{yards} yards", inline=True)
        footer_parts = [p for p in [play_type, clock, label] if p]
        embed.set_footer(text=" · ".join(footer_parts))
        _apply_team_branding(embed, game, thumbnail_override=headshot_url, compact=True)
        await channel.send(embed=embed)

    def _get_fantasy_team_name(self, guild_id: int, espn_player_id: int) -> str | None:
        """
        Look up which fantasy team (if any) owns this player in the guild's
        cached ESPN Fantasy roster snapshot — see cogs/fantasy.py, which
        populates fantasy_roster_entries on a daily refresh. Returns the
        team's own name (e.g. "The Gridiron Gang"), not the manager's
        personal display name — that's what gets called out in the scoring
        embed. Returns None if the fantasy feature isn't configured for this
        guild, or the player isn't on anyone's roster (free agent, or on a
        team not in this league at all).
        """
        with SessionLocal() as session:
            entry = (
                session.query(FantasyRosterEntry)
                .filter_by(guild_id=guild_id, espn_player_id=espn_player_id)
                .first()
            )
            return entry.team_name if entry else None

    async def _post_score_update(
        self,
        channel: discord.TextChannel,
        game: dict,
    ) -> None:
        """
        Post a generic score update embed when the scoreline changed but ESPN's
        details array hasn't populated the corresponding scoring play yet.

        ESPN updates its score field and its details array asynchronously — the
        score can jump without any new entries in details for up to one poll
        interval. This fallback ensures the channel always reflects the current
        score even when we can't attribute the change to a specific player.

        Parameters
        ----------
        channel : Discord channel to post in.
        game    : Current game state dict from get_live_playoff_games().
        """
        sport = game.get("sport", "")
        emoji = _SPORT_EMOJI.get(sport, "")

        score_line = (
            f"{game['away_team']} {game['away_score']} "
            f"— {game['home_score']} {game['home_team']}"
        )

        embed = discord.Embed(
            title=f"{emoji} Score Update",
            description=score_line,
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text=_season_footer(sport, game))
        _apply_team_branding(embed, game)
        await channel.send(embed=embed)

    async def _post_final_score(
        self,
        channel: discord.TextChannel,
        game: dict,
    ) -> None:
        """
        Post a "final score" embed when a game concludes.

        Handles regulation, extra time (AET), and penalty shootout (PEN) endings.
        The embed title reflects how the game ended. A "Time" field shows the
        final elapsed clock when available from ESPN's displayClock field.
        The penalty score is shown in a separate field when available.

        Parameters
        ----------
        channel : Discord channel to post in.
        game    : Game dict — either the live ESPN dict (for STATUS_FINAL events)
                  or a synthetic dict built from DB state (for feed-disappearance
                  cases). Must contain home_team, away_team, home_score, away_score,
                  and sport. Optionally contains status_name, home_penalty_score,
                  away_penalty_score, display_clock, and period.
        """
        sport = game.get("sport", "")
        status_name = game.get("status_name", "")

        home = game["home_team"]
        away = game["away_team"]
        home_score = game["home_score"]
        away_score = game["away_score"]
        home_pen = game.get("home_penalty_score")
        away_pen = game.get("away_penalty_score")

        # Choose an appropriate title based on how the game ended.
        # The primary signal is status_name. When ESPN stalls at a transitional
        # status (e.g. STATUS_SHOOTOUT or STATUS_FULL_TIME) and we're here via
        # completed=True rather than a recognised final status, we fall back to
        # inferring the ending from penalty score presence and the period number.
        if status_name == "STATUS_FINAL_AET":
            title = "Final (After Extra Time)"
        elif status_name == "STATUS_FINAL_PEN":
            title = "Final (After Penalties)"
        elif status_name == "STATUS_FINAL_OT":
            # ESPN's overtime-final status for NFL/NHL (soccer uses
            # STATUS_FINAL_AET instead — see above).
            title = "Final (Overtime)"
        elif home_pen is not None and away_pen is not None:
            # Penalty shootout scores are present — game ended in a shootout even
            # though ESPN never surfaced STATUS_FINAL_PEN.
            title = "Final (After Penalties)"
        elif sport == "soccer" and game.get("period", 0) >= 3:
            # Period 3+ only means extra time under SOCCER's period numbering
            # (1-2 = halves, 3-4 = extra-time halves). This must stay gated to
            # soccer: for NFL (1-4 = Q1-Q4) and NHL (1-3 = P1-P3), period 3 or
            # 4 is a completely ordinary regulation period, not overtime — a
            # ungated period>=3 check here previously mislabeled any NFL game
            # that finished in the 3rd or 4th quarter as "After Extra Time"
            # (e.g. an ordinary STATUS_FINAL Q4 finish, period=4, tripped this
            # branch before the STATUS_FINAL_OT branch above existed to catch
            # actual NFL overtime games instead).
            title = "Final (After Extra Time)"
        else:
            title = "Final Score"

        # Regulation score is always shown in the description.
        description = f"{away} **{away_score}** — **{home_score}** {home}"

        # Determine the winner from the regulation/AET scoreline.
        # For penalty finals the regulation score is tied, so the penalty
        # score determines the actual winner.
        if status_name == "STATUS_FINAL_PEN" and home_pen is not None and away_pen is not None:
            if home_pen > away_pen:
                result = f"{home} wins on penalties!"
            else:
                result = f"{away} wins on penalties!"
        elif home_score > away_score:
            result = f"{home} wins!"
        elif away_score > home_score:
            result = f"{away} wins!"
        else:
            result = "Draw!"

        embed = discord.Embed(
            title=title,
            description=description,
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Result", value=result, inline=False)

        # Show the penalty shootout score as an extra field when available.
        if home_pen is not None and away_pen is not None:
            embed.add_field(
                name="Penalty Score",
                value=f"{away} {away_pen} — {home_pen} {home}",
                inline=False,
            )

        # Show the final elapsed time when ESPN provides it. For soccer ESPN
        # uses a count-up clock so displayClock at full time shows "90:00" or
        # similar; for AET/PEN it reflects the full 120 minutes. For NFL/NHL/MLB
        # the clock counts down to "0:00" — we still show it so the user knows
        # exactly when the game ended (e.g. "Q4 0:00"). Not available for the
        # synthetic game dict built from DB state when a game disappears mid-poll.
        display_clock: str = game.get("display_clock", "")
        period: int = game.get("period", 0)
        if display_clock:
            time_label = _format_period_label(sport, period)
            time_value = f"{time_label} · {display_clock}" if time_label else display_clock
            embed.add_field(name="Time", value=time_value, inline=True)

        embed.set_footer(text=_season_footer(sport, game))
        _apply_team_branding(embed, game)
        await channel.send(embed=embed)

    # ──────────────────────────────────────────────────────────────────────────
    # Slash commands
    # ──────────────────────────────────────────────────────────────────────────

    scores_group = app_commands.Group(
        name="scores",
        description="Live playoff score alert commands",
    )

    @scores_group.command(
        name="status",
        description="Show the current score alert configuration",
    )
    async def scores_status(self, interaction: discord.Interaction) -> None:
        """
        Show whether the feature is enabled, which channel it posts in, and
        which sports are currently being tracked. Available to all members.
        """
        with SessionLocal() as session:
            cfg = (
                session.query(ScheduleConfig)
                .filter_by(guild_id=interaction.guild_id, feature="sports_scores")
                .first()
            )
            if cfg is None:
                await interaction.response.send_message(
                    "Score alerts haven't been configured yet. "
                    "Use `/scores config channel` to get started.",
                    ephemeral=True,
                )
                return

            options = cfg.content_options
            channel_id = cfg.channel_id

        enabled = options.get("enabled", True)
        enabled_sports: list[str] = options.get("enabled_sports", _ALL_SPORTS)

        channel_mention = (
            f"<#{channel_id}>" if channel_id
            else f"#{_DEFAULT_CHANNEL_NAME} (fallback — set with /scores config channel)"
        )

        sport_lines = "\n".join(
            f"{'on ' if s in enabled_sports else 'off'} — {_SPORT_LABELS[s]}"
            for s in _ALL_SPORTS
        )

        await interaction.response.send_message(
            f"**Score alerts:** {'Enabled' if enabled else 'Disabled'}\n"
            f"**Channel:** {channel_mention}\n\n"
            f"**Sports:**\n{sport_lines}",
            ephemeral=True,
        )

    # ── Admin config subgroup: /scores config ─────────────────────────────────

    scores_config = app_commands.Group(
        name="config",
        description="Configure live score alert settings (admin only)",
        parent=scores_group,
    )

    @scores_config.command(
        name="channel",
        description="Set the channel where score alerts are posted (admin only)",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def scores_config_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        """Admin: Change which channel receives live score alert embeds."""
        with SessionLocal() as session:
            cfg = (
                session.query(ScheduleConfig)
                .filter_by(guild_id=interaction.guild_id, feature="sports_scores")
                .first()
            )
            if cfg is None:
                await interaction.response.send_message(
                    "No score alert config exists yet. "
                    "Use `/scores status` to initialize it.",
                    ephemeral=True,
                )
                return
            cfg.channel_id = channel.id
            session.commit()

        await interaction.response.send_message(
            f"Score alerts will now post to {channel.mention}.", ephemeral=True
        )

    @scores_config.command(
        name="sports",
        description="Toggle which sports are tracked (admin only)",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(
        nfl="True to enable NFL alerts (playoffs + one regular-season game per day), False to disable",
        nhl="True to enable NHL playoff alerts, False to disable",
        mlb="True to enable MLB playoff alerts, False to disable",
        soccer="True to enable soccer tournament alerts, False to disable",
    )
    async def scores_config_sports(
        self,
        interaction: discord.Interaction,
        nfl: bool | None = None,
        nhl: bool | None = None,
        mlb: bool | None = None,
        soccer: bool | None = None,
    ) -> None:
        """
        Admin: Toggle individual sports on or off without affecting the others.

        Only sports you explicitly pass are changed. Omitting a sport leaves
        its current state untouched. Example:
          /scores config sports nhl:True soccer:False
        enables NHL and disables soccer, leaving NFL and MLB as they were.
        """
        toggles = {"nfl": nfl, "nhl": nhl, "mlb": mlb, "soccer": soccer}

        if all(v is None for v in toggles.values()):
            await interaction.response.send_message(
                "Provide at least one sport to toggle, e.g. `/scores config sports nhl:True`.",
                ephemeral=True,
            )
            return

        with SessionLocal() as session:
            cfg = (
                session.query(ScheduleConfig)
                .filter_by(guild_id=interaction.guild_id, feature="sports_scores")
                .first()
            )
            if cfg is None:
                await interaction.response.send_message(
                    "No score alert config exists yet. Use `/scores status` first.",
                    ephemeral=True,
                )
                return

            options = cfg.content_options
            current: set[str] = set(options.get("enabled_sports", _ALL_SPORTS))

            for sport, value in toggles.items():
                if value is True:
                    current.add(sport)
                elif value is False:
                    current.discard(sport)

            options["enabled_sports"] = sorted(current)
            cfg.content_options = options
            session.commit()

        # Summarize the new state of all sports in the response.
        sport_lines = "\n".join(
            f"{'on ' if s in current else 'off'} — {_SPORT_LABELS[s]}"
            for s in _ALL_SPORTS
        )
        await interaction.response.send_message(
            f"Sports updated:\n{sport_lines}", ephemeral=True
        )

    @scores_config.command(
        name="enable",
        description="Enable live score alerts for this server",
    )
    async def scores_config_enable(self, interaction: discord.Interaction) -> None:
        """Turn on the live score alerts feature for this guild."""
        await self._set_enabled(interaction, True)

    @scores_config.command(
        name="disable",
        description="Disable live score alerts for this server",
    )
    async def scores_config_disable(self, interaction: discord.Interaction) -> None:
        """Turn off the live score alerts feature for this guild."""
        await self._set_enabled(interaction, False)

    async def _set_enabled(
        self,
        interaction: discord.Interaction,
        enabled: bool,
    ) -> None:
        """
        Shared helper for the enable and disable commands.

        Updates the "enabled" key in content_options and responds to the
        interaction.

        Parameters
        ----------
        interaction : The Discord interaction to respond to.
        enabled     : True to enable the feature, False to disable it.
        """
        with SessionLocal() as session:
            cfg = (
                session.query(ScheduleConfig)
                .filter_by(guild_id=interaction.guild_id, feature="sports_scores")
                .first()
            )
            if cfg is None:
                await interaction.response.send_message(
                    "No score alert config exists yet. Use `/scores status` first.",
                    ephemeral=True,
                )
                return

            options = cfg.content_options
            options["enabled"] = enabled
            cfg.content_options = options
            session.commit()

        word = "enabled" if enabled else "disabled"
        await interaction.response.send_message(
            f"Live score alerts {word}.", ephemeral=True
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _resolve_channel(
        self,
        guild: discord.Guild,
        channel_id: int | None,
        fallback_name: str,
    ) -> discord.TextChannel | None:
        """
        Resolve the target channel using a two-step priority:
          1. The admin-configured channel ID stored in the database.
          2. Any text channel whose name matches fallback_name exactly.
        Returns None if neither step finds a valid text channel.
        """
        if channel_id:
            channel = guild.get_channel(channel_id)
            if isinstance(channel, discord.TextChannel):
                return channel

        target = fallback_name.lstrip("#").lower()
        for ch in guild.text_channels:
            if ch.name.lower() == target:
                return ch

        return None


async def setup(bot: commands.Bot) -> None:
    """Called by bot.load_extension('cogs.sports_scores')."""
    await bot.add_cog(SportsScoresCog(bot))
