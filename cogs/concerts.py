"""
cogs/concerts.py
────────────────
Concert alerts cog. Checks Ticketmaster weekly for upcoming shows in Toronto
and Montreal by artists on a per-guild watchlist, then posts alert embeds in
the configured channel.

How it works
────────────
1. On bot connect (on_ready), seeds the artist watchlist for each guild from
   _SEED_ARTISTS if no watchlist rows exist yet, then registers one APScheduler
   CronJob per guild (default Monday 9 AM ET).
2. When the job fires, it fetches all upcoming music events in the configured
   cities from Ticketmaster and matches artist names against the watchlist using
   case-insensitive comparison.
3. For each matched event not already in concert_alerts_posted for this guild,
   a Discord embed with event details and a ticket link is posted.
4. New artists from the weekly music release feature are auto-added to the
   watchlist by cogs/music.py with source="new_releases".

Slash commands
──────────────
/concert add <artist>          — Add artist to watchlist (admin)
/concert remove <artist>       — Remove artist from watchlist (admin)
/concert list                  — Show first 20 watchlist artists (anyone)
/concert check                 — Run the concert check right now (admin)
/concert config channel <#ch>  — Set the alert channel (admin)
/concert config day <weekday>  — Set check day of week (admin)
/concert config time <HH:MM>   — Set check time in ET (admin)

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

import config
from database.db import SessionLocal
from database.models import ArtistWatchlist, ConcertAlertPosted, ScheduleConfig
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

# ── Seed artist list ──────────────────────────────────────────────────────────
# Pre-loaded into every guild's watchlist on first setup. Focused on indie
# rock, alternative, experimental electronic, and club music — the genres
# most likely to be touring through Toronto and Montreal.
_SEED_ARTISTS: list[str] = [
    # Rock / Indie — guitar bands, singer-songwriters, post-punk, shoegaze
    "Arctic Monkeys", "Radiohead", "The National", "Tame Impala",
    "Vampire Weekend", "Arcade Fire", "LCD Soundsystem", "Bon Iver",
    "Fleet Foxes", "Sufjan Stevens", "Phoebe Bridgers", "boygenius",
    "Japanese Breakfast", "Big Thief", "Snail Mail", "Soccer Mommy",
    "Angel Olsen", "Car Seat Headrest", "Parquet Courts", "Courtney Barnett",
    "King Krule", "Idles", "Fontaines D.C.", "Wet Leg", "The War on Drugs",
    "Yo La Tengo", "Pavement", "Built to Spill", "Modest Mouse", "The Shins",
    "Death Cab for Cutie", "Bright Eyes", "Jenny Lewis", "Julien Baker",
    "Lucy Dacus", "Clairo", "Weyes Blood", "Caroline Polachek", "Mitski",
    "Waxahatchee", "Beach House", "Real Estate", "Alex G", "Pinegrove",
    "Wednesday", "Lomelda", "Bellows", "Florist", "Hand Habits", "Hovvdy",
    "illuminati hotties", "Men I Trust", "Tops", "Helena Deland",
    # Indie / Alternative — classic and influential
    "Neutral Milk Hotel", "Guided by Voices", "Sebadoh", "Dinosaur Jr.",
    "Wilco", "Silver Jews", "Smog", "The Mountain Goats", "Okkervil River",
    "Nada Surf", "Grandaddy", "Sparklehorse", "Elliott Smith",
    "My Bloody Valentine", "Slowdive", "Ride", "Cocteau Twins", "Mazzy Star",
    "Low", "Galaxie 500", "Codeine", "American Football", "Mineral",
    "Sunny Day Real Estate", "The Promise Ring", "Braid", "Cap'n Jazz",
    "Interpol", "Spoon", "The Walkmen", "Clap Your Hands Say Yeah",
    "TV on the Radio", "Grizzly Bear", "Animal Collective", "Deerhunter",
    "Dirty Projectors", "Beirut", "The Decemberists", "Of Montreal",
    # Indie / Alternative — current wave
    "Yard Act", "Shame", "Squid", "black midi", "Dry Cleaning",
    "Sleaford Mods", "BC Camplight", "Porridge Radio", "Tirzah",
    "Osees", "Ty Segall", "Mdou Moctar", "Khruangbin", "Steve Gunn",
    "Rolling Blackouts Coastal Fever", "Julia Jacklin", "Camp Cope",
    "Hop Along", "Frankie Cosmos", "Girlpool", "Squirrel Flower",
    "Indigo De Souza", "Samia", "Faye Webster", "Bonny Light Horseman",
    "Allison Russell", "Adrianne Lenker", "Sharon Van Etten", "Hand Habits",
    "Nick Cave and the Bad Seeds", "PJ Harvey", "St. Vincent", "Bill Callahan",
    # Brit-pop / UK Indie
    "Blur", "Oasis", "Pulp", "Suede", "Elastica", "Primal Scream",
    "Teenage Fanclub", "The La's", "Gene", "Cast", "Supergrass",
    # Electronic — IDM, ambient, experimental
    "Aphex Twin", "Autechre", "Boards of Canada", "Squarepusher", "Plaid",
    "μ-Ziq", "Clark", "Amon Tobin", "Venetian Snares", "Prefuse 73",
    "Bibio", "Gold Panda", "Shackleton", "Actress", "Andy Stott",
    "Demdike Stare", "Tim Hecker", "William Basinski", "Stars of the Lid",
    "Grouper", "Fennesz", "Oval", "Ryoji Ikeda", "Alva Noto",
    "Forest Swords", "The Haxan Cloak", "Raime", "Emptyset", "Helm",
    # Electronic — dance, club, trip-hop
    "Daft Punk", "Justice", "Chemical Brothers", "Burial", "Four Tet",
    "Caribou", "Floating Points", "Bicep", "Fred again..", "Overmono",
    "Massive Attack", "Portishead", "Tricky", "The xx", "Jamie xx",
    "Bonobo", "Nicolas Jaar", "Jon Hopkins", "Moderat", "SOPHIE",
    "Arca", "Robyn", "Röyksopp", "Air", "Stereolab", "Broadcast",
    "Trentemøller", "James Blake", "FKA twigs", "Kelela", "Kaytranada",
    "Sault", "Washed Out", "Jlin", "Amnesia Scanner", "Tirzah",
    "KODE9", "Cooly G", "Mumdance", "Skee Mask", "Call Super",
    # Techno / Deep House / Club
    "Jeff Mills", "Carl Craig", "Robert Hood", "Juan Atkins",
    "Kevin Saunderson", "Derrick May", "Model 500", "Underground Resistance",
    "Richie Hawtin", "Ben Klock", "Marcel Dettmann", "Surgeon",
    "Blawan", "Andy Stott", "DVS1", "Len Faki",
    "Ben UFO", "Pearson Sound", "Tessela", "Pariah",
    "Objekt", "Move D", "Hunee", "Young Marco",
    "Donato Dozzy", "Voices from the Lake", "Vatican Shadow", "Basic Channel",
    "Larry Heard", "Frankie Knuckles", "Kerri Chandler", "Moodymann",
    "Theo Parrish", "DJ Sprinkles", "DJ Harvey", "Todd Terje",
    "Optimo", "Jennifer Cardini", "Levon Vincent", "KiNK",
    "DJ Stingray", "Peggy Gou", "Daniel Avery", "Dj Koze",
]


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
        """Seed watchlists and register a concert check job for every guild."""
        for guild in self.bot.guilds:
            await self._schedule_for_guild(guild.id)
        logger.info(
            "ConcertsCog ready — scheduled jobs for %d guild(s)", len(self.bot.guilds)
        )

    async def _schedule_for_guild(self, guild_id: int) -> None:
        """
        Load this guild's concert config, seed the artist watchlist if it is
        empty, and (re)register the APScheduler weekly check job.

        Seeding on every call is safe because the seed only runs when the
        watchlist count is zero — subsequent calls skip it in one DB query.
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

            # A single COUNT query is much cheaper than loading all rows on
            # every bot restart just to check whether seeding is needed.
            watchlist_count = (
                session.query(ArtistWatchlist)
                .filter_by(guild_id=guild_id)
                .count()
            )

        if watchlist_count == 0:
            await self._seed_watchlist(guild_id)

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

    async def _seed_watchlist(self, guild_id: int) -> None:
        """
        Bulk-insert all artists from _SEED_ARTISTS into this guild's watchlist.

        Uses per-row IntegrityError handling so a partial seed (e.g. from a
        crash mid-run) can be safely resumed — already-inserted rows are just
        skipped. This is less efficient than a single bulk insert but avoids
        any risk of leaving the watchlist in a broken state.
        """
        logger.info(
            "Seeding %d artists into watchlist for guild %d",
            len(_SEED_ARTISTS), guild_id,
        )
        inserted = 0
        with SessionLocal() as session:
            for name in _SEED_ARTISTS:
                try:
                    session.add(ArtistWatchlist(
                        guild_id=guild_id,
                        artist_name=name,
                        source="seed",
                    ))
                    session.flush()
                    inserted += 1
                except IntegrityError:
                    # Artist already present — skip and continue.
                    session.rollback()
            session.commit()

        logger.info(
            "Watchlist seeded for guild %d: %d artists inserted", guild_id, inserted
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
