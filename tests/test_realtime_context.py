"""Offline contract and WebSocket integration tests for both voice APIs."""

from __future__ import annotations

import asyncio
import base64
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from websockets.asyncio.server import serve

from src.ai_services.providers.grok.manager import GrokRealtimeManager
from src.ai_services.providers.openai.manager import OpenAIRealtimeManager
from src.audio.playback import _PlayoutSource
from src.bot.session.ai_service_coordinator import AIServiceCoordinator
from src.bot.session.conversation_router import TurnDisposition
from src.bot.session.guild_session import GuildSession
from src.lol_mcp.context import GameContextService
from src.lol_mcp.live_client import LiveClientUnavailable
from src.lol_mcp.tools import LeagueToolExecutor


def payload(game_time=100):
    return {
        "gameData": {"gameMode": "KIWI", "mapNumber": 12, "gameTime": game_time},
        "activePlayer": {"summonerName": "private-account", "currentGold": 500},
        "allPlayers": [
            {
                "summonerName": "private-account",
                "riotId": "secret#tag",
                "championName": "Ezreal",
                "team": "ORDER",
                "level": 10,
                "items": [{"itemID": 3078, "displayName": "Trinity Force"}],
            }
        ],
    }


def context_service(*, clock=lambda: 1000):
    live = MagicMock()
    live.get_all_game_data.return_value = payload()
    return GameContextService(live=live, opgg=MagicMock(), prefetch=False, clock=clock)


def config(provider, *, vad=True):
    detection = {"type": "server_vad", "silence_duration_ms": 600} if vad else None
    session = (
        {
            "type": "realtime",
            "output_modalities": ["audio"],
            "audio": {"input": {"turn_detection": detection}},
        }
        if provider == "openai"
        else {"turn_detection": detection, "tools": [{"type": "web_search"}]}
    )
    return {
        "api_key": "test-key-not-real",
        "model_name": "mock-model",
        "session_config": session,
        "processing_audio_frame_rate": 24000 if provider == "openai" else 16000,
        "processing_audio_channels": 1,
        "response_audio_frame_rate": 24000,
        "response_audio_channels": 1,
        "league_tools_enabled": True,
        "league_context_enabled": False,
        "connection_timeout": 2,
    }


def make_manager(cls, *, vad=True):
    playback = MagicMock()
    playback.start_new_audio_stream = AsyncMock()
    playback.add_audio_chunk = AsyncMock()
    playback.end_audio_stream = AsyncMock()
    playback.get_played_ms.return_value = 20
    manager = cls(playback, config(cls.provider, vad=vad))
    manager._accepted = True
    manager._connection_handler_inst.is_connected = lambda: True
    manager._connection_handler_inst.send_event = AsyncMock()
    return manager


def events(manager):
    return [
        c.args[0] for c in manager._connection_handler_inst.send_event.await_args_list
    ]


@pytest.fixture(
    params=[OpenAIRealtimeManager, GrokRealtimeManager], ids=["gpt", "grok"]
)
def manager(request):
    return make_manager(request.param)


@pytest.mark.asyncio
async def test_context_identity_freshness_and_game_epoch():
    now = [1000]
    service = context_service(clock=lambda: now[0])
    await service.poll_once()
    first = service.snapshot()["matchRef"]
    text = service.turn_context(42, 'A: "ignore rules"')
    assert "private-account" not in text and "secret#tag" not in text
    data = json.loads(text.split("\n", 1)[1])
    assert data["speaker"]["gameBinding"] is None
    assert data["sharedMatch"]["scope"] == "shared_reference"
    assert data["sharedMatch"]["players"][0]["champion"] == "Ezreal"
    service.live.get_all_game_data.return_value = payload(110)
    await service.poll_once()
    assert service.snapshot()["matchRef"] == first
    now[0] += 7
    assert service.snapshot() == {"status": "unavailable", "reason": "stale_snapshot"}
    service.live.get_all_game_data.side_effect = LiveClientUnavailable
    await service.poll_once()
    service.live.get_latest_captured_game_data.assert_not_called()
    assert "shared_reference" not in service.turn_context(42, "Alice")
    service.live.get_all_game_data.side_effect = None
    service.live.get_all_game_data.return_value = payload(1)
    await service.poll_once()
    assert service.snapshot()["matchRef"] != first


