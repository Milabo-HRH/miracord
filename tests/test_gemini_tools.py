"""Offline Gemini tool, capture, cancellation and snapshot contracts."""

import asyncio
import copy
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from google.genai import types

from src.ai_services.providers.gemini.manager import GeminiRealtimeManager
from src.ai_services.providers.gemini.event_handler import TurnStartEvent, TurnEndEvent
from src.observability import JsonlObserver, fingerprint
from src.lol_mcp.tools import LEAGUE_TOOLS


@pytest.fixture
def manager(tmp_path):
    observer = JsonlObserver(tmp_path / "trace.jsonl", capture_enabled=True)
    playback = MagicMock()
    playback.start_new_audio_stream = AsyncMock()
    playback.end_audio_stream = AsyncMock()
    playback.add_audio_chunk = AsyncMock()
    instance = GeminiRealtimeManager(playback, {
        "api_key": "private-provider-key", "model_name": "gemini-3.1-flash-live-preview",
        "live_connect_config": {"response_modalities": ["AUDIO"], "system_instruction": "Short answers.",
                                "realtime_input_config": {"automatic_activity_detection": {"disabled": True}}},
        "league_tools_enabled": True, "league_context_enabled": False,
        "processing_audio_frame_rate": 16000, "processing_audio_channels": 1,
        "response_audio_frame_rate": 24000, "response_audio_channels": 1,
        "observer": observer, "gemini_client": MagicMock(),
    })
    instance._accepted = True
    session = MagicMock()
    session.send_realtime_input = AsyncMock()
    session.send_tool_response = AsyncMock()
    instance._connection_handler_inst.is_connected = lambda: True
    instance._connection_handler_inst.get_active_session = lambda: session
    instance._connection_handler_inst.disconnect = AsyncMock()
    yield instance
    observer.close()


def rows(manager):
    assert manager.observer.flush()
    return [json.loads(line) for line in manager.observer.path.read_text(encoding="utf-8").splitlines()]


def tool_message(*ids):
    return {"tool_call": {"function_calls": [
        {"id": cid, "name": "get_mayhem_champion_tier", "args": {"champion": "ezreal"}} for cid in ids
    ]}}


async def tools_done(manager):
    await asyncio.gather(*tuple(manager._tool_tasks), return_exceptions=True)


def test_native_schema_contains_local_tools_transcription_and_manual_activity(manager):
    config = manager._session_config
    parsed = types.LiveConnectConfig(**config)
    names = [tool["name"] for tool in config["tools"][0]["function_declarations"]]
    assert names == [tool["name"] for tool in LEAGUE_TOOLS]
    assert parsed.realtime_input_config.automatic_activity_detection.disabled
    assert config["input_audio_transcription"] == config["output_audio_transcription"] == {}
    assert not any("google_search" in tool for tool in config["tools"])
    assert "Never claim you searched the web" in config["system_instruction"]["parts"][0]["text"]
    native = parsed.model_dump_json(exclude_none=True)
    for rejected in ("additional_properties", "additionalProperties", "max_length", "max_items"):
        assert rejected not in native
    tier_tool = next(tool for tool in config["tools"][0]["function_declarations"] if tool["name"] == "get_mayhem_champion_tier")
    champion = tier_tool["parameters"]["properties"]["champion"]
    assert "Maximum characters: 40" in champion["description"]


