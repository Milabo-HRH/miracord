"""Run Discord wake gating and both desktop Voice buses end to end."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from types import SimpleNamespace

import discord
import numpy as np
import sounddevice as sd
from discord.ext import commands

from src.ai_services.providers.desktop_voice.config import (
    DESKTOP_VOICE_SERVICE_CONFIG,
)
from src.ai_services.providers.desktop_voice.manager import DesktopVoiceManager
from src.bot.session.guild_session import GuildSession
from src.bot.state import BotStateEnum
from src.config.config import Config


class LogContext:
    async def send(self, message: str) -> None:
        print(message)


async def feed_discord_pcm(session, user, path: Path) -> None:
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
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
    outcome = []
    session = None

    async def smoke() -> None:
        nonlocal session
        try:
            guild = bot.get_guild(guild_id)
            channel = bot.get_channel(voice_channel_id)
            if guild is None or not isinstance(channel, discord.VoiceChannel):
                raise RuntimeError("Discord guild or voice channel is unavailable")

            session = GuildSession(
                guild=guild,
                bot=bot,
                ai_service_factories={
                    "desktop_voice": (
                        DesktopVoiceManager,
                        DESKTOP_VOICE_SERVICE_CONFIG,
                    )
                },
            )
            if not await session.ai_coordinator.ensure_connected(
                LogContext(),
                on_connect=session._on_ai_connect,
                on_disconnect=session._on_ai_disconnect,
            ):
                raise RuntimeError("Desktop provider connection failed")
            if not await session.voice_connection.connect_to_channel(channel):
                raise RuntimeError("Discord voice connection failed")

            fixture_user = SimpleNamespace(
                id=1,
                name="desktop-e2e",
                display_name="desktop-e2e",
            )
            await session.bot_state.grant_consent(fixture_user.id)
            await session._initialize_sink()
            await session.bot_state.set_state(BotStateEnum.STANDBY)
            await session.start_background_tasks()

            await feed_discord_pcm(session, fixture_user, negative_pcm)
            await asyncio.sleep(1)
            if session._live_input_active:
                raise RuntimeError("Negative fixture opened the desktop wake gate")

            b1_device = DesktopVoiceManager.resolve_device(
                name_fragment="Voicemeeter Out B1",
                direction="input",
                channels=2,
                preferred_host_api=Config.DESKTOP_VOICE_HOST_API,
            )
            b1_chunks = []

            def b1_callback(indata, _frames, _time_info, _status):
                b1_chunks.append(bytes(indata))

            b1_capture = sd.RawInputStream(
                samplerate=48000,
                channels=2,
                dtype="int16",
                device=b1_device,
                callback=b1_callback,
            )
            b1_capture.start()
            try:
                await feed_discord_pcm(session, fixture_user, positive_pcm)
                wake_deadline = asyncio.get_running_loop().time() + 3
                while (
                    not session._live_input_active
                    and asyncio.get_running_loop().time() < wake_deadline
                ):
                    await asyncio.sleep(0.05)
                if not session._live_input_active:
                    raise RuntimeError("Positive fixture did not open the wake gate")
                await asyncio.sleep(1)
            finally:
                b1_capture.stop()
                b1_capture.close()

            sent = np.frombuffer(b"".join(b1_chunks), dtype=np.int16)
            sent_rms = (
                float(np.sqrt(np.mean(np.square(sent.astype(np.float32))))) / 32768
                if sent.size
                else 0.0
            )
            if sent_rms < 0.005:
                raise RuntimeError("Discord speech did not reach desktop Voice B1")

            aux_device = DesktopVoiceManager.resolve_device(
                name_fragment="Voicemeeter AUX Input",
                direction="output",
                channels=2,
                preferred_host_api=Config.DESKTOP_VOICE_HOST_API,
            )
            response_out = sd.RawOutputStream(
                samplerate=48000,
                channels=2,
                dtype="int16",
                device=aux_device,
            )
            response_out.start()
            try:
                await asyncio.to_thread(response_out.write, negative_pcm.read_bytes())
                await asyncio.to_thread(response_out.write, b"\x00" * (48000 * 2 * 2))
            finally:
                response_out.stop()
                response_out.close()

            playback_seen = False
            deadline = asyncio.get_running_loop().time() + 10
            while asyncio.get_running_loop().time() < deadline:
                stream_id = (
                    session.audio_playback_manager.get_current_playing_response_id()
                )
                if stream_id:
                    playback_seen = True
                elif playback_seen:
                    break
                await asyncio.sleep(0.05)
            if not playback_seen:
                raise RuntimeError("B2 response did not play back into Discord")
            print(
                "Discord desktop E2E passed: "
                f"wake gate, B1 upload RMS={sent_rms:.4f}, B2 playback"
            )
        except BaseException as exc:  # noqa: BLE001 - report task failures after cleanup
            outcome.append(exc)
        finally:
            if session is not None:
                await session.cleanup()
            await bot.close()

    @bot.event
    async def on_ready() -> None:
        asyncio.create_task(smoke())

    await bot.start(Config.DISCORD_TOKEN)
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
