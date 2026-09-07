"""Gemini Live SDK transport with explicit response and connection boundaries."""

import asyncio
import re
import uuid
from typing import Optional

from google import genai
from google.genai import types

from src.ai_services.base_connection import BaseConnectionHandler
from src.ai_services.realtime_connection import safe_connection_failure
from src.exceptions import AIConnectionError
from src.observability import get_observer
from .event_handler import TurnStartEvent, TurnMessageEvent, TurnEndEvent


def _failure_metadata(exc: Exception) -> dict:
    """Keep numeric status and fixed labels, never SDK/close response bodies."""
    metadata = safe_connection_failure(exc)
    if metadata.get("close_code") == 1007:
        metadata["error_code"] = "invalid_request"
    code = getattr(exc, "code", None)
    if code in (400, 401, 403, 404, 429):
        metadata["error_code"] = {
            400: "invalid_request", 401: "authentication_error",
            403: "permission_denied", 404: "model_not_found",
            429: "resource_exhausted",
        }[code]
    reason = getattr(getattr(exc, "rcvd", None), "reason", "")
    if isinstance(reason, str) and re.search(
        r"\b(resource_exhausted|quota|quota_exceeded)\b", reason[:2048], re.I
    ):
        metadata["error_code"] = "resource_exhausted"
    return metadata


class GeminiRealtimeConnection(BaseConnectionHandler):
    """Receive independently from tool execution; retry only recoverable failures."""

    _disconnect_timeout = 2.0

    def __init__(self, gemini_client: genai.Client, model_name: str,
                 live_connect_config_params: dict) -> None:
        super().__init__()
        self.gemini_client = gemini_client
        self.model_name = model_name
        self.live_connect_config_params = live_connect_config_params
        self._session_object: Optional[genai.live.AsyncSession] = None
        self.observer = get_observer()
        self.observation_context = lambda: {"provider": "gemini"}
        self._terminal_error_code = None

    @property
    def terminal_error_code(self) -> Optional[str]:
        return self._terminal_error_code

    async def connect(self, event_callback, on_connect, on_disconnect) -> None:
        if not self._is_attempting_connection and (
            self._event_loop_task is None or self._event_loop_task.done()
        ):
            self._terminal_error_code = None
        await super().connect(event_callback, on_connect, on_disconnect)

    async def _emit(self, event) -> None:
        if self._event_callback:
            # The manager schedules tool execution so incoming cancellation
            # messages remain live while a tool request is outstanding.
            await self._event_callback(event)

    @staticmethod
    def _starts_response(message) -> bool:
        content = getattr(message, "server_content", None)
        return bool(
            getattr(message, "tool_call", None)
            or (content and (
                getattr(content, "model_turn", None)
                or getattr(content, "output_transcription", None)
            ))
        )

    async def _connection_logic(self) -> None:
        connected = False
        try:
            config = types.LiveConnectConfig(**self.live_connect_config_params)
            async with self.gemini_client.aio.live.connect(
                model=self.model_name, config=config
            ) as session:
                self._session_object = session
                self._connected_event.set()
                connected = True
                if self._on_connect_callback:
                    await self._on_connect_callback()
                turn_id = None
                while not self._shutdown_signal.is_set():
                    received = False
                    async for message in session.receive():
                        received = True
                        if self._shutdown_signal.is_set():
                            break
                        if turn_id is None and self._starts_response(message):
                            turn_id = uuid.uuid4().hex
                            await self._emit(TurnStartEvent(turn_id=turn_id))
                        await self._emit(TurnMessageEvent(message=message))
                        content = getattr(message, "server_content", None)
                        if content and getattr(content, "turn_complete", False):
                            if turn_id is not None:
                                await self._emit(TurnEndEvent(turn_id=turn_id))
                                turn_id = None
                            # Reset after useful work, not a handshake followed
                            # by immediate closure (which would retry forever).
                            self._retry_delay = 1.0
                    if not received and not self._shutdown_signal.is_set():
                        raise AIConnectionError("Gemini receive stream closed.")
        except Exception as exc:
            metadata = _failure_metadata(exc)
            terminal = metadata["error_code"] in {
                "resource_exhausted", "rate_limit_exceeded", "rate_limit_error",
                "insufficient_quota", "insufficient_quota.credit_balance_exhausted",
                "authentication_error", "invalid_api_key", "permission_denied",
                "invalid_request", "model_not_found",
            }
            if terminal:
                self._terminal_error_code = metadata["error_code"]
                self._shutdown_signal.set()
            self.observer.emit(
                "transport.connection.failed", **self.observation_context(),
                **metadata, terminal=terminal,
                reason="retry_stopped" if terminal else "connection_or_dispatch_error",
            )
            if not terminal:
                raise AIConnectionError(
                    f"Gemini transport failed ({metadata['error_code']})."
                ) from None
        finally:
            self._session_object = None
            self._connected_event.clear()
            # The base retry loop cannot detect loss after this flag clears.
            if (connected or self._terminal_error_code) and self._on_disconnect_callback:
                try:
                    await self._on_disconnect_callback()
                except Exception as exc:
                    self.observer.emit(
                        "transport.callback.failed", **self.observation_context(),
                        error_code=type(exc).__name__, reason="disconnect_callback",
                    )

    def get_active_session(self) -> Optional[genai.live.AsyncSession]:
        return self._session_object if self.is_connected() else None

    async def disconnect(self) -> None:
        """Cancel a waiting receive immediately and bound SDK cleanup time."""
        self._shutdown_signal.set()
        self._connected_event.clear()
        task = self._event_loop_task
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()
            done, _ = await asyncio.wait({task}, timeout=self._disconnect_timeout)
            if not done:
                # Keep the task reference to prevent another receive loop from
                # starting while uncooperative SDK cleanup is still alive.
                self.observer.emit("transport.disconnect.timeout",
                                   **self.observation_context(), reason="sdk_cleanup")
                return
        if task and task.done() and not task.cancelled():
            task.exception()
        self._event_loop_task = None
        self._session_object = None
        self._is_attempting_connection = False
