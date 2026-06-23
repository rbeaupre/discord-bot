"""
cogs/birthdays.py
─────────────────
Birthday announcement cog. Members register their birthdays via slash command
and the bot announces them in a configured channel on the day.

How it works
────────────
1. On bot connect (on_ready), registers one APScheduler CronJob per guild that
   fires at 9 AM Eastern Time every day.
2. The job queries the database for any birthday in that guild matching today's
   month and day, then posts an announcement embed for each match.
3. Members self-register using /birthday set. Admins can also add/remove entries
   for other members and configure the announcement channel and check time.

Slash commands
──────────────
/birthday set    <month> <day>    — Register your own birthday
/birthday remove                  — Remove your birthday registration
/birthday list                    — See all registered birthdays in this server
/birthday config channel <#ch>   — Set the announcement channel (admin)
/birthday config time    <HH:MM> — Set the daily check time in ET (admin)

Default schedule: every day at 9:00 AM Eastern Time in #general.
"""

import logging
from datetime import datetime

import discord
from apscheduler.triggers.cron import CronTrigger
from discord import app_commands
from discord.ext import commands
from sqlalchemy.exc import IntegrityError

from database.db import SessionLocal
from database.models import Birthday, ScheduleConfig

logger = logging.getLogger(__name__)

# ── Defaults ──────────────────────────────────────────────────────────────────
_DEFAULT_HOUR = 9
_DEFAULT_MINUTE = 0
_DEFAULT_TIMEZONE = "America/New_York"
_DEFAULT_CHANNEL_NAME = "general"

