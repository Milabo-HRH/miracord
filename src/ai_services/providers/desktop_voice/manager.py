"""Provider that bridges Discord audio to ChatGPT/Codex desktop Voice."""

from __future__ import annotations

import asyncio
import audioop
import math
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sounddevice as sd
import webrtcvad

from src.ai_services.interface import (
    IRealtimeAIServiceManager,
    ProviderCapabilities,
)
from src.audio.playback import AudioPlaybackManager
from src.utils.logger import get_logger

from .voicemeeter import VoicemeeterRemote

logger = get_logger(__name__)


@dataclass(frozen=True)
class SegmentEvent:
    kind: str
    audio: bytes = b""


class DesktopResponseSegmenter:
    """Turn a continuous desktop capture into response stream events."""

    def __init__(
        self,
        *,
        frame_ms: int,
        start_ms: int,
        silence_ms: int,
        preroll_ms: int,
    ) -> None:
        self._start_frames = max(1, math.ceil(start_ms / frame_ms))
        self._silence_frames = max(1, math.ceil(silence_ms / frame_ms))
        self._preroll: deque[bytes] = deque(
            maxlen=max(1, math.ceil(preroll_ms / frame_ms))
        )
        self._speech_frames = 0
        self._silent_frames = 0
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    def feed(self, frame: bytes, *, is_speech: bool) -> list[SegmentEvent]:
        if self._active:
            events = [SegmentEvent("audio", frame)]
            if is_speech:
                self._silent_frames = 0
            else:
                self._silent_frames += 1
                if self._silent_frames >= self._silence_frames:
                    events.append(SegmentEvent("end"))
                    self.reset()
            return events

        self._preroll.append(frame)
        self._speech_frames = self._speech_frames + 1 if is_speech else 0
        if self._speech_frames < self._start_frames:
            return []

        self._active = True
        self._silent_frames = 0
        buffered = b"".join(self._preroll)
        self._preroll.clear()
        return [SegmentEvent("start"), SegmentEvent("audio", buffered)]

    def reset(self) -> None:
        self._speech_frames = 0
        self._silent_frames = 0
        self._active = False
        self._preroll.clear()


