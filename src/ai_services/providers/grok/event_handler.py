"""Maps xAI Realtime server events to Discord streaming playback."""

from __future__ import annotations

import base64
from typing import Any, Dict, Optional, Tuple
import uuid

from src.audio.playback import AudioPlaybackManager
from src.utils.logger import get_logger

logger = get_logger(__name__)


class GrokEventHandlerAdapter:
    """Consumes both current and OpenAI-compatible xAI event aliases."""

    AUDIO_DELTA_EVENTS = {
        "response.output_audio.delta",
        "response.audio.delta",
        "response.output_audio.delta.raw",
    }
    AUDIO_DONE_EVENTS = {
        "response.output_audio.done",
        "response.audio.done",
        "response.done",
    }

    def __init__(
        self,
        audio_playback_manager: AudioPlaybackManager,
        response_audio_format: Tuple[int, int],
    ) -> None:
        self.audio_playback_manager = audio_playback_manager
        self.response_audio_format = response_audio_format
        self._active_response_id: Optional[str] = None
        self._stream_started = False

    async def dispatch_event(self, event: Dict[str, Any]) -> None:
        event_type = event.get("type", "")
        if event_type == "response.created":
            response = event.get("response") or {}
            self._active_response_id = (
                event.get("response_id") or response.get("id") or str(uuid.uuid4())
            )
            self._stream_started = False
            return

        if event_type in self.AUDIO_DELTA_EVENTS:
            await self._handle_audio_delta(event)
            return

        if event_type in self.AUDIO_DONE_EVENTS:
            await self._finish_stream()
            return

        if event_type in {
            "response.output_audio_transcript.delta",
            "response.audio_transcript.delta",
        }:
            # Text belongs only in the explicitly enabled diagnostic capture.
            return

        if event_type == "error":
            logger.error("xAI Realtime error code: %s", (event.get("error") or {}).get("code", "unknown"))

    async def _handle_audio_delta(self, event: Dict[str, Any]) -> None:
        if not self._active_response_id:
            self._active_response_id = event.get("response_id") or str(uuid.uuid4())
        if not self._stream_started:
            await self.audio_playback_manager.start_new_audio_stream(
                self._active_response_id, self.response_audio_format
            )
            self._stream_started = True

        if isinstance(event.get("audio"), bytes):
            audio_data = event["audio"]
        else:
            encoded = event.get("delta") or event.get("audio")
            if not encoded:
                return
            try:
                audio_data = base64.b64decode(encoded)
            except (ValueError, TypeError) as exc:
                logger.warning("Invalid base64 audio delta from xAI: %s", exc)
                return
        await self.audio_playback_manager.add_audio_chunk(audio_data)

    async def _finish_stream(self) -> None:
        if self._stream_started:
            await self.audio_playback_manager.end_audio_stream()
        self._active_response_id = None
        self._stream_started = False
