"""
cogs/music.py
─────────────
New music releases cog. Posts one new release per genre every week.

How it works
────────────
1. On bot connect (on_ready), registers one APScheduler CronJob per guild
   with a day_of_week constraint (defaults to Monday).
2. At the scheduled time, the job fetches one new Spotify release per genre
   using the Spotify Client Credentials API (no user login needed).
3. For each release, Claude writes a short hype blurb.
4. Everything is posted as a single Discord embed in the target channel.
5. Admin commands let server admins reconfigure the channel, day, time, and
   which genres to include — all live without restarting.

Slash commands
──────────────
/music releases                        — Post new releases right now (any member)
/music config channel  <#channel>      — Set posting channel (admin)
/music config day      <weekday>       — Set which day of the week (admin)
/music config time     <HH:MM>        — Set posting time in ET (admin)
/music config genres   <booleans>      — Toggle genres (admin)

Default schedule: every Monday at 10:00 AM Eastern Time in #new_releases.
"""

import logging
from datetime import datetime, timedelta

import discord
from apscheduler.triggers.cron import CronTrigger
from discord import app_commands
from discord.ext import commands

from database.db import SessionLocal
from database.models import MusicPost, ScheduleConfig
from utils.claude_client import describe_release
from utils.spotify_client import get_new_releases

logger = logging.getLogger(__name__)

# ── Defaults ──────────────────────────────────────────────────────────────────
_DEFAULT_HOUR = 10
_DEFAULT_MINUTE = 0
_DEFAULT_TIMEZONE = "America/New_York"
_DEFAULT_DAY = "mon"                                  # Monday
_DEFAULT_GENRES = ["rock", "indie", "electronic"]
_DEFAULT_CHANNEL_NAME = "new_releases"

# Valid weekday abbreviations accepted by APScheduler's CronTrigger.
_VALID_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# How far back to look when building the artist exclusion list.
# Artists posted within this window won't be featured again.
_EXCLUSION_WINDOW_DAYS = 90  # 3 months


def _exclusion_cutoff() -> datetime:
    """Return the earliest posted_at timestamp still within the exclusion window."""
    return datetime.utcnow() - timedelta(days=_EXCLUSION_WINDOW_DAYS)


def _job_id(guild_id: int) -> str:
    """Return the stable APScheduler job ID for a guild's music job."""
    return f"music_{guild_id}"