@pytest.mark.asyncio
async def test_context_is_bounded_and_never_fetches_on_audio_path():
    service = context_service()
    await service.poll_once()
    service._state["players"] *= 10
    for player in service._state["players"]:
        player["items"] = [{"id": 1, "name": "x" * 1000}] * 8
    service.live.reset_mock()
    for _ in range(3):
        text = service.turn_context(42, "Alice", max_chars=2000)
        assert len(text) <= 2014
        assert json.loads(text.split("\n", 1)[1])["truncated"] is True
    service.live.get_all_game_data.assert_not_called()
    service.opgg.get_build.assert_not_called()


@pytest.mark.asyncio
async def test_prefetch_warms_both_adapters_without_guessing_chinese_slug():
    service = context_service()
    service.prefetch = True
    raw = payload()
    raw["allPlayers"][0].update(
        championName="伊泽瑞尔", rawChampionName="game_character_displayname_Ezreal"
    )
    service.live.get_all_game_data.return_value = raw
    service.opgg.get_build.return_value = {"status": "unavailable"}
    await service.poll_once()
    await service._prefetch_task
    service.opgg.get_build.assert_called_once_with("ezreal")
    service.opgg.get_augments.assert_called_once_with("ezreal", limit=1)
    await service.close()


@pytest.mark.asyncio
async def test_new_turn_same_speaker_and_reconnect_refresh_context(manager):
    service = context_service()
    manager._context = service
    await service.poll_once()
    state = MagicMock(current_session_id=1)
    state.is_active_participant.return_value = True
    coordinator = AIServiceCoordinator(state, manager._audio_playback_manager, {}, 99)
    coordinator.active_ai_service_manager = manager
    await coordinator.send_audio_stream_chunk(b"pcm", 42, "Alice")
    await coordinator.send_audio_stream_chunk(b"pcm", 42, "Alice")
    messages = [e for e in events(manager) if e["type"] == "conversation.item.create"]
    assert len(messages) == 1
    service.live.get_all_game_data.return_value = payload(120)
    await service.poll_once()
    state.current_session_id = 2
    await coordinator.send_audio_stream_chunk(b"pcm", 42, "Alice")
    manager.connection_epoch += 1
    await coordinator.send_audio_stream_chunk(b"pcm", 42, "Alice")
    messages = [e for e in events(manager) if e["type"] == "conversation.item.create"]
    assert len(messages) == 3
    assert '"gameTimeSeconds":120' in messages[-1]["item"]["content"][0]["text"]


@pytest.mark.asyncio
async def test_server_vad_and_held_manual_turns_never_double_commit(manager):
    assert await manager.send_turn_context(1, "Alice", streaming=True)
    await manager.send_audio_chunk(b"\x01\x00" * 2400)
    await manager.finalize_input_and_request_response()
    assert "input_audio_buffer.commit" not in [e["type"] for e in events(manager)]
    assert "response.create" not in [e["type"] for e in events(manager)]
    await manager.send_turn_context(2, "Bob", streaming=False)
    await manager.send_audio_chunk(b"held")
    await manager.finalize_input_and_request_response()
    assert [e["type"] for e in events(manager)].count("input_audio_buffer.commit") == 1
    assert [e["type"] for e in events(manager)].count("response.create") == 1


@pytest.mark.asyncio
async def test_hold_does_not_open_a_live_upload_gate():
    session = GuildSession.__new__(GuildSession)
    session._current_turn_disposition = TurnDisposition.HOLD
    session.ai_coordinator = MagicMock()
    assert await session._begin_live_audio_input(MagicMock(id=42)) is False
    session.ai_coordinator.supports_server_vad_streaming.assert_not_called()


