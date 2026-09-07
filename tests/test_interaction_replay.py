"""Deterministic trace-to-fixture and real orchestration regressions."""

import copy
import asyncio
import json

import pytest

from src.evaluation.interactions import export_case, read_events, replay, timeline


def case(provider="openai"):
    return {"schema_version": 1, "id": "synthetic-tier", "question": "安蓓萨什么评级？",
            "snapshot": {"provider": provider, "model": "offline-model",
                         "session_config": {"instructions": "Read source tier literally.",
                                            "tools": [{"type": "function", "name": "get_mayhem_champion_tier",
                                                       "parameters": {"type": "object"}}]}},
            "tool_calls": [{"name": "get_mayhem_champion_tier", "arguments": {"champion": "ambessa"},
                            "result": {"status": "ok", "championTier": 3, "source": "synthetic"}}],
            "expected": {"tool_names": ["get_mayhem_champion_tier"]}}


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "grok"])
async def test_offline_uses_production_orchestration_and_never_connects(monkeypatch, provider):
    from src.ai_services.realtime_connection import RealtimeWebSocketConnection

    async def forbidden(*args, **kwargs):
        pytest.fail("Offline replay attempted network connection")
    monkeypatch.setattr(RealtimeWebSocketConnection, "connect", forbidden)
    fixture = case(provider)
    fixture["tool_calls"] *= 2
    fixture["expected"]["tool_names"] *= 2
    report = await replay(fixture)
    assert report["passed"]
    assert report["checks"]["results_before_continuation"]
    assert report["checks"]["answer_quality"] == "not_evaluated_offline"
    assert report["answers"] == []


@pytest.mark.asyncio
async def test_route_regression_fails_report():
    fixture = case()
    fixture["expected"]["tool_names"] = ["get_mayhem_augments"]
    report = await replay(fixture)
    assert not report["passed"]
    assert not report["checks"]["tool_route"]


def events():
    base = {"schema_version": 1, "session_id": "session-test", "turn_id": "turn-test", "provider": "openai"}
    call = case()["tool_calls"][0]
    return [{**base, "event": "session.configured", "payload": case()["snapshot"]},
            {**base, "event": "transcript.user", "payload": {"text": case()["question"]}},
            {**base, "event": "tool.started", "call_id": "call-test", "payload": call},
            {**base, "event": "tool.completed", "call_id": "call-test", "payload": {"result": call["result"]}}]


def test_export_requires_full_capture_and_does_not_confuse_sessions():
    rows = events()
    assert export_case(rows, "turn-test")["tool_calls"][0]["result"]["status"] == "ok"
    private = copy.deepcopy(rows)
    private[1].pop("payload")
    with pytest.raises(ValueError, match="transcript missing"):
        export_case(private, "turn-test")
    rows.append({**rows[1], "session_id": "different"})
    with pytest.raises(ValueError, match="Ambiguous"):
        export_case(rows, "turn-test")


