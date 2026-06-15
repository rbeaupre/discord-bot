"""
cogs/trivia.py
──────────────
Sports trivia cog. Handles automatic daily trivia posts and on-demand play.

How it works
────────────
1. When the bot connects (on_ready), this cog reads each guild's config from
   the database and registers one APScheduler CronJob per guild.
2. At the scheduled time, the job calls _post_trivia() which asks Claude to
   generate a question and posts it as a Discord embed in the target channel.
3. Admin slash commands let server admins reconfigure the channel, post time,
   and which sports to include — all changes take effect immediately without
   restarting the bot.

Slash commands
──────────────
/trivia play                          — Post a question right now (any member)
/trivia config channel  <#channel>   — Set posting channel (admin)
/trivia config time     <HH:MM>      — Set daily post time in ET (admin)
/trivia config sports   <booleans>   — Toggle which sports are included (admin)

Default schedule: every day at 4:00 PM Eastern Time in #sports-chat.
"""

import logging
from datetime import datetime

import discord
from apscheduler.triggers.cron import CronTrigger
from discord import app_commands
from discord.ext import commands

from database.db import SessionLocal
from database.models import ScheduleConfig
from utils.claude_client import generate_trivia_question

logger = logging.getLogger(__name__)

# ── Defaults used when a guild has no config row yet ─────────────────────────
_DEFAULT_HOUR = 16                                          # 4 PM
_DEFAULT_MINUTE = 0
_DEFAULT_TIMEZONE = "America/New_York"
_DEFAULT_SPORTS = ["soccer", "baseball", "football", "hockey"]
_DEFAULT_CHANNEL_NAME = "sports-chat"                       # fallback channel name


def _job_id(guild_id: int) -> str:
    """Return the stable APScheduler job ID for a guild's trivia job."""
    return f"trivia_{guild_id}"