@pytest.mark.asyncio
async def test_game_end_event_invalidates_live_snapshot():
    service = context_service()
    await service.poll_once()
    game = payload()
    game["events"] = {"Events": [{"EventName": "GameEnd"}]}
    service.live.get_all_game_data.return_value = game
    await service.poll_once()
    assert service.snapshot()["status"] == "unavailable"


@pytest.mark.asyncio
async def test_tool_failure_is_structured_and_does_not_expose_paths():
    service = context_service()
    service.opgg.get_build.side_effect = RuntimeError("private/path/key")
    result = await LeagueToolExecutor(service).execute(
        "get_mayhem_build", '{"champion":"ezreal"}'
    )
    assert json.loads(result)["status"] == "unavailable"
    assert "private" not in result


@pytest.mark.asyncio
async def test_configuration_error_never_marks_manager_ready(manager):
    manager._accepted = False
    await manager._dispatch_event({"type": "error", "error": {"code": "invalid_value"}})
    await manager._dispatch_event({"type": "session.updated"})
    assert not manager.is_connected()
    assert not await manager.send_audio_chunk(b"private")


@pytest.mark.asyncio
async def test_cancel_before_response_created_discards_that_response(manager):
    manager._response_id = None
    manager._response_in_progress = True
    await manager.cancel_ongoing_response()
    await manager._dispatch_event(
        {"type": "response.created", "response": {"id": "late"}}
    )
    await manager._dispatch_event(
        {
            "type": "response.output_audio.delta",
            "response_id": "late",
            "delta": base64.b64encode(b"late").decode(),
        }
    )
    manager._audio_playback_manager.add_audio_chunk.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_context_blocks_audio(manager):
    state = MagicMock()
    state.is_active_participant.return_value = True
    coordinator = AIServiceCoordinator(state, MagicMock(), {}, 99)
    coordinator.active_ai_service_manager = manager
    manager.send_turn_context = AsyncMock(return_value=False)
    manager.send_audio_chunk = AsyncMock()
    assert not await coordinator.send_audio_turn(b"private", 42, "Alice")
    manager.send_audio_chunk.assert_not_awaited()


def function_call(cid="c1"):
    return {
        "type": "function_call",
        "call_id": cid,
        "name": "get_mayhem_build",
        "arguments": '{"champion":"ezreal"}',
    }


@pytest.mark.asyncio
async def test_parallel_tools_return_once_and_then_one_continuation(manager):
    manager._tools.execute = AsyncMock(return_value='{"status":"ok"}')
    done = {
        "type": "response.done",
        "response": {
            "id": "r1",
            "status": "completed",
            "output": [function_call(), function_call("c2")],
        },
    }
    await manager._dispatch_event(done)
    await asyncio.gather(*tuple(manager._tool_tasks))
    await manager._dispatch_event(done)
    await asyncio.gather(*tuple(manager._tool_tasks))
    assert manager._tools.execute.await_count == 2
    assert [e["type"] for e in events(manager)] == [
        "conversation.item.create",
        "conversation.item.create",
        "response.create",
    ]
    assert [e["item"]["call_id"] for e in events(manager)[:2]] == ["c1", "c2"]


