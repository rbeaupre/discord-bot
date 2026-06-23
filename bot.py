"""
bot.py
──────
Entry point for the Discord bot. Run this file to start the bot:

    python bot.py

What happens at startup
────────────────────────
1. init_db() creates any missing database tables.
2. DiscordBot.__init__() creates the APScheduler instance.
3. setup_hook() (called before the bot connects to Discord):
      a. Starts the APScheduler so it's running before cogs register their jobs.
      b. Loads all three feature cogs (trivia, music, birthdays).
      c. Syncs slash commands to Discord — instantly if DEV_GUILD_ID is set,
         otherwise globally (takes up to 1 hour to propagate).
4. The bot connects and on_ready() fires in each cog, which registers
   their APScheduler jobs based on config from the database.
"""

import asyncio
import logging

import discord
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from discord import app_commands
from discord.ext import commands

import config
from database.db import init_db

# ── Logging setup ─────────────────────────────────────────────────────────────
# Log INFO and above to stdout so we can see what the bot is doing.
# In production, redirect this to a file or Cloud Logging as needed.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


class DiscordBot(commands.Bot):
    """
    Subclass of commands.Bot that adds our scheduler and manages cog loading.

    We subclass rather than using the bare Bot instance so that setup_hook()
    and on_ready() are clean overrides with no decorator boilerplate.
    """

    def __init__(self) -> None:
        # Intents declare which Discord events the bot receives.
        # members=True is required to fetch server member lists (for birthday
        # lookups and @mentions). It must also be enabled in the Developer Portal.
        intents = discord.Intents.default()
        intents.members = True

        super().__init__(
            command_prefix="!",     # prefix for legacy text commands (mostly unused)
            intents=intents,
            # Suppress the default help command since we use slash commands.
            help_command=None,
        )

        # The scheduler is stored on the bot instance so every cog can access
        # it via self.bot.scheduler without needing a global variable.
        # We create it here but start it in setup_hook() (which runs in the
        # asyncio event loop) since AsyncIOScheduler requires an active loop.
        self.scheduler = AsyncIOScheduler(timezone="America/New_York")

    async def setup_hook(self) -> None:
        """
        Called once by discord.py after __init__ but before the bot connects
        to Discord. This is the right place for async setup work.

        Order matters here:
          1. Start the scheduler first so cog on_ready listeners can safely add jobs.
          2. Load cogs (their on_ready listeners are registered but not yet called).
          3. Sync slash commands so Discord knows about the bot's commands.
        """
        # Start the scheduler in the running event loop.
        self.scheduler.start()
        logger.info("APScheduler started")

        # Load each feature cog. If one fails, log and continue so the other
        # features still work.
        for cog_path in ["cogs.trivia", "cogs.music", "cogs.birthdays"]:
            try:
                await self.load_extension(cog_path)
                logger.info("Loaded cog: %s", cog_path)
            except Exception as exc:
                logger.error("Failed to load cog %s: %s", cog_path, exc, exc_info=True)

        # Sync the slash command tree with Discord.
        # Global error handler for all slash commands. Catches errors that bubble
        # up from any command, including missing-permissions failures on admin
        # commands. The per-group @group.error decorator doesn't work on class
        # methods (discord.py rejects the extra 'self' parameter), so we handle
        # it globally here instead.
        @self.tree.error
        async def on_app_command_error(
            interaction: discord.Interaction, error: app_commands.AppCommandError
        ) -> None:
            """Inform the user when they lack permissions for an admin command."""
            if isinstance(error, app_commands.MissingPermissions):
                msg = "You need Administrator permissions to use that command."
                # Use followup if the interaction was already deferred, otherwise respond directly.
                if interaction.response.is_done():
                    await interaction.followup.send(msg, ephemeral=True)
                else:
                    await interaction.response.send_message(msg, ephemeral=True)
            else:
                # Log unexpected errors so they're visible in the container logs.
                logger.error(
                    "Unhandled app command error in /%s: %s",
                    interaction.command.name if interaction.command else "unknown",
                    error,
                    exc_info=True,
                )

        if config.DEV_GUILD_ID:
            # Guild sync is instant and great for testing during development.
            # It only registers commands in the specified server, not globally.
            guild_obj = discord.Object(id=config.DEV_GUILD_ID)
            # copy_global_to copies globally-registered commands into the guild
            # scope so they appear instantly without waiting for global sync.
            self.tree.copy_global_to(guild=guild_obj)
            await self.tree.sync(guild=guild_obj)
            logger.info("Slash commands synced to dev guild %d", config.DEV_GUILD_ID)
        else:
            # Global sync makes commands available in all servers the bot joins,
            # but changes take up to 1 hour to propagate across Discord's CDN.
            await self.tree.sync()
            logger.info("Slash commands synced globally (may take up to 1 hour)")

    async def on_ready(self) -> None:
        """
        Called by discord.py when the bot has connected and all guilds are
        cached. Cog on_ready listeners are also called at this point, which
        is when they register their APScheduler jobs.
        """
        logger.info(
            "Bot ready — logged in as %s (ID: %d) in %d server(s)",
            self.user,
            self.user.id,
            len(self.guilds),
        )

    async def on_guild_join(self, guild: discord.Guild) -> None:
        """
        Fires when the bot is added to a new server. We proactively create
        default configs for the new guild so admin commands work immediately.
        The individual cogs handle this in their _schedule_for_guild() calls,
        so we just trigger each one here.
        """
        logger.info("Joined new guild: %s (ID: %d)", guild.name, guild.id)

        # Each cog's _schedule_for_guild creates default config rows if missing.
        for cog_name in ["Trivia", "Music", "Birthdays"]:
            cog = self.get_cog(cog_name)
            if cog:
                await cog._schedule_for_guild(guild.id)


def main() -> None:
    """Initialize the database and start the bot."""
    # Create database tables before connecting to Discord.
    # This is safe to call on every startup — only missing tables are created.
    logger.info("Initializing database...")
    init_db()
    logger.info("Database ready")

    # Create and run the bot. bot.run() blocks until the bot is stopped.
    bot = DiscordBot()
    bot.run(config.DISCORD_TOKEN, log_handler=None)  # log_handler=None avoids duplicate logs


if __name__ == "__main__":
    main()
