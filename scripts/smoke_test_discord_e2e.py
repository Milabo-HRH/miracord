"""Run the wake gate, Gemini Live, and Discord playback as one smoke test."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from types import SimpleNamespace

import discord
from discord.ext import commands

from src.ai_services.providers.gemini.config import GEMINI_SERVICE_CONFIG
from src.ai_services.providers.gemini.manager import GeminiRealtimeManager
from src.bot.session.guild_session import GuildSession
from src.bot.state import BotStateEnum
from src.config.config import Config
from src.utils.logger import get_logger

logger = get_logger(__name__)


class _LogContext:
    """Minimal command context used only for provider error reporting."""

    async def send(self, message: str) -> None:
        logger.info("Smoke-test context: %s", message)


async def _feed_discord_pcm(session: GuildSession, user: object, path: Path) -> None:
    if session._audio_sink is None:
        raise RuntimeError("Audio sink was not initialized")
    pcm = path.read_bytes()
    for offset in range(0, len(pcm), Config.DISCORD_CHUNK_SIZE):
        chunk = pcm[offset : offset + Config.DISCORD_CHUNK_SIZE]
        if len(chunk) < Config.DISCORD_CHUNK_SIZE:
            chunk += b"\x00" * (Config.DISCORD_CHUNK_SIZE - len(chunk))
        session._audio_sink.write(user, SimpleNamespace(pcm=chunk))
        await asyncio.sleep(0.02)


async def run(
    guild_id: int,
    voice_channel_id: int,
    negative_pcm: Path,
    positive_pcm: Path,
) -> None:
    intents = discord.Intents.default()
    bot = commands.Bot(command_prefix="!", intents=intents)
    completed = asyncio.Event()
    outcome: list[BaseException] = []
    session: GuildSession | None = None

    async def smoke() -> None:
        nonlocal session
        try:
            guild = bot.get_guild(guild_id)
            channel = bot.get_channel(voice_channel_id)
            if guild is None:
                raise RuntimeError(f"Guild {guild_id} is not available to the bot")
            if not isinstance(channel, discord.VoiceChannel):
                raise RuntimeError(
                    f"Channel {voice_channel_id} is not an available voice channel"
                )

            session = GuildSession(
                guild=guild,
                bot=bot,
                ai_service_factories={
                    "gemini": (GeminiRealtimeManager, GEMINI_SERVICE_CONFIG)
                },
            )
            ctx = _LogContext()
            if not await session.ai_coordinator.ensure_connected(
                ctx,
                on_connect=session._on_ai_connect,
                on_disconnect=session._on_ai_disconnect,
            ):
                raise RuntimeError("Gemini Live connection failed")
            if not await session.voice_connection.connect_to_channel(channel):
                raise RuntimeError("Discord voice connection failed")

            fixture_user = SimpleNamespace(
                id=1,
                name="e2e-fixture",
                display_name="e2e-fixture",
            )
            await session.bot_state.grant_consent(fixture_user.id)
            await session._initialize_sink()
            await session.bot_state.set_state(BotStateEnum.STANDBY)
            await session.start_background_tasks()

            await _feed_discord_pcm(session, fixture_user, negative_pcm)
            await asyncio.sleep(1)
            if session._live_input_active:
                raise RuntimeError("Negative fixture incorrectly opened the wake gate")
            logger.info("Discord E2E negative wake-gate check passed.")

            await _feed_discord_pcm(session, fixture_user, positive_pcm)
            wake_deadline = asyncio.get_running_loop().time() + 3
            while (
                not session._live_input_active
                and asyncio.get_running_loop().time() < wake_deadline
            ):
                await asyncio.sleep(0.05)
            if not session._live_input_active:
                raise RuntimeError("Positive fixture did not open the wake gate")
            logger.info("Discord E2E positive wake-gate check passed.")

            playback_seen = False
            playback_deadline = asyncio.get_running_loop().time() + 35
            while asyncio.get_running_loop().time() < playback_deadline:
                stream_id = (
                    session.audio_playback_manager.get_current_playing_response_id()
                )
                if stream_id:
                    playback_seen = True
                elif playback_seen:
                    break
                await asyncio.sleep(0.05)
            if not playback_seen:
                raise RuntimeError(
                    "Gemini server VAD produced no Discord playback within 35 seconds"
                )
            logger.info(
                "Discord E2E passed: keyword gate, server VAD, Gemini response, "
                "and voice playback all completed."
            )
        except BaseException as exc:
            outcome.append(exc)
        finally:
            if session is not None:
                await session.cleanup()
            completed.set()
            await bot.close()

    @bot.event
    async def on_ready() -> None:
        asyncio.create_task(smoke())

    await bot.start(Config.DISCORD_TOKEN)
    await completed.wait()
    if outcome:
        raise outcome[0]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("guild_id", type=int)
    parser.add_argument("voice_channel_id", type=int)
    parser.add_argument("negative_pcm", type=Path)
    parser.add_argument("positive_pcm", type=Path)
    args = parser.parse_args()
    asyncio.run(
        run(
            args.guild_id,
            args.voice_channel_id,
            args.negative_pcm,
            args.positive_pcm,
        )
    )


if __name__ == "__main__":
    main()
