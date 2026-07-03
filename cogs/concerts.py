"""
cogs/concerts.py
────────────────
Concert alerts cog. Checks Ticketmaster weekly for upcoming shows in Toronto
and Montreal by artists on a per-guild watchlist, then posts alert embeds in
the configured channel.

How it works
────────────
1. On bot connect (on_ready), registers one APScheduler CronJob per guild
   (default Monday 9 AM ET).
2. When the job fires, it fetches all upcoming music events in the configured
   cities from Ticketmaster and matches artist names against the watchlist using
   case-insensitive comparison.
3. For each matched event not already in concert_alerts_posted for this guild,
   a Discord embed with event details and a ticket link is posted.
4. The watchlist starts empty. Use /concert import with a Spotify playlist URL
   to bulk-populate it, or /concert add to add artists one at a time.
5. New artists from the weekly music release feature are auto-added to the
   watchlist by cogs/music.py with source="new_releases".

Slash commands
──────────────
/concert add <artist>              — Add artist to watchlist (admin)
/concert remove <artist>           — Remove artist from watchlist (admin)
/concert list                      — Show first 20 watchlist artists (anyone)
/concert check                     — Run the concert check right now (admin)
/concert import <playlist_url>     — Bulk-add all artists from a Spotify playlist (admin)
/concert config channel <#ch>      — Set the alert channel (admin)
/concert config day <weekday>      — Set check day of week (admin)
/concert config time <HH:MM>       — Set check time in ET (admin)

Default schedule: every Monday at 9:00 AM Eastern Time in #concert-alerts.
Default cities: Toronto, Montreal.
"""

import logging
from datetime import datetime, timezone

import discord
from apscheduler.triggers.cron import CronTrigger
from discord import app_commands
from discord.ext import commands
from sqlalchemy.exc import IntegrityError

import spotipy

import config
from database.db import SessionLocal
from database.models import ArtistWatchlist, ConcertAlertPosted, ScheduleConfig
from utils.spotify_client import get_playlist_artists
from utils.ticketmaster_client import TicketmasterError, get_upcoming_events

logger = logging.getLogger(__name__)

# ── Defaults ──────────────────────────────────────────────────────────────────
_DEFAULT_HOUR = 9
_DEFAULT_MINUTE = 0
_DEFAULT_TIMEZONE = "America/New_York"
_DEFAULT_DAY = "mon"
_DEFAULT_CITIES = ["Toronto", "Montreal"]
_DEFAULT_CHANNEL_NAME = "concert-alerts"
_VALID_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

def _job_id(guild_id: int) -> str:
    """Return the stable APScheduler job ID for a guild's concert check job."""
    return f"concerts_{guild_id}"