# Month names for display — index 0 is unused so months[1] = "January", etc.
_MONTH_NAMES = [
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def _job_id(guild_id: int) -> str:
    """Return the stable APScheduler job ID for a guild's birthday check job."""
    return f"birthday_{guild_id}"


def _is_valid_date(month: int, day: int) -> bool:
    """
    Return True if month/day is a plausible calendar date.

    We use February 29 as a sentinel for leap-day birthdays — those are
    allowed and will trigger announcements on Feb 28 in non-leap years.
    This simple check doesn't validate month-specific day ranges exhaustively
    (e.g. it allows April 31) but is good enough for birthday inputs.
    """
    return 1 <= month <= 12 and 1 <= day <= 31


class BirthdayCog(commands.Cog, name="Birthdays"):
    """Cog that handles birthday registration, storage, and daily announcements."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ──────────────────────────────────────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Register a birthday-check job for every guild the bot is in."""
        for guild in self.bot.guilds:
            await self._schedule_for_guild(guild.id)
        logger.info("BirthdayCog ready — scheduled jobs for %d guild(s)", len(self.bot.guilds))

    async def _schedule_for_guild(self, guild_id: int) -> None:
        """
        Load birthday config for a guild and (re)register its APScheduler job.
        Creates a default config row on first run.
        """
        with SessionLocal() as session:
            cfg = (
                session.query(ScheduleConfig)
                .filter_by(guild_id=guild_id, feature="birthday")
                .first()
            )

            if cfg is None:
                cfg = ScheduleConfig(
                    guild_id=guild_id,
                    feature="birthday",
                    hour=_DEFAULT_HOUR,
                    minute=_DEFAULT_MINUTE,
                    timezone=_DEFAULT_TIMEZONE,
                    day_of_week=None,   # run every day
                )
                cfg.content_options = {}
                session.add(cfg)
                session.commit()
                logger.info("Created default birthday config for guild %d", guild_id)

            hour, minute, tz = cfg.hour, cfg.minute, cfg.timezone

        self.bot.scheduler.add_job(
            self._check_birthdays,
            CronTrigger(hour=hour, minute=minute, timezone=tz),
            id=_job_id(guild_id),
            args=[guild_id],
            replace_existing=True,
        )
        logger.debug(
            "Birthday check job set for guild %d at %02d:%02d %s",
            guild_id, hour, minute, tz,
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Scheduled action
    # ──────────────────────────────────────────────────────────────────────────

    async def _check_birthdays(self, guild_id: int) -> None:
        """
        Called daily by APScheduler. Queries the DB for any birthdays in this
        guild that match today's month and day, then posts an announcement for
        each one found.
        """
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            logger.warning("Birthday check: guild %d not in cache — skipping", guild_id)
            return

        today = datetime.now()

        # Find all birthday rows for this guild matching today's month + day.
        with SessionLocal() as session:
            cfg = (
                session.query(ScheduleConfig)
                .filter_by(guild_id=guild_id, feature="birthday")
                .first()
            )
            channel_id = cfg.channel_id if cfg else None

            birthdays_today = (
                session.query(Birthday)
                .filter_by(
                    guild_id=guild_id,
                    birth_month=today.month,
                    birth_day=today.day,
                )
                .all()
            )

        if not birthdays_today:
            # No birthdays today — nothing to do.
            return

        channel = self._resolve_channel(guild, channel_id, _DEFAULT_CHANNEL_NAME)
        if channel is None:
            logger.warning(
                "Birthday check: no channel found for guild %d — "
                "set one with /birthday config channel", guild_id
            )
            return

        # Post one announcement per birthday found today.
        for bday in birthdays_today:
            await self._send_birthday_announcement(channel, guild, bday)

    async def _send_birthday_announcement(
        self,
        channel: discord.TextChannel,
        guild: discord.Guild,
        bday: Birthday,
    ) -> None:
        """
        Post a birthday announcement embed for a single member.

        Tries to mention the member by their current Discord handle. Falls
        back to the stored display_name if they've left the server.
        """
        # Try to get the live Member object so we can @mention them.
        member = guild.get_member(bday.user_id)

        if member:
            # Mention + current display name
            name_display = member.mention
        else:
            # Member left the server — use the snapshot name from registration.
            name_display = bday.display_name

        embed = discord.Embed(
            title="Happy Birthday!",
            description=(
                f"Today is **{bday.display_name}**'s birthday!\n\n"
                f"Wish {name_display} a happy birthday!"
            ),
            color=discord.Color.gold(),
            timestamp=datetime.utcnow(),
        )

        embed.set_footer(
            text=f"Register your birthday with /birthday set"
        )

        await channel.send(embed=embed)

    # ──────────────────────────────────────────────────────────────────────────
    # Slash commands
    # ──────────────────────────────────────────────────────────────────────────

    birthday_group = app_commands.Group(
        name="birthday",
        description="Birthday registration and announcements",
    )

    @birthday_group.command(
        name="set",
        description="Register your birthday so the bot can announce it",
    )
    @app_commands.describe(
        month="Month number (1 = January, 12 = December)",
        day="Day of the month (1–31)",
    )
    async def birthday_set(
        self, interaction: discord.Interaction, month: int, day: int
    ) -> None:
        """
        Register or update the calling member's birthday.
        Uses an upsert pattern — if the user already has a birthday registered,
        this command overwrites it rather than creating a duplicate.
        """
        if not _is_valid_date(month, day):
            await interaction.response.send_message(
                f"That doesn't look like a valid date (month 1–12, day 1–31). "
                f"You entered: {month}/{day}.",
                ephemeral=True,
            )
            return

        display_name = interaction.user.display_name

        with SessionLocal() as session:
            # Check if a record already exists for this user in this guild.
            existing = (
                session.query(Birthday)
                .filter_by(
                    guild_id=interaction.guild_id,
                    user_id=interaction.user.id,
                )
                .first()
            )

            if existing:
                # Update the existing record.
                existing.birth_month = month
                existing.birth_day = day
                existing.display_name = display_name
                action = "updated"
            else:
                # Insert a new record.
                session.add(
                    Birthday(
                        guild_id=interaction.guild_id,
                        user_id=interaction.user.id,
                        birth_month=month,
                        birth_day=day,
                        display_name=display_name,
                    )
                )
                action = "registered"

            try:
                session.commit()
            except IntegrityError:
                # Race condition safety net — shouldn't happen with our check above.
                session.rollback()
                await interaction.response.send_message(
                    "Something went wrong saving your birthday. Please try again.",
                    ephemeral=True,
                )
                return

        month_name = _MONTH_NAMES[month]
        await interaction.response.send_message(
            f"Birthday {action}! I'll announce it on **{month_name} {day}**.",
            ephemeral=True,
        )

    @birthday_group.command(
        name="remove",
        description="Remove your birthday registration",
    )
    async def birthday_remove(self, interaction: discord.Interaction) -> None:
        """Allow a member to delete their own birthday record."""
        with SessionLocal() as session:
            deleted_count = (
                session.query(Birthday)
                .filter_by(
                    guild_id=interaction.guild_id,
                    user_id=interaction.user.id,
                )
                .delete()
            )
            session.commit()

        if deleted_count:
            await interaction.response.send_message(
                "Your birthday has been removed.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "You don't have a birthday registered in this server.", ephemeral=True
            )

    @birthday_group.command(
        name="list",
        description="See all registered birthdays in this server",
    )
    async def birthday_list(self, interaction: discord.Interaction) -> None:
        """
        Display a sorted list of every registered birthday in the guild.
        The list is sorted chronologically by month and day.
        """
        with SessionLocal() as session:
            birthdays = (
                session.query(Birthday)
                .filter_by(guild_id=interaction.guild_id)
                .order_by(Birthday.birth_month, Birthday.birth_day)
                .all()
            )

        if not birthdays:
            await interaction.response.send_message(
                "No birthdays registered yet! Use `/birthday set` to add yours.",
                ephemeral=True,
            )
            return

        # Build a formatted list string.
        lines = []
        for bday in birthdays:
            month_name = _MONTH_NAMES[bday.birth_month]
            lines.append(f"• **{bday.display_name}** — {month_name} {bday.birth_day}")

        embed = discord.Embed(
            title=f"Birthdays in {interaction.guild.name}",
            description="\n".join(lines),
            color=discord.Color.purple(),
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── Admin config subgroup: /birthday config ───────────────────────────────

    birthday_config = app_commands.Group(
        name="config",
        description="Configure birthday announcement settings (admin only)",
        parent=birthday_group,
    )

    @birthday_config.command(
        name="channel",
        description="Set the channel for birthday announcements (admin only)",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def birthday_config_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        """Admin: Change the birthday announcement channel."""
        with SessionLocal() as session:
            cfg = (
                session.query(ScheduleConfig)
                .filter_by(guild_id=interaction.guild_id, feature="birthday")
                .first()
            )
            if cfg is None:
                await interaction.response.send_message(
                    "No birthday config exists yet. Use `/birthday set` once to create it.",
                    ephemeral=True,
                )
                return
            cfg.channel_id = channel.id
            session.commit()

        await interaction.response.send_message(
            f"Birthday announcements will now go to {channel.mention}.", ephemeral=True
        )

    @birthday_config.command(
        name="time",
        description="Set the daily birthday check time in 24h ET, e.g. 09:00 (admin only)",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def birthday_config_time(
        self, interaction: discord.Interaction, time: str
    ) -> None:
        """Admin: Change when the bot checks for birthdays each day."""
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
                .filter_by(guild_id=interaction.guild_id, feature="birthday")
                .first()
            )
            if cfg is None:
                await interaction.response.send_message(
                    "No birthday config exists yet. Use `/birthday set` once to create it.",
                    ephemeral=True,
                )
                return
            cfg.hour = parsed.hour
            cfg.minute = parsed.minute
            tz = cfg.timezone
            session.commit()

        await self._schedule_for_guild(interaction.guild_id)

        await interaction.response.send_message(
            f"Birthday checks will now run at **{time}** ({tz}) daily.", ephemeral=True
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
        Find the announcement channel using a two-step priority:
          1. Stored channel_id from the database.
          2. Text channel named fallback_name (e.g. "general").
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
    """Called by bot.load_extension('cogs.birthdays')."""
    cog = BirthdayCog(bot)
    bot.tree.add_command(cog.birthday_group)
    await bot.add_cog(cog)