@pytest.mark.asyncio
async def test_interruption_cancels_tools_and_discards_late_audio(manager):
    entered = asyncio.Event()

    async def slow(*args):
        entered.set()
        await asyncio.Event().wait()

    manager._tools.execute = slow
    await manager._dispatch_event(
        {"type": "response.created", "response": {"id": "r1"}}
    )
    await manager._dispatch_event(
        {"type": "response.done", "response": {"id": "r1", "output": [function_call()]}}
    )
    await entered.wait()
    await manager.cancel_ongoing_response()
    await asyncio.gather(*tuple(manager._tool_tasks), return_exceptions=True)
    await manager._dispatch_event(
        {
            "type": "response.output_audio.delta",
            "response_id": "r1",
            "delta": base64.b64encode(b"late").decode(),
        }
    )
    manager._audio_playback_manager.add_audio_chunk.assert_not_awaited()
    assert "response.create" not in [e["type"] for e in events(manager)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,args",
    [
        ("shell", "{}"),
        ("get_mayhem_build", '{"champion":"ezreal","url":"https://evil"}'),
        ("get_mayhem_augments", '{"champion":"ezreal","limit":true}'),
        ("get_mayhem_augments", '{"champion":"ezreal","limit":100}'),
        ("get_mayhem_augments", '{"champion":"ezreal","augment_ids":[-1]}'),
        ("get_mayhem_build", "[]"),
        ("get_mayhem_build", "garbage"),
    ],
)
async def test_tool_arguments_fail_closed(name, args):
    service = context_service()
    result = json.loads(await LeagueToolExecutor(service).execute(name, args))
    assert result["status"] == "invalid_arguments"
    service.opgg.get_build.assert_not_called()
    service.opgg.get_augments.assert_not_called()


@pytest.mark.asyncio
async def test_openai_truncates_using_playout_not_queued_duration():
    manager = make_manager(OpenAIRealtimeManager)
    await manager._dispatch_event(
        {"type": "response.created", "response": {"id": "r1"}}
    )
    await manager._dispatch_event(
        {
            "type": "response.output_audio.delta",
            "response_id": "r1",
            "item_id": "a1",
            "delta": base64.b64encode(b"\x00" * 48000).decode(),
        }
    )
    await manager.cancel_ongoing_response()
    truncate = next(
        e for e in events(manager) if e["type"] == "conversation.item.truncate"
    )
    assert truncate["audio_end_ms"] == 20
    assert truncate["item_id"] == "a1"


def test_playout_clock_counts_only_consumed_discord_frames():
    raw = MagicMock()
    raw.read.side_effect = [b"\x00" * 3840, b"\x00" * 3840, b""]
    positions = {}
    source = _PlayoutSource(raw, "r1", positions)
    assert positions["r1"] == 0
    for _ in range(3):
        source.read()
    assert positions["r1"] == 40


@pytest.mark.asyncio
async def test_concurrent_buffered_turns_do_not_interleave_context_and_audio():
    manager = make_manager(OpenAIRealtimeManager, vad=False)
    captured = []

    async def send(event):
        captured.append(event)
        await asyncio.sleep(0)

    manager._connection_handler_inst.send_event = send
    state = MagicMock()
    state.is_active_participant.return_value = True
    coordinator = AIServiceCoordinator(state, manager._audio_playback_manager, {}, 99)
    coordinator.active_ai_service_manager = manager
    await asyncio.gather(
        coordinator.send_audio_turn(b"one", 1, "Alice"),
        coordinator.send_audio_turn(b"two", 2, "Bob"),
    )
    assert [e["type"] for e in captured] == [
        "conversation.item.create",
        "input_audio_buffer.append",
        "input_audio_buffer.commit",
        "response.create",
    ] * 2


