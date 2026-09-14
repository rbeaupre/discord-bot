"""
cogs/fantasy.py
────────────────
ESPN Fantasy Football tie-in for live NFL scoring alerts. Enriches the
scoring play embeds cogs/sports_scores.py posts with which fantasy manager
owns the scoring player, e.g. "Alice's player, Kyren Williams, scores!"
(see cogs/sports_scores.py's _get_fantasy_manager / _post_scoring_play).

How it works
────────────
1. Admins point the bot at a league with /fantasy config league, and supply
   ESPN session cookies (espn_s2 + SWID) via /fantasy config cookies. The
   cookies are entered through a Discord modal, not a command argument —
   Discord shows slash command *argument values* in the channel as part of
   the "X used /command" message regardless of whether the bot's reply is
   ephemeral, but a modal submission has no such public echo. This matters
   because these cookies are a real credential — treat them like a password,
   not a config value.
2. A daily APScheduler CronJob refreshes the guild's player -> manager
   mapping into fantasy_roster_entries, replacing the previous snapshot
   wholesale (ESPN doesn't expose an incremental roster-change feed, so a
   full re-sync is the only reliable way to catch trades/waivers/drops).
3. ESPN's fantasy cookies have no published expiry and can stop working at
   any time. When a refresh gets a 401, the guild's cookies_valid flag flips
   to False and a one-time alert posts to the configured channel — flipping
   back on the next successful refresh, so a still-broken config doesn't
   re-alert every single day.

Slash commands
──────────────
/fantasy status                              — Show config + roster cache status (anyone)
/fantasy config league <league_id> [season]  — Set the ESPN league ID (admin)
/fantasy config cookies                      — Open a modal to set espn_s2/SWID (admin)
/fantasy config channel <#ch>                — Set the refresh-failure alert channel (admin)
/fantasy config time <HH:MM>                 — Set the daily refresh time in ET (admin)
/fantasy refresh                             — Refresh the roster cache right now (admin)

Default schedule: every day at 8:00 AM Eastern Time. No default channel —
alerts are dropped (logged only) until one is configured.
"""

import logging
from datetime import datetime

import discord
from apscheduler.triggers.cron import CronTrigger
from discord import app_commands
from discord.ext import commands

from database.db import SessionLocal
from database.models import FantasyLeagueConfig, FantasyRosterEntry, ScheduleConfig
from utils.espn_fantasy_client import FantasyAPIError, FantasyAuthError, get_league_rosters

logger = logging.getLogger(__name__)

# ── Defaults ──────────────────────────────────────────────────────────────────
_DEFAULT_HOUR = 8
_DEFAULT_MINUTE = 0
_DEFAULT_TIMEZONE = "America/New_York"


def _job_id(guild_id: int) -> str:
    """Return the stable APScheduler job ID for a guild's daily roster refresh."""
    return f"fantasy_refresh_{guild_id}"


def _current_nfl_season() -> int:
    """
    Return the NFL season year for "today".

    ESPN's fantasy API keys a season by the year it started in — the
    Jan/Feb tail end of a season (e.g. January 2027, wrapping up the
    2026 season) still needs season=2026, not 2027.
    """
    now = datetime.now()
    return now.year - 1 if now.month <= 2 else now.year


class CookiesModal(discord.ui.Modal, title="ESPN Fantasy Cookies"):
    """
    Collects espn_s2 and SWID privately from the invoking admin.

    Used instead of slash command arguments because Discord echoes command
    argument values into the channel as part of the "X used /command"
    message — visible to everyone with access to that channel, regardless of
    whether the bot's own reply is ephemeral. A modal's submitted values are
    never posted as a visible message at all, which is the right handling
    for a real credential.
    """

    espn_s2 = discord.ui.TextInput(
        label="espn_s2 cookie value",
        style=discord.TextStyle.paragraph,
        required=True,
    )
    swid = discord.ui.TextInput(
        label="SWID cookie value (with { } braces)",
        required=True,
    )

    def __init__(self, guild_id: int) -> None:
        super().__init__()
        self._guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        with SessionLocal() as session:
            cfg = (
                session.query(FantasyLeagueConfig)
                .filter_by(guild_id=self._guild_id)
                .first()
            )
            if cfg is None:
                cfg = FantasyLeagueConfig(
                    guild_id=self._guild_id,
                    league_id=0,
                    season=_current_nfl_season(),
                )
                session.add(cfg)
            cfg.espn_s2 = str(self.espn_s2)
            cfg.swid = str(self.swid)
            cfg.cookies_valid = True
            session.commit()

        needs_league = cfg.league_id == 0
        msg = "Fantasy cookies saved."
        if needs_league:
            msg += " Set your league with `/fantasy config league` next, then `/fantasy refresh` to sync it."
        else:
            msg += " Run `/fantasy refresh` to sync your roster now."
        await interaction.response.send_message(msg, ephemeral=True)


