from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from src.lol_mcp.live_client import LeagueLiveClient
from src.lol_mcp.opgg import (
    BASE_URL,
    OpggMayhemClient,
    OpggUnavailable,
    champion_slug,
    parse_augments,
    parse_build,
)
from src.lol_mcp.server import create_mcp_server


def metadata(section: str, champion: str = "samira") -> str:
    return (
        f'<link rel="canonical" href="{BASE_URL}/{champion}/{section}"/>'
        '<meta name="description" content="Mayhem builds in Patch 16.17."/>'
    )


def augment_page() -> str:
    rows = [
        {
            "id": 1000 + tier,
            "tier": tier,
            "rarity": 4,
            "key": f"ARAM_Test{tier}",
            "name": f"Test {tier}",
            "performance": 150.1,
            "popular": 0.1,
            "desc": "<b>Damage</b><br/>10 &amp; more",
        }
        for tier in range(6)
    ]
    flight = "5b:" + json.dumps(["$", "$L5c", None, {"data": rows}]) + "\n"
    # Flight chunks can split inside an object or an escaped string.
    chunks = [flight[:91], flight[91:]]
    return metadata("augments") + "".join(
        f"<script>self.__next_f.push({json.dumps([1, chunk])})</script>"
        for chunk in chunks
    )


def build_page() -> str:
    return metadata("build") + "".join(
        f"<table><thead><tr><th>{title}</th></tr></thead><tbody><tr><td>"
        '<div class="relative"><img alt="Health Potion" src="https://static.example/item/2003.png?q=1"/>'
        '<div class="absolute bottom-0">2</div></div>'
        '<div><img alt="Test Item" src="https://static.example/item/1001.png"/></div>'
        "</td></tr></tbody></table>"
        for title in ("Starter items", "Boots", "Core builds")
    )


def test_all_tiers_survive_and_no_win_rate_is_invented() -> None:
    data = parse_augments(augment_page(), "samira")
    assert data["patch"] == "16.17"
    assert [row["tier"] for row in data["augments"]] == list(range(6))
    assert data["augments"][0]["performance"] == 150.1
    assert data["augments"][0]["description"] == "Damage 10 & more"
    assert "winRate" not in json.dumps(data)


@pytest.mark.parametrize(
    "page",
    [
        "<html>Rate limited</html>",
        augment_page().replace("aram-mayhem/samira/augments", "aram/samira/augments"),
        augment_page().replace("Patch 16.17", "Unknown"),
        metadata("augments") + "<script>alert('not executable data')</script>",
    ],
)
def test_schema_errors_and_wrong_mode_fail_closed(page: str) -> None:
    with pytest.raises(OpggUnavailable):
        parse_augments(page, "samira")


@pytest.mark.parametrize(
    "name", ["../samira", "https://example.com", "samira?mode=aram", "", "360"]
)
def test_champion_input_cannot_inject_urls_or_cache_paths(name: str) -> None:
    with pytest.raises(ValueError):
        champion_slug(name)


def test_english_name_normalization() -> None:
    assert champion_slug("Kai'Sa") == "kaisa"
    assert champion_slug("Dr. Mundo") == "drmundo"


@pytest.mark.parametrize(
    "extra", [{}, {"rarity": None, "key": None, "name": None, "desc": None}]
)
def test_null_upstream_metadata_is_preserved_not_dropped(extra: dict) -> None:
    row = {"id": 1032, "tier": 5, "performance": 100.0, "popular": 0.0, **extra}
    flight = json.dumps({"data": [row]})
    page = (
        metadata("augments")
        + f"<script>self.__next_f.push({json.dumps([1, flight])})</script>"
    )
    data = parse_augments(page, "samira")
    assert data["augments"][0]["id"] == 1032
    assert data["augments"][0]["rarity"] is None
    client = OpggMayhemClient(cache_dir=None, fetch=lambda _: page)
    assert client.get_augments("samira", query="missing")["totalMatched"] == 0


def test_build_sections_and_quantities() -> None:
    data = parse_build(build_page(), "samira")
    assert data["starterItems"][0][0] == {
        "id": 2003,
        "name": "Health Potion",
        "count": 2,
    }
    assert data["coreBuilds"][0][1]["count"] == 1
    with pytest.raises(OpggUnavailable):
        parse_build(build_page().replace("Core builds", "Unknown"), "samira")


def test_cache_survives_restart_and_coalesces_concurrent_calls(tmp_path: Path) -> None:
    calls = []

    def fetch(url: str) -> str:
        calls.append(url)
        return augment_page()

    client = OpggMayhemClient(cache_dir=tmp_path, fetch=fetch)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: client.get_augments("samira"), range(8)))
    assert len(calls) == 1
    assert sum(not result["cache"]["hit"] for result in results) == 1
    restarted = OpggMayhemClient(cache_dir=tmp_path, fetch=fetch)
    assert restarted.get_augments("samira")["cache"]["hit"]
    assert len(calls) == 1


def test_filter_pagination_missing_ids_and_independent_results() -> None:
    client = OpggMayhemClient(cache_dir=None, fetch=lambda _: augment_page())
    first = client.get_augments("samira", limit=2)
    assert first["totalAvailable"] == 6
    assert first["nextOffset"] == 2
    assert "description" not in first["augments"][0]
    assert [row["sourceOrder"] for row in first["augments"]] == [1, 2]
    assert (
        client.get_augments("samira", offset=2, limit=1)["augments"][0]["sourceOrder"]
        == 3
    )
    assert (
        client.get_augments("samira", query="test 5")["augments"][0]["sourceOrder"] == 6
    )
    assert "sourceOrder" not in client._cache["samira-augments"]["data"]["augments"][0]
    assert client.get_augments("samira", offset=2, limit=4)["nextOffset"] is None
    subset = client.get_augments(
        "samira", augment_ids=[1000, 9999], include_descriptions=True
    )
    assert subset["missingIds"] == [9999]
    assert subset["augments"][0]["tier"] == 0
    subset["augments"][0]["name"] = "mutated"
    query = client.get_augments("samira", query="test 0", include_descriptions=True)
    assert query["augments"][0]["name"] == "Test 0"
    assert query["augments"][0]["description"]
    assert client.get_augments("samira", augment_ids=[])["totalMatched"] == 0
    with pytest.raises(ValueError):
        client.get_augments("samira", limit=201)


