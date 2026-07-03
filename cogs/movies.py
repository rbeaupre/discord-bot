"""
cogs/movies.py
──────────────
Movie night cog. On the configured day each month, picks a random unwatched
film from the Criterion Collection catalog and posts a Claude-written pitch
with the movie poster.

How it works
────────────
1. On the configured day each month (default: 1st at 7 PM ET), the scheduled
   job first syncs the Criterion catalog from TMDB to pick up any newly added
   films, then picks a random unwatched film, asks Claude to write a short
   pitch, and posts an embed with the movie poster.
2. The TMDB sync is incremental — it only fetches /credits for films not
   already in the database, so monthly refreshes are fast. /movie setup can
   still be used to force an immediate refresh on demand.
3. A per-guild movie_night_picks table tracks which films have been selected,
   so the same film is never repeated until every film in the catalog has been
   shown — at which point the guild's pick history resets automatically.

Slash commands
──────────────
/movie pick                  — Pick and post a random Criterion film now (admin)
/movie setup                 — Load/refresh the Criterion catalog from TMDB (admin)
/movie config channel <#ch>  — Set the posting channel (admin)
/movie config day <1–31>     — Set which day of the month to post (admin)
/movie config time <HH:MM>   — Set posting time in ET (admin)

Default schedule: 1st of every month at 7:00 PM Eastern Time in #movie-night.
"""

import logging
import random
from datetime import datetime, timezone

import discord
from apscheduler.triggers.cron import CronTrigger
from discord import app_commands
from discord.ext import commands

import config
from database.db import SessionLocal
from database.models import CriterionFilm, MovieNightPick, ScheduleConfig
from utils.claude_client import summarize_criterion_film
from utils.tmdb_client import TMDBError, get_criterion_films

logger = logging.getLogger(__name__)

# ── Defaults ──────────────────────────────────────────────────────────────────
_DEFAULT_HOUR = 19          # 7 PM ET
_DEFAULT_MINUTE = 0
_DEFAULT_TIMEZONE = "America/New_York"
_DEFAULT_DAY_OF_MONTH = 1
_DEFAULT_CHANNEL_NAME = "movie-night"


def _job_id(guild_id: int) -> str:
    """Return the stable APScheduler job ID for a guild's movie night job."""
    return f"movies_{guild_id}"