class MusicCog(commands.Cog, name="Music"):
    """Cog that manages weekly new-music-release posts and configuration."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ──────────────────────────────────────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Register (or replace) a scheduled music job for every guild."""
        for guild in self.bot.guilds:
            await self._schedule_for_guild(guild.id)
        logger.info("MusicCog ready — scheduled jobs for %d guild(s)", len(self.bot.guilds))

    async def _schedule_for_guild(self, guild_id: int) -> None:
        """
        Load this guild's music config and (re)register its APScheduler job.
        Creates a default config row if none exists.
        """
        with SessionLocal() as session:
            cfg = (
                session.query(ScheduleConfig)
                .filter_by(guild_id=guild_id, feature="music")
                .first()
            )

            if cfg is None:
                cfg = ScheduleConfig(
                    guild_id=guild_id,
                    feature="music",
                    hour=_DEFAULT_HOUR,
                    minute=_DEFAULT_MINUTE,
                    timezone=_DEFAULT_TIMEZONE,
                    day_of_week=_DEFAULT_DAY,
                )
                cfg.content_options = {"genres": _DEFAULT_GENRES}
                session.add(cfg)
                session.commit()
                logger.info("Created default music config for guild %d", guild_id)

            hour = cfg.hour
            minute = cfg.minute
            tz = cfg.timezone
            day = cfg.day_of_week or _DEFAULT_DAY

        self.bot.scheduler.add_job(
            self._post_releases,
            # day_of_week restricts firing to a single weekday each week.
            CronTrigger(day_of_week=day, hour=hour, minute=minute, timezone=tz),
            id=_job_id(guild_id),
            args=[guild_id],
            replace_existing=True,
        )
        logger.debug(
            "Music job set for guild %d — %s at %02d:%02d %s",
            guild_id, day, hour, minute, tz,
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Scheduled action
    # ──────────────────────────────────────────────────────────────────────────

    async def _post_releases(self, guild_id: int) -> None:
        """
        Called by APScheduler on the scheduled day/time. Loads fresh config,
        fetches Spotify releases, generates Claude blurbs, and posts the embed.
        """
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            logger.warning("Music job fired but guild %d not in cache — skipping", guild_id)
            return

        with SessionLocal() as session:
            cfg = (
                session.query(ScheduleConfig)
                .filter_by(guild_id=guild_id, feature="music")
                .first()
            )
            channel_id = cfg.channel_id if cfg else None
            genres = (
                cfg.content_options.get("genres", _DEFAULT_GENRES)
                if cfg else _DEFAULT_GENRES
            )
            # Load artist IDs posted in the last 3 months — artists outside
            # that window are allowed to cycle back into the rotation.
            posted_artist_ids = [
                row.artist_id
                for row in session.query(MusicPost)
                .filter(
                    MusicPost.guild_id == guild_id,
                    MusicPost.posted_at >= _exclusion_cutoff(),
                )
                .all()
            ]

        channel = self._resolve_channel(guild, channel_id, _DEFAULT_CHANNEL_NAME)
        if channel is None:
            logger.warning(
                "Music job: no channel found for guild %d — "
                "set one with /music config channel", guild_id
            )
            return

        releases = await self._send_releases_embed(channel, genres, posted_artist_ids)

        # Record what we just posted so these artists are excluded next time.
        if releases:
            await self._record_posted_artists(guild_id, releases)

    # ──────────────────────────────────────────────────────────────────────────
    # Embed builder
    # ──────────────────────────────────────────────────────────────────────────

    async def _send_releases_embed(
        self,
        channel: discord.TextChannel,
        genres: list[str],
        recent_artist_ids: list[str] | None = None,
    ) -> list[dict]:
        """
        Fetch new releases from Spotify, generate Claude blurbs, and post
        a single embed listing all genres. Each genre gets one field showing
        the artist, album, Spotify link, and a Claude-written description.

        Returns the list of releases that were posted so the caller can save
        the artist IDs to the recent-history list in the database.
        """
        releases = get_new_releases(genres, exclude_artist_ids=recent_artist_ids or [])

        if not releases:
            await channel.send(
                "Could not find any new releases this week. Try `/music releases` again later."
            )
            return []

        embed = discord.Embed(
            title="New Releases This Week",
            description=f"Fresh drops across {len(releases)} genre(s):",
            color=discord.Color.green(),
            timestamp=datetime.utcnow(),
        )

        for release in releases:
            # Ask Claude for a short hype blurb for each release.
            try:
                blurb = describe_release(
                    artist=release["artist"],
                    title=release["title"],
                    genre=release["genre"],
                )
            except Exception as exc:
                # If Claude fails for one track, use a fallback instead of
                # skipping the whole post.
                logger.error("Claude blurb failed for '%s': %s", release["title"], exc)
                blurb = "Check it out on Spotify!"

            # Build the field value: artist + linked title + blurb.
            field_value = (
                f"**{release['artist']}** — [{release['title']}]({release['spotify_url']})\n"
                f"Released: {release['release_date']}\n"
                f"{blurb}"
            )

            embed.add_field(
                name=release["genre"].title(),
                value=field_value,
                inline=False,
            )

            # Use the first release's cover art as the embed thumbnail.
            if embed.thumbnail.url is None and release.get("image_url"):
                embed.set_thumbnail(url=release["image_url"])

        embed.set_footer(text="Use /music releases to fetch new releases any time!")

        await channel.send(embed=embed)
        return releases

    # ──────────────────────────────────────────────────────────────────────────
    # Slash commands
    # ──────────────────────────────────────────────────────────────────────────

    music_group = app_commands.Group(
        name="music",
        description="New music release commands",
    )

    @music_group.command(
        name="releases",
        description="Post new music releases right now",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def music_releases(self, interaction: discord.Interaction) -> None:
        """On-demand release post — restricted to members with Manage Server permission."""
        await interaction.response.defer(thinking=True)

        with SessionLocal() as session:
            cfg = (
                session.query(ScheduleConfig)
                .filter_by(guild_id=interaction.guild_id, feature="music")
                .first()
            )
            genres = (
                cfg.content_options.get("genres", _DEFAULT_GENRES)
                if cfg else _DEFAULT_GENRES
            )
            posted_artist_ids = [
                row.artist_id
                for row in session.query(MusicPost)
                .filter(
                    MusicPost.guild_id == interaction.guild_id,
                    MusicPost.posted_at >= _exclusion_cutoff(),
                )
                .all()
            ]

        releases = await self._send_releases_embed(
            interaction.channel, genres, posted_artist_ids
        )

        if releases:
            await self._record_posted_artists(interaction.guild_id, releases)

        await interaction.followup.send("New releases posted!", ephemeral=True)

    # ── Admin config subgroup: /music config ──────────────────────────────────

    music_config = app_commands.Group(
        name="config",
        description="Configure music release settings (admin only)",
        parent=music_group,
    )

    @music_config.command(
        name="channel",
        description="Set the channel for weekly new-release posts (admin only)",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def music_config_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        """Admin: Change the posting channel."""
        with SessionLocal() as session:
            cfg = (
                session.query(ScheduleConfig)
                .filter_by(guild_id=interaction.guild_id, feature="music")
                .first()
            )
            if cfg is None:
                await interaction.response.send_message(
                    "No music config exists yet. Use `/music releases` once to create it.",
                    ephemeral=True,
                )
                return
            cfg.channel_id = channel.id
            session.commit()

        await interaction.response.send_message(
            f"Weekly new releases will now post to {channel.mention}.", ephemeral=True
        )

    @music_config.command(
        name="day",
        description="Set which weekday new releases are posted (admin only)",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(weekday="Weekday abbreviation: mon, tue, wed, thu, fri, sat, sun")
    async def music_config_day(
        self, interaction: discord.Interaction, weekday: str
    ) -> None:
        """Admin: Change which day of the week the release post fires."""
        weekday = weekday.lower().strip()
        if weekday not in _VALID_DAYS:
            await interaction.response.send_message(
                f"Invalid day. Use one of: {', '.join(_VALID_DAYS)}", ephemeral=True
            )
            return

        with SessionLocal() as session:
            cfg = (
                session.query(ScheduleConfig)
                .filter_by(guild_id=interaction.guild_id, feature="music")
                .first()
            )
            if cfg is None:
                await interaction.response.send_message(
                    "No music config exists yet. Use `/music releases` once to create it.",
                    ephemeral=True,
                )
                return
            cfg.day_of_week = weekday
            session.commit()

        # Reschedule immediately with the new day.
        await self._schedule_for_guild(interaction.guild_id)

        await interaction.response.send_message(
            f"Weekly releases will now post every **{weekday.capitalize()}**.", ephemeral=True
        )

    @music_config.command(
        name="time",
        description="Set the posting time in 24h ET, e.g. 10:00 (admin only)",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def music_config_time(
        self, interaction: discord.Interaction, time: str
    ) -> None:
        """Admin: Change the posting time (HH:MM, 24-hour, Eastern Time)."""
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
                .filter_by(guild_id=interaction.guild_id, feature="music")
                .first()
            )
            if cfg is None:
                await interaction.response.send_message(
                    "No music config exists yet. Use `/music releases` once to create it.",
                    ephemeral=True,
                )
                return
            cfg.hour = parsed.hour
            cfg.minute = parsed.minute
            tz = cfg.timezone
            session.commit()

        await self._schedule_for_guild(interaction.guild_id)

        await interaction.response.send_message(
            f"Weekly releases will now post at **{time}** ({tz}).", ephemeral=True
        )

    @music_config.command(
        name="genres",
        description="Choose which genres appear in the weekly post (admin only)",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(
        rock="Include rock releases",
        indie="Include indie releases",
        electronic="Include electronic releases",
    )
    async def music_config_genres(
        self,
        interaction: discord.Interaction,
        rock: bool = True,
        indie: bool = True,
        electronic: bool = True,
    ) -> None:
        """Admin: Toggle which genres are included in the weekly release post."""
        selected = [
            name
            for name, enabled in [
                ("rock", rock),
                ("indie", indie),
                ("electronic", electronic),
            ]
            if enabled
        ]

        if not selected:
            await interaction.response.send_message(
                "At least one genre must be enabled.", ephemeral=True
            )
            return

        with SessionLocal() as session:
            cfg = (
                session.query(ScheduleConfig)
                .filter_by(guild_id=interaction.guild_id, feature="music")
                .first()
            )
            if cfg is None:
                await interaction.response.send_message(
                    "No music config exists yet. Use `/music releases` once to create it.",
                    ephemeral=True,
                )
                return
            cfg.content_options = {"genres": selected}
            session.commit()

        await interaction.response.send_message(
            f"Weekly release genres updated: **{', '.join(selected)}**.", ephemeral=True
        )


    # ──────────────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────────────

    async def _record_posted_artists(
        self, guild_id: int, releases: list[dict]
    ) -> None:
        """
        Write a MusicPost row for each release that was just posted.
        These rows are read back on the next post to exclude the same artists,
        preventing the weekly releases from repeating the same acts indefinitely.
        """
        with SessionLocal() as session:
            for release in releases:
                artist_id = release.get("artist_id")
                if not artist_id:
                    continue
                session.add(MusicPost(
                    guild_id=guild_id,
                    artist_id=artist_id,
                    artist_name=release.get("artist", "Unknown"),
                    release_title=release.get("title", "Unknown"),
                ))
            session.commit()

        logger.debug(
            "Recorded %d posted artists for guild %d", len(releases), guild_id
        )

    def _resolve_channel(
        self,
        guild: discord.Guild,
        channel_id: int | None,
        fallback_name: str,
    ) -> discord.TextChannel | None:
        """
        Resolve the target channel with a two-step fallback:
          1. The admin-configured channel ID from the database.
          2. Any text channel named fallback_name (e.g. "new_releases").
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
    """Called by bot.load_extension('cogs.music')."""
    await bot.add_cog(MusicCog(bot))
