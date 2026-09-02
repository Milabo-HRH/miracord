"""Exercise the desktop provider capture path with real virtual devices."""

from __future__ import annotations

import asyncio
from pathlib import Path

import sounddevice as sd

from src.ai_services.providers.desktop_voice.config import (
    DESKTOP_VOICE_SERVICE_CONFIG,
)
from src.ai_services.providers.desktop_voice.manager import DesktopVoiceManager
from src.config.config import Config


class PlaybackProbe:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.ended = asyncio.Event()
        self.stream_id = None
        self.bytes_received = 0

    async def start_new_audio_stream(self, stream_id, _response_format):
        self.stream_id = stream_id
        self.started.set()

    async def add_audio_chunk(self, audio):
        self.bytes_received += len(audio)

    async def end_audio_stream(self):
        self.ended.set()


async def run(pcm_path: Path) -> None:
    probe = PlaybackProbe()
    manager = DesktopVoiceManager(probe, DESKTOP_VOICE_SERVICE_CONFIG)

    async def noop():
        return None

    if not await manager.connect(noop, noop):
        raise RuntimeError("Desktop Voice provider could not connect")
    try:
        aux_device = DesktopVoiceManager.resolve_device(
            name_fragment="Voicemeeter AUX Input",
            direction="output",
            channels=2,
            preferred_host_api=Config.DESKTOP_VOICE_HOST_API,
        )
        stream = sd.RawOutputStream(
            samplerate=48000,
            channels=2,
            dtype="int16",
            device=aux_device,
            blocksize=0,
        )
        stream.start()
        try:
            await asyncio.to_thread(stream.write, pcm_path.read_bytes())
            await asyncio.to_thread(stream.write, b"\x00" * (48000 * 2 * 2))
        finally:
            stream.stop()
            stream.close()

        await asyncio.wait_for(probe.started.wait(), timeout=3)
        await asyncio.wait_for(probe.ended.wait(), timeout=4)
        if probe.bytes_received <= 0:
            raise RuntimeError("Desktop response started but yielded no PCM")
        print(
            "Desktop provider capture passed: "
            f"stream={probe.stream_id}, bytes={probe.bytes_received}"
        )
    finally:
        await manager.disconnect()


def main() -> None:
    pcm_path = (
        Path.home()
        / "AppData"
        / "Local"
        / "Temp"
        / "voicecord_doubao_positive_48k_stereo.pcm"
    )
    asyncio.run(run(pcm_path))


if __name__ == "__main__":
    main()
