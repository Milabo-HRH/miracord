"""Shared JSON WebSocket transport with provider-specific fixed endpoints."""

from __future__ import annotations

import json
import re
from urllib.parse import quote

import websockets

from src.ai_services.base_connection import BaseConnectionHandler
from src.exceptions import AIConnectionError
from src.observability import get_observer


_KNOWN_CLOSE_ERRORS = (
    "insufficient_quota.credit_balance_exhausted", "insufficient_quota",
    "invalid_api_key", "authentication_error", "permission_denied",
    "rate_limit_exceeded", "rate_limit_error",
)
_TERMINAL_CLOSE_ERRORS = frozenset({
    "insufficient_quota.credit_balance_exhausted", "insufficient_quota",
})


def safe_connection_failure(exc: Exception) -> dict:
    """Extract only protocol codes and known error labels, never a close body."""
    frame = getattr(exc, "rcvd", None)
    code = getattr(frame, "code", None)
    reason = getattr(frame, "reason", "")
    metadata = {"error_code": type(exc).__name__}
    if isinstance(code, int) and 1000 <= code <= 4999:
        metadata["close_code"] = code
    if isinstance(reason, str):
        reason = reason[:2048].lower()
        for known in _KNOWN_CLOSE_ERRORS:
            if re.search(r"(?<![a-z0-9_])" + re.escape(known) + r"(?![a-z0-9_])", reason):
                metadata["error_code"] = known
                break
    if metadata["error_code"] == type(exc).__name__:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in (401, 403, 429):
            metadata["error_code"] = {401: "authentication_error", 403: "permission_denied",
                                      429: "rate_limit_exceeded"}[status]
    return metadata


class RealtimeWebSocketConnection(BaseConnectionHandler):
    endpoint = ""

    def __init__(self, api_key: str, model_name: str) -> None:
        super().__init__()
        self._api_key = api_key
        self._model_name = model_name
        self._websocket = None
        self.observer = get_observer()
        self.observation_context = lambda: {}
        self._terminal_error_code: str | None = None

    @property
    def terminal_error_code(self) -> str | None:
        return self._terminal_error_code

    async def connect(self, event_callback, on_connect, on_disconnect) -> None:
        if not self._is_attempting_connection and (
            self._event_loop_task is None or self._event_loop_task.done()
        ):
            # A later explicit user action may retry after the credit is restored.
            # The automatic retry loop must retain terminal state for this attempt.
            self._terminal_error_code = None
        await super().connect(event_callback, on_connect, on_disconnect)

    async def _connection_logic(self) -> None:
        connected = False
        try:
            async with websockets.connect(
                f"{self.endpoint}?model={quote(self._model_name, safe='')}",
                additional_headers={"Authorization": f"Bearer {self._api_key}"},
                ping_interval=20,
                ping_timeout=20,
                max_size=4 * 1024 * 1024,
            ) as websocket:
                self._websocket = websocket
                self._connected_event.set()
                connected = True
                self._retry_delay = 1.0
                if self._on_connect_callback:
                    await self._on_connect_callback()
                async for message in websocket:
                    if self._shutdown_signal.is_set():
                        break
                    try:
                        event = json.loads(message)
                    except (ValueError, UnicodeError):
                        self.observer.emit("transport.event.rejected", **self.observation_context(),
                                           reason="invalid_json")
                        continue
                    if isinstance(event, dict) and self._event_callback:
                        await self._event_callback(event)
        except Exception as exc:
            metadata = safe_connection_failure(exc)
            if metadata["error_code"] in _TERMINAL_CLOSE_ERRORS:
                self._terminal_error_code = metadata["error_code"]
                self._shutdown_signal.set()
            self.observer.emit("transport.connection.failed", **self.observation_context(),
                               **metadata, terminal=bool(self._terminal_error_code),
                               reason="retry_stopped" if self._terminal_error_code else "connection_or_dispatch_error")
            if self._terminal_error_code:
                return
            # Provider exception bodies can contain request metadata. Keep the
            # retry loop's existing textual traceback free of those bodies.
            raise AIConnectionError(f"Realtime transport failed ({metadata['error_code']}).") from None
        finally:
            self._websocket = None
            self._connected_event.clear()
            # Notify here: the base retry loop cannot see the cleared flag.
            if (connected or self._terminal_error_code) and self._on_disconnect_callback:
                await self._on_disconnect_callback()

    async def send_event(self, event: dict) -> None:
        if not self._websocket or not self.is_connected():
            raise AIConnectionError("Realtime transport is not connected.")
        await self._websocket.send(json.dumps(event, ensure_ascii=False))

    async def disconnect(self) -> None:
        self._shutdown_signal.set()
        if self._websocket:
            await self._websocket.close()
        await super().disconnect()
