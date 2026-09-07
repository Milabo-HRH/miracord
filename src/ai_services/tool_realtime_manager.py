"""Realtime audio, per-turn context and read-only tools for GPT and Grok."""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import time
from collections import OrderedDict, deque
from typing import Any

from src.ai_services.base_manager import BaseRealtimeManager
from src.ai_services.interface import ProviderCapabilities
from src.ai_services.web_search import WEB_SEARCH_INSTRUCTIONS, WEB_SEARCH_TOOL, WebSearchExecutor
from src.lol_mcp.context import CONTEXT_INSTRUCTIONS, GameContextService, encode
from src.lol_mcp.prompts import MAYHEM_TOOL_INSTRUCTIONS
from src.lol_mcp.tools import LEAGUE_TOOLS, LeagueToolExecutor
from src.utils.logger import get_logger
from src.observability import fingerprint, get_observer

logger = get_logger(__name__)


class ToolRealtimeManager(BaseRealtimeManager):
    """Share orchestration, not session schemas, between realtime providers."""

    connection_class: Any = None
    provider = ""

    def __init__(self, audio_playback_manager, service_config: dict) -> None:
        super().__init__(audio_playback_manager, service_config)
        self._connection_handler_inst = self.connection_class(
            self._api_key, self._model_name
        )
        self._session_config = copy.deepcopy(service_config.get("session_config", {}))
        self.observer = service_config.get("observer") or get_observer()
        self.observer.register_secrets(self._api_key)
        self._connection_handler_inst.observer = self.observer
        self._connection_handler_inst.observation_context = lambda: self.observation_context
        self._observation_session = self.observer.new_id("session")
        self._observation_turn: str | None = None
        self._turn_started_at = time.monotonic()
        self._turn_first_audio = False
        self._response_observations: OrderedDict[str, dict] = OrderedDict()
        self._input_observations: OrderedDict[str, dict] = OrderedDict()
        self._pending_input_observations: deque[dict] = deque(maxlen=256)
        self._pending_response_observations: deque[dict] = deque(maxlen=256)
        self._ambiguous_response_count = 0
        self._committed_input_items: deque[str] = deque(maxlen=256)
        self._last_input_turn_id: str | None = None
        self._last_context_text: str | None = None
        self._transcript_seen: deque[tuple] = deque(maxlen=512)
        if self.observer.capture_enabled:
            # Both providers document this nested configuration. ASR is diagnostic
            # guidance; the voice model still consumes the original audio directly.
            audio_input = self._session_config.setdefault("audio", {}).setdefault("input", {})
            if not audio_input.get("transcription"):
                audio_input["transcription"] = {
                    "model": "gpt-4o-mini-transcribe" if self.provider == "openai" else "grok-transcribe"
                }
        self._context = GameContextService(
            enabled=service_config.get("league_context_enabled", False),
            prefetch=service_config.get("opgg_prefetch_enabled", True),
        )
        self._tools = LeagueToolExecutor(self._context)
        self._tools_enabled = service_config.get("league_tools_enabled", False)
        self._web_search = None
        self._searches_this_turn = 0
        self._on_web_search_result = service_config.get("on_web_search_result")
        if self.provider == "openai" and service_config.get("web_search_enabled", False):
            self._web_search = WebSearchExecutor(
                self._api_key,
                model=service_config.get("web_search_model", "gpt-5.4-mini"),
                reasoning_effort=service_config.get("web_search_reasoning_effort", "none"),
                timeout=service_config.get("web_search_timeout", 15),
                cache_seconds=service_config.get("web_search_cache_seconds", 60),
                preferred_sources=service_config.get("preferred_search_sources", ()),
            )
            self._session_config.setdefault("tools", []).append(copy.deepcopy(WEB_SEARCH_TOOL))
        if self._tools_enabled:
            self._session_config.setdefault("tools", []).extend(
                copy.deepcopy(LEAGUE_TOOLS)
            )
        self._session_config["instructions"] = (
            self._session_config.get("instructions", "") + "\n" + CONTEXT_INSTRUCTIONS
        )
        if self._tools_enabled:
            self._session_config["instructions"] += "\n\n" + MAYHEM_TOOL_INSTRUCTIONS
        if self._web_search:
            self._session_config["instructions"] += "\n\n" + WEB_SEARCH_INSTRUCTIONS
        self._configured_vad = copy.deepcopy(
            self._vad_container().get("turn_detection")
        )
        self._server_vad = self._configured_vad is not None
        self._ready = asyncio.Event()
        self._accepted = False
        self._configuration_failed = False
        self._on_ready = None
        self._on_lost = None
        self._generation = 0
        self.connection_epoch = 0
        self._tool_tasks: set[asyncio.Task] = set()
        self._seen_calls: set[str] = set()
        self._pending_calls: dict[str, list[dict]] = {}
        self._tool_rounds = 0
        self._ignored_responses: deque[str] = deque(maxlen=256)
        self._response_id: str | None = None
        self._audio_item: str | None = None
        self._audio_received_ms = 0.0
        self._stream_started = False
        self._has_audio = False
        self._response_in_progress = False
        self._cancel_unidentified = False

    @property
    def observation_context(self) -> dict:
        return {"session_id": self._observation_session,
                "turn_id": self._observation_turn, "provider": self.provider,
                "connection_epoch": self.connection_epoch}

    def _observe(self, event: str, *, context: dict | None = None, **fields) -> None:
        self.observer.emit(event, **{**(context or self.observation_context), **fields})

    def _capture(self, event: str, payload, *, context: dict | None = None, **fields) -> None:
        self.observer.capture(event, payload, **{**(context or self.observation_context), **fields})

    @staticmethod
    def _remember(mapping: OrderedDict, key: str, value: dict) -> None:
        mapping[key] = value
        while len(mapping) > 256:
            mapping.popitem(last=False)

    def _vad_container(self) -> dict:
        if self.provider == "openai":
            return self._session_config.setdefault("audio", {}).setdefault("input", {})
        return self._session_config

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            native_web_search=bool(
                self._service_config.get("native_web_search", False)
            ),
            native_social_search=bool(
                self._service_config.get("native_social_search", False)
            ),
            cancel_response=True,
            manual_commit=True,
            realtime_audio_input=self._configured_vad is not None,
            server_vad=self._configured_vad is not None,
            turn_context=True,
        )

    @property
    def _connection_handler(self):
        return self._connection_handler_inst

    @property
    def _event_callback(self):
        return self._dispatch_event

    def is_connected(self) -> bool:
        return self._accepted and self._connection_handler.is_connected()

    async def connect(self, on_connect, on_disconnect) -> bool:
        if self.is_connected():
            return True
        self._on_ready, self._on_lost = on_connect, on_disconnect
        self._observe("session.connecting", model=self._model_name)
        self._ready.clear()
        self._configuration_failed = False
        self._context.start()
        await self._connection_handler.connect(
            self._dispatch_event, self._configure_socket, self._transport_lost
        )
        try:
            await asyncio.wait_for(
                self._ready.wait(), self._service_config.get("connection_timeout", 30)
            )
            if not self.is_connected():
                await self.disconnect()
                return False
            return True
        except asyncio.TimeoutError:
            self._observe("session.failed", status="timeout", reason="connection_timeout")
            await self.disconnect()
            return False

    async def _configure_socket(self) -> None:
        self.connection_epoch += 1
        self._accepted = False
        self._configuration_failed = False
        self._ready.clear()
        self._invalidate_work()
        self._searches_this_turn = 0
        self._seen_calls.clear()
        self._ignored_responses.clear()
        self._response_id = None
        self._audio_item = None
        self._audio_received_ms = 0.0
        self._stream_started = self._has_audio = False
        self._response_in_progress = False
        self._cancel_unidentified = False
        self._pending_input_observations.clear()
        self._pending_response_observations.clear()
        self._ambiguous_response_count = 0
        self._last_input_turn_id = None
        self._server_vad = self._configured_vad is not None
        self._vad_container()["turn_detection"] = copy.deepcopy(self._configured_vad)
        await self._post_connect_hook()

    async def _post_connect_hook(self) -> bool:
        self._capture("session.configured", {
            "instructions": self._session_config.get("instructions", ""),
            "tools": self._session_config.get("tools", []), "model": self._model_name,
            "session_config": self._session_config,
        }, model=self._model_name,
            prompt_hash=fingerprint(self._session_config.get("instructions", "")),
            tools_hash=fingerprint(self._session_config.get("tools", [])),
            capture_enabled=self.observer.capture_enabled)
        logger.info(
            "%s configured function tools: %s", self.provider,
            ", ".join(tool.get("name", "") for tool in self._session_config.get("tools", [])
                      if tool.get("type") == "function"),
        )
        await self._connection_handler.send_event(
            {"type": "session.update", "session": self._session_config}
        )
        return True

    async def _transport_lost(self) -> None:
        self._observe("session.disconnected", reason="transport_lost")
        self._accepted = False
        self._invalidate_work()
        terminal = getattr(self._connection_handler, "terminal_error_code", None)
        if terminal:
            self._configuration_failed = True
            self._ready.set()
            self._observe("session.failed", status="unavailable", reason="terminal_provider_error",
                          error_code=terminal)
        if self._on_lost:
            await self._on_lost()

    def _invalidate_work(self) -> None:
        self._generation += 1
        for task in tuple(self._tool_tasks):
            task.cancel()
        self._pending_calls.clear()

    async def disconnect(self) -> None:
        self._observe("session.disconnected", reason="requested")
        self._accepted = False
        self._invalidate_work()
        await asyncio.gather(*tuple(self._tool_tasks), return_exceptions=True)
        await self._connection_handler.disconnect()
        await self._context.close()
        if self._web_search:
            await self._web_search.close()

    async def _send(self, event: dict) -> bool:
        if not self.is_connected():
            self._observe("transport.send.failed", event_type=event.get("type"), reason="not_connected")
            return False
        try:
            await self._connection_handler.send_event(event)
            if event.get("type") == "input_audio_buffer.clear":
                self._pending_input_observations.clear()
            return True
        except Exception:  # noqa: BLE001 - transport boundary; do not log credentials
            self._observe("transport.send.failed", event_type=event.get("type"), reason="transport_error")
            logger.warning(
                "%s realtime send failed (%s).", self.provider, event.get("type")
            )
            return False

    async def send_turn_context(
        self, user_id: int, display_name: str, *, streaming: bool = False
    ) -> bool:
        self._observation_turn = self.observer.new_id("turn")
        self._turn_started_at = time.monotonic()
        self._turn_first_audio = False
        self.observer.register_secrets(str(user_id), display_name)
        self._observe("turn.context.started", speaker_id=self.observer.pseudonym(user_id), streaming=streaming)
        self._invalidate_work()
        self._tool_rounds = 0
        self._searches_this_turn = 0
        if self._response_id:
            self._ignored_responses.append(self._response_id)
        self._response_id = self._audio_item = None
        self._audio_received_ms = 0.0
        self._response_in_progress = False
        if self._has_audio:
            if not await self._send({"type": "input_audio_buffer.clear"}):
                return False
            self._has_audio = False
        use_vad = streaming and self._configured_vad is not None
        if use_vad != self._server_vad:
            value = copy.deepcopy(self._configured_vad) if use_vad else None
            update = (
                {"type": "realtime", "audio": {"input": {"turn_detection": value}}}
                if self.provider == "openai"
                else {"turn_detection": value}
            )
            if not await self._send({"type": "session.update", "session": update}):
                return False
            self._server_vad = use_vad
        text = self._context.turn_context(user_id, display_name)
        sent = await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                },
            }
        )
        self._capture("turn.context.sent" if sent else "turn.context.failed", {"text": text},
                      status="ok" if sent else "failed", reason="ordered_transport_send",
                      speaker_id=self.observer.pseudonym(user_id),
                      duration_ms=round((time.monotonic() - self._turn_started_at) * 1000, 3))
        if sent:
            self._last_context_text = text
        return sent

    async def send_speaker_marker(self, user_id: int, display_name: str) -> bool:
        return await self.send_turn_context(user_id, display_name)

    async def send_audio_chunk(self, audio_data: bytes) -> bool:
        sent = await self._send(
            {
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(audio_data).decode("ascii"),
            }
        )
        self._has_audio = self._has_audio or sent
        if sent and not self._turn_first_audio:
            self._turn_first_audio = True
            self._pending_input_observations.append(dict(self.observation_context))
            self._observe("turn.audio.first_sent", audio_bytes=len(audio_data),
                          elapsed_ms=round((time.monotonic() - self._turn_started_at) * 1000, 3))
        return sent

    async def finalize_input_and_request_response(self) -> bool:
        if not self._has_audio:
            return True
        if self._server_vad:
            # Flush a PTT tail through VAD; never commit/create twice.
            duration = (self._configured_vad or {}).get(
                "silence_duration_ms", 600
            ) + 200
            rate, channels = self.processing_audio_format
            sent = await self.send_audio_chunk(
                b"\x00" * (rate * channels * 2 * duration // 1000)
            )
        else:
            sent = await self._send({"type": "input_audio_buffer.commit"})
            if sent:
                sent = await self._send({"type": "response.create"})
                self._response_in_progress = sent
        self._has_audio = False
        self._observe("turn.input.finalized", status="ok" if sent else "failed", streaming=self._server_vad)
        return sent

    async def cancel_ongoing_response(self) -> bool:
        self._observe("response.cancelled", response_id=self._response_id, reason="interruption")
        self._pending_response_observations.clear()
        self._ambiguous_response_count = 0
        self._invalidate_work()
        response_id = self._response_id
        if response_id:
            self._ignored_responses.append(response_id)
        sent = True
        if self._response_in_progress:
            event = {"type": "response.cancel"}
            if response_id:
                event["response_id"] = response_id
            else:
                self._cancel_unidentified = True
            sent = await self._send(event)
        if self._has_audio:
            sent = await self._send({"type": "input_audio_buffer.clear"}) and sent
        if self._stream_started:
            await self._audio_playback_manager.end_audio_stream()
        if self.provider == "openai" and self._audio_item and response_id:
            played = self._audio_playback_manager.get_played_ms(response_id)
            played = max(0, min(int(played), int(self._audio_received_ms)))
            sent = (
                await self._send(
                    {
                        "type": "conversation.item.truncate",
                        "item_id": self._audio_item,
                        "content_index": 0,
                        "audio_end_ms": played,
                    }
                )
                and sent
            )
            self._audio_item = None
        self._stream_started = self._has_audio = self._response_in_progress = False
        return sent

    async def _dispatch_event(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "session.updated":
            if not self._accepted and not self._configuration_failed:
                self._accepted = True
                self._ready.set()
                accepted = event.get("session") or {}
                self._observe("session.ready", status="ok", model=accepted.get("model", self._model_name))
                logger.info(
                    "%s realtime session accepted: model=%s reasoning=%s.",
                    self.provider, accepted.get("model", self._model_name),
                    (accepted.get("reasoning") or {}).get("effort", "default"),
                )
                if self._on_ready:
                    await self._on_ready()
            return
        if kind == "error":
            self._observe("provider.error", error_code=(event.get("error") or {}).get("code", "unknown"))
            logger.warning(
                "%s realtime error code: %s",
                self.provider,
                (event.get("error") or {}).get("code", "unknown"),
            )
            if not self._accepted:
                self._configuration_failed = True
                self._ready.set()
            return
        if kind in {"input_audio_buffer.speech_started", "input_audio_buffer.speech_stopped",
                    "input_audio_buffer.committed"}:
            item_id = event.get("item_id")
            context = self._input_observations.get(item_id)
            if context is None:
                if self._pending_input_observations:
                    context = self._pending_input_observations.popleft()
                else:
                    # One prepared Discord speaker can generate many server-VAD
                    # messages. Each message is a replayable interaction turn.
                    if self._last_input_turn_id == self._observation_turn:
                        self._observation_turn = self.observer.new_id("turn")
                        self._turn_started_at = time.monotonic()
                        if self._last_context_text is not None:
                            self._capture("turn.context.reused", {"text": self._last_context_text},
                                          reason="server_vad_segment", status="ok")
                    context = dict(self.observation_context)
                self._last_input_turn_id = context.get("turn_id")
                if item_id:
                    self._remember(self._input_observations, item_id, context)
            if kind == "input_audio_buffer.committed" and item_id not in self._committed_input_items:
                self._committed_input_items.append(item_id)
                self._pending_response_observations.append(context)
            self._observe("input." + kind.rsplit(".", 1)[1], context=context, item_id=item_id)
            logger.info("%s realtime input event: %s", self.provider, kind)
            return
        if kind == "conversation.item.input_audio_transcription.completed":
            item_id = event.get("item_id")
            context = self._input_observations.get(item_id)
            self._record_transcript("user", event.get("transcript", ""), item_id=item_id,
                                    context=context or {**self.observation_context, "turn_id": None},
                                    correlation="item" if context else "unmapped")
            return
        if kind == "conversation.item.input_audio_transcription.failed":
            self._observe("transcript.failed", item_id=event.get("item_id"),
                          error_code=(event.get("error") or {}).get("code", "unknown"))
            return
        response = event.get("response") or {}
        rid = event.get("response_id") or response.get("id") or self._response_id
        context = self._response_observations.get(rid, self.observation_context)
        if kind in {"response.output_audio_transcript.done", "response.audio_transcript.done",
                    "response.output_text.done", "response.text.done"}:
            self._record_transcript("assistant", event.get("transcript", event.get("text", "")),
                                    context=context, response_id=rid, item_id=event.get("item_id"))
            return
        if rid and rid in self._ignored_responses:
            return
        if kind == "response.created":
            if self._cancel_unidentified:
                self._cancel_unidentified = False
                if rid:
                    self._ignored_responses.append(rid)
                    await self._send({"type": "response.cancel", "response_id": rid})
                return
            self._response_id = rid
            self._audio_item = None
            self._audio_received_ms = 0.0
            self._stream_started = False
            self._response_in_progress = True
            if len(self._pending_response_observations) > 1 or self._ambiguous_response_count:
                # Automatic VAD response creation can race a tool continuation.
                # Providers do not expose the originating input item on created;
                # never silently assign an uncertain response to a speaker.
                self._ambiguous_response_count += len(self._pending_response_observations)
                self._pending_response_observations.clear()
                self._ambiguous_response_count = max(0, self._ambiguous_response_count - 1)
                response_context = {**self.observation_context, "turn_id": None,
                                    "correlation": "ambiguous"}
            else:
                response_context = (self._pending_response_observations.popleft()
                                    if self._pending_response_observations else self.observation_context)
            context = {**response_context, "response_id": rid,
                       "_started_at": time.monotonic()}
            if rid:
                self._remember(self._response_observations, rid, context)
            self._observe("response.started", context=context)
            logger.info("%s realtime response started: %s", self.provider, rid)
        elif kind in {"response.output_audio.delta", "response.audio.delta"}:
            if not rid:
                return
            try:
                audio = base64.b64decode(event.get("delta", ""), validate=True)
            except (ValueError, TypeError):
                return
            if not audio:
                return
            self._response_id = rid
            self._audio_item = event.get("item_id", self._audio_item)
            rate, channels = self.response_audio_format
            self._audio_received_ms += len(audio) * 1000 / (rate * channels * 2)
            if not self._stream_started:
                await self._audio_playback_manager.start_new_audio_stream(
                    rid, self.response_audio_format
                )
                self._stream_started = True
                self._observe("response.audio.first_queued", context=context, response_id=rid,
                              elapsed_ms=round((time.monotonic() - context.get("_started_at", time.monotonic())) * 1000, 3))
            await self._audio_playback_manager.add_audio_chunk(audio)
        elif kind in {"response.output_audio.done", "response.audio.done"}:
            if self._stream_started:
                await self._audio_playback_manager.end_audio_stream()
                self._stream_started = False
        elif kind == "response.function_call_arguments.done":
            pending = self._pending_calls.setdefault(rid or "", [])
            if len(pending) < 8:
                pending.append(event)
        elif kind == "response.done":
            details = response.get("status_details") or {}
            logger.info(
                "%s realtime response finished: %s status=%s reason=%s error_code=%s audio_ms=%d",
                self.provider, rid, response.get("status"), details.get("reason"),
                (details.get("error") or {}).get("code"), self._audio_received_ms,
            )
            if rid:
                self._ignored_responses.append(rid)
            self._response_in_progress = False
            if self._stream_started:
                await self._audio_playback_manager.end_audio_stream()
                self._stream_started = False
            calls = [
                item
                for item in response.get("output", [])
                if item.get("type") == "function_call"
            ]
            pending = self._pending_calls.pop(rid or "", [])
            if not calls:
                calls = pending
            for item in response.get("output", []):
                if item.get("type") == "message" and item.get("role", "assistant") == "assistant":
                    for part in item.get("content", []):
                        text = part.get("transcript") or part.get("text")
                        if isinstance(text, str):
                            self._record_transcript("assistant", text, context=context,
                                                    response_id=rid, item_id=item.get("id"))
            self._observe("response.completed", context=context, response_id=rid,
                          status=response.get("status", "completed"), reason=details.get("reason"),
                          error_code=(details.get("error") or {}).get("code"), tool_count=len(calls),
                          audio_ms=round(self._audio_received_ms, 3),
                          duration_ms=round((time.monotonic() - context.get("_started_at", time.monotonic())) * 1000, 3))
            if calls and response.get("status", "completed") == "completed":
                task = asyncio.create_task(
                    self._complete_tools(calls, self._generation, context=context)
                )
                self._tool_tasks.add(task)
                task.add_done_callback(self._tool_tasks.discard)

    def _record_transcript(self, role: str, text: str, **fields) -> None:
        if not isinstance(text, str) or not text:
            return
        key = (role, fields.get("response_id"), fields.get("item_id"), fingerprint(text))
        if key in self._transcript_seen:
            return
        self._transcript_seen.append(key)
        self._capture("transcript." + role, {"text": text}, **fields)

    async def _complete_tools(self, calls: list[dict], generation: int, *, context: dict | None = None) -> None:
        context = dict(context or self.observation_context)
        selected = []
        for call in calls:
            cid = call.get("call_id")
            if isinstance(cid, str) and cid not in self._seen_calls:
                self._seen_calls.add(cid)
                selected.append(call)
        if not selected:
            return
        self._tool_rounds += 1
        allowed = self._tool_rounds <= 3 and len(selected) <= 8
        if allowed:
            outputs = await asyncio.gather(
                *(
                    self._observed_tool(c, context)
                    for c in selected
                )
            )
        else:
            outputs = [
                encode({"status": "unavailable", "reason": "tool_budget_or_disabled"})
            ] * len(selected)
            for call, output in zip(selected, outputs):
                self._capture("tool.completed", {"result": json.loads(output)}, context=context,
                              function_name=call.get("name"), call_id=call.get("call_id"),
                              status="unavailable", reason="tool_budget_or_disabled", duration_ms=0)
        if generation != self._generation or not self.is_connected():
            self._observe("tool.results.discarded", context=context, reason="stale_generation_or_disconnected")
            return
        for call, output in zip(selected, outputs):
            if generation != self._generation:
                self._observe("tool.results.discarded", context=context, reason="stale_generation")
                return
            if not await self._send(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "function_call_output",
                        "call_id": call["call_id"],
                        "output": output,
                    },
                }
            ):
                self._observe("tool.result.failed", context=context, call_id=call.get("call_id"), reason="send_failed")
                return
            self._observe("tool.result.submitted", context=context, call_id=call.get("call_id"),
                          function_name=call.get("name"), status="ok")
        # All parallel results precede one continuation. Bound tool loops.
        if generation == self._generation and self._tool_rounds <= 3:
            continuation_context = dict(context)
            self._pending_response_observations.append(continuation_context)
            self._response_in_progress = await self._send({"type": "response.create"})
            if not self._response_in_progress:
                try:
                    self._pending_response_observations.remove(continuation_context)
                except ValueError:
                    pass
            self._observe("response.continuation.requested", context=context,
                          status="ok" if self._response_in_progress else "failed", tool_count=len(selected))
        # Publish citations after starting the voice continuation, so text-channel
        # latency does not delay the first spoken answer. The task remains cancellable.
        for call, output in zip(selected, outputs):
            if generation != self._generation or not self.is_connected():
                return
            if call.get("name") == "search_web" and self._on_web_search_result:
                result = json.loads(output)
                if result.get("status") == "ok":
                    try:
                        async with asyncio.timeout(3):
                            await self._on_web_search_result(result)
                    except Exception:
                        self._observe("tool.citations.failed", context=context, call_id=call.get("call_id"), reason="publish_failed")
                        logger.warning("Could not publish web-search citations to the text channel.")

    async def _observed_tool(self, call: dict, context: dict) -> str:
        name, arguments = call.get("name", ""), call.get("arguments", "")
        metadata = {"context": context, "function_name": name, "call_id": call.get("call_id")}
        try:
            parsed_args = json.loads(arguments)
        except (ValueError, TypeError):
            parsed_args = {"invalid_json": True}
        self._capture("tool.started", {"name": name, "arguments": parsed_args}, **metadata)
        started = time.monotonic()
        try:
            output = await self._execute_tool(name, arguments)
            result = json.loads(output)
            if not isinstance(result, dict):
                raise ValueError("Tool result must be an object")
        except asyncio.CancelledError:
            self._observe("tool.cancelled", **metadata, status="cancelled", reason="interruption_or_reconnect",
                          duration_ms=round((time.monotonic() - started) * 1000, 3))
            raise
        except TimeoutError:
            result = {"status": "unavailable", "reason": "tool_timeout"}
            output = encode(result)
        except Exception:
            result = {"status": "unavailable", "reason": "tool_execution_error"}
            output = encode(result)
        cache = result.get("cache") if isinstance(result.get("cache"), dict) else {}
        self._capture("tool.completed", {"result": result}, **metadata,
                      status=result.get("status", "unknown"), reason=result.get("reason") or cache.get("refreshError"),
                      duration_ms=round((time.monotonic() - started) * 1000, 3),
                      cache_hit=result.get("cacheHit", cache.get("hit", cache.get("cacheHit"))),
                      stale=result.get("stale", cache.get("stale")))
        return output

    async def _execute_tool(self, name: str, arguments: str) -> str:
        if name == "search_web":
            if not self._web_search:
                return encode({"status": "unavailable", "reason": "search_disabled"})
            if self._searches_this_turn >= 1:
                return encode({"status": "unavailable", "reason": "search_budget_exceeded"})
            self._searches_this_turn += 1
            return await self._web_search.execute(arguments)
        if self._tools_enabled:
            return await self._tools.execute(name, arguments)
        return encode({"status": "unavailable", "reason": "tool_disabled"})
