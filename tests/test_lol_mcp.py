from __future__ import annotations

import asyncio
import json
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_connected_server_and_client_session

from src.lol_mcp.live_client import LeagueLiveClient, LiveClientUnavailable
from src.lol_mcp.server import create_mcp_server
from src.lol_mcp.summary import (
    build_live_game_events,
    build_live_game_state,
    parse_augments,
)

PRIVATE_ONE = "A Private Player#NA1"
PRIVATE_TWO = "Another Private Player#NA2"


def mayhem_payload() -> dict[str, Any]:
    return {
        "activePlayer": {
            "summonerName": PRIVATE_ONE,
            "riotId": PRIVATE_ONE,
            "level": 14,
            "currentGold": 725.0,
            "abilities": {
                "augment": {
                    "displayName": "Spin Me Right Round",
                    "rawDisplayName": (
                        "GeneratedTip_Spell_Augment_YouSpinMeRightRound3_DisplayName"
                    ),
                    "rawDescription": (
                        "GeneratedTip_Spell_Augment_YouSpinMeRightRound3_Description"
                    ),
                }
            },
        },
        "gameData": {
            "gameMode": "KIWI",
            "gameTime": 456.78,
            "mapName": "Map12",
            "mapNumber": 12,
            "mapTerrain": "Default",
        },
        "allPlayers": [
            {
                "summonerName": PRIVATE_ONE,
                "riotId": PRIVATE_ONE,
                "riotIdGameName": "A Private Player",
                "riotIdTagLine": "NA1",
                "championName": "Ezreal",
                "team": "ORDER",
                "level": 14,
                "isBot": False,
                "isDead": False,
                "scores": {
                    "kills": 5,
                    "deaths": 2,
                    "assists": 11,
                    "creepScore": 34,
                },
                "items": [
                    {
                        "itemID": 3078,
                        "displayName": "Trinity Force",
                        "count": 1,
                        "slot": 0,
                    }
                ],
                "summonerSpells": {
                    "summonerSpellOne": {
                        "displayName": "Spin Me Right Round",
                        "rawDisplayName": (
                            "GeneratedTip_Spell_Augment_YouSpinMeRightRound3_DisplayName"
                        ),
                        "rawDescription": (
                            "GeneratedTip_Spell_Augment_YouSpinMeRightRound3_Description"
                        ),
                    },
                    "summonerSpellTwo": {
                        "displayName": "Mark",
                        "rawDisplayName": (
                            "GeneratedTip_SummonerSpell_SummonerSnowball_DisplayName"
                        ),
                    },
                },
                "runes": {},
            },
            {
                "summonerName": PRIVATE_TWO,
                "riotId": PRIVATE_TWO,
                "championName": "Garen",
                "team": "CHAOS",
                "level": 13,
                "isBot": False,
                "isDead": True,
                "respawnTimer": 8.4,
                "scores": {"kills": 2, "deaths": 5, "assists": 7},
                "items": [],
                "summonerSpells": {},
                "runes": {},
            },
        ],
        "events": {
            "Events": [
                {
                    "EventID": 4,
                    "EventName": "ChampionKill",
                    "EventTime": 450.25,
                    "KillerName": PRIVATE_ONE,
                    "VictimName": PRIVATE_TWO,
                    "Assisters": [PRIVATE_ONE, "Unknown Private Player"],
                }
            ]
        },
    }


@contextmanager
def mock_live_client(
    payload: dict[str, Any] | None,
) -> Iterator[tuple[str, dict[str, int]]]:
    calls: dict[str, int] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            calls[self.path] = calls.get(self.path, 0) + 1
            if payload is None:
                self.send_error(HTTPStatus.SERVICE_UNAVAILABLE)
                return
            if self.path == "/liveclientdata/allgamedata":
                self.send_payload(payload)
                return
            if self.path == "/liveclientdata/gamestats":
                self.send_payload(payload["gameData"])
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def send_payload(self, value: Any) -> None:
            body = json.dumps(value).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_build_state_detects_mayhem_and_augment_stage() -> None:
    state = build_live_game_state(mayhem_payload())

    assert state["game"] == {
        "mode": "KIWI",
        "mapName": "Map12",
        "mapNumber": 12,
        "mapTerrain": "Default",
        "gameTimeSeconds": 456.8,
        "isAramMayhem": True,
    }
    assert state["activePlayer"]["slot"] == "order-01"
    assert state["players"][0]["mayhemAugments"] == [
        {
            "name": "Spin Me Right Round",
            "internalId": "YouSpinMeRightRound",
            "stage": 3,
        }
    ]
    assert state["players"][0]["summonerSpells"] == ["Mark"]


