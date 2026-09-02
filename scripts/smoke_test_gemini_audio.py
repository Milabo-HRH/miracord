"""Send a known PCM utterance through the configured Gemini Live session."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from google import genai
from google.genai import types

from src.ai_services.providers.gemini.config import (
    GEMINI_DEFAULT_LIVE_CONNECT_CONFIG,
    GEMINI_REALTIME_MODEL_NAME,
)
from src.config.config import Config


async def run(pcm_path: Path, auto_vad: bool) -> None:
    client = genai.Client(api_key=Config.GEMINI_API_KEY)
    config = types.LiveConnectConfig(**GEMINI_DEFAULT_LIVE_CONNECT_CONFIG)
    pcm = pcm_path.read_bytes()

    async with client.aio.live.connect(
        model=GEMINI_REALTIME_MODEL_NAME, config=config
    ) as session:
        # Gemini Live expects realtime PCM in roughly 100 ms chunks. Pace the
        # known fixture like a microphone instead of submitting one large blob.
        for offset in range(0, len(pcm), 3200):
            await session.send_realtime_input(
                audio=types.Blob(
                    data=pcm[offset : offset + 3200],
                    mime_type="audio/pcm;rate=16000",
                )
            )
            await asyncio.sleep(0.1)
        if auto_vad:
            for _ in range(20):
                await session.send_realtime_input(
                    audio=types.Blob(
                        data=b"\x00" * 3200,
                        mime_type="audio/pcm;rate=16000",
                    )
                )
                await asyncio.sleep(0.1)
        else:
            await session.send_realtime_input(audio_stream_end=True)

        response_audio_bytes = 0

        async def receive_turn() -> None:
            nonlocal response_audio_bytes
            async for message in session.receive():
                if message.data:
                    response_audio_bytes += len(message.data)

        await asyncio.wait_for(receive_turn(), timeout=30)
        if response_audio_bytes <= 0:
            raise RuntimeError("Gemini completed the turn without response audio")
        print(f"Gemini known-audio response: OK ({response_audio_bytes} bytes)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("pcm_path", type=Path)
    parser.add_argument("--auto-vad", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.pcm_path, args.auto_vad))


if __name__ == "__main__":
    main()
