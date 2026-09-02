"""Resilient WebSocket connection for xAI Grok Speech-to-Speech."""

from __future__ import annotations

import json
from typing import Any, Optional
from urllib.parse import quote

import websockets
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed

from src.ai_services.base_connection import BaseConnectionHandler
from src.exceptions import AIConnectionError
from src.utils.logger import get_logger

logger = get_logger(__name__)


class GrokRealtimeConnection(BaseConnectionHandler):
    """Connects to the xAI Realtime endpoint and dispatches JSON events."""

    def __init__(self, api_key: str, model_name: str) -> None:
        super().__init__()
        self._api_key = api_key
        self._model_name = model_name
        self._websocket: Optional[ClientConnection] = None

    async def _connection_logic(self) -> None:
        url = f"wss://api.x.ai/v1/realtime?model={quote(self._model_name)}"
        logger.info("Connecting to xAI Realtime with model %s", self._model_name)
        try:
            async with websockets.connect(
                url,
                additional_headers={"Authorization": f"Bearer {self._api_key}"},
                ping_interval=20,
                ping_timeout=20,
                max_size=None,
            ) as websocket:
                self._websocket = websocket
                self._connected_event.set()
                self._retry_delay = 1.0
                if self._on_connect_callback:
                    await self._on_connect_callback()

                async for message in websocket:
                    if self._shutdown_signal.is_set():
                        break
                    if not self._event_callback:
                        continue
                    if isinstance(message, bytes):
                        await self._event_callback(
                            {
                                "type": "response.output_audio.delta.raw",
                                "audio": message,
                            }
                        )
                        continue
                    try:
                        event: Any = json.loads(message)
                    except json.JSONDecodeError:
                        logger.warning("Ignoring malformed JSON event from xAI")
                        continue
                    await self._event_callback(event)
        except ConnectionClosed as exc:
            if not self._shutdown_signal.is_set():
                raise AIConnectionError(
                    f"xAI Realtime connection closed: {exc.code} {exc.reason}"
                ) from exc
        except OSError as exc:
            raise AIConnectionError(
                f"Unable to connect to xAI Realtime: {exc}"
            ) from exc
        finally:
            self._websocket = None
            self._connected_event.clear()

    async def send_event(self, event: dict[str, Any]) -> None:
        if not self._websocket or not self.is_connected():
            raise AIConnectionError("xAI Realtime session is not connected.")
        await self._websocket.send(json.dumps(event))