def test_parse_augments_understands_observed_upgrade_stages() -> None:
    values = [
        {
            "displayName": "Spin Me Right Round",
            "rawDisplayName": (
                f"GeneratedTip_Spell_Augment_YouSpinMeRightRound{suffix}_DisplayName"
            ),
        }
        for suffix in ("", "2", "3")
    ]

    assert [augment["stage"] for augment in parse_augments(values)] == [1, 2, 3]
    assert {augment["internalId"] for augment in parse_augments(values)} == {
        "YouSpinMeRightRound"
    }


def test_state_and_events_do_not_expose_player_identifiers() -> None:
    state = build_live_game_state(mayhem_payload())
    events = build_live_game_events(mayhem_payload())
    rendered = json.dumps({"state": state, "events": events})

    assert PRIVATE_ONE not in rendered
    assert PRIVATE_TWO not in rendered
    assert "A Private Player" not in rendered
    assert "Another Private Player" not in rendered
    assert "Unknown Private Player" not in rendered
    assert state["recentEvents"][0]["killerSlot"] == "order-01"
    assert state["recentEvents"][0]["victimSlot"] == "chaos-01"
    assert state["recentEvents"][0]["assisterSlots"] == ["order-01"]


def test_live_client_requires_loopback_and_calls_all_game_data() -> None:
    with pytest.raises(ValueError, match="loopback"):
        LeagueLiveClient("https://example.com:2999")

    with mock_live_client(mayhem_payload()) as (base_url, calls):
        payload = LeagueLiveClient(base_url).get_all_game_data()

    assert payload["gameData"]["gameMode"] == "KIWI"
    assert calls == {"/liveclientdata/allgamedata": 1}


def test_live_client_uses_stable_unavailable_exception() -> None:
    with (
        mock_live_client(None) as (base_url, _),
        pytest.raises(LiveClientUnavailable) as caught,
    ):
        LeagueLiveClient(base_url).get_all_game_data()

    assert str(caught.value) == ""


def test_live_client_reads_only_latest_captured_all_game_data(tmp_path: Path) -> None:
    latest = tmp_path / "latest.json"
    latest.write_text(
        json.dumps(
            {
                "capturedAt": "2026-09-02T23:14:12Z",
                "sharedEndpoints": {
                    "allgamedata": {"ok": True, "data": mayhem_payload()}
                },
            }
        ),
        encoding="utf-8",
    )
    client = LeagueLiveClient(
        "http://127.0.0.1:1",
        latest_snapshot_path=latest,
    )

    payload, captured_at = client.get_latest_captured_game_data()

    assert payload["gameData"]["gameMode"] == "KIWI"
    assert captured_at == "2026-09-02T23:14:12Z"


def _tool_payload(result: Any) -> dict[str, Any]:
    if isinstance(result.structuredContent, dict):
        structured = result.structuredContent
        if isinstance(structured.get("result"), dict):
            return structured["result"]
        return structured
    for content in result.content:
        text = getattr(content, "text", None)
        if isinstance(text, str):
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
    raise AssertionError("MCP tool did not return an object payload")


async def _call_mcp_tools(base_url: str) -> tuple[Any, Any, Any]:
    server = create_mcp_server(LeagueLiveClient(base_url, timeout=1))
    async with create_connected_server_and_client_session(server) as session:
        tools = await session.list_tools()
        tool_names = {tool.name for tool in tools.tools}
        assert tool_names == {
            "lookup_game_item",
            "get_arammeta_stats",
            "get_mayhem_team_comparison",
            "get_mayhem_champion_tier",
            "get_mayhem_build",
            "identify_mayhem_augment",
            "compare_mayhem_choices",
            "get_live_game_state",
            "get_live_game_events",
            "get_live_game_status",
        }
        state = await session.call_tool("get_live_game_state")
        events = await session.call_tool("get_live_game_events", {"limit": 1})
        status = await session.call_tool("get_live_game_status")
        return state, events, status


