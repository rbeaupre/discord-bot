"""
cogs/album_reviews.py
─────────────────────
Monthly album review cog. On the configured day of each month, scrapes the
Pitchfork Best New Albums page, generates a Claude summary of the review, looks
up the album on Spotify, and posts a rich embed in #album-reviews.

How it works
────────────
1. On bot connect (on_ready), registers one APScheduler CronJob per guild that
   fires on a specific day of each month (default: 1st, 10 AM Eastern Time).
2. The job scrapes Pitchfork's Best New Albums page via pitchfork_client.
3. Claude summarizes the Pitchfork review text into 3–4 sentences.
4. Spotify is searched for the album to obtain a "Listen" link.
5. A rich Discord embed is posted in the configured channel (#album-reviews).
6. The album's Pitchfork URL is saved to the database so it won't be posted
   again if the job fires a second time before next month's update.
7. If the Pitchfork scraper fails (e.g. site redesign), an alert is posted in
   the channel so the admin knows to investigate.

Slash commands
──────────────
/album post                    — Post the latest Pitchfork Best New Album now (admin)
/album config channel <#ch>    — Set the posting channel (admin)
/album config day     <1–31>   — Set which day of the month to post (admin)
/album config time    <HH:MM>  — Set posting time in ET (admin)

Default schedule: 1st of every month at 10:00 AM Eastern Time in #album-reviews.
"""

import logging
from datetime import datetime, timezone

import discord
from apscheduler.triggers.cron import CronTrigger
from discord import app_commands
from discord.ext import commands

from database.db import SessionLocal
from database.models import AlbumReviewPost, ScheduleConfig
from utils.claude_client import summarize_pitchfork_review
from utils.pitchfork_client import PitchforkScrapingError, get_latest_best_new_album
from utils.spotify_client import find_album_url

logger = logging.getLogger(__name__)

# ── Defaults ──────────────────────────────────────────────────────────────────
_DEFAULT_HOUR = 10
_DEFAULT_MINUTE = 0
_DEFAULT_TIMEZONE = "America/New_York"
_DEFAULT_DAY_OF_MONTH = 1           # 1st of every month
_DEFAULT_CHANNEL_NAME = "album-reviews"


def _job_id(guild_id: int) -> str:
    """Return the stable APScheduler job ID for a guild's album review job."""
    return f"album_review_{guild_id}"