def test_stale_fallback_backoff_and_expiry() -> None:
    now = [1000.0]
    calls = []

    def fetch(url: str) -> str:
        calls.append(url)
        if len(calls) > 1:
            raise OSError("network failed")
        return augment_page()

    client = OpggMayhemClient(
        cache_dir=None,
        ttl_seconds=10,
        max_stale_seconds=100,
        fetch=fetch,
        clock=lambda: now[0],
    )
    assert not client.get_augments("samira")["cache"]["stale"]
    now[0] += 11
    stale = client.get_augments("samira")
    assert stale["cache"]["stale"]
    assert stale["cache"]["refreshError"] == "fetch_failed"
    assert stale["source"]["patch"] == "16.17"
    assert client.get_augments("samira")["cache"]["stale"]
    assert len(calls) == 2
    now[0] += 100
    assert client.get_augments("samira")["status"] == "unavailable"
    assert len(calls) == 3


def test_invalid_cache_and_network_error_are_reported_without_aram_fallback(
    tmp_path: Path,
) -> None:
    (tmp_path / "samira-augments.json").write_text("[broken", encoding="utf-8")
    calls = []

    def fetch(url: str) -> str:
        calls.append(url)
        raise OSError("do not expose internal exception details")

    client = OpggMayhemClient(cache_dir=tmp_path, fetch=fetch)
    result = client.get_augments("samira")
    assert result["status"] == "unavailable"
    assert result["reason"] == "fetch_failed"
    assert calls == [f"{BASE_URL}/samira/augments"]
    assert "internal exception" not in json.dumps(result)


def test_mcp_protocol_lists_readonly_tools_and_serves_repaired_data() -> None:
    async def run() -> None:
        client = OpggMayhemClient(
            cache_dir=None,
            fetch=lambda url: (
                augment_page() if url.endswith("augments") else build_page()
            ),
        )
        server = create_mcp_server(LeagueLiveClient(), client)
        async with create_connected_server_and_client_session(server) as session:
            listed = await session.list_tools()
            tools = {tool.name: tool for tool in listed.tools}
            assert 'get_mayhem_augments' not in tools
            for name in ('identify_mayhem_augment', 'compare_mayhem_choices', 'get_mayhem_build'):
                assert tools[name].annotations.readOnlyHint
            result = await session.call_tool('get_mayhem_build', {'champion':'samira'})
            assert result.structuredContent['status'] == 'ok'
            result = await session.call_tool('identify_mayhem_augment', {'champion':'samira','rarity':'prismatic'})
            assert not result.isError
            assert all('tier' not in row and 'performance' not in row for row in result.structuredContent['augments'])

    asyncio.run(run())


def test_rarity_filter_and_performance_sort_before_pagination(monkeypatch):
    client = OpggMayhemClient(cache_dir=None)
    rows = [dict(id=i, name=str(i), key=str(i), rarity=rarity, tier=5,
                 performance=performance, popular=1) for i, rarity, performance in
            [(1, 1, 99), (2, 8, 20), (3, 4, 100), (4, 8, 90), (5, None, 200)]]
    import copy
    monkeypatch.setattr(client, '_get', lambda *a: {'status': 'ok', 'augments': copy.deepcopy(rows)})
    result = client.get_augments('Aurora', rarity='prismatic', sort_by='performance', limit=1)
    assert result['totalMatched'] == 2
    assert result['augments'][0]['id'] == 4
    assert result['augments'][0]['sourceOrder'] == 4
    assert client.get_augments('Aurora', rarity='prismatic', sort_by='performance', limit=1, offset=1)['augments'][0]['id'] == 2
    assert client.get_augments('Aurora', rarity='silver', augment_ids=[4])['augments'] == []
    with pytest.raises(ValueError):
        client.get_augments('Aurora', rarity='T1')


def test_popular_filter_blocks_zero_usage_170_and_unnamed_rows(monkeypatch):
    import copy
    client = OpggMayhemClient(cache_dir=None)
    rows = [dict(id=1, name='Old', key='Old', rarity=8, tier=5, performance=170, popular=0),
            dict(id=2, name=None, key=None, rarity=8, tier=5, performance=170, popular=1),
            dict(id=3, name='Useful', key='Useful', rarity=8, tier=1, performance=85, popular=.5),
            dict(id=4, name='Rare', key='Rare', rarity=8, tier=1, performance=100, popular=.01)]
    monkeypatch.setattr(client, '_get', lambda *a: {'status':'ok', 'augments':copy.deepcopy(rows)})
    result = client.get_augments('mordekaiser', sort_by='performance', min_popular=.1, limit=1)
    assert [r['id'] for r in result['augments']] == [3]
    assert result['excludedByQuality'] == 3
    historical = client.get_augments('mordekaiser', query='Old')
    assert historical['augments'][0]['recommendationWarning']
    assert historical['augments'][0]['availability'] == 'unverified'
    assert client.get_augments('mordekaiser', sort_by='performance', min_popular=0)['augments'][0]['id'] == 4
    for value in [float('nan'), float('inf'), -1, True]:
        with pytest.raises(ValueError):
            client.get_augments('mordekaiser', min_popular=value)