class MoviesCog(commands.Cog, name="Movies"):
    """Cog that manages the monthly Criterion Collection movie night post."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ──────────────────────────────────────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Register (or replace) a monthly movie night job for every guild."""
        for guild in self.bot.guilds:
            await self._schedule_for_guild(guild.id)
        logger.info(
            "MoviesCog ready — scheduled jobs for %d guild(s)", len(self.bot.guilds)
        )

    async def _schedule_for_guild(self, guild_id: int) -> None:
        """
        Load this guild's movie config and (re)register its APScheduler job.
        Creates a default config row if none exists yet.

        The day of month is stored in content_options["day_of_month"] since
        ScheduleConfig.day_of_week is not applicable for a monthly cadence.
        """
        with SessionLocal() as session:
            cfg = (
                session.query(ScheduleConfig)
                .filter_by(guild_id=guild_id, feature="movies")
                .first()
            )

            if cfg is None:
                cfg = ScheduleConfig(
                    guild_id=guild_id,
                    feature="movies",
                    hour=_DEFAULT_HOUR,
                    minute=_DEFAULT_MINUTE,
                    timezone=_DEFAULT_TIMEZONE,
                    day_of_week=None,   # not applicable — monthly feature
                )
                cfg.content_options = {"day_of_month": _DEFAULT_DAY_OF_MONTH}
                session.add(cfg)
                session.commit()
                logger.info("Created default movies config for guild %d", guild_id)

            hour = cfg.hour
            minute = cfg.minute
            tz = cfg.timezone
            day_of_month = cfg.content_options.get("day_of_month", _DEFAULT_DAY_OF_MONTH)

        self.bot.scheduler.add_job(
            self._post_movie_pick,
            CronTrigger(day=day_of_month, hour=hour, minute=minute, timezone=tz),
            id=_job_id(guild_id),
            args=[guild_id],
            replace_existing=True,
        )
        logger.debug(
            "Movie night job set for guild %d — day %d at %02d:%02d %s",
            guild_id, day_of_month, hour, minute, tz,
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Catalog sync
    # ──────────────────────────────────────────────────────────────────────────

    def _sync_catalog(self) -> tuple[int, int]:
        """
        Fetch all Criterion Collection films from TMDB and upsert them into
        the criterion_films table.

        Only fetches /credits for films not already in the database, so monthly
        auto-refreshes are fast (a handful of new titles at most). The initial
        run — when the table is empty — takes a few minutes because every film
        needs a separate /credits call for the director.

        Returns
        -------
        tuple[int, int]
            (added, updated) — counts of newly inserted and refreshed rows.

        Raises
        ------
        TMDBError
            If the TMDB API call fails.
        """
        with SessionLocal() as session:
            existing_ids: set[int] = {
                row.tmdb_id for row in session.query(CriterionFilm.tmdb_id).all()
            }

        films = get_criterion_films(
            api_key=config.TMDB_API_KEY,
            existing_ids=existing_ids,
        )

        added = 0
        updated = 0
        with SessionLocal() as session:
            for film_data in films:
                existing = session.get(CriterionFilm, film_data["tmdb_id"])
                if existing is None:
                    session.add(CriterionFilm(**film_data))
                    added += 1
                else:
                    existing.title = film_data["title"]
                    existing.year = film_data["year"]
                    existing.overview = film_data["overview"]
                    existing.poster_url = film_data["poster_url"]
                    # Only overwrite director if credits were fetched this run
                    # (director is None for existing_ids where /credits was skipped).
                    if film_data["director"] is not None:
                        existing.director = film_data["director"]
                    updated += 1
            session.commit()

        return added, updated

    # ──────────────────────────────────────────────────────────────────────────
    # Scheduled action
    # ──────────────────────────────────────────────────────────────────────────

    async def _post_movie_pick(self, guild_id: int) -> None:
        """
        Called by APScheduler on the configured day of the month. Syncs the
        Criterion catalog from TMDB, then picks a random unwatched film,
        generates a Claude pitch, and posts the embed in the configured channel.
        """
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            logger.warning(
                "Movie night job fired but guild %d not in cache — skipping", guild_id
            )
            return

        with SessionLocal() as session:
            cfg = (
                session.query(ScheduleConfig)
                .filter_by(guild_id=guild_id, feature="movies")
                .first()
            )
            channel_id = cfg.channel_id if cfg else None

        channel = self._resolve_channel(guild, channel_id, _DEFAULT_CHANNEL_NAME)
        if channel is None:
            logger.warning(
                "Movie night job: no channel found for guild %d — "
                "set one with /movie config channel", guild_id
            )
            return

        # Refresh the catalog before picking so newly added Criterion titles
        # are available. Incremental — only new films need a /credits call.
        try:
            added, _ = self._sync_catalog()
            if added:
                logger.info(
                    "Monthly catalog sync added %d new Criterion film(s) before pick "
                    "for guild %d", added, guild_id,
                )
        except TMDBError as exc:
            # Non-fatal: log the error and proceed with whatever is already
            # in the database rather than cancelling the pick entirely.
            logger.error(
                "TMDB catalog sync failed for guild %d monthly pick: %s — "
                "proceeding with existing catalog", guild_id, exc,
            )

        film = self._pick_film(guild_id)
        if film is None:
            await channel.send(
                "No Criterion films are in the database yet. "
                "An admin can run `/movie setup` to load the catalog from TMDB."
            )
            return

        await self._send_movie_embed(channel, guild_id, film)

    # ──────────────────────────────────────────────────────────────────────────
    # Film picker and embed builder
    # ──────────────────────────────────────────────────────────────────────────

    def _pick_film(self, guild_id: int) -> CriterionFilm | None:
        """
        Pick a random Criterion film that hasn't been chosen for this guild yet.

        If all films in the catalog have been picked, clears this guild's pick
        history and starts a fresh rotation from the full catalog. Returns None
        if the criterion_films table is empty (setup hasn't been run yet).
        """
        with SessionLocal() as session:
            # tmdb_ids of films already picked for this guild.
            picked_ids: set[int] = {
                row.tmdb_id
                for row in session.query(MovieNightPick)
                .filter_by(guild_id=guild_id)
                .all()
            }

            if picked_ids:
                unwatched = (
                    session.query(CriterionFilm)
                    .filter(CriterionFilm.tmdb_id.not_in(picked_ids))
                    .all()
                )
            else:
                # No picks yet — every film is available.
                unwatched = session.query(CriterionFilm).all()

            if not unwatched:
                # Every film has been picked — reset and restart the rotation.
                total = session.query(CriterionFilm).count()
                if total == 0:
                    return None

                logger.info(
                    "All %d Criterion films picked for guild %d — resetting rotation",
                    total, guild_id,
                )
                session.query(MovieNightPick).filter_by(guild_id=guild_id).delete()
                session.commit()

                unwatched = session.query(CriterionFilm).all()

            if not unwatched:
                return None

            chosen = random.choice(unwatched)

            # Expunge detaches the object from the session so its attributes
            # remain accessible after the session closes below.
            session.expunge(chosen)
            return chosen

    async def _send_movie_embed(
        self,
        channel: discord.TextChannel,
        guild_id: int,
        film: CriterionFilm,
    ) -> None:
        """
        Generate a Claude pitch, build the movie embed, post it, and record
        the pick in the database.

        Parameters
        ----------
        channel  : The Discord channel to post in.
        guild_id : Used to record the pick in movie_night_picks.
        film     : The CriterionFilm row to feature.
        """
        title = film.title
        year = film.year
        director = film.director or "Unknown Director"
        overview = film.overview or ""
        poster_url = film.poster_url
        tmdb_id = film.tmdb_id

        # Ask Claude to write a short, enthusiastic pitch for tonight's pick.
        try:
            pitch = summarize_criterion_film(
                title=title,
                director=director,
                year=year or 0,
                overview=overview,
            )
        except Exception as exc:
            logger.error("Claude pitch failed for film %r: %s", title, exc)
            pitch = "A Criterion Collection classic — grab some popcorn and enjoy."

        # Embed title shows "Title (Year)" for context, e.g. "Tokyo Story (1953)".
        display_title = f"{title} ({year})" if year else title

        embed = discord.Embed(
            title=display_title,
            description=pitch,
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc),
        )

        # Director in the author line keeps it visually prominent above the title.
        embed.set_author(name=f"Directed by {director}")

        # Full-width poster image below the description looks much better than
        # a thumbnail for a movie pick.
        if poster_url:
            embed.set_image(url=poster_url)

        embed.set_footer(text="Use /movie pick to choose another Criterion film any time")

        await channel.send(embed=embed)

        # Record the pick so this film is excluded from future rotations until
        # all films have been shown.
        with SessionLocal() as session:
            session.add(MovieNightPick(
                guild_id=guild_id,
                tmdb_id=tmdb_id,
                movie_title=title,
            ))
            session.commit()

        logger.info(
            "Posted movie night pick for guild %d: %s (%s)", guild_id, title, year
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Slash commands
    # ──────────────────────────────────────────────────────────────────────────

    movie_group = app_commands.Group(
        name="movie",
        description="Criterion Collection movie night commands",
    )

    @movie_group.command(
        name="pick",
        description="Pick and post a random Criterion film right now (admin only)",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def movie_pick(self, interaction: discord.Interaction) -> None:
        """
        Admin: Immediately post a random unwatched Criterion film.

        Unlike the scheduled job, this command posts regardless of the monthly
        schedule — useful for testing or for an impromptu movie night.
        """
        await interaction.response.defer(thinking=True)

        with SessionLocal() as session:
            catalog_count = session.query(CriterionFilm).count()

        if catalog_count == 0:
            await interaction.followup.send(
                "The Criterion film catalog is empty. "
                "Run `/movie setup` first to load it from TMDB.",
                ephemeral=True,
            )
            return

        film = self._pick_film(interaction.guild_id)
        if film is None:
            await interaction.followup.send(
                "No films available. Try `/movie setup` to refresh the catalog.",
                ephemeral=True,
            )
            return

        await self._send_movie_embed(interaction.channel, interaction.guild_id, film)
        await interaction.followup.send("Movie night pick posted!", ephemeral=True)

    @movie_group.command(
        name="setup",
        description="Load or refresh the Criterion Collection film catalog from TMDB (admin only)",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def movie_setup(self, interaction: discord.Interaction) -> None:
        """
        Admin: Populate or refresh the Criterion film catalog.

        Fetches all Criterion Collection films from TMDB including director
        credits. The initial load may take a few minutes because a /credits
        call is needed per film to get the director. Refreshes only fetch
        credits for newly discovered films. The catalog is global (shared
        across all guilds) so this only needs to be run once.
        """
        await interaction.response.defer(thinking=True)

        try:
            added, updated = self._sync_catalog()
        except TMDBError as exc:
            logger.error("TMDB catalog fetch failed during /movie setup: %s", exc)
            await interaction.followup.send(
                f"Failed to fetch films from TMDB: {exc}", ephemeral=True
            )
            return

        logger.info(
            "Criterion catalog refreshed by guild %d: %d added, %d updated",
            interaction.guild_id, added, updated,
        )
        await interaction.followup.send(
            f"Criterion catalog updated: **{added}** new films added, "
            f"**{updated}** existing films refreshed "
            f"({added + updated} total in catalog).",
            ephemeral=True,
        )

    # ── Admin config subgroup: /movie config ──────────────────────────────────

    movie_config = app_commands.Group(
        name="config",
        description="Configure movie night settings (admin only)",
        parent=movie_group,
    )

    @movie_config.command(
        name="channel",
        description="Set the channel for monthly movie night posts (admin only)",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def movie_config_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        """Admin: Change which channel receives the monthly movie night embed."""
        with SessionLocal() as session:
            cfg = (
                session.query(ScheduleConfig)
                .filter_by(guild_id=interaction.guild_id, feature="movies")
                .first()
            )
            if cfg is None:
                await interaction.response.send_message(
                    "No movie config exists yet. Use `/movie pick` once to create it.",
                    ephemeral=True,
                )
                return
            cfg.channel_id = channel.id
            session.commit()

        await interaction.response.send_message(
            f"Movie night posts will now go to {channel.mention}.", ephemeral=True
        )

    @movie_config.command(
        name="day",
        description="Set which day of the month movie night posts (admin only)",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(day="Day of the month (1–28 recommended to work in all months)")
    async def movie_config_day(
        self, interaction: discord.Interaction, day: int
    ) -> None:
        """
        Admin: Change the day of the month the movie night post fires.

        Days 1–28 are safe in every month (February is the short one).
        Days 29–31 will be skipped in months that don't have that many days.
        """
        if not 1 <= day <= 31:
            await interaction.response.send_message(
                "Day must be between 1 and 31.", ephemeral=True
            )
            return

        with SessionLocal() as session:
            cfg = (
                session.query(ScheduleConfig)
                .filter_by(guild_id=interaction.guild_id, feature="movies")
                .first()
            )
            if cfg is None:
                await interaction.response.send_message(
                    "No movie config exists yet. Use `/movie pick` once to create it.",
                    ephemeral=True,
                )
                return
            options = cfg.content_options
            options["day_of_month"] = day
            cfg.content_options = options
            session.commit()

        await self._schedule_for_guild(interaction.guild_id)
        await interaction.response.send_message(
            f"Movie night will now post on the **{day}th** of each month.", ephemeral=True
        )

    @movie_config.command(
        name="time",
        description="Set the posting time in 24h ET, e.g. 19:00 (admin only)",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def movie_config_time(
        self, interaction: discord.Interaction, time: str
    ) -> None:
        """Admin: Change the time the monthly movie night post fires (HH:MM, 24-hour ET)."""
        try:
            parsed = datetime.strptime(time, "%H:%M")
        except ValueError:
            await interaction.response.send_message(
                "Invalid format. Use HH:MM in 24-hour notation, e.g. `19:00`.",
                ephemeral=True,
            )
            return

        with SessionLocal() as session:
            cfg = (
                session.query(ScheduleConfig)
                .filter_by(guild_id=interaction.guild_id, feature="movies")
                .first()
            )
            if cfg is None:
                await interaction.response.send_message(
                    "No movie config exists yet. Use `/movie pick` once to create it.",
                    ephemeral=True,
                )
                return
            cfg.hour = parsed.hour
            cfg.minute = parsed.minute
            tz = cfg.timezone
            session.commit()

        await self._schedule_for_guild(interaction.guild_id)
        await interaction.response.send_message(
            f"Movie night will now post at **{time}** ({tz}).", ephemeral=True
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
          2. Any text channel named fallback_name (e.g. "movie-night").
        Returns None if neither step finds a matching text channel.
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
    """Called by bot.load_extension('cogs.movies')."""
    await bot.add_cog(MoviesCog(bot))
