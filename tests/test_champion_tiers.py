"""Champion ratings stay distinct from augment tiers across API and MCP."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from scripts.eval_voice_questions import route_check
from src.lol_mcp.context import GameContextService
from src.lol_mcp.live_client import LeagueLiveClient
from src.lol_mcp.opgg import BASE_URL, OpggMayhemClient, OpggUnavailable, parse_champion_tiers
from src.lol_mcp.server import create_mcp_server
from src.lol_mcp.tools import LeagueToolExecutor


def champion_page(rows=None, patch="16.17"):
    if rows is None:
        rows = [
            {"key": "ambessa", "name": "Ambessa", "champion_id": 799, "tier": 3, "rank": 51},
            {"key": "samira", "name": "Samira", "champion_id": 360, "tier": 0, "rank": 1},
        ]
    flight = json.dumps({"patch": patch, "champions": rows})
    # Nested augment tables use the same key; those must not be selected.
    flight += json.dumps({"data": [{"id": 2095, "tier": 0, "champions": [
        {"id": 799, "tier": 0, "performance": 108.79, "popular": 1.59},
    ]}]})
    return f'<link rel="canonical" href="{BASE_URL}"/>' + "".join(
        f"<script>self.__next_f.push({json.dumps([1, chunk])})</script>"
        for chunk in [flight[:73], flight[73:]]
    )


def test_champion_tier_never_comes_from_augment_table():
    result = parse_champion_tiers(champion_page())
    assert result["patch"] == "16.17"
    ambessa, samira = result["champions"]
    assert ambessa["championTier"] == 3
    assert ambessa["tierLabel"] == "T3"
    assert ambessa["championRank"] == 51
    assert samira["tierLabel"] == "OP"
    assert "performance" not in ambessa
    assert "winRate" not in json.dumps(result)


def test_catalog_cache_is_shared_across_champions_and_restart(tmp_path):
    calls = []
    def fetch(url):
        calls.append(url)
        return champion_page()
    client = OpggMayhemClient(cache_dir=tmp_path, fetch=fetch)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(client.get_champion_tier, ["Ambessa", "Samira"] * 2))
    assert calls == [BASE_URL]
    assert results[0]["champion"] == "ambessa"
    assert results[1]["champion"] == "samira"
    assert results[0]["tierType"] == "champion"
    assert results[0]["totalChampions"] == 2
    assert "champions" not in results[0]
    restarted = OpggMayhemClient(cache_dir=tmp_path, fetch=fetch)
    assert restarted.get_champion_tier("Ambessa")["cache"]["hit"]
    assert calls == [BASE_URL]
    results[0]["championTier"] = 5
    assert restarted.get_champion_tier("Ambessa")["championTier"] == 3


def test_unknown_champion_and_unrated_rows_are_not_fabricated():
    client = OpggMayhemClient(cache_dir=None, fetch=lambda _: champion_page())
    assert client.get_champion_tier("unknown")["status"] == "not_found"
    row = {"key": "ambessa", "name": "Ambessa", "champion_id": 799, "tier": None, "rank": None}
    data = parse_champion_tiers(champion_page([row]))
    assert data["champions"][0]["tierLabel"] is None


@pytest.mark.parametrize("replacement", [
    {"tier": True}, {"tier": 6}, {"rank": 0}, {"rank": True}, {"key": "../evil"},
    {"champion_id": False}, {"name": None},
])
def test_schema_drift_fails_closed(replacement):
    row = {"key": "ambessa", "name": "Ambessa", "champion_id": 799, "tier": 3, "rank": 51}
    with pytest.raises(OpggUnavailable):
        parse_champion_tiers(champion_page([{**row, **replacement}]))


@pytest.mark.parametrize("page", [
    champion_page().replace(BASE_URL, "https://op.gg/lol/modes/aram"),
    champion_page(patch="unknown"),
    champion_page(rows=[]),
])
def test_no_fallback_to_normal_aram_or_missing_metadata(page):
    with pytest.raises(OpggUnavailable):
        parse_champion_tiers(page)


def test_stale_rating_is_flagged_and_expired_rating_is_unavailable():
    now = [100]
    fetch = MagicMock(side_effect=[champion_page(), OSError("offline"), OSError("offline")])
    client = OpggMayhemClient(cache_dir=None, fetch=fetch, clock=lambda: now[0],
                              ttl_seconds=10, max_stale_seconds=80)
    assert not client.get_champion_tier("Ambessa")["cache"]["stale"]
    now[0] += 11
    assert client.get_champion_tier("Ambessa")["cache"]["stale"]
    now[0] += 81
    assert client.get_champion_tier("Ambessa")["status"] == "unavailable"


@pytest.mark.asyncio
async def test_voice_executor_routes_champion_rating_not_build_or_augment():
    opgg = MagicMock()
    opgg.get_champion_tier.return_value = {"status": "ok", "championTier": 3}
    executor = LeagueToolExecutor(GameContextService(enabled=False, opgg=opgg))
    result = json.loads(await executor.execute("get_mayhem_champion_tier", '{"champion":"Ambessa"}'))
    assert result["championTier"] == 3
    opgg.get_champion_tier.assert_called_once_with(champion="Ambessa")
    opgg.get_build.assert_not_called()
    opgg.get_augments.assert_not_called()
    for args in ['{}', '{"champion":"Ambessa","tier":3}', '{"champion":false}']:
        assert json.loads(await executor.execute("get_mayhem_champion_tier", args))["status"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_mcp_protocol_exposes_and_executes_champion_tier():
    client = OpggMayhemClient(cache_dir=None, fetch=lambda _: champion_page())
    server = create_mcp_server(LeagueLiveClient(), client)
    async with create_connected_server_and_client_session(server) as session:
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        assert tools["get_mayhem_champion_tier"].annotations.readOnlyHint
        result = await session.call_tool("get_mayhem_champion_tier", {"champion": "Ambessa"})
        assert not result.isError
        assert result.structuredContent["championTier"] == 3


def test_eval_detects_accidental_augment_route():
    case = {"route": "champion_tier"}
    assert route_check(case, [{"name": "get_mayhem_champion_tier"}])
    assert not route_check(case, [{"name": "get_mayhem_augments"}])
