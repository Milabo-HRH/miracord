"""Exercise real FFmpeg across barge-in and subsequent turns without devices."""

import asyncio
import json
import threading
import time
from unittest.mock import MagicMock

from discord.ext import voice_recv

from src.audio.playback import AudioPlaybackManager


class SilentConsumer:
    """Drain Discord PCM on a thread without opening speakers or joining Discord."""

    def __init__(self):
        self.stop_event = threading.Event()
        self.thread = None
        self.frames = 0
        self.finished = 0

    def play(self, source):
        self.stop_event = threading.Event()
        stop_event = self.stop_event

        def consume():
            try:
                while not stop_event.is_set():
                    if not source.read():
                        break
                    self.frames += 1
                    time.sleep(0.02)
            finally:
                source.cleanup()
                self.finished += 1

        self.thread = threading.Thread(target=consume, daemon=True)
        self.thread.start()

    def playing(self):
        return bool(self.thread and self.thread.is_alive() and not self.stop_event.is_set())


async def wait_until(predicate, timeout=8):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.02)

    await asyncio.wait_for(poll(), timeout)


async def run():
    consumer = SilentConsumer()
    client = MagicMock(spec=voice_recv.VoiceRecvClient)
    client.is_connected.return_value = True
    client.is_paused.return_value = False
    client.is_playing.side_effect = consumer.playing
    client.play.side_effect = consumer.play
    client.stop_playing.side_effect = lambda: consumer.stop_event.set()
    manager = AudioPlaybackManager(MagicMock(id=0, voice_client=client))
    streams = []
    prepare = manager._prepare_new_playback_stream

    def track(stream_id):
        stream = prepare(stream_id)
        streams.append(stream)
        return stream

    manager._prepare_new_playback_stream = track
    manager.start()
    try:
        await manager.start_new_audio_stream("first", (24000, 1))
        await manager.add_audio_chunk(b"\x01\x00" * (24000 * 20))
        await manager.end_audio_stream()
        await wait_until(lambda: consumer.frames >= 5)
        # Stop draining while the producer still has many seconds of queued PCM.
        client.stop_playing()
        manager.interrupt_audio_stream()
        assert manager.get_current_playing_response_id() is None
        await wait_until(lambda: manager._monitor_task.done())
        for stream_id in ("second", "third"):
            previous_finished = consumer.finished
            await manager.start_new_audio_stream(stream_id, (24000, 1))
            await manager.add_audio_chunk(b"\x01\x00" * 24000)
            await manager.end_audio_stream()
            await wait_until(lambda previous=previous_finished: consumer.finished > previous)
            await wait_until(lambda: manager.get_current_playing_response_id() is None)
        assert consumer.finished == 3
        assert manager.get_played_ms("second") >= 900
        assert manager.get_played_ms("third") >= 900
        print(json.dumps({"ok": True, "interrupted_turn": 1, "subsequent_turns": 2}))
    finally:
        client.stop_playing()
        for stream in streams:
            process = getattr(stream.ffmpeg_audio_source, "_process", None)
            if process and process.poll() is None:
                process.kill()
        await manager.stop()


if __name__ == "__main__":
    asyncio.run(run())