@pytest.mark.asyncio
async def test_native_schema_relaxation_does_not_relax_local_argument_validation(manager):
    output = await manager._execute_tool("get_mayhem_champion_tier", '{"champion":"ezreal","url":"https://invalid.test"}')
    assert json.loads(output)["status"] == "invalid_arguments"
    output = await manager._execute_tool("get_mayhem_champion_tier", json.dumps({"champion": "x" * 41}))
    assert json.loads(output)["status"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_parallel_tools_submit_native_results_once_without_extra_text(manager):
    await manager.send_text_turn("查英雄评级", context_text="Shared context")
    session = await manager._get_active_session()
    session.send_realtime_input.reset_mock()
    manager._tools.execute = AsyncMock(return_value='{"status":"ok","championTier":"T2","cache":{"hit":true,"stale":false}}')
    message = tool_message("c1", "c2")
    await manager._dispatch_message(message)
    await tools_done(manager)
    await manager._dispatch_message(message)
    await tools_done(manager)
    session.send_tool_response.assert_awaited_once()
    results = session.send_tool_response.await_args.kwargs["function_responses"]
    assert [result.id for result in results] == ["c1", "c2"]
    assert all(result.response["championTier"] == "T2" for result in results)
    assert manager._tools.execute.await_count == 2
    session.send_realtime_input.assert_not_awaited()
    telemetry = rows(manager)
    completed = [row for row in telemetry if row["event"] == "tool.completed"]
    assert len(completed) == 2 and all(row["cache_hit"] for row in completed)
    submitted = [row["sequence"] for row in telemetry if row["event"] == "tool.result.submitted"]
    continued = next(row["sequence"] for row in telemetry if row["event"] == "response.continuation.requested")
    assert max(submitted) < continued


@pytest.mark.asyncio
async def test_fragmented_transcripts_and_audio_are_aggregated_at_turn_end(manager):
    await manager.send_turn_context(123456789012345678, "Private Name")
    await manager.send_audio_chunk(b"pcm")
    await manager.finalize_input_and_request_response()
    await manager._dispatch_message({"server_content": {"input_transcription": {"text": "查一下"}}})
    await manager._dispatch_message({"server_content": {"input_transcription": {"text": "英雄评级", "finished": True},
        "output_transcription": {"text": "当前"}, "model_turn": {"parts": [{"inline_data": {"mime_type": "audio/pcm", "data": b"reply"}}]}}})
    await manager._dispatch_message({"server_content": {"output_transcription": {"text": "是T2"}, "turn_complete": True}})
    assert manager._last_input_text == "查一下英雄评级"
    assert manager._last_response_text == "当前是T2"
    assert manager._last_response_status == "completed" and manager._response_completed.is_set()
    manager._audio_playback_manager.add_audio_chunk.assert_awaited_once_with(b"reply")
    telemetry = rows(manager)
    fragments = [row for row in telemetry if row["event"] == "transcript.user.fragment"]
    assert [row["payload"]["text"] for row in fragments] == ["查一下", "英雄评级"]
    transcripts = [row for row in telemetry if row["event"] in {"transcript.user", "transcript.assistant"}]
    assert [row["payload"]["text"] for row in transcripts] == ["查一下英雄评级", "当前是T2"]
    raw = manager.observer.path.read_text(encoding="utf-8")
    assert "Private Name" not in raw and "123456789012345678" not in raw and '"reply"' not in raw


@pytest.mark.asyncio
async def test_cancelled_tools_and_late_audio_never_reach_provider_or_playback(manager):
    await manager.send_text_turn("Question")
    entered = asyncio.Event()

    async def slow(*args):
        entered.set()
        await asyncio.Event().wait()

    manager._tools.execute = slow
    await manager._dispatch_message(tool_message("c1"))
    await entered.wait()
    response_id = manager._response_id
    assert await manager.cancel_ongoing_response()
    await tools_done(manager)
    await manager._dispatch_message({"server_content": {"model_turn": {"parts": [{"inline_data": {"mime_type": "audio/pcm", "data": b"late"}}]}}})
    session = await manager._get_active_session()
    session.send_tool_response.assert_not_awaited()
    manager._audio_playback_manager.add_audio_chunk.assert_not_awaited()
    await manager._dispatch_event(TurnEndEvent(response_id))
    await manager.send_text_turn("New question")
    await manager._dispatch_message({"server_content": {"model_turn": {"parts": [{"inline_data": {"mime_type": "audio/pcm", "data": b"new"}}]}}})
    manager._audio_playback_manager.add_audio_chunk.assert_awaited_once_with(b"new")


@pytest.mark.asyncio
async def test_cancel_before_response_start_suppresses_late_unidentified_reply(manager):
    await manager.send_text_turn("Question")
    await manager.cancel_ongoing_response()
    await manager._dispatch_message({"server_content": {"model_turn": {"parts": [{"inline_data": {"mime_type": "audio/pcm", "data": b"late"}}]}, "turn_complete": True}})
    manager._audio_playback_manager.add_audio_chunk.assert_not_awaited()
    assert manager._last_response_status == "cancelled"
    assert not manager._needs_reconnect
    manager._connection_handler.disconnect.assert_not_awaited()
    await manager.send_text_turn("Fresh question")
    await manager._dispatch_message({"server_content": {"model_turn": {"parts": [{"inline_data": {"mime_type": "audio/pcm", "data": b"fresh"}}]}, "turn_complete": True}})
    manager._audio_playback_manager.add_audio_chunk.assert_awaited_once_with(b"fresh")
    assert manager._last_response_status == "completed"


@pytest.mark.asyncio
async def test_unidentified_handoff_preserves_connection_and_accepts_answer_after_interrupted(manager):
    await manager.send_text_turn("Old question")
    await manager.cancel_ongoing_response()
    await manager.send_turn_context(43, "Bob")
    new_turn = manager.observation_context["turn_id"]
    generation = manager._generation
    await manager._dispatch_message({"server_content": {"interrupted": True}})
    assert manager._generation == generation
    assert not manager._cancel_pending_response
    await manager.send_audio_chunk(b"new pcm")
    await manager.finalize_input_and_request_response()
    await manager._dispatch_message({"server_content": {"model_turn": {"parts": [{"inline_data": {"mime_type": "audio/pcm", "data": b"fresh"}}]}, "turn_complete": True}})
    manager._audio_playback_manager.add_audio_chunk.assert_awaited_once_with(b"fresh")
    manager._connection_handler.disconnect.assert_not_awaited()
    completed = [row for row in rows(manager) if row["event"] == "response.completed"]
    assert completed[-1]["turn_id"] == new_turn


@pytest.mark.asyncio
async def test_provider_cancellation_drops_only_selected_parallel_tool(manager):
    entered = asyncio.Event()

    async def execute(name, arguments):
        entered.set()
        await asyncio.Event().wait()

    manager._tools.execute = execute
    await manager._dispatch_message(tool_message("c1"))
    await entered.wait()
    await manager._dispatch_message({"tool_call_cancellation": {"ids": ["c1"]}})
    await tools_done(manager)
    (await manager._get_active_session()).send_tool_response.assert_not_awaited()
    assert any(row["event"] == "tool.cancelled" and row["reason"] == "provider_cancelled" for row in rows(manager))


@pytest.mark.asyncio
async def test_tool_budget_and_unknown_routes_fail_closed(manager):
    manager._tools.execute = AsyncMock(return_value='{"status":"ok"}')
    await manager._dispatch_message(tool_message(*[f"c{i}" for i in range(13)]))
    await tools_done(manager)
    assert manager._tools.execute.await_count == 12
    responses = (await manager._get_active_session()).send_tool_response.await_args.kwargs["function_responses"]
    assert len(responses) == 13
    assert all(response.response["status"] == "ok" for response in responses[:12])
    assert responses[-1].response["reason"] == "tool_budget_exceeded"
    manager._tools.execute.reset_mock()
    manager._tool_rounds = 3
    await manager._dispatch_message(tool_message("fourth-round"))
    await tools_done(manager)
    manager._tools.execute.assert_not_awaited()
    responses = (await manager._get_active_session()).send_tool_response.await_args.kwargs["function_responses"]
    assert responses[0].response["reason"] == "tool_budget_exceeded"
    manager._tools_enabled = False
    assert json.loads(await manager._execute_tool("search_web", "{}"))["reason"] == "tool_disabled"


@pytest.mark.asyncio
async def test_full_match_tiers_reach_provider_with_each_champion_identity(manager):
    champions = ["draven", "katarina", "shyvana", "tristana", "twistedfate",
                 "senna", "fiora", "diana", "aurora", "karthus"]
    async def lookup(name, arguments):
        assert name == "get_mayhem_champion_tier"
        return json.dumps({"status": "ok", "champion": json.loads(arguments)["champion"], "tierLabel": "T3"})
    manager._tools.execute = AsyncMock(side_effect=lookup)
    await manager.send_text_turn("Query all ten champion tiers")
    message = tool_message(*champions)
    for call, champion in zip(message["tool_call"]["function_calls"], champions):
        call["args"] = {"champion": champion}
    await manager._dispatch_message(message)
    await tools_done(manager)
    session = await manager._get_active_session()
    session.send_tool_response.assert_awaited_once()
    responses = session.send_tool_response.await_args.kwargs["function_responses"]
    assert [(r.id, r.response["champion"]) for r in responses] == [(c, c) for c in champions]
    assert all(r.response["status"] == "ok" for r in responses)
    assert manager._tools.execute.await_count == 10


@pytest.mark.asyncio
async def test_frozen_model_and_config_snapshot_are_applied_before_connect(manager):
    manager._accepted = False
    snapshot = {"model": "gemini-review-model", "session_config": copy.deepcopy(manager._session_config)}
    snapshot["session_config"]["system_instruction"] = "Reviewed prompt"
    manager.apply_replay_config(snapshot)
    assert manager._model_name == manager._connection_handler.model_name == "gemini-review-model"
    assert manager._session_config is manager._connection_handler.live_connect_config_params
    await manager._post_connect_hook()
    row = next(row for row in rows(manager) if row["event"] == "session.configured")
    assert row["payload"]["model"] == "gemini-review-model"
    assert row["prompt_hash"] == fingerprint("Reviewed prompt")


@pytest.mark.asyncio
async def test_terminal_connection_failure_wakes_setup_and_response_waiters(manager):
    manager._connection_handler._terminal_error_code = "resource_exhausted"
    await manager._transport_lost()
    assert manager._ready.is_set() and manager._response_completed.is_set()
    assert manager._last_response_status == "failed"


@pytest.mark.asyncio
async def test_late_old_transcript_never_claims_new_speaker_and_old_response_does_not_finish_new_turn(manager):
    await manager.send_turn_context(42, "Alice")
    await manager.send_audio_chunk(b"pcm")
    await manager.finalize_input_and_request_response()
    old_turn = manager.observation_context["turn_id"]
    await manager._dispatch_message({"server_content": {"input_transcription": {"text": "old "}}})
    await manager._dispatch_event(TurnStartEvent("old_response"))
    await manager.send_turn_context(43, "Bob")
    new_turn = manager.observation_context["turn_id"]
    await manager._dispatch_message({"server_content": {"input_transcription": {"text": "question", "finished": True}}})
    transcript = next(row for row in rows(manager) if row["event"] == "transcript.user")
    assert transcript["turn_id"] == old_turn and transcript["payload"]["text"] == "old question"
    await manager.send_audio_chunk(b"new pcm")
    await manager.finalize_input_and_request_response()
    await manager._dispatch_event(TurnEndEvent("old_response"))
    assert not manager._response_completed.is_set()
    await manager._dispatch_message({"server_content": {"output_transcription": {"text": "New answer"}, "turn_complete": True}})
    answer = next(row for row in rows(manager) if row["event"] == "transcript.assistant")
    assert answer["turn_id"] == new_turn
    assert manager._response_completed.is_set() and manager._last_response_text == "New answer"


@pytest.mark.asyncio
async def test_server_vad_same_floor_responses_have_separate_turns_without_reopening_context(manager):
    manager._session_config["realtime_input_config"]["automatic_activity_detection"] = {"disabled": False}
    session = await manager._get_active_session()

    async def acknowledge(**kwargs):
        if "text" in kwargs:
            await manager._dispatch_message({"server_content": {"turn_complete": True}})

    session.send_realtime_input.side_effect = acknowledge
    assert await manager.send_turn_context(42, "Alice")
    await manager.send_audio_chunk(b"first")
    first = manager.observation_context["turn_id"]
    await manager._dispatch_message({"server_content": {"input_transcription": {"text": "Question one", "finished": True}}})
    await manager._dispatch_message({"server_content": {"output_transcription": {"text": "Answer one"}, "turn_complete": True}})
    await manager.send_audio_chunk(b"second")
    await manager._dispatch_message({"server_content": {"input_transcription": {"text": "Question two", "finished": True}}})
    second = manager.observation_context["turn_id"]
    await manager._dispatch_message({"server_content": {"output_transcription": {"text": "Answer two"}, "turn_complete": True}})
    assert first != second
    answers = [row for row in rows(manager) if row["event"] == "transcript.assistant"]
    assert [row["turn_id"] for row in answers] == [first, second]
    assert manager._context_prepared and manager._activity_open
    assert [next(iter(call.kwargs)) for call in session.send_realtime_input.await_args_list] == ["text", "audio", "audio"]


@pytest.mark.asyncio
async def test_metadata_tool_request_is_rejected_without_local_execution(manager):
    manager._session_config["realtime_input_config"]["automatic_activity_detection"] = {"disabled": False}
    manager._tools.execute = AsyncMock()
    preparing = asyncio.create_task(manager.send_turn_context(42, "Alice"))
    await asyncio.sleep(0)
    await manager._dispatch_message(tool_message("metadata_call"))
    manager._tools.execute.assert_not_awaited()
    session = await manager._get_active_session()
    response = session.send_tool_response.await_args.kwargs["function_responses"][0]
    assert response.response["reason"] == "context_only_not_user_request"
    assert preparing.done() and manager._context_prepared
    await manager._dispatch_message({"server_content": {"turn_complete": True}})
    assert await preparing