class DesktopVoiceManager(IRealtimeAIServiceManager):
    """Use an already signed-in desktop Voice UI as the model backend."""

    def __init__(
        self,
        audio_playback_manager: AudioPlaybackManager,
        service_config: dict[str, Any],
    ) -> None:
        super().__init__(audio_playback_manager, service_config)
        self._connected = False
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._capture_queue: asyncio.Queue[bytes] | None = None
        self._capture_task: asyncio.Task[None] | None = None
        self._send_stream: Any = None
        self._receive_stream: Any = None
        self._send_lock = asyncio.Lock()
        self._on_disconnect: Callable[[], Awaitable[None]] | None = None
        self._active_response_id: str | None = None
        self._ignore_capture_until = 0.0

        self._sample_rate, self._channels = self.processing_audio_format
        self._frame_ms = int(service_config["frame_ms"])
        self._frame_samples = self._sample_rate * self._frame_ms // 1000
        self._frame_bytes = self._frame_samples * self._channels * 2
        self._vad = webrtcvad.Vad(int(service_config["vad_aggressiveness"]))
        self._segmenter = DesktopResponseSegmenter(
            frame_ms=self._frame_ms,
            start_ms=int(service_config["response_start_ms"]),
            silence_ms=int(service_config["response_silence_ms"]),
            preroll_ms=int(service_config["response_preroll_ms"]),
        )
        self._voicemeeter: VoicemeeterRemote | None = None

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            native_web_search=True,
            manual_commit=False,
            cancel_response=True,
            image_input=False,
            realtime_audio_input=True,
            server_vad=True,
        )

    @staticmethod
    def _matching_devices(
        *,
        name_fragment: str,
        direction: str,
        channels: int,
        preferred_host_api: str,
    ) -> Iterable[tuple[int, dict[str, Any], str]]:
        devices = sd.query_devices()
        host_apis = sd.query_hostapis()
        key = "max_output_channels" if direction == "output" else "max_input_channels"
        fragment = name_fragment.casefold()
        matches = []
        for index, device in enumerate(devices):
            if fragment not in str(device["name"]).casefold():
                continue
            if int(device[key]) < channels:
                continue
            host_name = str(host_apis[int(device["hostapi"])]["name"])
            matches.append((index, device, host_name))
        preferred = preferred_host_api.casefold()
        return sorted(
            matches,
            key=lambda item: (
                0 if preferred and preferred in item[2].casefold() else 1,
                0 if "wasapi" in item[2].casefold() else 1,
                item[0],
            ),
        )

    @classmethod
    def resolve_device(
        cls,
        *,
        name_fragment: str,
        direction: str,
        channels: int,
        preferred_host_api: str,
    ) -> int:
        matches = list(
            cls._matching_devices(
                name_fragment=name_fragment,
                direction=direction,
                channels=channels,
                preferred_host_api=preferred_host_api,
            )
        )
        if not matches:
            raise RuntimeError(
                f"No {direction} audio device containing {name_fragment!r} "
                f"with {channels} channel(s) was found"
            )
        index, device, host_name = matches[0]
        logger.info(
            "Desktop Voice selected %s device %s (%s, index %s).",
            direction,
            device["name"],
            host_name,
            index,
        )
        return index

    async def connect(
        self,
        on_connect: Callable[[], Awaitable[None]],
        on_disconnect: Callable[[], Awaitable[None]],
    ) -> bool:
        if self._connected:
            return True
        self._on_disconnect = on_disconnect
        self._event_loop = asyncio.get_running_loop()
        self._capture_queue = asyncio.Queue(maxsize=256)
        try:
            if self._service_config.get("auto_route", True):
                self._voicemeeter = VoicemeeterRemote(
                    Path(self._service_config["voicemeeter_remote_dll"])
                )
                await asyncio.to_thread(self._voicemeeter.connect_and_route)

            preferred_host_api = str(self._service_config.get("preferred_host_api", ""))
            send_device = self.resolve_device(
                name_fragment=str(self._service_config["send_device"]),
                direction="output",
                channels=self._channels,
                preferred_host_api=preferred_host_api,
            )
            receive_device = self.resolve_device(
                name_fragment=str(self._service_config["receive_device"]),
                direction="input",
                channels=self.response_audio_format[1],
                preferred_host_api=preferred_host_api,
            )

            self._send_stream = sd.RawOutputStream(
                samplerate=self._sample_rate,
                channels=self._channels,
                dtype="int16",
                device=send_device,
                blocksize=0,
                latency="low",
            )
            self._receive_stream = sd.RawInputStream(
                samplerate=self.response_audio_format[0],
                channels=self.response_audio_format[1],
                dtype="int16",
                device=receive_device,
                blocksize=self._frame_samples,
                latency="low",
                callback=self._capture_callback,
            )
            self._send_stream.start()
            self._connected = True
            self._capture_task = asyncio.create_task(self._capture_loop())
            self._receive_stream.start()
            await on_connect()
            logger.info(
                "Desktop Voice bridge is ready. Start a Voice conversation in "
                "ChatGPT/Codex and select the Voicemeeter microphone/speaker pair."
            )
            return True
        except Exception:
            logger.exception("Could not start the desktop Voice bridge.")
            await self._close_resources(notify=False)
            return False

    def _capture_callback(
        self, indata: Any, _frames: int, _time_info: Any, status: Any
    ) -> None:
        if status:
            logger.warning("Desktop Voice capture status: %s", status)
        if not self._connected or self._event_loop is None:
            return
        self._event_loop.call_soon_threadsafe(self._enqueue_capture, bytes(indata))

    def _enqueue_capture(self, data: bytes) -> None:
        queue = self._capture_queue
        if queue is None:
            return
        if queue.full():
            try:
                queue.get_nowait()
                queue.task_done()
            except asyncio.QueueEmpty:
                pass
            logger.warning("Desktop Voice capture queue overflow; dropped one frame.")
        queue.put_nowait(data)

    async def _capture_loop(self) -> None:
        buffer = bytearray()
        try:
            while self._connected and self._capture_queue is not None:
                data = await self._capture_queue.get()
                self._capture_queue.task_done()
                buffer.extend(data)
                while len(buffer) >= self._frame_bytes:
                    frame = bytes(buffer[: self._frame_bytes])
                    del buffer[: self._frame_bytes]
                    await self._handle_capture_frame(frame)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Desktop Voice response capture loop failed.")

    async def _handle_capture_frame(self, frame: bytes) -> None:
        if self._event_loop is None:
            return
        if self._event_loop.time() < self._ignore_capture_until:
            self._segmenter.reset()
            return
        mono = (
            audioop.tomono(frame, 2, 0.5, 0.5)
            if self.response_audio_format[1] == 2
            else frame
        )
        is_speech = self._vad.is_speech(mono, self.response_audio_format[0])
        for event in self._segmenter.feed(frame, is_speech=is_speech):
            if event.kind == "start":
                self._active_response_id = f"desktop-{uuid.uuid4()}"
                await self._audio_playback_manager.start_new_audio_stream(
                    self._active_response_id, self.response_audio_format
                )
            elif event.kind == "audio":
                await self._audio_playback_manager.add_audio_chunk(event.audio)
            elif event.kind == "end":
                await self._audio_playback_manager.end_audio_stream()
                self._active_response_id = None

    async def send_audio_chunk(self, audio_data: bytes) -> bool:
        if not self._connected or self._send_stream is None:
            return False
        if len(audio_data) % (self._channels * 2):
            logger.error("Desktop Voice input chunk is not sample-frame aligned.")
            return False
        try:
            async with self._send_lock:
                underflowed = await asyncio.to_thread(
                    self._send_stream.write, audio_data
                )
            if underflowed:
                logger.warning("Desktop Voice send stream reported an underflow.")
            return True
        except Exception:
            logger.exception("Could not write audio to the desktop Voice microphone.")
            return False

    async def finalize_input_and_request_response(self) -> bool:
        # ChatGPT/Codex Voice performs its own endpointing. Discord silence is
        # already forwarded as zero PCM by GuildSession.
        return self._connected

    async def cancel_ongoing_response(self) -> bool:
        if not self._connected:
            return False
        if self._active_response_id:
            await self._audio_playback_manager.end_audio_stream()
        self._active_response_id = None
        self._segmenter.reset()
        if self._event_loop is not None:
            self._ignore_capture_until = self._event_loop.time() + 0.35
        return True

    async def send_speaker_marker(self, user_id: int, display_name: str) -> bool:
        # The desktop Voice UI accepts only audio through this bridge.
        return True

    async def disconnect(self) -> None:
        await self._close_resources(notify=True)

    async def _close_resources(self, *, notify: bool) -> None:
        was_connected = self._connected
        self._connected = False

        for stream in (self._receive_stream, self._send_stream):
            if stream is None:
                continue
            try:
                await asyncio.to_thread(stream.stop)
            except Exception:
                logger.debug("Desktop Voice stream stop failed.", exc_info=True)
            try:
                await asyncio.to_thread(stream.close)
            except Exception:
                logger.debug("Desktop Voice stream close failed.", exc_info=True)
        self._receive_stream = None
        self._send_stream = None

        if self._capture_task is not None:
            self._capture_task.cancel()
            try:
                await self._capture_task
            except asyncio.CancelledError:
                pass
            self._capture_task = None

        if self._active_response_id:
            await self._audio_playback_manager.end_audio_stream()
        self._active_response_id = None
        self._segmenter.reset()
        if self._voicemeeter is not None:
            await asyncio.to_thread(self._voicemeeter.close)
            self._voicemeeter = None

        if notify and was_connected and self._on_disconnect is not None:
            await self._on_disconnect()
        logger.info("Desktop Voice bridge disconnected.")

    def is_connected(self) -> bool:
        return self._connected
