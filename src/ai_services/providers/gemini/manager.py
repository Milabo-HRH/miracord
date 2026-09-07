"""Gemini Live voice with ordered identity, bounded local tools and diagnostics."""

from __future__ import annotations

import asyncio
import copy
import json
import time
from collections import deque
from typing import Any

from google import genai
from google.genai import types

from src.ai_services.base_manager import BaseRealtimeManager
from src.ai_services.interface import ProviderCapabilities
from src.ai_services.tool_realtime_manager import ToolRealtimeManager
from src.lol_mcp.context import CONTEXT_INSTRUCTIONS, GameContextService, encode
from src.lol_mcp.prompts import MAYHEM_TOOL_INSTRUCTIONS
from src.lol_mcp.tools import LEAGUE_TOOLS, LeagueToolExecutor
from src.observability import fingerprint, get_observer
from .connection import GeminiRealtimeConnection
from .event_handler import GeminiEventHandlerAdapter, TurnEndEvent, TurnMessageEvent, TurnStartEvent


def _field(value: Any, name: str, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _instructions(value: Any) -> str:
    if isinstance(value, str):
        return value
    return "\n".join(str(_field(part, "text", "")) for part in _field(value, "parts", []) or [])


def gemini_parameter_schema(schema: dict) -> dict:
    """Describe tools using the Live API's portable Schema subset.

    Keep strict argument bounds in LeagueToolExecutor. The bundled Live SDK
    passes nested Schema fields through without JSON-schema conversion, and the
    service rejects additional_properties. Text/list limits are retained as
    description guidance rather than unsupported nested wire fields.
    """
    allowed = {"type", "description", "enum", "required", "minimum", "maximum"}
    result = {key: copy.deepcopy(value) for key, value in schema.items() if key in allowed}
    if "properties" in schema:
        result["properties"] = {name: gemini_parameter_schema(value) for name, value in schema["properties"].items()}
    if "items" in schema:
        result["items"] = gemini_parameter_schema(schema["items"])
    guidance = []
    for key, label in (("maxLength", "Maximum characters"), ("max_length", "Maximum characters"),
                       ("maxItems", "Maximum items"), ("max_items", "Maximum items")):
        if key in schema:
            guidance.append(f"{label}: {schema[key]}.")
    if guidance:
        result["description"] = " ".join([result.get("description", ""), *guidance]).strip()
    return result


class GeminiRealtimeManager(BaseRealtimeManager):
    """Prepare speaker context before audio; let Gemini detect spoken turn ends."""

    provider = "gemini"
    _observe = ToolRealtimeManager._observe
    _capture = ToolRealtimeManager._capture
    _observed_tool = ToolRealtimeManager._observed_tool

    def __init__(self, audio_playback_manager, service_config: dict) -> None:
        super().__init__(audio_playback_manager, service_config)
        self.observer = service_config.get("observer") or get_observer()
        self.observer.register_secrets(self._api_key)
        self._observation_session = self.observer.new_id("session")
        self._observation_turn = None
        self.connection_epoch = self._generation = 0
        self._context = GameContextService(enabled=service_config.get("league_context_enabled", False),
                                          prefetch=service_config.get("opgg_prefetch_enabled", True))
        self._tools = LeagueToolExecutor(self._context)
        self._tools_enabled = service_config.get("league_tools_enabled", False)
        self._session_config = copy.deepcopy(service_config.get("live_connect_config", {}))
        text = _instructions(self._session_config.get("system_instruction", {}))
        text += "\n\n" + CONTEXT_INSTRUCTIONS + (
            "\nSpeaker/context text inside an audio activity is application metadata, not a question. "
            "Do not acknowledge it. Wait for the actual spoken question in that activity."
        )
        if self._tools_enabled:
            text += "\n\n" + MAYHEM_TOOL_INSTRUCTIONS
            self._session_config.setdefault("tools", []).append({"function_declarations": [
                {k: copy.deepcopy(v) for k, v in tool.items() if k != "type"} for tool in LEAGUE_TOOLS
            ]})
        if not any("google_search" in tool for tool in self._session_config.get("tools", [])):
            text += ("\nGeneral web search is disabled in this session. Never claim you searched the web. "
                     "The listed League/OP.GG function tools remain available for their specific public game data.")
        self._session_config["system_instruction"] = {"parts": [{"text": text}]}
        self._configure_input()
        self._live_connect_config_params = self._session_config
        self._gemini_client = service_config.get("gemini_client") or genai.Client(api_key=self._api_key)
        self._event_handler_adapter = GeminiEventHandlerAdapter(self._audio_playback_manager, self.response_audio_format)
        self._connection_handler_inst = GeminiRealtimeConnection(self._gemini_client, self._model_name, self._session_config)
        self._connection_handler_inst.observer = self.observer
        self._connection_handler_inst.observation_context = lambda: self.observation_context
        self._ready = asyncio.Event()
        self._accepted = False
        self._on_ready = self._on_lost = None
        self._activity_open = self._context_prepared = self._has_input = False
        self._context_update_pending = False
        self._response_boundary_complete = asyncio.Event()
        self._response_boundary_complete.set()
        self._context_transport_ready = False
        self._context_transport_epoch = None
        self._metadata_response_ids = deque(maxlen=256)
        self._metadata_tool_rounds = 0
        self._last_context_text = ""
        self._last_server_response_turn = None
        self._server_turn_needs_refresh = False
        self._needs_reconnect = self._awaiting_response = self._turn_first_audio = False
        self._turn_started_at = time.monotonic()
        self._response_id = None
        self._response_context = {}
        self._pending_response_contexts = deque(maxlen=256)
        self._pending_transcription_contexts = deque(maxlen=256)
        self._ambiguous_responses = 0
        self._response_started_at = time.monotonic()
        self._response_audio_ms = 0
        self._response_first_audio = False
        self._response_completed = asyncio.Event()
        self._last_response_text = self._last_input_text = ""
        self._last_response_status = None
        self._input_text = self._output_text = ""
        self._input_context = {}
        self._input_truncated = self._output_truncated = False
        self._suppressed_responses = deque(maxlen=256)
        self._cancel_pending_response = False
        self._cancelled_pending_context = {}
        self._tool_tasks: set[asyncio.Task] = set()
        self._call_tasks: dict[str, asyncio.Task] = {}
        self._seen_calls = deque(maxlen=512)
        self._cancelled_calls = deque(maxlen=512)
        self._tool_rounds = self._response_tool_count = 0

    def _configure_input(self) -> None:
        for tool in self._session_config.get("tools", []):
            for declaration in tool.get("function_declarations", []):
                if isinstance(declaration.get("parameters"), dict):
                    declaration["parameters"] = gemini_parameter_schema(declaration["parameters"])
        self._session_config.setdefault("realtime_input_config", {}).setdefault("automatic_activity_detection", {}).setdefault("disabled", False)
        if self.observer.capture_enabled:
            self._session_config["input_audio_transcription"] = {}
            self._session_config["output_audio_transcription"] = {}

    def apply_replay_config(self, snapshot: dict) -> None:
        if self.is_connected():
            raise ValueError("Apply replay configuration before connecting")
        self._session_config = copy.deepcopy(snapshot.get("session_config", snapshot))
        if "session_config" in snapshot and isinstance(snapshot.get("model"), str):
            self._model_name = snapshot["model"]
            self._connection_handler_inst.model_name = self._model_name
        self._configure_input()
        self._live_connect_config_params = self._session_config
        self._connection_handler_inst.live_connect_config_params = self._session_config

    @property
    def _server_vad(self) -> bool:
        return not self._session_config.get("realtime_input_config", {}).get("automatic_activity_detection", {}).get("disabled", False)

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(native_web_search=any("google_search" in t for t in self._session_config.get("tools", [])),
                                    manual_commit=True, cancel_response=False, image_input=True,
                                    realtime_audio_input=True, server_vad=self._server_vad, client_vad_streaming=not self._server_vad, turn_context=True)

    @property
    def observation_context(self) -> dict:
        return {"session_id": self._observation_session, "turn_id": self._observation_turn,
                "provider": self.provider, "connection_epoch": self.connection_epoch}

    @property
    def _connection_handler(self):
        return self._connection_handler_inst

    @property
    def _event_callback(self):
        return self._dispatch_event

    def is_connected(self) -> bool:
        return bool(getattr(self, "_accepted", False) and self._connection_handler.is_connected())

    async def connect(self, on_connect, on_disconnect) -> bool:
        if self.is_connected():
            return True
        self._on_ready, self._on_lost = on_connect, on_disconnect
        self._connection_handler_inst.observer = self.observer
        self._ready.clear()
        self._context.start()
        self._observe("session.connecting", model=self._model_name)
        try:
            await self._connection_handler.connect(self._dispatch_event, self._socket_ready, self._transport_lost)
            await asyncio.wait_for(self._ready.wait(), self._service_config.get("connection_timeout", 30))
            if self.is_connected():
                return True
        except asyncio.TimeoutError:
            self._observe("session.failed", status="timeout", reason="connection_timeout")
        except Exception as exc:
            self._observe("session.failed", status="unavailable", error_code=type(exc).__name__)
        await self.disconnect()
        return False

    async def _socket_ready(self) -> None:
        self.connection_epoch += 1
        self._invalidate_work()
        self._accepted = True
        self._needs_reconnect = self._activity_open = self._context_prepared = False
        self._has_input = self._awaiting_response = self._cancel_pending_response = False
        self._context_update_pending = self._context_transport_ready = False
        self._metadata_response_ids.clear()
        self._response_id = None
        self._pending_response_contexts.clear()
        self._pending_transcription_contexts.clear()
        self._ambiguous_responses = 0
        self._suppressed_responses.clear()
        self._seen_calls.clear()
        self._cancelled_calls.clear()
        await self._post_connect_hook()
        self._ready.set()
        self._observe("session.ready", status="ok", model=self._model_name)
        if self._on_ready:
            await self._on_ready()

    async def _post_connect_hook(self) -> bool:
        self._connection_handler_inst.observer = self.observer
        text = _instructions(self._session_config.get("system_instruction", {}))
        self._capture("session.configured", {"instructions": text, "tools": self._session_config.get("tools", []),
                      "model": self._model_name, "session_config": self._session_config}, model=self._model_name,
                      prompt_hash=fingerprint(text), tools_hash=fingerprint(self._session_config.get("tools", [])),
                      capture_enabled=self.observer.capture_enabled)
        return True

    async def _transport_lost(self) -> None:
        self._accepted = self._activity_open = self._context_prepared = False
        self._context_transport_ready = False
        self._context_update_pending = False
        self._invalidate_work()
        self._observe("session.disconnected", reason="transport_lost")
        terminal = getattr(self._connection_handler, "terminal_error_code", None)
        if terminal:
            self._observe("session.failed", status="unavailable", error_code=terminal, reason="terminal_provider_error")
            self._last_response_status = "failed"
            self._response_completed.set()
            self._ready.set()
        if self._on_lost:
            await self._on_lost()

    def _invalidate_work(self) -> None:
        self._generation += 1
        for task in (*tuple(self._tool_tasks), *tuple(self._call_tasks.values())):
            task.cancel()

    async def disconnect(self) -> None:
        self._accepted = self._activity_open = self._context_prepared = False
        self._context_transport_ready = False
        self._invalidate_work()
        await asyncio.gather(*tuple(self._tool_tasks), *tuple(self._call_tasks.values()), return_exceptions=True)
        await self._connection_handler.disconnect()
        await self._context.close()
        self._observe("session.disconnected", reason="requested")

    async def _get_active_session(self):
        return self._connection_handler.get_active_session() if self.is_connected() else None

    async def _send_realtime(self, **kwargs) -> bool:
        session = await self._get_active_session()
        if session is None:
            self._observe("transport.send.failed", event_type=next(iter(kwargs)), reason="not_connected")
            return False
        try:
            await session.send_realtime_input(**kwargs)
            return True
        except Exception as exc:
            self._observe("transport.send.failed", event_type=next(iter(kwargs)), reason="transport_error", error_code=type(exc).__name__)
            return False

    async def _prepare_turn(self, *, preserve_server_activity: bool = False) -> bool:
        if (self._activity_open and not preserve_server_activity) or self._response_id is not None:
            await self.cancel_ongoing_response()
        if self._needs_reconnect:
            if not await self.connect(self._on_ready, self._on_lost):
                return False
        self._invalidate_work()
        self._observation_turn = self.observer.new_id("turn")
        self._turn_started_at = time.monotonic()
        self._turn_first_audio = self._has_input = self._context_prepared = False
        self._server_turn_needs_refresh = False
        self._tool_rounds = 0
        self._last_input_text = ""
        if not self._pending_transcription_contexts:
            self._input_text = ""
            self._input_truncated = False
        self._input_context = dict(self.observation_context)
        self._response_completed.clear()
        self._last_response_status = None
        self._last_response_text = ""
        if self._response_id is None:
            self._output_text = ""
            self._response_tool_count = self._response_audio_ms = 0
        return True

    async def send_turn_context(self, user_id: int, display_name: str, *, streaming: bool = False) -> bool:
        self.observer.register_secrets(str(user_id), display_name)
        text = self._context.turn_context(user_id, display_name)
        reuse_context = (
            self._server_vad and self._context_transport_ready
            and self._context_transport_epoch == self.connection_epoch
            and text == self._last_context_text
            and not self._needs_reconnect and not self._context_update_pending
            and not self._cancel_pending_response and not self._awaiting_response
            and self._response_id is None and not self._pending_response_contexts
        )
        if not await self._prepare_turn(preserve_server_activity=reuse_context):
            return False
        if reuse_context:
            # Identical identity and snapshot were already sent on this socket.
            # Start a fresh local turn without another metadata write/end.
            self._activity_open = self._context_prepared = True
            self._capture("turn.context.reused", {"text": text}, status="ok",
                          reason="identical_sent_context",
                          speaker_id=self.observer.pseudonym(user_id))
            return True
        self._observe("turn.context.started", speaker_id=self.observer.pseudonym(user_id), streaming=streaming)
        if not self._server_vad and not await self._send_realtime(activity_start=types.ActivityStart()):
            self._observe("turn.context.failed", status="failed", reason="activity_start_failed")
            return False
        self._activity_open = True
        self._last_context_text = text
        self._context_transport_ready = False
        self._context_update_pending = self._server_vad
        self._metadata_tool_rounds = 0
        epoch, generation = self.connection_epoch, self._generation
        if not await self._send_realtime(text=text):
            self._observe("turn.context.failed", status="failed", reason="context_send_failed")
            await self.cancel_ongoing_response()
            return False
        if epoch != self.connection_epoch or generation != self._generation or not self._activity_open:
            self._observe("turn.context.failed", status="failed", reason="context_cancelled_or_transport_changed")
            return False
        # Gemini 3.1 has no silent mid-session context acknowledgement. Await
        # the transport write before PCM; waiting for a model reply contradicts
        # the instruction to wait for speech and can deadlock the first turn.
        self._context_transport_ready = True
        self._context_prepared = True
        self._context_transport_epoch = self.connection_epoch if self._server_vad else None
        self._capture("turn.context.sent", {"text": text}, status="ok", reason="ordered_transport_send" if self._server_vad else "ordered_activity_context",
                      speaker_id=self.observer.pseudonym(user_id), duration_ms=round((time.monotonic() - self._turn_started_at) * 1000, 3))
        return True

    async def send_speaker_marker(self, user_id: int, display_name: str) -> bool:
        return await self.send_turn_context(user_id, display_name)

    async def send_audio_chunk(self, audio_data: bytes) -> bool:
        if not self._context_prepared or not self._activity_open:
            self._observe("transport.send.failed", event_type="audio", reason="context_not_prepared")
            return False
        epoch, generation = self.connection_epoch, self._generation
        rate, channels = self.processing_audio_format
        bytes_per_second = rate * channels * 2
        chunk_size = max(1, bytes_per_second // 10)
        for offset in range(0, len(audio_data), chunk_size):
            if generation != self._generation or not self._activity_open:
                return False
            chunk = audio_data[offset:offset + chunk_size]
            if not await self._send_realtime(audio=types.Blob(data=chunk, mime_type=f"audio/pcm;rate={rate}")):
                return False
            if (generation != self._generation or epoch != self.connection_epoch
                    or not self._activity_open or not self._context_prepared):
                return False
            self._has_input = True
            if not self._turn_first_audio:
                # Keep already identified metadata response IDs suppressed;
                # only future responses may belong to the real microphone.
                self._context_update_pending = False
                self._turn_first_audio = True
                self._awaiting_response = True
                self._pending_response_contexts.append(dict(self.observation_context))
                self._pending_transcription_contexts.append(dict(self.observation_context))
                self._observe("turn.audio.first_sent", audio_bytes=len(chunk), elapsed_ms=round((time.monotonic() - self._turn_started_at) * 1000, 3))
            if offset + chunk_size < len(audio_data):
                await asyncio.sleep(len(chunk) / bytes_per_second)
        return True

    async def _abandon_empty_activity(self) -> None:
        # Gemini rejects activity_end when no audio has arrived. Discard the
        # incomplete session; never inject fake audio just to close the activity.
        self._needs_reconnect = True
        self._accepted = self._activity_open = self._context_prepared = False
        await self._connection_handler.disconnect()
        self._observe("session.disconnected", reason="empty_activity_reset")

    async def finalize_input_and_request_response(self) -> bool:
        if not self._activity_open:
            return True
        if not self._has_input:
            if self._server_vad:
                self._activity_open = self._context_prepared = False
            else:
                await self._abandon_empty_activity()
            return True
        sent = await self._send_realtime(audio_stream_end=True) if self._server_vad else await self._send_realtime(activity_end=types.ActivityEnd())
        if sent:
            self._activity_open = self._context_prepared = False
        self._observe("turn.input.finalized", status="ok" if sent else "failed", reason="audio_stream_end" if self._server_vad else "activity_end")
        return sent

    async def send_text_turn(self, text: str, *, context_text: str | None = None) -> bool:
        """Text replay uses one realtime text message, not an empty audio activity."""
        if not isinstance(text, str) or not text or len(text) > 32768 or not await self._prepare_turn():
            return False
        combined = (context_text + "\n\nCurrent user question:\n" if context_text else "") + text
        sent = await self._send_realtime(text=combined)
        if sent:
            self._has_input = self._awaiting_response = True
            self._pending_response_contexts.append(dict(self.observation_context))
            self._last_input_text = text
            self._capture("turn.context.sent", {"text": context_text or ""}, status="ok", reason="same_text_message_prefix")
            self._capture("transcript.user", {"text": text}, correlation="text_input")
            self._observe("turn.input.finalized", status="ok", reason="realtime_text")
        return sent

    async def cancel_ongoing_response(self) -> bool:
        """Suppress local output/work. Activity end is not a server cancel RPC."""
        self._invalidate_work()
        unidentified = self._response_id is None and (self._awaiting_response or self._context_update_pending)
        if self._response_id:
            self._suppressed_responses.append(self._response_id)
        elif unidentified:
            self._cancel_pending_response = True
            self._cancelled_pending_context = dict(self.observation_context)
            self._response_boundary_complete.clear()
        self._observe("response.cancelled", response_id=self._response_id, reason="local_suppression")
        self._context_update_pending = self._context_transport_ready = False
        await self._event_handler_adapter.cancel_current_turn()
        sent = True
        if self._activity_open:
            if self._has_input:
                sent = await self._send_realtime(audio_stream_end=True) if self._server_vad else await self._send_realtime(activity_end=types.ActivityEnd())
            elif not self._server_vad:
                await self._abandon_empty_activity()
        self._activity_open = self._context_prepared = self._has_input = False
        self._last_response_status = "cancelled"
        self._response_completed.set()
        return sent

    async def _begin_response(self, response_id: str) -> None:
        if self._server_vad and self._server_turn_needs_refresh and not self._context_update_pending and not self._cancel_pending_response:
            self._advance_server_turn()
        self._response_id = response_id
        self._response_boundary_complete.clear()
        if self._context_update_pending:
            self._metadata_response_ids.append(response_id)
            self._suppressed_responses.append(response_id)
            origin = {**self.observation_context, "correlation": "context_only"}
        elif self._cancel_pending_response:
            origin = {**self._cancelled_pending_context, "turn_id": None, "correlation": "ambiguous"}
        elif len(self._pending_response_contexts) > 1 or self._ambiguous_responses:
            self._ambiguous_responses += len(self._pending_response_contexts)
            self._pending_response_contexts.clear()
            self._ambiguous_responses = max(0, self._ambiguous_responses - 1)
            origin = {**self.observation_context, "turn_id": None, "correlation": "ambiguous"}
        else:
            origin = self._pending_response_contexts.popleft() if self._pending_response_contexts else self.observation_context
        self._response_context = {**origin, "response_id": response_id}
        self._response_started_at = time.monotonic()
        self._response_audio_ms = self._response_tool_count = 0
        self._response_first_audio = self._output_truncated = False
        self._output_text = ""
        if self._cancel_pending_response:
            self._suppressed_responses.append(response_id)
        self._observe("response.started", context=self._response_context, status="suppressed" if response_id in self._suppressed_responses else "in_progress")
        await self._event_handler_adapter.dispatch_event(TurnStartEvent(response_id))
        if response_id in self._suppressed_responses:
            await self._event_handler_adapter.cancel_current_turn()

    def _context_response_finished(self) -> None:
        # This is optional metadata output, not a context-ready acknowledgement.
        # Continue suppressing metadata until the first successful PCM send.
        self._observe("turn.context.metadata_completed", status="suppressed", reason="context_only")

    def _advance_server_turn(self) -> None:
        """A continuing floor owner may ask another question without a new marker."""
        self._server_turn_needs_refresh = False
        self._observation_turn = self.observer.new_id("turn")
        self._turn_started_at = time.monotonic()
        self._tool_rounds = 0
        self._awaiting_response = True
        self._response_completed.clear()
        self._last_input_text = ""
        self._input_context = dict(self.observation_context)
        self._pending_response_contexts.append(dict(self.observation_context))
        self._pending_transcription_contexts.append(dict(self.observation_context))
        self._capture("turn.context.reused", {"text": self._last_context_text}, status="ok", reason="server_vad_next_turn")

    async def _reject_metadata_tools(self, tool_call) -> None:
        """Complete metadata-only tool requests without querying local sources."""
        self._metadata_tool_rounds += 1
        if self._metadata_tool_rounds > 3:
            await self.cancel_ongoing_response()
            self._observe("turn.context.failed", status="failed", reason="metadata_tool_budget")
            return
        responses = [types.FunctionResponse(id=_field(call, "id"), name=_field(call, "name", ""),
                     response={"status": "unavailable", "reason": "context_only_not_user_request"})
                     for call in (_field(tool_call, "function_calls", []) or [])[:64]]
        session = await self._get_active_session()
        if session and responses:
            try:
                await session.send_tool_response(function_responses=responses)
                self._observe("tool.context_only.rejected", reason="not_user_request", tool_count=len(responses))
            except Exception as exc:
                self._observe("transport.send.failed", event_type="tool_response", reason="context_only", error_code=type(exc).__name__)

    async def _dispatch_event(self, event) -> None:
        if self._needs_reconnect:
            return
        if isinstance(event, TurnStartEvent):
            await self._begin_response(event.turn_id)
        elif isinstance(event, TurnMessageEvent):
            await self._dispatch_message(event.message, _from_connection=True)
        elif isinstance(event, TurnEndEvent):
            await self._finish_response(event.turn_id)
        else:
            await self._dispatch_message(event)

    def _append_transcript(self, role: str, text: Any) -> None:
        if not isinstance(text, str) or not text:
            return
        attr = "_input_text" if role == "user" else "_output_text"
        old = getattr(self, attr)
        if len(old) + len(text) > 32768:
            setattr(self, "_input_truncated" if role == "user" else "_output_truncated", True)
        setattr(self, attr, (old + text)[:32768])

    def _flush_input_transcript(self) -> None:
        if self._input_text:
            self._last_input_text = self._input_text
            if len(self._pending_transcription_contexts) > 1:
                context = {**self.observation_context, "turn_id": None, "correlation": "ambiguous"}
            else:
                context = self._pending_transcription_contexts[0] if self._pending_transcription_contexts else self._input_context
            if self._pending_transcription_contexts:
                self._pending_transcription_contexts.popleft()
            self._capture("transcript.user", {"text": self._input_text + ("[TRUNCATED]" if self._input_truncated else "")},
                          context=context or None, correlation=context.get("correlation", "activity"))
            self._input_text = ""

    def _clear_pending_cancellation(self) -> None:
        cancelled_turn = self._cancelled_pending_context.get("turn_id")
        self._pending_response_contexts = deque(
            (context for context in self._pending_response_contexts if context.get("turn_id") != cancelled_turn), maxlen=256
        )
        self._cancel_pending_response = False
        self._cancelled_pending_context = {}
        self._response_boundary_complete.set()

    async def _dispatch_message(self, message, *, _from_connection: bool = False) -> None:
        if self._needs_reconnect:
            return
        content, tool_call = _field(message, "server_content"), _field(message, "tool_call")
        if self._server_vad and self._server_turn_needs_refresh and _field(content, "input_transcription") and not self._context_update_pending:
            self._advance_server_turn()
        if self._context_update_pending and tool_call:
            await self._reject_metadata_tools(tool_call)
        cancellation = _field(message, "tool_call_cancellation")
        if cancellation:
            for call_id in (_field(cancellation, "ids", []) or [])[:64]:
                self._cancelled_calls.append(call_id)
                if call_id in self._call_tasks:
                    self._call_tasks[call_id].cancel()
                self._observe("tool.cancelled", context=self._response_context or None, call_id=call_id, status="cancelled", reason="provider_cancelled")
        if content and _field(content, "interrupted", False):
            if (self._server_vad and self._activity_open and self._context_prepared
                    and self._response_id is not None
                    and self._response_context.get("turn_id") == self._observation_turn):
                self._advance_server_turn()
            # The interruption can acknowledge a NEW user's activity_start.
            # Do not invalidate that user's audio generation while cancelling
            # the previous response's tool work.
            for task in (*tuple(self._tool_tasks), *tuple(self._call_tasks.values())):
                task.cancel()
            if self._response_id:
                self._suppressed_responses.append(self._response_id)
            if self._cancel_pending_response and self._response_id is None:
                self._clear_pending_cancellation()
            await self._event_handler_adapter.cancel_current_turn()
            self._observe("response.cancelled", context=self._response_context or None, reason="provider_interrupted")
        model_turn, output = _field(content, "model_turn"), _field(content, "output_transcription")
        if not _from_connection and self._response_id is None and (model_turn or output or tool_call):
            await self._begin_response(self.observer.new_id("response"))
        transcription = _field(content, "input_transcription")
        if transcription:
            self._append_transcript("user", _field(transcription, "text"))
            self._capture("transcript.user.fragment", {"text": _field(transcription, "text"), "finished": bool(_field(transcription, "finished", False))}, context=self._input_context or None)
            if _field(transcription, "finished", False):
                self._flush_input_transcript()
        if output:
            self._append_transcript("assistant", _field(output, "text"))
        suppressed = self._response_id in self._suppressed_responses
        if model_turn:
            for part in _field(model_turn, "parts", []) or []:
                if not output and _field(part, "text") and not _field(part, "thought", False):
                    self._append_transcript("assistant", _field(part, "text"))
                audio = _field(_field(part, "inline_data"), "data")
                if isinstance(audio, bytes) and not suppressed:
                    rate, channels = self.response_audio_format
                    self._response_audio_ms += len(audio) * 1000 / (rate * channels * 2)
                    if not self._response_first_audio:
                        self._response_first_audio = True
                        self._observe("response.audio.first_queued", context=self._response_context, elapsed_ms=round((time.monotonic() - self._response_started_at) * 1000, 3))
        if tool_call and not suppressed:
            calls = [{"call_id": _field(call, "id"), "name": _field(call, "name", ""),
                      "arguments": json.dumps(_field(call, "args", {}), ensure_ascii=False)}
                     for call in (_field(tool_call, "function_calls", []) or [])[:64]]
            self._response_tool_count += len(calls)
            task = asyncio.create_task(self._complete_tools(calls, self._generation, context=dict(self._response_context)))
            self._tool_tasks.add(task)
            task.add_done_callback(self._tool_tasks.discard)
        if _field(message, "go_away"):
            self._observe("provider.go_away", reason="session_expiring")
        if not suppressed:
            parsed = types.LiveServerMessage.model_validate(message) if isinstance(message, dict) else message
            await self._event_handler_adapter.dispatch_event(TurnMessageEvent(parsed))
        if content and _field(content, "turn_complete", False):
            if self._response_id is None:
                if self._cancel_pending_response:
                    self._clear_pending_cancellation()
                elif self._context_update_pending:
                    self._context_response_finished()
                else:
                    await self._finish_response(None)
            elif not _from_connection:
                await self._finish_response(self._response_id)

    async def _finish_response(self, response_id) -> None:
        if response_id != self._response_id:
            return
        self._response_boundary_complete.set()
        if response_id in self._metadata_response_ids:
            self._observe("response.completed", context=self._response_context, response_id=response_id,
                          status="suppressed", reason="context_only", audio_ms=0, tool_count=0)
            await self._event_handler_adapter.dispatch_event(TurnEndEvent(response_id))
            self._response_id = None
            self._response_context = {}
            if self._context_update_pending:
                self._context_response_finished()
            return
        if response_id is None and self._pending_response_contexts:
            self._response_context = self._pending_response_contexts.popleft()
        origin = self._response_context.get("turn_id", self._observation_turn)
        if not self._pending_transcription_contexts or self._pending_transcription_contexts[0].get("turn_id") == origin:
            self._flush_input_transcript()
        status = "cancelled" if response_id in self._suppressed_responses else "completed"
        if self._output_text:
            self._capture("transcript.assistant", {"text": self._output_text + ("[TRUNCATED]" if self._output_truncated else "")},
                          context=self._response_context, status=status)
        self._observe("response.completed", context=self._response_context or None, response_id=response_id,
                      status=status, tool_count=self._response_tool_count, audio_ms=round(self._response_audio_ms, 3),
                      duration_ms=round((time.monotonic() - self._response_started_at) * 1000, 3))
        if response_id:
            await self._event_handler_adapter.dispatch_event(TurnEndEvent(response_id))
        if self._cancel_pending_response and response_id in self._suppressed_responses:
            self._clear_pending_cancellation()
        if not self._context_update_pending and (origin == self._observation_turn or (origin is None and status != "cancelled")):
            self._last_response_text = self._output_text
            self._last_response_status = status if origin is not None else "ambiguous"
            self._awaiting_response = False
            self._response_completed.set()
            if self._server_vad and self._activity_open and self._context_prepared:
                self._server_turn_needs_refresh = True
        self._response_id = None
        self._response_context = {}

    async def _complete_tools(self, calls: list[dict], generation: int, *, context: dict | None = None) -> None:
        context = dict(context or self.observation_context)
        selected = []
        for call in calls[:64]:
            cid = call.get("call_id")
            if isinstance(cid, str) and cid and cid not in self._seen_calls and cid not in self._cancelled_calls:
                self._seen_calls.append(cid)
                selected.append(call)
        if not selected:
            return
        self._tool_rounds += 1
        tasks = []
        # A match has ten champions. Keep bounded headroom for a context/name
        # lookup, and reject only overflow instead of discarding the whole batch.
        for index, call in enumerate(selected):
            allowed = self._tool_rounds <= 3 and index < 12
            task = asyncio.create_task(self._observed_tool(call, context)) if allowed else asyncio.create_task(self._budget_result(call, context))
            self._call_tasks[call["call_id"]] = task
            tasks.append(task)
        try:
            outputs = await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            for call in selected:
                self._call_tasks.pop(call["call_id"], None)
        if generation != self._generation or not self.is_connected():
            self._observe("tool.results.discarded", context=context, reason="stale_generation_or_disconnected")
            return
        pairs = [(call, output) for call, output in zip(selected, outputs)
                 if not isinstance(output, BaseException) and call["call_id"] not in self._cancelled_calls]
        if not pairs:
            return
        session = await self._get_active_session()
        if session is None or generation != self._generation:
            self._observe("tool.results.discarded", context=context, reason="stale_generation_or_disconnected")
            return
        try:
            await session.send_tool_response(function_responses=[types.FunctionResponse(
                id=call["call_id"], name=call["name"], response=json.loads(output)) for call, output in pairs])
        except Exception as exc:
            self._observe("tool.result.failed", context=context, reason="send_failed", error_code=type(exc).__name__)
            return
        for call, _ in pairs:
            self._observe("tool.result.submitted", context=context, call_id=call["call_id"], function_name=call["name"], status="ok")
        self._observe("response.continuation.requested", context=context, status="ok", reason="native_after_tool_response", tool_count=len(pairs))

    async def _budget_result(self, call: dict, context: dict) -> str:
        result = {"status": "unavailable", "reason": "tool_budget_exceeded"}
        try:
            arguments = json.loads(call.get("arguments", ""))
        except (ValueError, TypeError):
            arguments = {"invalid_json": True}
        self._capture("tool.started", {"name": call["name"], "arguments": arguments}, context=context,
                      call_id=call["call_id"], function_name=call["name"])
        self._capture("tool.completed", {"result": result}, context=context, call_id=call["call_id"], function_name=call["name"], **result, duration_ms=0)
        return encode(result)

    async def _execute_tool(self, name: str, arguments: str) -> str:
        if self._tools_enabled:
            return await self._tools.execute(name, arguments)
        return encode({"status": "unavailable", "reason": "tool_disabled"})