class ConcertsCog(commands.Cog, name="Concerts"):
    """Cog that manages weekly concert alerts for artists on the guild watchlist."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ──────────────────────────────────────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Register a concert check job for every guild the bot is in."""
        for guild in self.bot.guilds:
            await self._schedule_for_guild(guild.id)
        logger.info(
            "ConcertsCog ready — scheduled jobs for %d guild(s)", len(self.bot.guilds)
        )

    async def _schedule_for_guild(self, guild_id: int) -> None:
        """
        Load this guild's concert config and (re)register the APScheduler
        weekly check job.

        Safe to call multiple times — APScheduler replaces the existing job
        in place when replace_existing=True, so this can be called on reconnect
        or after any config change without creating duplicate jobs.
        """
        with SessionLocal() as session:
            cfg = (
                session.query(ScheduleConfig)
                .filter_by(guild_id=guild_id, feature="concerts")
                .first()
            )

            if cfg is None:
                cfg = ScheduleConfig(
                    guild_id=guild_id,
                    feature="concerts",
                    hour=_DEFAULT_HOUR,
                    minute=_DEFAULT_MINUTE,
                    timezone=_DEFAULT_TIMEZONE,
                    day_of_week=_DEFAULT_DAY,
                )
                cfg.content_options = {"cities": _DEFAULT_CITIES}
                session.add(cfg)
                session.commit()
                logger.info("Created default concerts config for guild %d", guild_id)

            hour = cfg.hour
            minute = cfg.minute
            tz = cfg.timezone
            day = cfg.day_of_week or _DEFAULT_DAY

        self.bot.scheduler.add_job(
            self._check_concerts,
            CronTrigger(day_of_week=day, hour=hour, minute=minute, timezone=tz),
            id=_job_id(guild_id),
            args=[guild_id],
            replace_existing=True,
        )
        logger.debug(
            "Concert check job set for guild %d — %s at %02d:%02d %s",
            guild_id, day, hour, minute, tz,
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Scheduled action
    # ──────────────────────────────────────────────────────────────────────────

    async def _check_concerts(self, guild_id: int) -> None:
        """
        Called by APScheduler on the configured day/time. Fetches all upcoming
        music events in the configured cities from Ticketmaster, matches them
        against this guild's artist watchlist, and posts alert embeds for any
        new matches.
        """
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            logger.warning(
                "Concert check fired but guild %d not in cache — skipping", guild_id
            )
            return

        with SessionLocal() as session:
            cfg = (
                session.query(ScheduleConfig)
                .filter_by(guild_id=guild_id, feature="concerts")
                .first()
            )
            channel_id = cfg.channel_id if cfg else None
            cities = (
                cfg.content_options.get("cities", _DEFAULT_CITIES) if cfg else _DEFAULT_CITIES
            )

            # Load the watchlist as a normalized set for O(1) per-event lookup.
            watchlist: set[str] = {
                row.artist_name.lower().strip()
                for row in session.query(ArtistWatchlist)
                .filter_by(guild_id=guild_id)
                .all()
            }

            # Build the set of already-posted event IDs to skip duplicates.
            already_posted: set[str] = {
                row.ticketmaster_event_id
                for row in session.query(ConcertAlertPosted)
                .filter_by(guild_id=guild_id)
                .all()
            }

        if not watchlist:
            logger.info(
                "Watchlist is empty for guild %d — skipping concert check", guild_id
            )
            return

        channel = self._resolve_channel(guild, channel_id, _DEFAULT_CHANNEL_NAME)
        if channel is None:
            logger.warning(
                "Concert check: no channel found for guild %d — "
                "set one with /concert config channel", guild_id
            )
            return

        try:
            events = get_upcoming_events(cities=cities, api_key=config.TICKETMASTER_API_KEY)
        except TicketmasterError as exc:
            logger.error("Ticketmaster API failed for guild %d: %s", guild_id, exc)
            await channel.send(
                "Could not check Ticketmaster for upcoming shows — the API may be temporarily "
                "unavailable. Use `/concert check` to try again later."
            )
            return

        matched_count = 0
        for event in events:
            if event["event_id"] in already_posted:
                continue

            # Check whether any credited attraction matches the watchlist.
            # We normalize both sides (lowercase + strip) so "Arctic Monkeys"
            # matches "arctic monkeys" etc.
            matched_artist: str | None = None
            for artist_name in event["artist_names"]:
                if artist_name.lower().strip() in watchlist:
                    matched_artist = artist_name
                    break

            if matched_artist is None:
                continue

            await self._post_concert_embed(channel, event, matched_artist)

            # Record the alert so it won't fire again on the next weekly check.
            with SessionLocal() as session:
                try:
                    session.add(ConcertAlertPosted(
                        guild_id=guild_id,
                        ticketmaster_event_id=event["event_id"],
                        artist_name=matched_artist,
                        event_name=event["event_name"],
                        venue_name=event.get("venue_name") or "",
                        city=event.get("city") or "",
                        event_date=event.get("event_date") or "",
                    ))
                    session.commit()
                    already_posted.add(event["event_id"])
                except IntegrityError:
                    # Another concurrent check already inserted this row.
                    session.rollback()

            matched_count += 1

        logger.info(
            "Concert check complete for guild %d: %d new alert(s) posted across %s",
            guild_id, matched_count, cities,
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Embed builder
    # ──────────────────────────────────────────────────────────────────────────

    async def _post_concert_embed(
        self,
        channel: discord.TextChannel,
        event: dict,
        matched_artist: str,
    ) -> None:
        """
        Build and send a Discord embed for a single matched concert event.

        Parameters
        ----------
        channel        : The channel to post in.
        event          : Event dict as returned by get_upcoming_events().
        matched_artist : The watchlist artist whose name matched this event.
        """
        venue = event.get("venue_name") or "Venue TBA"
        city = event.get("city") or ""
        date = event.get("event_date") or "Date TBA"
        url = event.get("event_url")

        embed = discord.Embed(
            title=event["event_name"],
            url=url,
            description=f"**{matched_artist}** is playing in {city}!",
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc),
        )

        embed.add_field(name="Date", value=date, inline=True)
        embed.add_field(name="Venue", value=venue, inline=True)
        embed.add_field(name="City", value=city, inline=True)

        if url:
            embed.add_field(
                name="Tickets", value=f"[Buy on Ticketmaster]({url})", inline=False
            )

        embed.set_footer(text="Use /concert check to search for new shows any time")

        await channel.send(embed=embed)

    # ──────────────────────────────────────────────────────────────────────────
    # Slash commands
    # ──────────────────────────────────────────────────────────────────────────

    concert_group = app_commands.Group(
        name="concert",
        description="Concert alerts and watchlist commands",
    )

    @concert_group.command(
        name="add",
        description="Add an artist to the concert alerts watchlist (admin only)",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(artist="Artist name to watch, e.g. 'Arctic Monkeys'")
    async def concert_add(
        self, interaction: discord.Interaction, artist: str
    ) -> None:
        """
        Admin: Add an artist to this guild's concert watchlist.
        Reports without erroring if the artist is already present.
        """
        artist = artist.strip()
        if not artist:
            await interaction.response.send_message(
                "Artist name cannot be empty.", ephemeral=True
            )
            return

        with SessionLocal() as session:
            # Case-insensitive check to avoid duplicates like "radiohead" and "Radiohead".
            existing = (
                session.query(ArtistWatchlist)
                .filter(
                    ArtistWatchlist.guild_id == interaction.guild_id,
                    ArtistWatchlist.artist_name.ilike(artist),
                )
                .first()
            )
            if existing:
                await interaction.response.send_message(
                    f"**{artist}** is already on the watchlist.", ephemeral=True
                )
                return

            session.add(ArtistWatchlist(
                guild_id=interaction.guild_id,
                artist_name=artist,
                source="manual",
            ))
            session.commit()

        logger.info(
            "Added artist %r to watchlist for guild %d (manual)",
            artist, interaction.guild_id,
        )
        await interaction.response.send_message(
            f"Added **{artist}** to the concert watchlist.", ephemeral=True
        )

    @concert_group.command(
        name="remove",
        description="Remove an artist from the concert alerts watchlist (admin only)",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(artist="Artist name to remove")
    async def concert_remove(
        self, interaction: discord.Interaction, artist: str
    ) -> None:
        """Admin: Remove an artist from this guild's concert watchlist."""
        artist = artist.strip()
        with SessionLocal() as session:
            row = (
                session.query(ArtistWatchlist)
                .filter(
                    ArtistWatchlist.guild_id == interaction.guild_id,
                    ArtistWatchlist.artist_name.ilike(artist),
                )
                .first()
            )
            if row is None:
                await interaction.response.send_message(
                    f"**{artist}** was not found on the watchlist.", ephemeral=True
                )
                return
            session.delete(row)
            session.commit()

        logger.info(
            "Removed artist %r from watchlist for guild %d",
            artist, interaction.guild_id,
        )
        await interaction.response.send_message(
            f"Removed **{artist}** from the concert watchlist.", ephemeral=True
        )

    @concert_group.command(
        name="list",
        description="Show the current concert watchlist",
    )
    async def concert_list(self, interaction: discord.Interaction) -> None:
        """
        Show the first 20 artists in the watchlist along with the total count.
        Available to all members.
        """
        with SessionLocal() as session:
            total = (
                session.query(ArtistWatchlist)
                .filter_by(guild_id=interaction.guild_id)
                .count()
            )
            rows = (
                session.query(ArtistWatchlist)
                .filter_by(guild_id=interaction.guild_id)
                .order_by(ArtistWatchlist.artist_name)
                .limit(20)
                .all()
            )
            names = [row.artist_name for row in rows]

        if not names:
            await interaction.response.send_message(
                "The concert watchlist is empty. Use `/concert add` to add artists.",
                ephemeral=True,
            )
            return

        artist_list = "\n".join(f"• {name}" for name in names)
        footer = f"\nShowing 20 of {total} — use /concert add to add more." if total > 20 else ""

        await interaction.response.send_message(
            f"**Concert watchlist** ({total} artists):\n{artist_list}{footer}",
            ephemeral=True,
        )

    @concert_group.command(
        name="check",
        description="Search Ticketmaster for upcoming shows right now (admin only)",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def concert_check(self, interaction: discord.Interaction) -> None:
        """Admin: Trigger the concert check immediately without waiting for the schedule."""
        await interaction.response.defer(thinking=True)
        await self._check_concerts(interaction.guild_id)
        await interaction.followup.send("Concert check complete!", ephemeral=True)

    @concert_group.command(
        name="import",
        description="Import all artists from a public Spotify playlist into the watchlist (admin only)",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(
        playlist_url="Spotify playlist URL from the Share menu "
                     "(open.spotify.com/playlist/...)"
    )
    async def concert_import(
        self, interaction: discord.Interaction, playlist_url: str
    ) -> None:
        """
        Admin: Bulk-add every unique artist from a public Spotify playlist to
        this guild's concert watchlist.

        Useful for seeding the watchlist from a curated playlist without having
        to add artists one by one. Artists already in the watchlist are silently
        skipped. The playlist must be public — private playlists return a 403.
        """
        await interaction.response.defer(thinking=True)

        # Fetch all unique artist names from the playlist.
        try:
            artist_names = get_playlist_artists(playlist_url)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except spotipy.SpotifyException as exc:
            await interaction.followup.send(
                f"Spotify error — make sure the playlist is public.\n`{exc}`",
                ephemeral=True,
            )
            return

        if not artist_names:
            await interaction.followup.send(
                "No artists found in that playlist.", ephemeral=True
            )
            return

        # Bulk-insert, skipping any artist already in the watchlist (case-insensitive).
        added = 0
        skipped = 0
        with SessionLocal() as session:
            for name in artist_names:
                existing = (
                    session.query(ArtistWatchlist)
                    .filter(
                        ArtistWatchlist.guild_id == interaction.guild_id,
                        ArtistWatchlist.artist_name.ilike(name),
                    )
                    .first()
                )
                if existing:
                    skipped += 1
                    continue
                session.add(ArtistWatchlist(
                    guild_id=interaction.guild_id,
                    artist_name=name,
                    source="manual",
                ))
                added += 1
            session.commit()

        logger.info(
            "Playlist import for guild %d: %d artists added, %d already in watchlist",
            interaction.guild_id, added, skipped,
        )
        await interaction.followup.send(
            f"Done — **{added}** new artists added to the watchlist "
            f"({skipped} already present).",
            ephemeral=True,
        )

    # ── Admin config subgroup: /concert config ────────────────────────────────

    concert_config = app_commands.Group(
        name="config",
        description="Configure concert alert settings (admin only)",
        parent=concert_group,
    )

    @concert_config.command(
        name="channel",
        description="Set the channel for concert alerts (admin only)",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def concert_config_channel(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        """Admin: Change which channel receives concert alert embeds."""
        with SessionLocal() as session:
            cfg = (
                session.query(ScheduleConfig)
                .filter_by(guild_id=interaction.guild_id, feature="concerts")
                .first()
            )
            if cfg is None:
                await interaction.response.send_message(
                    "No concerts config exists yet. Use `/concert check` once to create it.",
                    ephemeral=True,
                )
                return
            cfg.channel_id = channel.id
            session.commit()

        await interaction.response.send_message(
            f"Concert alerts will now post to {channel.mention}.", ephemeral=True
        )

    @concert_config.command(
        name="day",
        description="Set which weekday the weekly concert check runs (admin only)",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(weekday="Weekday abbreviation: mon, tue, wed, thu, fri, sat, sun")
    async def concert_config_day(
        self, interaction: discord.Interaction, weekday: str
    ) -> None:
        """Admin: Change which day of the week the concert check fires."""
        weekday = weekday.lower().strip()
        if weekday not in _VALID_DAYS:
            await interaction.response.send_message(
                f"Invalid day. Use one of: {', '.join(_VALID_DAYS)}", ephemeral=True
            )
            return

        with SessionLocal() as session:
            cfg = (
                session.query(ScheduleConfig)
                .filter_by(guild_id=interaction.guild_id, feature="concerts")
                .first()
            )
            if cfg is None:
                await interaction.response.send_message(
                    "No concerts config exists yet. Use `/concert check` once to create it.",
                    ephemeral=True,
                )
                return
            cfg.day_of_week = weekday
            session.commit()

        await self._schedule_for_guild(interaction.guild_id)
        await interaction.response.send_message(
            f"Concert check will now run every **{weekday.capitalize()}**.", ephemeral=True
        )

    @concert_config.command(
        name="time",
        description="Set the weekly check time in 24h ET, e.g. 09:00 (admin only)",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def concert_config_time(
        self, interaction: discord.Interaction, time: str
    ) -> None:
        """Admin: Change the time the weekly concert check fires (HH:MM, 24-hour ET)."""
        try:
            parsed = datetime.strptime(time, "%H:%M")
        except ValueError:
            await interaction.response.send_message(
                "Invalid format. Use HH:MM in 24-hour notation, e.g. `09:00`.",
                ephemeral=True,
            )
            return

        with SessionLocal() as session:
            cfg = (
                session.query(ScheduleConfig)
                .filter_by(guild_id=interaction.guild_id, feature="concerts")
                .first()
            )
            if cfg is None:
                await interaction.response.send_message(
                    "No concerts config exists yet. Use `/concert check` once to create it.",
                    ephemeral=True,
                )
                return
            cfg.hour = parsed.hour
            cfg.minute = parsed.minute
            tz = cfg.timezone
            session.commit()

        await self._schedule_for_guild(interaction.guild_id)
        await interaction.response.send_message(
            f"Concert check will now run at **{time}** ({tz}).", ephemeral=True
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
          2. Any text channel named fallback_name (e.g. "concert-alerts").
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
    """Called by bot.load_extension('cogs.concerts')."""
    await bot.add_cog(ConcertsCog(bot))
