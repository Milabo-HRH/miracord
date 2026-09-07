"""Offline observability contracts: privacy, capture, retention and turn identity."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from src.ai_services.providers.grok.manager import GrokRealtimeManager
from src.ai_services.providers.openai.manager import OpenAIRealtimeManager
from src.observability import JsonlObserver, fingerprint
from src.ai_services.realtime_connection import safe_connection_failure


@pytest.fixture
def observer(tmp_path):
    # Full tools appear twice in session.configured; current schema exceeds 32 KB.
    # Retention/truncation behavior is tested separately with an explicit small cap.
    instance = JsonlObserver(tmp_path / "interactions.jsonl", capture_enabled=True,
                            capture_chars=131072)
    yield instance
    instance.close()


def records(observer):
    assert observer.flush()
    return [json.loads(line) for line in observer.path.read_text(encoding="utf-8").splitlines()]


def make_manager(observer, cls=OpenAIRealtimeManager):
    playback = MagicMock()
    playback.start_new_audio_stream = AsyncMock()
    playback.end_audio_stream = AsyncMock()
    playback.add_audio_chunk = AsyncMock()
    manager = cls(playback, {
        "api_key": "private-provider-key", "model_name": "offline-model",
        "session_config": {"instructions": "Short grounded game answers."},
        "league_context_enabled": False, "league_tools_enabled": True,
        "processing_audio_frame_rate": 24000, "processing_audio_channels": 1,
        "response_audio_frame_rate": 24000, "response_audio_channels": 1,
        "observer": observer,
    })
    manager._accepted = True
    manager._connection_handler_inst.is_connected = lambda: True
    manager._connection_handler_inst.send_event = AsyncMock()
    return manager


def function_call(cid="call1", name="get_mayhem_build", args='{"champion":"ezreal"}'):
    return {"type": "function_call", "call_id": cid, "name": name, "arguments": args}


def test_default_metadata_has_no_transcripts_queries_payloads_or_identity(tmp_path):
    observer = JsonlObserver(tmp_path / "events.jsonl")
    try:
        observer.capture("tool.completed", {"text": "private transcript", "query": "private query"},
                         function_name="search_web", status="not_found", reason="no_results",
                         display_name="Alice", user_id=123456789012345678, audio=b"raw audio",
                         arguments="private query", response_id="r1", call_id="c1")
        row = records(observer)[0]
        assert row["event"] == "tool.completed"
        assert row["function_name"] == "search_web"
        assert row["status"] == "not_found"
        raw = observer.path.read_text()
        for forbidden in ["payload", "private", "Alice", "123456789012345678", "raw audio", "arguments"]:
            assert forbidden not in raw
    finally:
        observer.close()


def test_capture_redacts_known_secrets_tokens_ids_and_keeps_session_audio_config(observer):
    observer.register_secrets("a-custom-secret", "Alice")
    observer.capture("session.configured", {
        "text": "Alice said a-custom-secret sk-abcdefghijklmnop Bearer xxx password=pw123 user a@b.com 123456789012345678",
        "api_key": "plain-secret", "nested": {"authorization": "private"},
        "audio": {"input": {"format": {"type": "audio/pcm", "rate": 24000}}},
        "raw_audio": "raw audio", "pcm": b"bytes",
    })
    payload = records(observer)[0]["payload"]
    assert payload["audio"]["input"]["format"]["rate"] == 24000
    assert payload["api_key"] == "[REDACTED]"
    raw = json.dumps(payload)
    for forbidden in ["Alice", "a-custom-secret", "sk-abcdefghijklmnop", "xxx", "pw123", "a@b.com", "123456789012345678", "plain-secret", "private", "raw audio"]:
        assert forbidden not in raw


@pytest.mark.parametrize("payload", [{"text": "x" * 3000}, {"items": list(range(400))}])
def test_incomplete_payload_is_explicit_and_never_silently_replayable(tmp_path, payload):
    observer = JsonlObserver(tmp_path / "events.jsonl", capture_enabled=True, capture_chars=1024)
    try:
        observer.capture("transcript.user", payload)
        row = records(observer)[0]
        assert row["payload_truncated"] is True
        assert "payload" not in row
    finally:
        observer.close()


def test_rotation_bounds_retention_and_records_remain_json(tmp_path):
    observer = JsonlObserver(tmp_path / "events.jsonl", max_bytes=2048, backups=2)
    try:
        for number in range(60):
            observer.emit("response.completed", call_id=f"c{number}", status="ok")
        assert observer.flush()
        files = list(tmp_path.glob("events.jsonl*"))
        assert len(files) == 3
        for path in files:
            assert path.stat().st_size <= 2048
            for line in path.read_text().splitlines():
                assert json.loads(line)["schema_version"] == 1
    finally:
        observer.close()


def test_pseudonyms_are_stable_only_inside_one_run(tmp_path):
    left = JsonlObserver(tmp_path / "left", enabled=False)
    right = JsonlObserver(tmp_path / "right", enabled=False)
    assert left.pseudonym(42) == left.pseudonym(42)
    assert left.pseudonym(42) != right.pseudonym(42)
    assert fingerprint({"a": 1, "b": 2}) == fingerprint({"b": 2, "a": 1})


@pytest.mark.parametrize("cls,model", [(OpenAIRealtimeManager, "gpt-4o-mini-transcribe"), (GrokRealtimeManager, "grok-transcribe")])
@pytest.mark.asyncio
async def test_capture_enables_provider_transcription_and_frozen_configuration(observer, cls, model):
    manager = make_manager(observer, cls)
    assert manager._session_config["audio"]["input"]["transcription"]["model"] == model
    await manager._post_connect_hook()
    row = records(observer)[0]
    assert row["event"] == "session.configured"
    assert row["prompt_hash"] == fingerprint(row["payload"]["instructions"])
    assert row["tools_hash"] == fingerprint(row["payload"]["tools"])
    assert row["payload"]["session_config"]["audio"]["input"]["transcription"]["model"] == model
    observer.capture_enabled = False
    safe = make_manager(observer, cls)
    assert "transcription" not in safe._session_config.get("audio", {}).get("input", {})


@pytest.mark.asyncio
async def test_late_user_and_assistant_transcripts_keep_original_turn(observer):
    manager = make_manager(observer)
    assert await manager.send_turn_context(111111111111111111, "Alice")
    turn_one = manager.observation_context["turn_id"]
    await manager.send_audio_chunk(b"first pcm")
    await manager._dispatch_event({"type": "input_audio_buffer.committed", "item_id": "input1"})
    await manager._dispatch_event({"type": "response.created", "response": {"id": "r1"}})
    await manager._dispatch_event({"type": "response.done", "response": {"id": "r1", "output": []}})
    await manager.send_turn_context(222222222222222222, "Bob")
    assert manager.observation_context["turn_id"] != turn_one
    await manager._dispatch_event({"type": "conversation.item.input_audio_transcription.completed", "item_id": "input1", "transcript": "查一下伊泽瑞尔"})
    await manager._dispatch_event({"type": "response.output_audio_transcript.done", "response_id": "r1", "item_id": "answer1", "transcript": "这个来源没有找到"})
    rows = records(observer)
    transcripts = [r for r in rows if r["event"].startswith("transcript.")]
    assert len(transcripts) == 2
    assert all(r["turn_id"] == turn_one for r in transcripts)
    assert transcripts[0]["payload"]["text"] == "查一下伊泽瑞尔"
    names = [r["event"] for r in rows]
    assert names.index("turn.context.sent") < names.index("turn.audio.first_sent")
    assert "first pcm" not in observer.path.read_text(encoding="utf-8")
    context = next(r for r in rows if r["event"] == "turn.context.sent")
    assert "Alice" not in context["payload"]["text"]


@pytest.mark.asyncio
async def test_parallel_tool_route_result_and_continuation_are_correlated(observer):
    manager = make_manager(observer)
    await manager.send_turn_context(42, "Alice")
    manager._tools.execute = AsyncMock(return_value=json.dumps({"status": "ok", "cache": {"hit": True, "stale": True}, "champion": "ezreal"}))
    await manager._dispatch_event({"type": "response.created", "response": {"id": "r1"}})
    await manager._dispatch_event({"type": "response.done", "response": {"id": "r1", "output": [function_call(), function_call("call2")]}})
    await asyncio.gather(*tuple(manager._tool_tasks))
    rows = records(observer)
    completed = [r for r in rows if r["event"] == "tool.completed"]
    assert len(completed) == 2
    assert all(r["cache_hit"] is True and r["stale"] is True and r["duration_ms"] >= 0 for r in completed)
    assert all(r["response_id"] == "r1" and r["turn_id"] == manager.observation_context["turn_id"] for r in completed)
    assert completed[0]["payload"]["result"]["champion"] == "ezreal"
    submitted = [r["sequence"] for r in rows if r["event"] == "tool.result.submitted"]
    continuation = [r["sequence"] for r in rows if r["event"] == "response.continuation.requested"]
    assert len(continuation) == 1 and max(submitted) < continuation[0]


@pytest.mark.asyncio
async def test_tool_timeout_and_exception_report_safe_reasons(observer):
    manager = make_manager(observer)
    for failure, reason in [(TimeoutError("private-provider-key"), "tool_timeout"), (RuntimeError("private-provider-key"), "tool_execution_error")]:
        manager._tools.execute = AsyncMock(side_effect=failure)
        output = await manager._observed_tool(function_call(), manager.observation_context)
        assert json.loads(output)["reason"] == reason
    raw = observer.path.read_text() if observer.flush() else ""
    assert "private-provider-key" not in raw
    assert len([r for r in records(observer) if r["event"] == "tool.completed"]) == 2


@pytest.mark.asyncio
async def test_tool_cancellation_keeps_original_turn_and_never_submits(observer):
    manager = make_manager(observer)
    await manager.send_turn_context(42, "Alice")
    turn = manager.observation_context["turn_id"]
    entered = asyncio.Event()

    async def slow(*args):
        entered.set()
        await asyncio.Event().wait()

    manager._tools.execute = slow
    await manager._dispatch_event({"type": "response.created", "response": {"id": "r1"}})
    await manager._dispatch_event({"type": "response.done", "response": {"id": "r1", "output": [function_call()]}})
    await entered.wait()
    await manager.send_turn_context(43, "Bob")
    await asyncio.gather(*tuple(manager._tool_tasks), return_exceptions=True)
    rows = records(observer)
    cancelled = next(r for r in rows if r["event"] == "tool.cancelled")
    assert cancelled["turn_id"] == turn and cancelled["response_id"] == "r1"
    assert not any(r["event"] == "tool.result.submitted" for r in rows)


@pytest.mark.asyncio
async def test_stale_tool_results_are_discarded_and_visible(observer):
    manager = make_manager(observer)

    async def becomes_stale(*args):
        manager._generation += 1
        return '{"status":"ok"}'

    manager._tools.execute = becomes_stale
    await manager._complete_tools([function_call()], manager._generation)
    rows = records(observer)
    assert any(r["event"] == "tool.results.discarded" for r in rows)
    assert not any(r["event"] == "tool.result.submitted" for r in rows)


@pytest.mark.asyncio
async def test_same_speaker_vad_segments_have_distinct_replayable_turns(observer):
    manager = make_manager(observer)
    await manager.send_turn_context(42, "Alice", streaming=True)
    await manager.send_audio_chunk(b"pcm")
    for number in (1, 2):
        item = f"input{number}"
        await manager._dispatch_event({"type": "input_audio_buffer.speech_started", "item_id": item})
        await manager._dispatch_event({"type": "input_audio_buffer.speech_stopped", "item_id": item})
        await manager._dispatch_event({"type": "input_audio_buffer.committed", "item_id": item})
        await manager._dispatch_event({"type": "response.created", "response": {"id": f"r{number}"}})
        await manager._dispatch_event({"type": "response.done", "response": {"id": f"r{number}", "output": []}})
    for number in (2, 1):
        await manager._dispatch_event({"type": "conversation.item.input_audio_transcription.completed", "item_id": f"input{number}", "transcript": f"question{number}"})
    rows = records(observer)
    first = next(r for r in rows if r["event"] == "transcript.user" and r["item_id"] == "input1")
    second = next(r for r in rows if r["event"] == "transcript.user" and r["item_id"] == "input2")
    assert first["turn_id"] != second["turn_id"]
    for number, transcript in ((1, first), (2, second)):
        response = next(r for r in rows if r["event"] == "response.started" and r["response_id"] == f"r{number}")
        assert response["turn_id"] == transcript["turn_id"]
    inherited = next(r for r in rows if r["event"] == "turn.context.reused")
    original = next(r for r in rows if r["event"] == "turn.context.sent")
    assert inherited["turn_id"] == second["turn_id"]
    assert inherited["payload"]["text"] == original["payload"]["text"]


@pytest.mark.asyncio
async def test_unmapped_transcript_is_marked_instead_of_attached_to_wrong_speaker(observer):
    manager = make_manager(observer)
    await manager.send_turn_context(42, "Alice")
    await manager._dispatch_event({"type": "conversation.item.input_audio_transcription.completed", "item_id": "unknown", "transcript": "old question"})
    row = next(r for r in records(observer) if r["event"] == "transcript.user")
    assert row["turn_id"] is None
    assert row["correlation"] == "unmapped"


@pytest.mark.asyncio
async def test_overlapping_response_origins_are_marked_ambiguous(observer):
    manager = make_manager(observer)
    await manager.send_turn_context(42, "Alice")
    await manager.send_audio_chunk(b"pcm")
    await manager._dispatch_event({"type": "input_audio_buffer.committed", "item_id": "input1"})
    await manager._dispatch_event({"type": "input_audio_buffer.committed", "item_id": "input2"})
    for number in (1, 2):
        await manager._dispatch_event({"type": "response.created", "response": {"id": f"r{number}"}})
    rows = [r for r in records(observer) if r["event"] == "response.started"]
    assert len(rows) == 2
    assert all(r["turn_id"] is None and r["correlation"] == "ambiguous" for r in rows)


@pytest.mark.asyncio
async def test_cancelled_pending_response_does_not_steal_next_turn(observer):
    manager = make_manager(observer)
    await manager.send_turn_context(42, "Alice")
    await manager.send_audio_chunk(b"pcm")
    await manager._dispatch_event({"type": "input_audio_buffer.committed", "item_id": "input1"})
    manager._response_in_progress = True
    await manager.cancel_ongoing_response()
    await manager._dispatch_event({"type": "response.created", "response": {"id": "cancelled"}})
    await manager.send_turn_context(43, "Bob")
    turn = manager.observation_context["turn_id"]
    await manager.send_audio_chunk(b"pcm")
    await manager._dispatch_event({"type": "input_audio_buffer.committed", "item_id": "input2"})
    await manager._dispatch_event({"type": "response.created", "response": {"id": "r2"}})
    rows = [r for r in records(observer) if r["event"] == "response.started"]
    assert len(rows) == 1
    assert rows[0]["turn_id"] == turn


@pytest.mark.parametrize("reason,expected", [
    ("insufficient_quota.credit_balance_exhausted", "insufficient_quota.credit_balance_exhausted"),
    ("rate_limit_exceeded: private request content", "rate_limit_exceeded"),
    ("authentication_error: private key content", "authentication_error"),
    ("private unknown close reason", "ConnectionClosedError"),
])
def test_connection_close_classification_never_copies_reason(reason, expected):
    result = safe_connection_failure(ConnectionClosedError(Close(1013, reason), None))
    assert result == {"close_code": 1013, "error_code": expected}
    assert "private" not in json.dumps(result)


@pytest.mark.asyncio
async def test_quota_close_stops_retry_and_fails_setup_promptly(observer, monkeypatch):
    manager = make_manager(observer)
    manager._accepted = False
    connection = manager._connection_handler
    del connection.is_connected  # Exercise the real transport flag, not the helper stub.
    close_error = ConnectionClosedError(
        Close(1013, "insufficient_quota.credit_balance_exhausted private-provider-key private-body"), None
    )

    async def closed_stream():
        raise close_error
        yield  # Make this an async event iterator without making any network request.

    websocket = MagicMock()
    websocket.__aiter__.side_effect = closed_stream
    socket_context = AsyncMock()
    socket_context.__aenter__.return_value = websocket
    connect_socket = MagicMock(return_value=socket_context)
    monkeypatch.setattr("src.ai_services.realtime_connection.websockets.connect", connect_socket)
    on_ready, on_lost = AsyncMock(), AsyncMock()
    assert await asyncio.wait_for(manager.connect(on_ready, on_lost), timeout=1.5) is False
    assert connect_socket.call_count == 1
    assert connection.terminal_error_code == "insufficient_quota.credit_balance_exhausted"
    assert connection._shutdown_signal.is_set()
    assert manager._configuration_failed
    on_ready.assert_not_awaited()
    on_lost.assert_awaited_once()
    rows = records(observer)
    failure = next(r for r in rows if r["event"] == "transport.connection.failed")
    assert failure["close_code"] == 1013 and failure["terminal"] is True
    assert failure["reason"] == "retry_stopped"
    assert any(r["event"] == "session.failed" and r["reason"] == "terminal_provider_error" for r in rows)
    raw = observer.path.read_text(encoding="utf-8")
    assert "private-provider-key" not in raw and "private-body" not in raw

    # A later explicit attempt resets terminal state, then can fail independently.
    resets = []
    original_configure = manager._configure_socket

    async def check_reset():
        resets.append(connection.terminal_error_code)
        await original_configure()

    manager._configure_socket = check_reset
    assert await asyncio.wait_for(manager.connect(on_ready, on_lost), timeout=1.5) is False
    assert resets == [None]
    assert connect_socket.call_count == 2


@pytest.mark.asyncio
async def test_transient_close_retains_reconnect_behavior(observer, monkeypatch):
    manager = make_manager(observer)
    connection = manager._connection_handler

    async def closed_stream():
        raise ConnectionClosedError(Close(1013, "rate_limit_exceeded private-body"), None)
        yield

    websocket = MagicMock()
    websocket.__aiter__.side_effect = closed_stream
    socket_context = AsyncMock()
    socket_context.__aenter__.return_value = websocket
    monkeypatch.setattr("src.ai_services.realtime_connection.websockets.connect", MagicMock(return_value=socket_context))
    with pytest.raises(Exception, match="rate_limit_exceeded"):
        await connection._connection_logic()
    assert connection.terminal_error_code is None
    assert not connection._shutdown_signal.is_set()
    failure = next(r for r in records(observer) if r["event"] == "transport.connection.failed")
    assert failure["terminal"] is False
