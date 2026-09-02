"""Exercise the real Discord-format wake gate with positive/negative fixtures."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from types import SimpleNamespace

from src.audio.sinks import ManualControlSink
from src.bot.state import BotState, BotStateEnum
from src.config.config import Config


async def fixture_triggers(path: Path) -> bool:
    state = BotState()
    await state.set_state(BotStateEnum.STANDBY)
    detected = asyncio.Event()

    async def on_wake_word(_user: object) -> None:
        detected.set()

    async def on_vad_end(_audio: bytes) -> None:
        return None

    sink = ManualControlSink(
        bot_state=state,
        initial_consented_users={42},
        on_wake_word_detected=on_wake_word,
        on_vad_speech_end=on_vad_end,
        action_lock=asyncio.Lock(),
    )
    user = SimpleNamespace(id=42, name="fixture")
    pcm = path.read_bytes()
    try:
        for offset in range(0, len(pcm), Config.DISCORD_CHUNK_SIZE):
            chunk = pcm[offset : offset + Config.DISCORD_CHUNK_SIZE]
            if len(chunk) < Config.DISCORD_CHUNK_SIZE:
                chunk += b"\x00" * (Config.DISCORD_CHUNK_SIZE - len(chunk))
            sink.write(user, SimpleNamespace(pcm=chunk))
            await asyncio.sleep(0)
        await asyncio.sleep(0.2)
        return detected.is_set()
    finally:
        sink.cleanup()
        await asyncio.sleep(0)


async def run(positive: Path, negative: Path) -> None:
    negative_triggered = await fixture_triggers(negative)
    positive_triggered = await fixture_triggers(positive)
    print(
        "Wake gate integration: "
        f"negative={negative_triggered}, positive={positive_triggered}"
    )
    if negative_triggered or not positive_triggered:
        raise RuntimeError("Wake gate integration did not separate the fixtures")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("positive", type=Path)
    parser.add_argument("negative", type=Path)
    args = parser.parse_args()
    asyncio.run(run(args.positive, args.negative))


if __name__ == "__main__":
    main()