def test_partial_jsonl_tail_ignored_but_complete_corruption_rejected(tmp_path):
    path = tmp_path / "trace.jsonl"
    path.write_text(json.dumps(events()[1]) + '\n{"partial":', encoding="utf-8")
    assert len(read_events(path)) == 1
    assert "transcript.user" in timeline(read_events(path))
    path.write_text('{broken}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="row 1"):
        read_events(path)


@pytest.mark.asyncio
async def test_native_tools_rejected_before_live_connect(monkeypatch):
    from src.evaluation import interactions
    original = interactions.production_manager
    monkeypatch.setattr(interactions, "production_manager", lambda provider, live: original(provider))
    fixture = case("grok")
    fixture["snapshot"]["session_config"]["tools"] = [{"type": "web_search"}]
    with pytest.raises(ValueError, match="function tools only"):
        await replay(fixture, live=True)


@pytest.mark.asyncio
async def test_real_manager_capture_exports_and_replays(tmp_path):
    from src.evaluation.interactions import production_manager
    from src.observability import JsonlObserver

    observer = JsonlObserver(tmp_path / "interactions.jsonl", capture_enabled=True,
                             capture_chars=131072)
    manager = production_manager("openai")
    manager.observer = observer
    manager._accepted = True
    manager._connection_handler_inst.is_connected = lambda: True

    async def send(event):
        pass

    async def execute(name, arguments):
        return json.dumps({"status": "ok", "tierLabel": "T3"})

    manager._connection_handler_inst.send_event = send
    manager._execute_tool = execute
    try:
        await manager._post_connect_hook()
        assert await manager.send_turn_context(42, "SyntheticSpeaker", streaming=True)
        await manager.send_audio_chunk(b"\0\0" * 80)
        for event in (
            {"type": "input_audio_buffer.speech_started", "item_id": "input-test"},
            {"type": "input_audio_buffer.committed", "item_id": "input-test"},
            {"type": "conversation.item.input_audio_transcription.completed", "item_id": "input-test",
             "transcript": case()["question"]},
            {"type": "response.created", "response": {"id": "response-test"}},
            {"type": "response.done", "response": {"id": "response-test", "status": "completed",
                "output": [{"type": "function_call", "name": "get_mayhem_champion_tier",
                            "arguments": '{"champion":"ambessa"}', "call_id": "call-test"}]}},
        ):
            await manager._dispatch_event(event)
        await asyncio.gather(*tuple(manager._tool_tasks))
        observer.flush()
        rows = read_events(observer.path)
        turn = next(row["turn_id"] for row in rows if row["event"] == "transcript.user")
        exported = export_case(rows, turn)
        assert exported["history"]
        report = await replay(exported)
        assert report["passed"]
    finally:
        await manager.disconnect()
        observer.close()


@pytest.mark.asyncio
async def test_candidate_removed_tool_and_multi_round_ordering():
    fixture = case()
    second = copy.deepcopy(fixture["tool_calls"][0])
    second["round"] = "follow-up"
    fixture["tool_calls"].append(second)
    fixture["expected"]["tool_names"] *= 2
    assert (await replay(fixture))["passed"]
    candidate = copy.deepcopy(fixture["snapshot"])
    candidate["session_config"]["tools"] = []
    report = await replay(fixture, snapshot=candidate)
    assert not report["passed"]
    assert not report["checks"]["tool_schema_available"]


def test_export_rejects_incomplete_config_and_merged_questions():
    rows = events()
    rows[0]["payload_truncated"] = True
    with pytest.raises(ValueError, match="truncated"):
        export_case(rows, "turn-test")
    rows = events()
    rows.append(copy.deepcopy(rows[1]))
    with pytest.raises(ValueError, match="one user utterance"):
        export_case(rows, "turn-test")


def test_export_rejects_unmapped_response_in_same_connection():
    rows = events()
    rows.append({**rows[0], "event": "response.started", "turn_id": None,
                 "correlation": "ambiguous"})
    with pytest.raises(ValueError, match="ambiguous response"):
        export_case(rows, "turn-test")


@pytest.mark.asyncio
async def test_live_setup_failure_reports_safe_terminal_reason(monkeypatch):
    from src.evaluation import interactions
    manager = interactions.production_manager("openai")
    manager._connection_handler_inst._terminal_error_code = "insufficient_quota.credit_balance_exhausted"

    async def rejected(*args):
        return False

    manager.connect = rejected
    monkeypatch.setattr(interactions, "production_manager", lambda *args, **kwargs: manager)
    report = await replay(case(), live=True)
    assert not report["passed"]
    assert report["error"] == "insufficient_quota.credit_balance_exhausted"
    assert report["tool_calls"] == []



def gemini_case():
    from pathlib import Path
    return json.loads((Path(__file__).parent / "fixtures/interactions/gemini_champion_tier.json").read_text(encoding="utf-8"))


def test_gemini_native_schema_and_instruction_overrides():
    from types import SimpleNamespace
    from src.evaluation.interactions import override_manager_config, snapshot_manager
    manager = SimpleNamespace(provider="gemini", _model_name="test-model", _session_config={})
    override_manager_config(manager, instructions="literal tier only", tools=[{
        "type": "function", "name": "lookup", "parameters": {"type": "object"}}])
    snapshot = snapshot_manager(manager)
    assert snapshot["session_config"]["system_instruction"] == {"parts": [{"text": "literal tier only"}]}
    assert snapshot["session_config"]["tools"][0]["function_declarations"][0]["name"] == "lookup"
    assert "instructions" not in snapshot["session_config"]
    with pytest.raises(ValueError, match="function tools only"):
        override_manager_config(manager, tools=[{"google_search": {}}])


@pytest.mark.asyncio
async def test_gemini_offline_native_tool_rounds_and_complete_trace(monkeypatch, tmp_path):
    from src.ai_services.providers.gemini.connection import GeminiRealtimeConnection

    async def forbidden(*args, **kwargs):
        pytest.fail("Offline Gemini replay attempted network connection")
    monkeypatch.setattr(GeminiRealtimeConnection, "connect", forbidden)
    fixture = gemini_case()
    fixture["context_text"] = "Speaker context: synthetic-speaker. Shared game metadata is reference only."
    second = copy.deepcopy(fixture["tool_calls"][0])
    second["round"] = "second"
    fixture["tool_calls"].append(second)
    fixture["expected"]["tool_names"] *= 2
    path = tmp_path / "gemini.trace.jsonl"
    report = await replay(fixture, trace_path=path)
    assert report["passed"], report
    assert report["checks"]["native_tool_response_protocol"]
    assert report["checks"]["results_before_continuation"]
    assert report["answers"] == []
    rows = read_events(path)
    assert any(row["event"] == "session.configured" and row["provider"] == "gemini" for row in rows)
    turn = next(row["turn_id"] for row in rows if row["event"] == "transcript.user")
    exported = export_case(rows, turn)
    assert exported["snapshot"]["provider"] == "gemini"
    assert exported["context_text"] == fixture["context_text"]
    assert exported["tool_calls"][0]["result"] == fixture["tool_calls"][0]["result"]
    assert (await replay(exported))["passed"]


@pytest.mark.asyncio
async def test_gemini_removed_function_and_wrong_route_fail():
    fixture = gemini_case()
    candidate = copy.deepcopy(fixture["snapshot"])
    candidate["session_config"]["tools"] = []
    removed = await replay(fixture, snapshot=candidate)
    assert not removed["passed"]
    assert not removed["checks"]["tool_schema_available"]
    fixture["expected"]["tool_names"] = ["wrong_route"]
    assert not (await replay(fixture))["passed"]


@pytest.mark.asyncio
async def test_gemini_native_search_rejected_before_any_connect(monkeypatch):
    from src.evaluation import interactions
    original = interactions.production_manager
    monkeypatch.setattr(interactions, "production_manager", lambda provider, live: original(provider))
    fixture = gemini_case()
    fixture["snapshot"]["session_config"]["tools"].append({"google_search": {}})
    with pytest.raises(ValueError, match="function tools only"):
        await replay(fixture, live=True)


@pytest.mark.asyncio
async def test_mocked_gemini_live_uses_native_text_history_and_answer_checks(monkeypatch):
    from src.evaluation import interactions
    from src.evaluation.interactions import FrozenGeminiSession
    manager = interactions.production_manager("gemini")
    sent = []
    session = FrozenGeminiSession(sent)
    manager._connection_handler_inst.is_connected = lambda: True
    manager._connection_handler_inst.get_active_session = lambda: session

    async def connect(*args):
        assert manager._model_name == "gemini-frozen-model-override"
        assert manager._connection_handler_inst.model_name == "gemini-frozen-model-override"
        manager._accepted = True
        await manager._post_connect_hook()
        return True

    original_text = manager.send_text_turn
    async def text_turn(text, *, context_text=None):
        accepted = await original_text(text, context_text=context_text)
        await manager._dispatch_message({"tool_call": {"function_calls": [{
            "id": "live-fixture-1", "name": "get_mayhem_champion_tier", "args": {"champion": "ambessa"}}]}})
        await asyncio.gather(*tuple(manager._tool_tasks))
        await manager._dispatch_message({"server_content": {
            "output_transcription": {"text": "T3"}, "turn_complete": True}})
        return accepted

    manager.connect = connect
    manager.send_text_turn = text_turn
    monkeypatch.setattr(interactions, "production_manager", lambda *args, **kwargs: manager)
    fixture = gemini_case()
    fixture["snapshot"]["model"] = "gemini-frozen-model-override"
    fixture["history"] = [["user", "Current speaker is synthetic-speaker."],
                           ["assistant", "Ready."]]
    report = await replay(fixture, live=True, timeout=1)
    assert report["passed"], report
    assert report["answers"] == ["T3"]
    text_inputs = [event["fields"]["text"] for event in sent
                   if event["type"] == "gemini.realtime_input" and "text" in event["fields"]]
    assert any("synthetic-speaker" in text for text in text_inputs)
    assert any(fixture["question"] in text for text in text_inputs)
    assert not any(event["type"] == "response.create" for event in sent)
    fixture["expected"]["answer_contains"] = ["T1"]
    assert not (await replay(fixture, live=True, timeout=1))["passed"]