def test_mcp_protocol_e2e_calls_tools_against_mock_live_client() -> None:
    with mock_live_client(mayhem_payload()) as (base_url, calls):
        state_result, events_result, status_result = asyncio.run(
            _call_mcp_tools(base_url)
        )

    assert not state_result.isError
    assert not events_result.isError
    assert not status_result.isError
    state = _tool_payload(state_result)
    events = _tool_payload(events_result)
    status = _tool_payload(status_result)
    assert state["game"]["isAramMayhem"] is True
    assert state["players"][0]["mayhemAugments"][0]["stage"] == 3
    assert events["events"][0]["name"] == "ChampionKill"
    assert status["status"] == "in_game"
    assert calls["/liveclientdata/allgamedata"] == 2
    assert calls["/liveclientdata/gamestats"] == 1
    assert PRIVATE_ONE not in json.dumps({"state": state, "events": events})


async def _call_stdio_server(base_url: str) -> Any:
    script = Path(__file__).resolve().parents[1] / "scripts" / "run_lol_mcp.py"
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[str(script), "--api-base-url", base_url, "--timeout", "1"],
    )
    async with (
        stdio_client(parameters) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        tools = await session.list_tools()
        assert {tool.name for tool in tools.tools} == {
            "lookup_game_item",
            "get_arammeta_stats",
            "get_mayhem_team_comparison",
            "get_mayhem_champion_tier",
            "get_mayhem_build",
            "identify_mayhem_augment",
            "compare_mayhem_choices",
            "get_live_game_state",
            "get_live_game_events",
            "get_live_game_status",
        }
        return await session.call_tool("get_live_game_state")


def test_stdio_entrypoint_e2e_starts_and_serves_a_tool() -> None:
    with mock_live_client(mayhem_payload()) as (base_url, calls):
        result = asyncio.run(_call_stdio_server(base_url))

    assert not result.isError
    payload = _tool_payload(result)
    assert payload["game"]["isAramMayhem"] is True
    assert payload["players"][0]["mayhemAugments"][0]["stage"] == 3
    assert calls == {"/liveclientdata/allgamedata": 1}


async def _call_unavailable_state(base_url: str) -> Any:
    server = create_mcp_server(LeagueLiveClient(base_url))
    async with create_connected_server_and_client_session(server) as session:
        return await session.call_tool("get_live_game_state")


def test_mcp_e2e_returns_stable_not_in_game_result() -> None:
    with mock_live_client(None) as (base_url, calls):
        result = asyncio.run(_call_unavailable_state(base_url))

    assert not result.isError
    assert _tool_payload(result) == {
        "status": "not_in_game",
        "available": False,
        "reason": "live_client_unavailable",
        "message": (
            "No active League match is exposing Live Client Data. "
            "Start or enter a match, then try again."
        ),
    }
    assert calls == {"/liveclientdata/allgamedata": 1}


async def _call_cached_state(base_url: str, latest: Path) -> Any:
    server = create_mcp_server(
        LeagueLiveClient(base_url, latest_snapshot_path=latest)
    )
    async with create_connected_server_and_client_session(server) as session:
        return await session.call_tool("get_live_game_state")


def test_mcp_returns_only_latest_capture_when_game_is_unavailable(
    tmp_path: Path,
) -> None:
    latest = tmp_path / "latest.json"
    latest.write_text(
        json.dumps(
            {
                "capturedAt": "2026-09-02T23:14:12Z",
                "sharedEndpoints": {
                    "allgamedata": {"ok": True, "data": mayhem_payload()}
                },
            }
        ),
        encoding="utf-8",
    )
    with mock_live_client(None) as (base_url, calls):
        result = asyncio.run(_call_cached_state(base_url, latest))

    assert not result.isError
    payload = _tool_payload(result)
    assert payload["game"]["isAramMayhem"] is True
    assert payload["snapshot"] == {
        "kind": "last_captured",
        "capturedAt": "2026-09-02T23:14:12Z",
    }
    assert calls == {"/liveclientdata/allgamedata": 1}
