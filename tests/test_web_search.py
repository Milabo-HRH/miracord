"""Offline search, privacy, citation, budget, and Realtime integration contracts."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.ai_services.providers.grok.manager import GrokRealtimeManager
from src.ai_services.providers.openai.manager import OpenAIRealtimeManager
from src.ai_services.web_search import (
    WebSearchExecutor, discord_search_message, parse_search_response, public_url,
)
from src.bot.session.ai_service_coordinator import AIServiceCoordinator


def response_payload():
    return {
        "status": "completed",
        "output": [
            {"type": "web_search_call", "status": "completed"},
            {"type": "message", "content": [{
                "type": "output_text", "text": "A sourced fact.\ue200cite\ue202x\ue201",
                "annotations": [{
                    "type": "url_citation", "url": "https://www.leagueoflegends.com/en-us/news/",
                    "title": "Official news",
                }],
            }]},
        ],
    }


def make_executor(**kwargs):
    response = MagicMock()
    response.model_dump.return_value = response_payload()
    client = SimpleNamespace(
        responses=SimpleNamespace(create=AsyncMock(return_value=response)), close=AsyncMock(),
    )
    return WebSearchExecutor("test-key-not-real", client=client, **kwargs), client


def make_manager(*, league=True, web=True, cls=OpenAIRealtimeManager, publish=None):
    manager = cls(MagicMock(), {
        "api_key": "test-key-not-real", "model_name": "mock-model",
        "session_config": {}, "league_context_enabled": False,
        "league_tools_enabled": league, "web_search_enabled": web,
        "on_web_search_result": publish,
        "processing_audio_frame_rate": 24000, "processing_audio_channels": 1,
        "response_audio_frame_rate": 24000, "response_audio_channels": 1,
    })
    manager._accepted = True
    manager._connection_handler_inst.is_connected = lambda: True
    manager._connection_handler_inst.send_event = AsyncMock()
    return manager


def call(cid="c1"):
    return {"type": "function_call", "name": "search_web", "call_id": cid,
            "arguments": '{"query":"ARAM Mayhem patch notes","source":"riot"}'}


@pytest.mark.asyncio
async def test_forced_search_request_is_bounded_and_retains_citations():
    executor, client = make_executor(preferred_sources=("op.gg",))
    result = json.loads(await executor.execute(call()["arguments"]))
    request = client.responses.create.call_args.kwargs
    assert request["store"] is False
    assert request["tool_choice"] == "required"
    assert request["max_tool_calls"] == 1
    assert request["max_output_tokens"] == 700
    assert request["model"] == "gpt-5.4-mini"
    assert request["reasoning"] == {"effort": "none"}
    assert request["tools"][0]["filters"]["allowed_domains"] == ["leagueoflegends.com", "riotgames.com"]
    assert result["status"] == "ok"
    assert result["answer"] == "A sourced fact."
    assert result["sources"][0]["url"].startswith("https://www.leagueoflegends.com/")
    assert result["cacheHit"] is False
    await executor.close()
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_cache_singleflight_and_expiry():
    now = [100]
    executor, client = make_executor(clock=lambda: now[0])
    rows = await asyncio.gather(*[executor.execute(call()["arguments"]) for _ in range(3)])
    assert client.responses.create.await_count == 1
    assert sum(json.loads(row)["cacheHit"] for row in rows) == 2
    now[0] += 61
    await executor.execute(call()["arguments"])
    assert client.responses.create.await_count == 2


@pytest.mark.asyncio
async def test_auto_preferences_do_not_silently_restrict_the_web():
    executor, client = make_executor(preferred_sources=("op.gg", "reddit.com/r/ARAM"))
    await executor.execute('{"query":"Mayhem changes"}')
    request = client.responses.create.call_args.kwargs
    assert "filters" not in request["tools"][0]
    assert "reddit.com/r/ARAM" in request["instructions"]


@pytest.mark.parametrize("args", [
    "[]", "null", "bad", "{}", '{"query":false}', '{"query":" "}',
    '{"query":"q","source":"http://127.0.0.1"}', '{"query":"q","extra":1}',
    json.dumps({"query": "x" * 501}), '{"query":"lookup 123456789012345678"}',
    '{"query":"token sk-testingabcdefghijk"}',
])
@pytest.mark.asyncio
async def test_invalid_or_private_query_does_not_reach_network(args):
    executor, client = make_executor()
    result = json.loads(await executor.execute(args))
    assert result["status"] == "invalid_arguments"
    client.responses.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_timeout_includes_queue_and_does_not_retry():
    executor, client = make_executor(timeout=0.01)
    await executor._semaphore.acquire()
    result = json.loads(await executor.execute(call()["arguments"]))
    assert result["reason"] == "search_timeout"
    client.responses.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_failure_is_not_reported_as_no_data_or_leaked(caplog):
    executor, client = make_executor()
    client.responses.create.side_effect = RuntimeError("sk-privateSECRET filesystem/private")
    result = await executor.execute('{"query":"private spoken phrase"}')
    assert json.loads(result) == {"status": "unavailable", "reason": "search_failed"}
    assert "SECRET" not in result + caplog.text
    assert "private spoken phrase" not in caplog.text
    assert client.responses.create.await_count == 1


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "javascript:alert(1)", "http://localhost/x",
    "http://127.0.0.1/x", "http://10.0.0.1/x", "https://user:password@op.gg/x",
    "https://a.local/x", "https://op.gg/\n@everyone",
])
def test_citations_reject_unsafe_links(url):
    assert public_url(url) is None


def test_uncited_or_unperformed_search_is_not_ok():
    payload = response_payload()
    payload["output"].pop(0)
    assert parse_search_response(payload)["reason"] == "search_not_performed"
    payload = response_payload()
    payload["output"][1]["content"][0]["annotations"] = []
    assert parse_search_response(payload)["status"] == "no_results"
    payload["status"] = "incomplete"
    assert parse_search_response(payload)["status"] == "unavailable"


def test_speech_summary_does_not_include_raw_markdown_urls():
    payload = response_payload()
    payload["output"][1]["content"][0]["text"] = "Fact. ([Riot](https://leagueoflegends.com/news))"
    result = parse_search_response(payload)
    assert "https://" not in result["answer"]
    assert result["sources"]


@pytest.mark.asyncio
async def test_api_compatibility_error_is_not_a_connection_failure():
    executor, client = make_executor()
    error = RuntimeError("unsupported filters")
    error.status_code = 400
    client.responses.create.side_effect = error
    result = json.loads(await executor.execute(call()["arguments"]))
    assert result["reason"] == "search_configuration_error"


@pytest.mark.asyncio
async def test_search_idle_does_not_create_client_or_call_network():
    manager = make_manager()
    assert manager._web_search._client is None
    assert manager._searches_this_turn == 0
    await manager._web_search.close()


@pytest.mark.asyncio
async def test_diagnostic_override_can_omit_reasoning():
    executor, client = make_executor(reasoning_effort="")
    await executor.execute(call()["arguments"])
    assert "reasoning" not in client.responses.create.call_args.kwargs


@pytest.mark.asyncio
async def test_publisher_targets_text_channel_without_pings_or_config_mutation():
    ctx = SimpleNamespace(send=AsyncMock())
    original = {"model_name": "unchanged"}
    config = AIServiceCoordinator._config_for_channel(original, ctx)
    result = parse_search_response(response_payload())
    result["answer"] = "@everyone **page content** " + "x" * 4000
    await config["on_web_search_result"](result)
    message = ctx.send.call_args.args[0]
    assert len(message) <= 2000
    assert "@everyone" not in message
    assert "]( <" not in message
    assert "[1](<https://" in message
    assert ctx.send.call_args.kwargs["allowed_mentions"].everyone is False
    assert original == {"model_name": "unchanged"}


@pytest.mark.parametrize("league,web", [(True, True), (False, True), (True, False), (False, False)])
def test_search_switch_is_independent_of_league_tools(league, web):
    manager = make_manager(league=league, web=web)
    names = {tool["name"] for tool in manager._session_config.get("tools", [])}
    assert ("search_web" in names) is web
    assert ("identify_mayhem_augment" in names) is league
    assert ("compare_mayhem_choices" in names) is league
    assert "get_mayhem_augments" not in names
    assert ("## Online search" in manager._session_config["instructions"]) is web
    assert manager.capabilities.native_web_search is False


def test_grok_keeps_its_own_native_search_not_an_openai_key():
    manager = make_manager(cls=GrokRealtimeManager)
    assert manager._web_search is None
    assert "search_web" not in {tool["name"] for tool in manager._session_config.get("tools", [])}


@pytest.mark.asyncio
async def test_search_tool_roundtrip_without_league_then_publish():
    publish = AsyncMock()
    manager = make_manager(league=False, publish=publish)
    result = parse_search_response(response_payload())
    manager._web_search.execute = AsyncMock(return_value=json.dumps(result))
    await manager._complete_tools([call()], manager._generation)
    sent = [entry.args[0] for entry in manager._connection_handler_inst.send_event.await_args_list]
    assert [event["type"] for event in sent] == ["conversation.item.create", "response.create"]
    assert sent[0]["item"]["call_id"] == "c1"
    assert json.loads(sent[0]["item"]["output"])["status"] == "ok"
    publish.assert_awaited_once_with(result)
    assert manager._tools_enabled is False


@pytest.mark.asyncio
async def test_search_budget_one_per_turn_resets_for_next_turn():
    manager = make_manager()
    manager._web_search.execute = AsyncMock(return_value='{"status":"ok"}')
    first, second = await asyncio.gather(*[
        manager._execute_tool("search_web", call()["arguments"]) for _ in range(2)
    ])
    assert json.loads(first)["status"] == "ok"
    assert json.loads(second)["reason"] == "search_budget_exceeded"
    await manager.send_turn_context(0, "Test")
    assert json.loads(await manager._execute_tool("search_web", call()["arguments"]))["status"] == "ok"
    assert manager._web_search.execute.await_count == 2


@pytest.mark.asyncio
async def test_disabled_search_cannot_be_called():
    manager = make_manager(web=False)
    result = await manager._execute_tool("search_web", call()["arguments"])
    assert json.loads(result)["reason"] == "search_disabled"


@pytest.mark.asyncio
async def test_interruption_cancels_search_and_prevents_late_reply_or_citations():
    publish, entered = AsyncMock(), asyncio.Event()
    manager = make_manager(publish=publish)

    async def slow(arguments):
        entered.set()
        await asyncio.Event().wait()

    manager._web_search.execute = slow
    await manager._dispatch_event({"type": "response.done", "response": {
        "id": "r1", "status": "completed", "output": [call()],
    }})
    await entered.wait()
    tasks = tuple(manager._tool_tasks)
    await manager.cancel_ongoing_response()
    await asyncio.gather(*tasks, return_exceptions=True)
    sent = [entry.args[0] for entry in manager._connection_handler_inst.send_event.await_args_list]
    assert not any(event["type"] == "response.create" for event in sent)
    assert not any(event.get("item", {}).get("type") == "function_call_output" for event in sent)
    publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_search_cache_key_separates_explicit_source():
    executor, client = make_executor()
    await executor.execute('{"query":"A","source":"reddit"}')
    await executor.execute('{"query":"A","source":"riot"}')
    assert client.responses.create.await_count == 2
