"""
Voice Cog module for managing per-guild voice interaction sessions.

This module provides the VoiceCog class, which acts as a stateless manager for
GuildSession objects. It is responsible for receiving user commands and events,
and routing them to the appropriate session for handling. This design allows the
bot to support concurrent voice sessions across multiple guilds.
"""

import asyncio
from collections import defaultdict
from typing import Dict, Optional

import discord
from discord.ext import commands

from src.bot.session.guild_session import GuildSession
from src.bot.state import BotStateEnum
from src.config.config import Config
from src.exceptions import SessionError, StateTransitionError
from src.utils.logger import get_logger


logger = get_logger(__name__)


class _AutoConnectContext:
    """Minimal command context used for configured startup connections."""

    def __init__(
        self,
        guild: discord.Guild,
        author: discord.Member,
        channel: discord.TextChannel,
    ) -> None:
        self.guild = guild
        self.author = author
        self.channel = channel

    async def send(self, content: str, **kwargs) -> discord.Message:
        return await self.channel.send(content, **kwargs)


class VoiceCog(commands.Cog):
    """
    A stateless Discord Cog that manages GuildSessions for voice interactions.

    This cog acts as a dispatcher, receiving user commands and Discord events,
    and routing them to the appropriate GuildSession instance. It maintains a
    dictionary of active sessions, keyed by guild ID, ensuring that each guild's
    state is completely isolated.
    """

    def __init__(
        self,
        bot: commands.Bot,
        ai_service_factories: Dict[str, tuple],
    ):
        """
        Initializes the VoiceCog.

        Args:
            bot: The Discord bot instance.
            ai_service_factories: A dictionary of factories for creating AI service managers.
        """
        self.bot = bot
        self.ai_service_factories = ai_service_factories
        self._sessions: Dict[int, GuildSession] = {}
        self._session_locks: Dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._auto_connect_task: Optional[asyncio.Task] = None
        logger.info("VoiceCog initialized.")

    @staticmethod
    async def _edit_command_status(
        message: discord.Message, content: str
    ) -> None:
        """Show the actual connection state instead of Discord's thinking label."""
        try:
            await message.edit(content=content)
        except discord.DiscordException:
            logger.warning("Could not update command status message.", exc_info=True)

    def _get_or_create_session(self, guild: discord.Guild) -> GuildSession:
        """
        Retrieves an existing session for a guild or creates a new one.

        Args:
            guild: The guild for which to get the session.

        Returns:
            The GuildSession instance for the specified guild.
        """
        if guild.id not in self._sessions:
            logger.info(f"Creating new session for guild {guild.id} ({guild.name})")
            self._sessions[guild.id] = GuildSession(
                guild=guild,
                bot=self.bot,
                ai_service_factories=self.ai_service_factories,
            )
        return self._sessions[guild.id]

    async def cog_unload(self) -> None:
        """
        Clean up all active sessions when the cog is unloaded.
        """
        logger.info(f"Unloading VoiceCog, cleaning up {len(self._sessions)} sessions.")
        if self._auto_connect_task and not self._auto_connect_task.done():
            self._auto_connect_task.cancel()
        cleanup_tasks = [session.cleanup() for session in self._sessions.values()]
        if cleanup_tasks:
            await asyncio.gather(*cleanup_tasks, return_exceptions=True)
        self._sessions.clear()
        logger.info("All active sessions cleaned up.")

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Join the configured voice channel after Discord is ready."""
        if not Config.AUTO_CONNECT_ENABLED:
            return
        if self._auto_connect_task and not self._auto_connect_task.done():
            return
        self._auto_connect_task = asyncio.create_task(self._try_auto_connect())

    async def _try_auto_connect(
        self, preferred_member: Optional[discord.Member] = None
    ) -> bool:
        """Start a configured session when a human is present in the target channel."""
        guild_id = Config.AUTO_CONNECT_GUILD_ID
        voice_channel_id = Config.AUTO_CONNECT_VOICE_CHANNEL_ID
        if guild_id is None or voice_channel_id is None:
            return False

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            logger.warning("Auto-connect guild %s is not available.", guild_id)
            return False
        if guild.id in self._sessions:
            return True

        voice_channel = guild.get_channel(voice_channel_id)
        if not isinstance(voice_channel, (discord.VoiceChannel, discord.StageChannel)):
            logger.warning(
                "Auto-connect voice channel %s is not available in guild %s.",
                voice_channel_id,
                guild_id,
            )
            return False

        member = preferred_member
        if (
            member is None
            or member.bot
            or member.voice is None
            or member.voice.channel != voice_channel
        ):
            member = next((item for item in voice_channel.members if not item.bot), None)
        if member is None:
            logger.info(
                "Auto-connect is waiting for a human to join voice channel %s.",
                voice_channel_id,
            )
            return False

        text_channel = None
        if Config.AUTO_CONNECT_TEXT_CHANNEL_ID is not None:
            configured_text_channel = guild.get_channel(
                Config.AUTO_CONNECT_TEXT_CHANNEL_ID
            )
            if isinstance(configured_text_channel, discord.TextChannel):
                text_channel = configured_text_channel
        if text_channel is None:
            candidates = [guild.system_channel, *guild.text_channels]
            text_channel = next(
                (
                    item
                    for item in candidates
                    if item is not None
                    and guild.me is not None
                    and item.permissions_for(guild.me).send_messages
                ),
                None,
            )
        if text_channel is None:
            logger.warning("Auto-connect could not find a writable text channel.")
            return False

        logger.info(
            "Auto-connecting guild %s to voice channel %s.",
            guild_id,
            voice_channel_id,
        )
        ctx = _AutoConnectContext(guild, member, text_channel)
        return await self._handle_connect_command(ctx)

    async def _handle_connect_command(self, ctx: commands.Context) -> bool:
        """
        Handles the logic for connecting the bot to a voice channel.

        Args:
            ctx: The command context.
        """
        async with self._session_locks[ctx.guild.id]:
            if ctx.guild.id in self._sessions:
                await ctx.send(
                    "I'm already in a session in this server. Use `/disconnect` to end it first."
                )
                return True

            session = self._get_or_create_session(ctx.guild)
            try:
                success = await session.initialize_session(ctx)
                if not success:
                    logger.warning(
                        f"Connection process failed for guild {ctx.guild.id}. Cleaning up session."
                    )
                    if ctx.guild.id in self._sessions:
                        del self._sessions[ctx.guild.id]
                return success
            except StateTransitionError as e:
                logger.critical(
                    f"Caught unrecoverable state error in guild {ctx.guild.id} during connect: {e}",
                    exc_info=True,
                )
                await ctx.send(
                    "An unexpected internal error occurred. The session will now terminate."
                )
                await session.cleanup()
                if ctx.guild.id in self._sessions:
                    del self._sessions[ctx.guild.id]
                return False

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """
        Listen for the bot's own voice connection changes to handle disconnects.

        This listener filters for events where the bot is moved, disconnected, or
        connected to a voice channel, ignoring state changes like mute or deafen.
        It then delegates the event to the appropriate GuildSession.
        """
        if not member.guild:
            return

        if before.channel == after.channel:
            return

        session = self._sessions.get(member.guild.id)
        if not session:
            if (
                Config.AUTO_CONNECT_ENABLED
                and not member.bot
                and after.channel is not None
                and after.channel.id == Config.AUTO_CONNECT_VOICE_CHANNEL_ID
            ):
                self._auto_connect_task = asyncio.create_task(
                    self._try_auto_connect(member)
                )
            return

        if member.id == self.bot.user.id:
            is_connected = after.channel is not None
            await session.handle_voice_connection_update(is_connected)
            return

        if Config.VOICE_ACCESS_MODE != "implicit" or member.bot:
            return
        voice_client = member.guild.voice_client
        bot_channel = voice_client.channel if voice_client else None
        if bot_channel is None:
            return
        if after.channel == bot_channel:
            await session.handle_voice_member_access(member, joined=True)
        elif before.channel == bot_channel:
            await session.handle_voice_member_access(member, joined=False)

    @commands.Cog.listener()
    async def on_reaction_add(
        self, reaction: discord.Reaction, user: discord.User
    ) -> None:
        """
        Delegates reaction add events to the appropriate GuildSession.

        Args:
            reaction: The reaction that was added.
            user: The user who added the reaction.
        """
        if not reaction.message.guild:
            return

        session = self._sessions.get(reaction.message.guild.id)
        if session:
            try:
                await session.handle_reaction_add(reaction, user)
            except StateTransitionError as e:
                logger.critical(
                    f"Caught unrecoverable state error in guild {reaction.message.guild.id} during reaction add: {e}",
                    exc_info=True,
                )
                # Clean up the session and notify users
                await session.cleanup()
                if reaction.message.guild.id in self._sessions:
                    del self._sessions[reaction.message.guild.id]

    @commands.Cog.listener()
    async def on_reaction_remove(
        self, reaction: discord.Reaction, user: discord.User
    ) -> None:
        """
        Delegates reaction remove events to the appropriate GuildSession.

        Args:
            reaction: The reaction that was removed.
            user: The user who removed the reaction.
        """
        if not reaction.message.guild:
            return

        session = self._sessions.get(reaction.message.guild.id)
        if session:
            try:
                await session.handle_reaction_remove(reaction, user)
            except StateTransitionError as e:
                logger.critical(
                    f"Caught unrecoverable state error in guild {reaction.message.guild.id} during reaction remove: {e}",
                    exc_info=True,
                )
                # Clean up the session and notify users
                await session.cleanup()
                if reaction.message.guild.id in self._sessions:
                    del self._sessions[reaction.message.guild.id]

    @commands.hybrid_command(name="connect")
    @commands.guild_only()
    async def connect_command(self, ctx: commands.Context) -> None:
        """
        Connects the bot to the user's voice channel.

        The bot will join the voice channel and start listening for voice input.
        Users can interact via push-to-talk or wake word detection.

        Args:
            ctx: The command context.
        """
        if ctx.guild.id in self._sessions:
            await ctx.send("✅ The bot is already connected in this server.")
            return
        if ctx.author.voice is None:
            await ctx.send("Join a voice channel before using `/connect`.")
            return
        status = await ctx.send("🔌 Connecting to Discord voice and the AI provider…")
        success = await self._handle_connect_command(ctx)
        if success:
            await self._edit_command_status(
                status, f"✅ Connected to **{ctx.author.voice.channel.name}**."
            )
        else:
            await self._edit_command_status(status, "❌ Connection failed.")

    @commands.hybrid_command(name="set")
    @commands.guild_only()
    async def set_provider_command(
        self, ctx: commands.Context, provider_name: str
    ) -> None:
        """
        Delegates the 'set' command to an active GuildSession.

        This command can only be used when the bot is in an active session
        (i.e., after the 'connect' command has been used).

        Args:
            ctx: The command context.
            provider_name: The name of the AI provider to switch to.
        """
        session = self._sessions.get(ctx.guild.id)
        if not session:
            await ctx.send(
                "The bot is not currently in a session. Use the 'connect' command first."
            )
            return

        status = await ctx.send(f"🔄 Switching provider to **{provider_name.upper()}**…")
        try:
            await session.set_provider(ctx, provider_name)
            await self._edit_command_status(
                status, f"✅ Provider switch finished: **{provider_name.upper()}**."
            )
        except StateTransitionError as e:
            logger.critical(
                f"Caught unrecoverable state error in guild {ctx.guild.id} during set provider: {e}",
                exc_info=True,
            )
            await ctx.send(
                "An unexpected internal error occurred. The session will now terminate."
            )
            await session.cleanup()
            if ctx.guild.id in self._sessions:
                del self._sessions[ctx.guild.id]
            await self._edit_command_status(status, "❌ Provider switch failed.")

    @commands.hybrid_command(name="talk")
    @commands.guild_only()
    async def talk_command(self, ctx: commands.Context) -> None:
        """Toggle deterministic voice recording for the invoking user."""
        session = self._sessions.get(ctx.guild.id)
        if not session:
            await ctx.send("Use `/connect` before `/talk`.")
            return

        state = session.bot_state.current_state
        if state == BotStateEnum.STANDBY:
            await session.handle_pushtotalk_reaction(ctx.author, added=True)
            await ctx.send(
                "🔴 Live input started. Speak naturally and stop; "
                "server VAD will answer automatically. Run `/talk` again only "
                "to force-submit."
            )
            return

        if (
            state == BotStateEnum.RECORDING
            and session.bot_state.is_authorized(ctx.author)
        ):
            await session.handle_pushtotalk_reaction(ctx.author, added=False)
            await ctx.send("✅ Submitted to the voice assistant.")
            return

        await ctx.send("Another voice turn is currently active.")

    @commands.hybrid_command(name="disconnect")
    @commands.guild_only()
    async def disconnect_command(self, ctx: commands.Context) -> None:
        """
        Terminates and cleans up the session for the current guild.

        This command delegates the cleanup logic to the GuildSession and ensures
        the session is removed from the cog's memory, effectively ending its
        lifecycle.

        Args:
            ctx: The command context.
        """
        # CONCURRENCY FIX: Atomic session operations per guild
        async with self._session_locks[ctx.guild.id]:
            session = self._sessions.get(ctx.guild.id)
            if not session:
                await ctx.send("The bot is not currently in a session in this server.")
                return

            status = await ctx.send("🔌 Disconnecting from the voice channel…")
            disconnected = False
            try:
                await session.cleanup()
                disconnected = True
            except StateTransitionError as e:
                logger.critical(
                    f"Caught unrecoverable state error in guild {ctx.guild.id} during disconnect: {e}",
                    exc_info=True,
                )
                await ctx.send(
                    "An unexpected internal error occurred during cleanup. The session has been forcefully removed."
                )
            except SessionError as e:
                logger.error(
                    f"Session error during cleanup for guild {ctx.guild.id}: {e}",
                    exc_info=True,
                )
            except Exception as e:
                logger.error(
                    f"Unexpected error during session cleanup for guild {ctx.guild.id}: {e}",
                    exc_info=True,
                )
                await ctx.send(
                    "An error occurred during cleanup. The session has been forcefully removed."
                )
            finally:
                if ctx.guild.id in self._sessions:
                    del self._sessions[ctx.guild.id]
                logger.info(
                    f"Session for guild {ctx.guild.id} has been fully cleaned up and removed."
                )
                await self._edit_command_status(
                    status,
                    "✅ Disconnected." if disconnected else "❌ Disconnect failed.",
                )


async def setup(bot: commands.Bot) -> None:
    """
    Stub for the setup function to prevent loading this cog as a standard extension.

    This cog requires a manual instantiation with specific dependencies and should not
    be loaded via `bot.load_extension()`.

    Args:
        bot: The bot instance.
    """
    raise NotImplementedError(
        "VoiceCog requires dependencies (ai_service_factories) and cannot be loaded as a standard extension. "
        "Instantiate and add it manually in your main script."
    )