class AlbumReviewsCog(commands.Cog, name="AlbumReviews"):
    """Cog that manages the monthly Pitchfork Best New Album post."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ──────────────────────────────────────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Register (or replace) a monthly album review job for every guild."""
        for guild in self.bot.guilds:
            await self._schedule_for_guild(guild.id)
        logger.info(
            "AlbumReviewsCog ready — scheduled jobs for %d guild(s)", len(self.bot.guilds)
        )

    async def _schedule_for_guild(self, guild_id: int) -> None:
        """
        Load this guild's album review config and (re)register its APScheduler job.
        Creates a default config row if none exists yet.

        The day of month is stored in content_options["day_of_month"] since
        ScheduleConfig.day_of_week is not meaningful for a monthly feature.
        """
        with SessionLocal() as session:
            cfg = (
                session.query(ScheduleConfig)
                .filter_by(guild_id=guild_id, feature="album_review")
                .first()
            )

            if cfg is None:
                cfg = ScheduleConfig(
                    guild_id=guild_id,
                    feature="album_review",
                    hour=_DEFAULT_HOUR,
                    minute=_DEFAULT_MINUTE,
                    timezone=_DEFAULT_TIMEZONE,
                    day_of_week=None,   # not used — this feature fires monthly, not weekly
                )
                cfg.content_options = {"day_of_month": _DEFAULT_DAY_OF_MONTH}
                session.add(cfg)
                session.commit()
                logger.info("Created default album review config for guild %d", guild_id)

            hour = cfg.hour
            minute = cfg.minute
            tz = cfg.timezone
            day_of_month = cfg.content_options.get("day_of_month", _DEFAULT_DAY_OF_MONTH)

        # CronTrigger with `day` fires on a specific calendar day each month.
        # For example, day=1 fires on the 1st of January, February, etc.
        self.bot.scheduler.add_job(
            self._post_album_review,
            CronTrigger(day=day_of_month, hour=hour, minute=minute, timezone=tz),
            id=_job_id(guild_id),
            args=[guild_id],
            replace_existing=True,
        )
        logger.debug(
            "Album review job set for guild %d — day %d at %02d:%02d %s",
            guild_id, day_of_month, hour, minute, tz,
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Scheduled action
    # ──────────────────────────────────────────────────────────────────────────

    async def _post_album_review(self, guild_id: int) -> None:
        """
        Called by APScheduler on the configured day of the month. Scrapes
        Pitchfork, generates a Claude summary, and posts the embed.

        If the album has already been posted this month (same Pitchfork URL
        in the database), the job skips silently to handle the edge case where
        APScheduler fires more than once in the same month due to a restart.

        If the Pitchfork scraper fails, an alert is posted in the channel so
        the admin knows to investigate.
        """
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            logger.warning(
                "Album review job fired but guild %d not in cache — skipping", guild_id
            )
            return

        with SessionLocal() as session:
            cfg = (
                session.query(ScheduleConfig)
                .filter_by(guild_id=guild_id, feature="album_review")
                .first()
            )
            channel_id = cfg.channel_id if cfg else None

        channel = self._resolve_channel(guild, channel_id, _DEFAULT_CHANNEL_NAME)
        if channel is None:
            logger.warning(
                "Album review job: no channel found for guild %d — "
                "set one with /album config channel", guild_id
            )
            return

        # Scrape Pitchfork and build the embed.
        try:
            review_data = get_latest_best_new_album()
        except PitchforkScrapingError as exc:
            # Post a clear alert so the admin knows to investigate.
            logger.error("Pitchfork scraping failed for guild %d: %s", guild_id, exc)
            await channel.send(
                "Could not parse the Pitchfork Best New Albums page — the site may have "
                "been redesigned. Use `/album post` to try again or check the bot logs."
            )
            return

        # Skip if we've already posted this exact album in this guild.
        with SessionLocal() as session:
            already_posted = (
                session.query(AlbumReviewPost)
                .filter_by(
                    guild_id=guild_id,
                    pitchfork_url=review_data["pitchfork_url"],
                )
                .first()
            )

        if already_posted:
            logger.info(
                "Album review already posted for guild %d (%s — %s), skipping",
                guild_id, review_data["artist"], review_data["album"],
            )
            return

        await self._send_review_embed(channel, guild_id, review_data)

    # ──────────────────────────────────────────────────────────────────────────
    # Embed builder
    # ──────────────────────────────────────────────────────────────────────────

    async def _send_review_embed(
        self,
        channel: discord.TextChannel,
        guild_id: int,
        review_data: dict,
    ) -> None:
        """
        Generate Claude's summary, look up the Spotify URL, build the embed,
        post it, and record the album in the database to prevent reposting.

        Parameters
        ----------
        channel     : The Discord channel to post in.
        guild_id    : Used to record the post in the database.
        review_data : Dict returned by get_latest_best_new_album().
        """
        artist = review_data["artist"]
        album = review_data["album"]
        review_text = review_data["review_text"]
        pitchfork_url = review_data["pitchfork_url"]
        image_url = review_data.get("image_url")

        # Ask Claude to summarize the Pitchfork review.
        try:
            summary = summarize_pitchfork_review(
                artist=artist,
                album=album,
                review_text=review_text,
            )
        except Exception as exc:
            # If Claude fails, fall back to a generic message rather than
            # skipping the post entirely.
            logger.error("Claude summary failed for '%s — %s': %s", artist, album, exc)
            summary = "Read the full review on Pitchfork."

        # Try to find the album on Spotify for a "Listen" link.
        spotify_url: str | None = None
        try:
            spotify_url = find_album_url(artist=artist, album=album)
        except Exception as exc:
            # Non-fatal — the post works fine without a Spotify link.
            logger.warning("Spotify lookup failed for '%s — %s': %s", artist, album, exc)

        # Build the embed. The title is clickable and links to the Pitchfork
        # review — no separate "Read on Pitchfork" link needed at the bottom.
        # Append the Spotify link inline in the description to avoid a bold
        # field header above it.
        spotify_line = f"\n\n[Listen on Spotify]({spotify_url})" if spotify_url else ""
        embed = discord.Embed(
            title=f"Album Review — {album}",
            url=pitchfork_url,
            description=f"Pitchfork Best New Album\n\n{summary}{spotify_line}",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )

        # Show artist name in the author line above the title.
        embed.set_author(name=artist)

        # Album cover art as a full-width image below the description.
        if image_url:
            embed.set_image(url=image_url)

        embed.set_footer(text="Use /album post to fetch the latest review any time")

        await channel.send(embed=embed)

        # Save to DB so this album isn't posted again.
        with SessionLocal() as session:
            session.add(AlbumReviewPost(
                guild_id=guild_id,
                pitchfork_url=pitchfork_url,
                artist_name=artist,
                album_title=album,
            ))
            session.commit()

        logger.info(
            "Posted album review for guild %d: %s — %s",
            guild_id, artist, album,
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Slash commands
    # ──────────────────────────────────────────────────────────────────────────

    album_group = app_commands.Group(
        name="album",
        description="Monthly Pitchfork album review commands",
    )

    @album_group.command(
        name="post",
        description="Post the latest Pitchfork Best New Album review right now",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def album_post(self, interaction: discord.Interaction) -> None:
        """
        On-demand album review post — restricted to members with Manage Server permission.

        Unlike the scheduled job, this command always posts even if the same
        album was recently posted, since the user is explicitly requesting it.
        """
        await interaction.response.defer(thinking=True)

        # Fetch the Pitchfork data.
        try:
            review_data = get_latest_best_new_album()
        except PitchforkScrapingError as exc:
            logger.error("Pitchfork scraping failed on /album post: %s", exc)
            await interaction.followup.send(
                "Could not parse the Pitchfork Best New Albums page — the site may have "
                "been redesigned. Check the bot logs for details.",
                ephemeral=True,
            )
            return

        await self._send_review_embed(interaction.channel, interaction.guild_id, review_data)
        await interaction.followup.send("Album review posted!", ephemeral=True)

    # ── Admin config subgroup: /album config ──────────────────────────────────

    album_config = app_commands.Group(
        name="config",
        description="Configure album review settings (admin only)",
        parent=album_group,
    )

    @album_config.command(
        name="channel",
        description="Set the channel for monthly album review posts (admin only)",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def album_config_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        """Admin: Change which channel receives the monthly album review embed."""
        with SessionLocal() as session:
            cfg = (
                session.query(ScheduleConfig)
                .filter_by(guild_id=interaction.guild_id, feature="album_review")
                .first()
            )
            if cfg is None:
                await interaction.response.send_message(
                    "No album review config exists yet. Use `/album post` once to create it.",
                    ephemeral=True,
                )
                return
            cfg.channel_id = channel.id
            session.commit()

        await interaction.response.send_message(
            f"Monthly album reviews will now post to {channel.mention}.", ephemeral=True
        )

    @album_config.command(
        name="day",
        description="Set which day of the month album reviews are posted (admin only)",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(day="Day of the month (1–28 recommended to work in all months)")
    async def album_config_day(
        self, interaction: discord.Interaction, day: int
    ) -> None:
        """
        Admin: Change the day of month the album review fires.

        Days 1–28 are safe in every month (February is the short one). Days
        29–31 will be skipped in months that don't have that many days.
        """
        if not 1 <= day <= 31:
            await interaction.response.send_message(
                "Day must be between 1 and 31.", ephemeral=True
            )
            return

        with SessionLocal() as session:
            cfg = (
                session.query(ScheduleConfig)
                .filter_by(guild_id=interaction.guild_id, feature="album_review")
                .first()
            )
            if cfg is None:
                await interaction.response.send_message(
                    "No album review config exists yet. Use `/album post` once to create it.",
                    ephemeral=True,
                )
                return
            # Store the day of month in content_options alongside any other settings.
            options = cfg.content_options
            options["day_of_month"] = day
            cfg.content_options = options
            session.commit()

        # Reschedule immediately so the new day takes effect without a restart.
        await self._schedule_for_guild(interaction.guild_id)

        await interaction.response.send_message(
            f"Monthly album review will now post on the **{day}th** of each month.",
            ephemeral=True,
        )

    @album_config.command(
        name="time",
        description="Set the posting time in 24h ET, e.g. 10:00 (admin only)",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def album_config_time(
        self, interaction: discord.Interaction, time: str
    ) -> None:
        """Admin: Change the time the monthly album review fires (HH:MM, 24-hour ET)."""
        try:
            parsed = datetime.strptime(time, "%H:%M")
        except ValueError:
            await interaction.response.send_message(
                "Invalid format. Use HH:MM in 24-hour notation, e.g. `10:00`.",
                ephemeral=True,
            )
            return

        with SessionLocal() as session:
            cfg = (
                session.query(ScheduleConfig)
                .filter_by(guild_id=interaction.guild_id, feature="album_review")
                .first()
            )
            if cfg is None:
                await interaction.response.send_message(
                    "No album review config exists yet. Use `/album post` once to create it.",
                    ephemeral=True,
                )
                return
            cfg.hour = parsed.hour
            cfg.minute = parsed.minute
            tz = cfg.timezone
            session.commit()

        await self._schedule_for_guild(interaction.guild_id)

        await interaction.response.send_message(
            f"Monthly album review will now post at **{time}** ({tz}).", ephemeral=True
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
          2. Any text channel named fallback_name (e.g. "album-reviews").
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
    """Called by bot.load_extension('cogs.album_reviews')."""
    await bot.add_cog(AlbumReviewsCog(bot))
