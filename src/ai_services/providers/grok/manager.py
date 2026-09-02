"""Provider manager for xAI Grok Speech-to-Speech."""

from __future__ import annotations

import base64
from typing import Any, Awaitable, Callable, Dict

from src.ai_services.base_manager import BaseRealtimeManager
from src.ai_services.interface import ProviderCapabilities
from src.audio.playback import AudioPlaybackManager
from src.utils.logger import get_logger

from .connection import GrokRealtimeConnection
from .event_handler import GrokEventHandlerAdapter

logger = get_logger(__name__)


class GrokRealtimeManager(BaseRealtimeManager):
    """Streams manually delimited audio turns over xAI's Realtime WebSocket."""

    def __init__(
        self,
        audio_playback_manager: AudioPlaybackManager,
        service_config: Dict[str, Any],
    ) -> None:
        super().__init__(audio_playback_manager, service_config)
        self._session_config = service_config.get("session_config", {})
        self._connection_handler_inst = GrokRealtimeConnection(
            self._api_key, self._model_name
        )
        self._event_handler_adapter = GrokEventHandlerAdapter(
            audio_playback_manager, self.response_audio_format
        )

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            native_web_search=bool(
                self._service_config.get("native_web_search", False)
            ),
            native_social_search=bool(
                self._service_config.get("native_social_search", False)
            ),
            manual_commit=True,
            cancel_response=True,
            image_input=False,
        )

    @property
    def _connection_handler(self) -> GrokRealtimeConnection:
        return self._connection_handler_inst

    @property
    def _event_callback(self) -> Callable[[Any], Awaitable[None]]:
        return self._event_handler_adapter.dispatch_event

    async def _post_connect_hook(self) -> bool:
        try:
            await self._connection_handler.send_event(
                {"type": "session.update", "session": self._session_config}
            )
            return True
        except Exception as exc:
            logger.error("Failed to configure xAI Realtime session: %s", exc)
            return False

    async def send_audio_chunk(self, audio_data: bytes) -> bool:
        try:
            await self._connection_handler.send_event(
                {
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(audio_data).decode("ascii"),
                }
            )
            return True
        except Exception as exc:
            logger.error("Failed to send audio to xAI Realtime: %s", exc)
            return False

    async def finalize_input_and_request_response(self) -> bool:
        try:
            await self._connection_handler.send_event(
                {"type": "input_audio_buffer.commit"}
            )
            await self._connection_handler.send_event({"type": "response.create"})
            return True
        except Exception as exc:
            logger.error("Failed to commit xAI audio turn: %s", exc)
            return False

    async def cancel_ongoing_response(self) -> bool:
        server_cancelled = True
        if self.is_connected():
            try:
                await self._connection_handler.send_event({"type": "response.cancel"})
            except Exception as exc:
                server_cancelled = False
                logger.warning("xAI response.cancel failed: %s", exc)
        if self._audio_playback_manager.get_current_playing_response_id():
            await self._audio_playback_manager.end_audio_stream()
        return server_cancelled

    async def send_speaker_marker(self, user_id: int, display_name: str) -> bool:
        try:
            await self._connection_handler.send_event(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": f"[speaker: {display_name} ({user_id})]",
                            }
                        ],
                    },
                }
            )
            return True
        except Exception as exc:
            logger.warning("Failed to send xAI speaker marker: %s", exc)
            return False
