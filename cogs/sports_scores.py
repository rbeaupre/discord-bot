"""
cogs/sports_scores.py
─────────────────────
Live playoff sports score updates cog. Polls the ESPN public API on a per-sport
cadence during playoff season and posts Discord embeds for game starts, scoring
plays, and final scores in the configured channel.

How it works
────────────
1. On bot connect (on_ready), registers one APScheduler IntervalJob per sport
   per guild. NFL polls every 15 seconds (to catch TD + PAT as separate events);
   NHL, MLB, and soccer poll every 1 minute. All jobs are independent so one
   sport's cadence doesn't affect others.
2. Each poll is a no-op when the feature is disabled, the sport is toggled off,
   or no playoff games are live — off-season overhead is negligible (one DB read).
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
from database.models import LiveGameState, ScheduleConfig
from utils.sports_client import (
    ACTIVE_STATUSES,
    FINAL_STATUSES,
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


def _job_id(guild_id: int, sport: str) -> str:
    """
    Return the stable APScheduler job ID for a guild + sport polling job.

    One job is registered per sport per guild so each sport can have its own
    independent polling interval (e.g. 15 s for NFL, 60 s for NHL/MLB/soccer).
    """
    return f"sports_scores_{sport}_{guild_id}"


class SportsScoresCog(commands.Cog, name="SportsScores"):
    """
    Cog that polls ESPN for live playoff game updates and posts them to Discord.

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

        Checks ESPN for live playoff games for a single sport, compares against
        the stored live_game_states rows for this guild + sport, and posts embeds:
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
        try:
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
                # First time we're seeing this game.
                if is_active:
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

                    if new_plays and game["sport"] in _UNIT_SCORE_SPORTS:
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
                    score_changed = (
                        game["home_score"] != row.home_score
                        or game["away_score"] != row.away_score
                    )
                    if not new_plays and score_changed:
                        await self._post_score_update(channel, game)

                    # Advance last_play_index to the most recent play we processed.
                    new_last_idx = (
                        new_plays[-1]["index"] if new_plays else row.last_play_index
                    )

                    if is_final:
                        # Game just ended — post the final score, then delete
                        # the tracking row. Deletion is cleaner than marking
                        # status="final": the row simply disappears so subsequent
                        # polls never need to inspect or filter on it.
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
        label = _SPORT_LABELS.get(sport, sport.upper())

        embed = discord.Embed(
            title=f"{emoji} Game Starting",
            description=f"{game['away_team']} vs {game['home_team']}",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text=f"{label} · Playoff")
        await channel.send(embed=embed)

    async def _post_scoring_play(
        self,
        channel: discord.TextChannel,
        game: dict,
        play: dict,
        display_home_score: int,
        display_away_score: int,
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
                              ESPN score passed through from the caller.
        display_away_score  : Equivalent away score.
        """
        sport = game["sport"]
        label = _SPORT_LABELS.get(sport, sport.upper())

        scorer = play.get("scorer") or ""
        team = play.get("team") or ""
        play_type = play.get("type") or "Score"
        clock = play.get("clock") or ""

        # Use the player name when available; fall back to the team name.
        title = f"{scorer} scores!" if scorer else f"{team} scores!"

        # Show the score at the moment of this specific play.
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
        footer_parts = [p for p in [play_type, clock, label] if p]
        embed.set_footer(text=" · ".join(footer_parts))
        await channel.send(embed=embed)

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
        label = _SPORT_LABELS.get(sport, sport.upper())
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
        embed.set_footer(text=f"{label} · Playoff")
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
        label = _SPORT_LABELS.get(sport, sport.upper())
        status_name = game.get("status_name", "")

        home = game["home_team"]
        away = game["away_team"]
        home_score = game["home_score"]
        away_score = game["away_score"]
        home_pen = game.get("home_penalty_score")
        away_pen = game.get("away_penalty_score")

        # Choose an appropriate title based on how the game ended.
        if status_name == "STATUS_FINAL_AET":
            title = "Final (After Extra Time)"
        elif status_name == "STATUS_FINAL_PEN":
            title = "Final (After Penalties)"
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

        embed.set_footer(text=f"{label} · Playoff")
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
        nfl="True to enable NFL playoff alerts, False to disable",
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
