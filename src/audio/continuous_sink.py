"""Continuous Discord PCM mixer for the local desktop voice bridge."""

import asyncio
import threading
import time

import numpy as np

from src.audio.sinks import AudioSink
from src.utils.logger import get_logger

logger = get_logger(__name__)


class ContinuousAudioSink(AudioSink):
    """Mix admitted humans at 48 kHz stereo, without keyword/VAD gating."""

    FRAME_BYTES = 3840  # 20 ms of Discord PCM16 stereo
    MAX_BUFFER = FRAME_BYTES * 10

    def __init__(self, users, send_audio):
        super().__init__()
        self._lock = threading.Lock()
        self._buffers = {user_id: bytearray() for user_id in users}
        self._last_packet = {}
        self._closed = False
        self._send_audio = send_audio
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.create_task(self._forward_loop())

    def wants_opus(self):
        return False

    def add_user(self, user_id):
        with self._lock:
            if not self._closed:
                self._buffers.setdefault(user_id, bytearray())

    def remove_user(self, user_id):
        with self._lock:
            self._buffers.pop(user_id, None)
            self._last_packet.pop(user_id, None)

    def write(self, user, data):
        if user is None or user.bot or not data.pcm or len(data.pcm) % 4:
            return
        with self._lock:
            if self._closed or user.id not in self._buffers:
                return
            now = time.monotonic()
            buffer = self._buffers[user.id]
            if now - self._last_packet.get(user.id, now) > 0.3:
                buffer.clear()
            self._last_packet[user.id] = now
            buffer.extend(data.pcm)
            if len(buffer) > self.MAX_BUFFER:
                del buffer[:-self.MAX_BUFFER]

    def mix_frame(self):
        mixed = np.zeros(self.FRAME_BYTES // 2, dtype=np.int32)
        with self._lock:
            for buffer in self._buffers.values():
                count = min(len(buffer), self.FRAME_BYTES)
                if count:
                    mixed[:count // 2] += np.frombuffer(bytes(buffer[:count]), dtype='<i2')
                    del buffer[:count]
        return np.clip(mixed, -32768, 32767).astype('<i2').tobytes()

    async def _forward_loop(self):
        deadline = asyncio.get_running_loop().time()
        try:
            while not self._closed:
                if not await self._send_audio(self.mix_frame()):
                    logger.error('Continuous desktop forwarding stopped: audio send failed.')
                    self.cleanup()
                    return
                deadline += 0.02
                now = asyncio.get_running_loop().time()
                if now - deadline > 0.2:
                    deadline = now
                await asyncio.sleep(max(0, deadline - now))
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception('Continuous desktop forwarding failed.')
            self.cleanup()

    def cleanup(self):
        with self._lock:
            self._closed = True
            self._buffers.clear()
            self._last_packet.clear()
        if not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._task.cancel)