class TriviaCog(commands.Cog, name="Trivia"):
    """Cog that manages sports trivia scheduling, generation, and config."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ──────────────────────────────────────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """
        Fires once the bot has a full list of guilds. We iterate every guild
        and register (or replace) its scheduled trivia job. Using replace_existing
        means reconnects don't create duplicate jobs.
        """
        for guild in self.bot.guilds:
            await self._schedule_for_guild(guild.id)
        logger.info("TriviaCog ready — scheduled jobs for %d guild(s)", len(self.bot.guilds))

    async def _schedule_for_guild(self, guild_id: int) -> None:
        """
        Read this guild's trivia config from the DB and (re)register its
        APScheduler job. Creates a default config row if none exists yet.
        """
        with SessionLocal() as session:
            cfg = (
                session.query(ScheduleConfig)
                .filter_by(guild_id=guild_id, feature="trivia")
                .first()
            )

            if cfg is None:
                # First time we've seen this guild — write default config so
                # future admin commands have a row to update.
                cfg = ScheduleConfig(
                    guild_id=guild_id,
                    feature="trivia",
                    hour=_DEFAULT_HOUR,
                    minute=_DEFAULT_MINUTE,
                    timezone=_DEFAULT_TIMEZONE,
                    day_of_week=None,   # None means every day (not weekly)
                )
                cfg.content_options = {"sports": _DEFAULT_SPORTS}
                session.add(cfg)
                session.commit()
                logger.info("Created default trivia config for guild %d", guild_id)

            # Snapshot the values we need before the session closes.
            hour, minute, tz = cfg.hour, cfg.minute, cfg.timezone

        # add_job with replace_existing=True is idempotent — calling it again
        # after a config change simply updates the existing job's trigger.
        self.bot.scheduler.add_job(
            self._post_trivia,
            CronTrigger(hour=hour, minute=minute, timezone=tz),
            id=_job_id(guild_id),
            args=[guild_id],
            replace_existing=True,
        )
        logger.debug("Trivia job set for guild %d at %02d:%02d %s", guild_id, hour, minute, tz)

    # ──────────────────────────────────────────────────────────────────────────
    # Scheduled action
    # ──────────────────────────────────────────────────────────────────────────

    async def _post_trivia(self, guild_id: int) -> None:
        """
        Main scheduled action. Called by APScheduler at the configured time.
        Fetches the latest config from the DB (so admin changes are picked up
        without a restart), then generates and posts a trivia question.
        """
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            # Bot may have been removed from the guild since the job was registered.
            logger.warning("Trivia job fired but guild %d not in cache — skipping", guild_id)
            return

        with SessionLocal() as session:
            cfg = (
                session.query(ScheduleConfig)
                .filter_by(guild_id=guild_id, feature="trivia")
                .first()
            )
            channel_id = cfg.channel_id if cfg else None
            sports = (
                cfg.content_options.get("sports", _DEFAULT_SPORTS)
                if cfg else _DEFAULT_SPORTS
            )

        # Try the admin-configured channel first, then fall back to #sports-chat.
        channel = self._resolve_channel(guild, channel_id, _DEFAULT_CHANNEL_NAME)
        if channel is None:
            logger.warning(
                "Trivia job: no channel found for guild %d — "
                "set one with /trivia config channel", guild_id
            )
            return

        await self._send_trivia_embed(channel, sports)

    # ──────────────────────────────────────────────────────────────────────────
    # Embed builder
    # ──────────────────────────────────────────────────────────────────────────

    async def _send_trivia_embed(
        self, channel: discord.TextChannel, sports: list[str]
    ) -> None:
        """
        Generate a trivia question via Claude and post it as a Discord embed.
        The answer is wrapped in a Discord spoiler (||text||) so members have
        to click/tap to reveal it.
        """
        try:
            data = generate_trivia_question(sports)
        except Exception as exc:
            # Log the full error but show a clean message to Discord users.
            logger.error("Trivia generation failed: %s", exc, exc_info=True)
            await channel.send(
                "Could not generate a trivia question right now. Try `/trivia play` again later."
            )
            return

        embed = discord.Embed(
            title=f"Sports Trivia — {data['sport'].title()}",
            description=data["question"],
            color=discord.Color.blue(),
            timestamp=datetime.utcnow(),
        )

        # Add the four answer options as inline fields (displayed in a 2×2 grid).
        for letter, text in data["options"].items():
            embed.add_field(name=letter, value=text, inline=True)

        # The answer + explanation hidden behind a spoiler tag.
        # Members click the blurred text to reveal it.
        embed.add_field(
            name="Answer (spoiler)",
            value=f"||**{data['correct']}** — {data['explanation']}||",
            inline=False,
        )

        embed.set_footer(text="Use /trivia play to get another question any time!")

        await channel.send(embed=embed)

    # ──────────────────────────────────────────────────────────────────────────
    # Slash commands
    # ──────────────────────────────────────────────────────────────────────────

    # Top-level command group: /trivia
    trivia_group = app_commands.Group(
        name="trivia",
        description="Sports trivia commands",
    )

    @trivia_group.command(
        name="play",
        description="Post a sports trivia question right now",
    )
    async def trivia_play(self, interaction: discord.Interaction) -> None:
        """
        On-demand trivia — any member can use this.
        We defer first because Claude may take a second or two to respond.
        """
        await interaction.response.defer(thinking=True)

        # Load the current sports list for this guild.
        with SessionLocal() as session:
            cfg = (
                session.query(ScheduleConfig)
                .filter_by(guild_id=interaction.guild_id, feature="trivia")
                .first()
            )
            sports = (
                cfg.content_options.get("sports", _DEFAULT_SPORTS)
                if cfg else _DEFAULT_SPORTS
            )

        # Post the embed directly to the channel where the command was used.
        await self._send_trivia_embed(interaction.channel, sports)

        # followup.send() is required after defer() — we send a silent ack.
        await interaction.followup.send("Here's your trivia question!", ephemeral=True)

    # ── Admin config subgroup: /trivia config ─────────────────────────────────

    trivia_config = app_commands.Group(
        name="config",
        description="Configure trivia settings (admin only)",
        parent=trivia_group,
    )

    @trivia_config.command(
        name="channel",
        description="Set the channel where daily trivia posts appear (admin only)",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def trivia_config_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        """Admin: Change which channel receives daily trivia posts."""
        with SessionLocal() as session:
            cfg = (
                session.query(ScheduleConfig)
                .filter_by(guild_id=interaction.guild_id, feature="trivia")
                .first()
            )
            if cfg is None:
                await interaction.response.send_message(
                    "No trivia config exists yet. Use `/trivia play` once to create it.",
                    ephemeral=True,
                )
                return

            cfg.channel_id = channel.id
            session.commit()

        await interaction.response.send_message(
            f"Daily trivia will now post to {channel.mention}.", ephemeral=True
        )

    @trivia_config.command(
        name="time",
        description="Set the daily trivia post time in 24h ET, e.g. 16:00 (admin only)",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def trivia_config_time(
        self, interaction: discord.Interaction, time: str
    ) -> None:
        """
        Admin: Change the daily posting time.
        The time string must be HH:MM in 24-hour format, e.g. "16:00" for 4 PM.
        All times are interpreted as Eastern Time.
        """
        try:
            parsed = datetime.strptime(time, "%H:%M")
        except ValueError:
            await interaction.response.send_message(
                "Invalid format. Use HH:MM in 24-hour notation, e.g. `16:00`.",
                ephemeral=True,
            )
            return

        with SessionLocal() as session:
            cfg = (
                session.query(ScheduleConfig)
                .filter_by(guild_id=interaction.guild_id, feature="trivia")
                .first()
            )
            if cfg is None:
                await interaction.response.send_message(
                    "No trivia config exists yet. Use `/trivia play` once to create it.",
                    ephemeral=True,
                )
                return

            cfg.hour = parsed.hour
            cfg.minute = parsed.minute
            tz = cfg.timezone
            session.commit()

        # Reschedule the APScheduler job immediately so the new time is live now.
        await self._schedule_for_guild(interaction.guild_id)

        await interaction.response.send_message(
            f"Daily trivia will now post at **{time}** ({tz}).", ephemeral=True
        )

    @trivia_config.command(
        name="sports",
        description="Choose which sports appear in trivia questions (admin only)",
    )
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(
        soccer="Include soccer questions",
        baseball="Include baseball questions",
        football="Include American football questions",
        hockey="Include hockey questions",
    )
    async def trivia_config_sports(
        self,
        interaction: discord.Interaction,
        soccer: bool = True,
        baseball: bool = True,
        football: bool = True,
        hockey: bool = True,
    ) -> None:
        """Admin: Toggle which sports Claude draws from when generating questions."""
        # Build the list of enabled sports from the boolean parameters.
        selected = [
            name
            for name, enabled in [
                ("soccer", soccer),
                ("baseball", baseball),
                ("football", football),
                ("hockey", hockey),
            ]
            if enabled
        ]

        if not selected:
            await interaction.response.send_message(
                "At least one sport must be enabled.", ephemeral=True
            )
            return

        with SessionLocal() as session:
            cfg = (
                session.query(ScheduleConfig)
                .filter_by(guild_id=interaction.guild_id, feature="trivia")
                .first()
            )
            if cfg is None:
                await interaction.response.send_message(
                    "No trivia config exists yet. Use `/trivia play` once to create it.",
                    ephemeral=True,
                )
                return

            cfg.content_options = {"sports": selected}
            session.commit()

        await interaction.response.send_message(
            f"Trivia will now draw from: **{', '.join(selected)}**.", ephemeral=True
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Error handler
    # ──────────────────────────────────────────────────────────────────────────

    @trivia_group.error
    async def trivia_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        """Catch admin-permission failures and explain them instead of silently ignoring."""
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "You need Administrator permissions to use that command.", ephemeral=True
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
        Find the best text channel to post to.

        Priority order:
          1. The channel_id stored in the database (set by an admin).
          2. The first text channel whose name matches fallback_name (e.g. "sports-chat").
          3. None — the caller should log a warning and skip posting.
        """
        if channel_id:
            channel = guild.get_channel(channel_id)
            if isinstance(channel, discord.TextChannel):
                return channel

        # Name-based fallback — case-insensitive, strips leading # if present.
        target = fallback_name.lstrip("#").lower()
        for ch in guild.text_channels:
            if ch.name.lower() == target:
                return ch

        return None


async def setup(bot: commands.Bot) -> None:
    """
    Called automatically by bot.load_extension('cogs.trivia').
    Registers the TriviaCog and its slash command group with the bot.
    """
    cog = TriviaCog(bot)
    # Manually add the slash command group since it's defined as a class attribute.
    bot.tree.add_command(cog.trivia_group)
    await bot.add_cog(cog)