class FantasyCog(commands.Cog, name="Fantasy"):
    """Cog that ties ESPN Fantasy Football rosters to live NFL scoring alerts."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ──────────────────────────────────────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Register a daily roster-refresh job for every guild the bot is in."""
        for guild in self.bot.guilds:
            await self._schedule_for_guild(guild.id)
        logger.info("FantasyCog ready — scheduled jobs for %d guild(s)", len(self.bot.guilds))

    async def _schedule_for_guild(self, guild_id: int) -> None:
        """
        Load (or create) this guild's fantasy schedule config and register
        the daily roster-refresh job. Reuses the generic ScheduleConfig table
        (feature="fantasy") for channel/time, the same pattern as every other
        feature — league_id/season/cookies live in FantasyLeagueConfig
        instead, since those are specific to this feature, not schedule/channel
        settings.
        """
        with SessionLocal() as session:
            cfg = (
                session.query(ScheduleConfig)
                .filter_by(guild_id=guild_id, feature="fantasy")
                .first()
            )
            if cfg is None:
                cfg = ScheduleConfig(
                    guild_id=guild_id,
                    feature="fantasy",
                    hour=_DEFAULT_HOUR,
                    minute=_DEFAULT_MINUTE,
                    timezone=_DEFAULT_TIMEZONE,
                    day_of_week=None,
                )
                cfg.content_options = {}
                session.add(cfg)
                session.commit()
                logger.info("Created default fantasy config for guild %d", guild_id)

            hour, minute, tz = cfg.hour, cfg.minute, cfg.timezone

        self.bot.scheduler.add_job(
            self._refresh_rosters,
            CronTrigger(hour=hour, minute=minute, timezone=tz),
            id=_job_id(guild_id),
            args=[guild_id],
            replace_existing=True,
        )
        logger.debug(
            "Fantasy refresh job set for guild %d at %02d:%02d %s", guild_id, hour, minute, tz
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Scheduled action
    # ──────────────────────────────────────────────────────────────────────────

    async def _refresh_rosters(self, guild_id: int) -> dict:
        """
        Refresh a guild's cached player -> manager roster mapping from ESPN.

        Called daily by APScheduler, and directly by /fantasy refresh (which
        uses the returned summary dict to report results to the admin).
        Replaces the guild's fantasy_roster_entries wholesale on success.

        Returns
        -------
        dict with keys: "ok" (bool), "player_count" (int, only if ok), and
        "error" (str, only if not ok) — lets /fantasy refresh report a
        specific outcome without duplicating the fetch/error-handling logic.
        """
        with SessionLocal() as session:
            league_cfg = session.query(FantasyLeagueConfig).filter_by(guild_id=guild_id).first()

        if league_cfg is None or not league_cfg.league_id or not league_cfg.espn_s2 or not league_cfg.swid:
            return {"ok": False, "error": "not_configured"}

        try:
            rosters = get_league_rosters(
                league_cfg.league_id, league_cfg.season, league_cfg.espn_s2, league_cfg.swid,
            )
        except FantasyAuthError:
            await self._handle_auth_failure(guild_id, league_cfg)
            return {"ok": False, "error": "auth"}
        except FantasyAPIError as exc:
            logger.warning("Fantasy roster refresh failed for guild %d: %s", guild_id, exc)
            return {"ok": False, "error": str(exc)}

        with SessionLocal() as session:
            session.query(FantasyRosterEntry).filter_by(guild_id=guild_id).delete()
            for entry in rosters:
                session.add(FantasyRosterEntry(
                    guild_id=guild_id,
                    espn_player_id=entry["espn_player_id"],
                    player_name=entry["player_name"],
                    manager_name=entry["manager_name"],
                ))
            fresh_cfg = session.get(FantasyLeagueConfig, league_cfg.id)
            fresh_cfg.last_refreshed_at = datetime.utcnow()
            was_invalid = not fresh_cfg.cookies_valid
            fresh_cfg.cookies_valid = True
            session.commit()

        logger.info("Fantasy roster refresh for guild %d: %d players cached", guild_id, len(rosters))

        if was_invalid:
            # Cookies had previously been flagged broken and are now working
            # again — say so rather than silently recovering.
            await self._post_alert(
                guild_id,
                "✅ Fantasy football cookies are working again — roster sync resumed.",
            )

        return {"ok": True, "player_count": len(rosters)}

    async def _handle_auth_failure(self, guild_id: int, league_cfg: FantasyLeagueConfig) -> None:
        """
        Mark the guild's cookies invalid and post a one-time alert. Repeated
        daily failures while broken don't re-alert — only the valid -> invalid
        transition does, so the channel isn't spammed every day the admin
        hasn't gotten around to refreshing the cookies yet.
        """
        already_alerted = not league_cfg.cookies_valid
        with SessionLocal() as session:
            cfg = session.get(FantasyLeagueConfig, league_cfg.id)
            cfg.cookies_valid = False
            session.commit()

        if not already_alerted:
            await self._post_alert(
                guild_id,
                "⚠️ ESPN rejected the stored fantasy football cookies (they've "
                "likely expired). Run `/fantasy config cookies` to enter fresh "
                "values — see the README for how to grab them from your browser.",
            )

    async def _post_alert(self, guild_id: int, message: str) -> None:
        """Post a plain-text alert to this guild's configured fantasy channel."""
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return

        with SessionLocal() as session:
            cfg = (
                session.query(ScheduleConfig)
                .filter_by(guild_id=guild_id, feature="fantasy")
                .first()
            )
            channel_id = cfg.channel_id if cfg else None

        channel = guild.get_channel(channel_id) if channel_id else None
        if not isinstance(channel, discord.TextChannel):
            logger.warning(
                "Fantasy alert dropped — no channel configured for guild %d "
                "(set one with /fantasy config channel): %s",
                guild_id, message,
            )
            return

        await channel.send(message)

    # ──────────────────────────────────────────────────────────────────────────
    # Slash commands
    # ──────────────────────────────────────────────────────────────────────────

    fantasy_group = app_commands.Group(
        name="fantasy",
        description="ESPN Fantasy Football tie-in for live NFL scoring alerts",
    )

    @fantasy_group.command(
        name="status",
        description="Show the current fantasy football configuration",
    )
    async def fantasy_status(self, interaction: discord.Interaction) -> None:
        """Show league config, cookie health, and roster cache size. Available to all members."""
        with SessionLocal() as session:
            league_cfg = (
                session.query(FantasyLeagueConfig)
                .filter_by(guild_id=interaction.guild_id)
                .first()
            )
            player_count = (
                session.query(FantasyRosterEntry)
                .filter_by(guild_id=interaction.guild_id)
                .count()
            )
            sched_cfg = (
                session.query(ScheduleConfig)
                .filter_by(guild_id=interaction.guild_id, feature="fantasy")
                .first()
            )

        if league_cfg is None or not league_cfg.league_id:
            await interaction.response.send_message(
                "Fantasy football hasn't been configured yet. "
                "Use `/fantasy config league` and `/fantasy config cookies` to get started.",
                ephemeral=True,
            )
            return

        cookie_status = (
            "not set" if not league_cfg.espn_s2 or not league_cfg.swid
            else "valid" if league_cfg.cookies_valid
            else "expired — run `/fantasy config cookies`"
        )
        last_refreshed = (
            league_cfg.last_refreshed_at.strftime("%Y-%m-%d %H:%M UTC")
            if league_cfg.last_refreshed_at else "never"
        )
        channel_mention = (
            f"<#{sched_cfg.channel_id}>" if sched_cfg and sched_cfg.channel_id
            else "not set — alerts are logged only"
        )
        schedule_line = (
            f"{sched_cfg.hour:02d}:{sched_cfg.minute:02d} ({sched_cfg.timezone})"
            if sched_cfg else "not set"
        )

        await interaction.response.send_message(
            f"**League ID:** {league_cfg.league_id} (season {league_cfg.season})\n"
            f"**Cookies:** {cookie_status}\n"
            f"**Cached players:** {player_count}\n"
            f"**Last refresh:** {last_refreshed}\n"
            f"**Daily refresh time:** {schedule_line}\n"
            f"**Alert channel:** {channel_mention}",
            ephemeral=True,
        )

    @fantasy_group.command(
        name="refresh",
        description="Refresh the fantasy roster cache from ESPN right now (admin only)",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def fantasy_refresh(self, interaction: discord.Interaction) -> None:
        """Admin: Force an immediate roster refresh, reporting the outcome."""
        await interaction.response.defer(ephemeral=True)
        result = await self._refresh_rosters(interaction.guild_id)

        if result["ok"]:
            await interaction.followup.send(
                f"Roster synced — {result['player_count']} players cached.", ephemeral=True
            )
        elif result["error"] == "not_configured":
            await interaction.followup.send(
                "Fantasy football isn't fully configured yet — set both "
                "`/fantasy config league` and `/fantasy config cookies` first.",
                ephemeral=True,
            )
        elif result["error"] == "auth":
            await interaction.followup.send(
                "ESPN rejected the stored cookies (expired). "
                "Run `/fantasy config cookies` to enter fresh values.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(f"Refresh failed: {result['error']}", ephemeral=True)

    # ── Admin config subgroup: /fantasy config ────────────────────────────────

    fantasy_config = app_commands.Group(
        name="config",
        description="Configure ESPN Fantasy Football settings (admin only)",
        parent=fantasy_group,
    )

    @fantasy_config.command(
        name="league",
        description="Set the ESPN Fantasy league ID (admin only)",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(
        league_id="The numeric leagueId from your league's fantasy.espn.com URL",
        season="NFL season year, e.g. 2026 — defaults to the current season",
    )
    async def fantasy_config_league(
        self,
        interaction: discord.Interaction,
        league_id: str,
        season: int | None = None,
    ) -> None:
        """Admin: Set (or update) which ESPN league this guild's fantasy tie-in reads from."""
        if not league_id.isdigit():
            await interaction.response.send_message(
                "League ID should be numeric — copy the `leagueId` value from your "
                "league's fantasy.espn.com URL.",
                ephemeral=True,
            )
            return

        resolved_season = season or _current_nfl_season()

        with SessionLocal() as session:
            cfg = session.query(FantasyLeagueConfig).filter_by(guild_id=interaction.guild_id).first()
            if cfg is None:
                cfg = FantasyLeagueConfig(guild_id=interaction.guild_id, league_id=int(league_id), season=resolved_season)
                session.add(cfg)
            else:
                cfg.league_id = int(league_id)
                cfg.season = resolved_season
            session.commit()
            has_cookies = bool(cfg.espn_s2 and cfg.swid)

        msg = f"Fantasy league set to `{league_id}` (season {resolved_season})."
        if has_cookies:
            msg += " Run `/fantasy refresh` to sync it now."
        else:
            msg += " Set cookies with `/fantasy config cookies` next, then `/fantasy refresh`."
        await interaction.response.send_message(msg, ephemeral=True)

    @fantasy_config.command(
        name="cookies",
        description="Set your ESPN session cookies via a private form (admin only)",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def fantasy_config_cookies(self, interaction: discord.Interaction) -> None:
        """
        Admin: Open a modal to enter espn_s2/SWID.

        Deliberately takes no command arguments — see CookiesModal's
        docstring for why these values must never be typed as slash command
        arguments.
        """
        await interaction.response.send_modal(CookiesModal(interaction.guild_id))

    @fantasy_config.command(
        name="channel",
        description="Set the channel for cookie-expiration alerts (admin only)",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def fantasy_config_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        """Admin: Change which channel receives cookie-expiration alerts."""
        with SessionLocal() as session:
            cfg = (
                session.query(ScheduleConfig)
                .filter_by(guild_id=interaction.guild_id, feature="fantasy")
                .first()
            )
            if cfg is None:
                await interaction.response.send_message(
                    "No fantasy config exists yet. Use `/fantasy status` to initialize it.",
                    ephemeral=True,
                )
                return
            cfg.channel_id = channel.id
            session.commit()

        await interaction.response.send_message(
            f"Fantasy alerts will now post to {channel.mention}.", ephemeral=True
        )

    @fantasy_config.command(
        name="time",
        description="Set the daily roster refresh time in 24h ET, e.g. 08:00 (admin only)",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def fantasy_config_time(self, interaction: discord.Interaction, time: str) -> None:
        """Admin: Change when the daily roster refresh runs."""
        try:
            parsed = datetime.strptime(time, "%H:%M")
        except ValueError:
            await interaction.response.send_message(
                "Invalid format. Use HH:MM in 24-hour notation, e.g. `08:00`.",
                ephemeral=True,
            )
            return

        with SessionLocal() as session:
            cfg = (
                session.query(ScheduleConfig)
                .filter_by(guild_id=interaction.guild_id, feature="fantasy")
                .first()
            )
            if cfg is None:
                await interaction.response.send_message(
                    "No fantasy config exists yet. Use `/fantasy status` to initialize it.",
                    ephemeral=True,
                )
                return
            cfg.hour = parsed.hour
            cfg.minute = parsed.minute
            tz = cfg.timezone
            session.commit()

        await self._schedule_for_guild(interaction.guild_id)

        await interaction.response.send_message(
            f"Fantasy roster refresh will now run at **{time}** ({tz}) daily.", ephemeral=True
        )


async def setup(bot: commands.Bot) -> None:
    """Called by bot.load_extension('cogs.fantasy')."""
    await bot.add_cog(FantasyCog(bot))
