"""
Main entry point for the Discord bot application.

This module initializes all the necessary components for the bot to function:
- Real-time AI service communication via a factory pattern
- Discord bot setup with appropriate intents and command prefix

The bot is configured to use the VoiceCog, which manages all voice-related commands
and events by delegating to per-guild session managers.
"""

import asyncio

import discord
from discord.ext import commands

from src.ai_services.providers.desktop_voice.config import (
    DESKTOP_VOICE_SERVICE_CONFIG,
)
from src.ai_services.providers.desktop_voice.manager import DesktopVoiceManager
from src.ai_services.providers.gemini.config import GEMINI_SERVICE_CONFIG
from src.ai_services.providers.gemini.manager import GeminiRealtimeManager
from src.ai_services.providers.grok.config import GROK_SERVICE_CONFIG
from src.ai_services.providers.grok.manager import GrokRealtimeManager
from src.ai_services.providers.openai.config import OPENAI_SERVICE_CONFIG

# Import managers and their configurations
from src.ai_services.providers.openai.manager import OpenAIRealtimeManager
from src.bot.cogs.voice_cog import VoiceCog
from src.config.config import Config
from src.utils.logger import get_logger

# Configure discord.py's internal logging.
discord.utils.setup_logging(level=Config.LOG_CONSOLE_LEVEL, root=False)

logger = get_logger(__name__)

if Config.WAKE_WORD_ENGINE == "paraformer":
    from src.audio.paraformer import prepare_paraformer
    prepare_paraformer()


# --- Set up AI Service Communication Layer ---
# Instead of instances, we create a factory registry.
ai_service_factories: dict[str, tuple] = {
    "gemini": (GeminiRealtimeManager, GEMINI_SERVICE_CONFIG),
    "grok": (GrokRealtimeManager, GROK_SERVICE_CONFIG),
    "desktop_voice": (DesktopVoiceManager, DESKTOP_VOICE_SERVICE_CONFIG),
    # Kept for backwards compatibility; it isn't on the V1 critical path.
    "openai": (OpenAIRealtimeManager, OPENAI_SERVICE_CONFIG),
}
logger.info(
    "AI service factories registered for: %s", list(ai_service_factories.keys())
)

# Validate that the default provider from Config is a valid factory choice.
# The actual key validity will be checked on-demand when the manager is created.
if Config.AI_SERVICE_PROVIDER not in Config.SUPPORTED_AI_PROVIDERS:
    logger.error(
        f"Default AI_SERVICE_PROVIDER '{Config.AI_SERVICE_PROVIDER}' is not supported. "
        f"Supported providers: {', '.join(sorted(Config.SUPPORTED_AI_PROVIDERS))}. Exiting."
    )
    raise SystemExit(
        f"Default AI_SERVICE_PROVIDER '{Config.AI_SERVICE_PROVIDER}' is not supported."
    )


# --- Configure Discord Bot ---
intents = discord.Intents.default()
intents.message_content = Config.ENABLE_PREFIX_COMMANDS
bot: commands.Bot = commands.Bot(command_prefix=Config.COMMAND_PREFIX, intents=intents)
_application_commands_synced = False


@bot.event
async def on_ready() -> None:
    """Register slash commands once after Discord authentication succeeds."""
    global _application_commands_synced
    if _application_commands_synced:
        return
    try:
        if Config.DISCORD_SYNC_GUILD_ID is not None:
            guild = discord.Object(id=Config.DISCORD_SYNC_GUILD_ID)
            bot.tree.copy_global_to(guild=guild)
            synced_commands = await bot.tree.sync(guild=guild)
            scope = f"guild {Config.DISCORD_SYNC_GUILD_ID}"
        else:
            synced_commands = await bot.tree.sync()
            scope = "global"
        _application_commands_synced = True
        logger.info(
            "Registered %d application commands for %s.",
            len(synced_commands),
            scope,
        )
    except discord.DiscordException:
        logger.exception(
            "Failed to register Discord application commands; text-prefix commands "
            "remain available."
        )


async def main() -> None:
    """
    Main asynchronous function that starts the Discord bot.

    This function:
    1. Adds the VoiceCog to the bot, which handles voice commands and events
    2. Starts the bot with the Discord token from the configuration

    The function uses an async context manager to ensure proper cleanup when the bot stops.
    """
    async with bot:
        voice_cog_instance = VoiceCog(
            bot=bot,
            ai_service_factories=ai_service_factories,  # Pass the dictionary of factories
        )
        await bot.add_cog(voice_cog_instance)
        logger.info("VoiceCog loaded and added to the bot.")

        logger.info("Starting Discord bot...")
        await bot.start(Config.DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