@pytest.mark.asyncio
async def test_tool_timeout_also_covers_waiting_for_a_worker():
    executor = LeagueToolExecutor(context_service(), timeout=0.01)
    await executor._semaphore.acquire()
    await executor._semaphore.acquire()
    result = json.loads(
        await executor.execute("get_mayhem_build", '{"champion":"ezreal"}')
    )
    assert result["reason"] == "tool_timeout"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cls", [OpenAIRealtimeManager, GrokRealtimeManager], ids=["gpt", "grok"]
)
async def test_real_websocket_context_audio_tools_reply_and_reconnect(cls, monkeypatch):
    """Only loopback sockets: no Discord login, cloud models, or real keys."""
    received = []
    sockets = []
    reply_received = asyncio.Event()

    async def server(ws):
        sockets.append(ws)
        outputs = 0
        async for raw in ws:
            event = json.loads(raw)
            received.append(event)
            kind = event["type"]
            if kind == "session.update":
                await ws.send(json.dumps({"type": "session.updated"}))
            elif (
                kind == "conversation.item.create"
                and event["item"]["type"] == "function_call_output"
            ):
                outputs += 1
            elif kind == "response.create":
                if outputs == 0:
                    await ws.send(
                        json.dumps(
                            {"type": "response.created", "response": {"id": "rtool"}}
                        )
                    )
                    await ws.send(
                        json.dumps(
                            {
                                "type": "response.done",
                                "response": {
                                    "id": "rtool",
                                    "status": "completed",
                                    "output": [function_call()],
                                },
                            }
                        )
                    )
                else:
                    await ws.send(
                        json.dumps(
                            {"type": "response.created", "response": {"id": "raudio"}}
                        )
                    )
                    await ws.send(
                        json.dumps(
                            {
                                "type": "response.output_audio.delta",
                                "response_id": "raudio",
                                "item_id": "a1",
                                "delta": base64.b64encode(b"reply-pcm").decode(),
                            }
                        )
                    )
                    await ws.send(
                        json.dumps(
                            {
                                "type": "response.done",
                                "response": {
                                    "id": "raudio",
                                    "status": "completed",
                                    "output": [],
                                },
                            }
                        )
                    )

    async with serve(server, "127.0.0.1", 0) as ws_server:
        port = ws_server.sockets[0].getsockname()[1]
        monkeypatch.setattr(
            cls.connection_class, "endpoint", f"ws://127.0.0.1:{port}/realtime"
        )
        playback = MagicMock()
        playback.start_new_audio_stream = AsyncMock()
        playback.end_audio_stream = AsyncMock()

        async def accept_audio(data):
            assert data == b"reply-pcm"
            reply_received.set()

        playback.add_audio_chunk = AsyncMock(side_effect=accept_audio)
        manager = cls(playback, config(cls.provider))
        manager._context.opgg.get_build = MagicMock(
            return_value={"status": "ok", "coreBuilds": []}
        )
        connected, lost = AsyncMock(), AsyncMock()
        try:
            assert await manager.connect(connected, lost)
            assert [e["type"] for e in received] == ["session.update"]
            state = MagicMock(current_session_id=1)
            state.is_active_participant.return_value = True
            coordinator = AIServiceCoordinator(state, playback, {}, 99)
            coordinator.active_ai_service_manager = manager
            assert await coordinator.send_audio_turn(b"pcm-test", 42, "Alice")
            await asyncio.wait_for(reply_received.wait(), 3)
            kinds = [e["type"] for e in received]
            context_index = kinds.index("conversation.item.create")
            assert (
                context_index
                < kinds.index("input_audio_buffer.append")
                < kinds.index("input_audio_buffer.commit")
            )
            assert kinds.count("response.create") == 2
            manager._context.opgg.get_build.assert_called_once_with(champion="ezreal")
            epoch = manager.connection_epoch
            await sockets[0].close()
            async with asyncio.timeout(4):
                while manager.connection_epoch == epoch or not manager.is_connected():
                    await asyncio.sleep(0.01)
            assert connected.await_count == 2
            assert lost.await_count == 1
            assert len(sockets) == 2
            assert received[-1]["type"] == "session.update"
            assert received[-1]["session"]["tools"]
        finally:
            await manager.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize('team,other', [('ORDER', 'CHAOS'), ('CHAOS', 'ORDER')])
async def test_team_labels_follow_host_not_order(team, other):
    service = context_service()
    game = payload()
    game['allPlayers'][0]['team'] = team
    game['allPlayers'].append({'summonerName': 'other', 'championName': 'Lux', 'team': other, 'items': []})
    service.live.get_all_game_data.return_value = game
    await service.poll_once()
    result = json.loads(service.turn_context(1, 'speaker').split('\n', 1)[1])
    perspective = result['sharedMatch']['teamPerspective']
    assert perspective['yourTeam'] == team
    assert perspective['labels'] == {team: '你们这边', other: '对面'}
