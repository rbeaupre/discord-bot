"""
cogs/chat.py
────────────
General chat cog. Lets members talk to Claude by @mentioning the bot.

How it works
────────────
1. on_message listens for any message that @mentions the bot.
2. The mention token is stripped out of the message text and whatever
   remains is sent to Claude via utils.claude_client.chat_reply().
3. Claude's reply is posted back as a reply to the triggering message.

No slash commands, no admin config, no per-guild schedule — this feature is
simply always on, in any channel the bot can see and read messages in.

Guardrails
──────────
See utils.claude_client.chat_reply() / _CHAT_SYSTEM_PROMPT for the full
detail. In short: Claude is only ever given the single message's text — no
channel history, no database contents, no environment/infrastructure
details, and no tools/function-calling — so there is nothing sensitive
available for it to leak even if a message successfully prompt-injects it.
The system prompt is a second layer that tells Claude to refuse outright if
asked to reveal internals or produce real personal information about a
server member.

Requires the "Message Content" privileged intent to be enabled for this bot
in the Discord Developer Portal (Bot → Privileged Gateway Intents) in
addition to intents.message_content = True in bot.py — without both,
message.content arrives empty for messages the bot didn't author itself.
"""

import logging

import discord
from discord.ext import commands

from utils.claude_client import chat_reply

logger = logging.getLogger(__name__)

# Discord hard-caps message content at 2000 characters. Claude's max_tokens
# for chat replies keeps responses well under this in practice, but truncate
# defensively in case of an unusually long reply.
_DISCORD_MESSAGE_LIMIT = 2000


class ChatCog(commands.Cog, name="Chat"):
    """Cog that answers @mentions with a Claude-generated reply."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """
        Reply to any message that @mentions the bot, using Claude.

        Ignores messages from bots (including itself, to avoid reply loops)
        and DMs — this feature is scoped to guild text channels only.
        """
        if message.author.bot:
            return
        if message.guild is None:
            return
        if self.bot.user not in message.mentions:
            return

        question = self._strip_mention(message.content).strip()
        if not question:
            await message.reply(
                "You rang? Ask me something after the @mention and I'll take a crack at it.",
                mention_author=False,
            )
            return

        async with message.channel.typing():
            try:
                answer = chat_reply(question)
            except Exception as exc:
                logger.error("Chat reply failed: %s", exc, exc_info=True)
                await message.reply(
                    "Something went wrong generating a reply — try again in a bit.",
                    mention_author=False,
                )
                return

        if len(answer) > _DISCORD_MESSAGE_LIMIT:
            answer = answer[: _DISCORD_MESSAGE_LIMIT - 1] + "…"

        await message.reply(answer, mention_author=False)

    def _strip_mention(self, content: str) -> str:
        """
        Remove the bot's @mention token(s) from the message text, leaving
        just what the member actually asked. Discord sends mentions as
        <@ID> or, for users with a per-server nickname, <@!ID>.
        """
        bot_id = self.bot.user.id
        return (
            content
            .replace(f"<@{bot_id}>", "")
            .replace(f"<@!{bot_id}>", "")
        )


async def setup(bot: commands.Bot) -> None:
    """Called by bot.load_extension('cogs.chat')."""
    await bot.add_cog(ChatCog(bot))
