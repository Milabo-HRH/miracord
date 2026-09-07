import copy
import json
from types import SimpleNamespace

import pytest

from src.lol_mcp.arammeta import AramMetaClient
from src.lol_mcp.name_catalog import NameCatalog
from src.lol_mcp.tools import LeagueToolExecutor


def resources():
    return {
        "tier-list.json": {"patch_prefix": "16.17", "detailVersion": "test",
            "champs": {"804": {"alias": "Yunara", "name_en": "Yunara", "top": {"kGold": [{"id": 1129, "g": 30, "wr": .51}]}}},
            "augs": {"1129": {"name_en": "Marksmage", "name_zh": "射手法師"}},
            "itemLut": {"3072": {"e": "Bloodthirster", "z": "嗜血者", "de": "Effect"},
                        "3153": {"e": "Blade of The Ruined King"}, "126697": {"e": "Hubris"}}},
        "names-zh-cn.json": {"ver": "16.13.1", "champs": {"804": "芸阿娜"},
            "items": {"3072": "饮血剑", "3153": "破败王者之刃", "126697": "狂妄"},
            "itemDescs": {"3072": "效果"}, "augs": {"1129": "神射法师"}},
        "champions/804.json": {"singleItems": {"top": [{"slug": "3072", "g": 100, "wr": .55},
                                                             {"slug": "3153", "g": 200, "wr": .53}]},
                                "bot": {"kGold": [{"id": 1129, "g": 30, "wr": .51}]}}
    }


def client(tmp_path=None):
    data = resources()
    return AramMetaClient(cache_dir=tmp_path, fetch=lambda url: copy.deepcopy(data[url.split('/docs/api/')[1]]))


def test_item_alias_identity_and_namespaced_mayhem_id():
    c = client()
    assert c.lookup_item("饮血")["items"][0]["nameEn"] == "Bloodthirster"
    assert c.lookup_item("狂妄")["items"][0]["id"] == 126697
    assert c.lookup_item("../../secret")["status"] == "not_found"
    assert c.lookup_item("")["status"] == "invalid_arguments"


def test_stats_compare_same_source_preserve_samples_versions_and_absence():
    c = client()
    r = c.get_stats("芸阿娜", "item", ids=[3153, 3072])
    assert [x["entity"]["id"] for x in r["records"]] == [3072, 3153]
    assert r["records"][0]["metrics"] == {"g": 100, "wr": .55}
    assert r["source"]["localizationVersion"] != r["source"]["patch"]
    absent = c.get_stats("Yunara", "item", query="狂妄")
    assert absent["reason"] == "no_published_stats"
    assert absent["knownEntities"][0]["nameEn"] == "Hubris"
    assert c.get_stats("Yunara", "item", ids=[])["records"] == []
    assert c.get_stats("wrong", "item")["reason"] == "champion_name_unresolved"


def test_augment_top_bottom_duplicates_are_not_double_counted():
    r = client().get_stats("804", "augment", ids=[1129])
    assert r["totalMatched"] == 1
    assert r["records"][0]["metrics"]["g"] == 30


def test_invalid_win_rate_is_not_returned_as_evidence():
    data = resources()
    data["champions/804.json"]["singleItems"]["top"][0]["wr"] = float("nan")
    c = AramMetaClient(cache_dir=None, fetch=lambda url: data[url.split('/docs/api/')[1]])
    assert c.get_stats("804", "item", "饮血")["status"] == "unavailable"


def test_cache_is_pinned_and_transport_errors_are_not_not_found(tmp_path):
    c = client(tmp_path)
    c.lookup_item("饮血")
    def forbidden(url):
        raise AssertionError("No network on pinned cache hit")
    cached = AramMetaClient(cache_dir=tmp_path, fetch=forbidden)
    assert cached.lookup_item("饮血")["status"] == "ok"
    missing = AramMetaClient(cache_dir=None, fetch=forbidden)
    assert missing.lookup_item("饮血")["status"] == "unavailable"


@pytest.mark.asyncio
async def test_real_tool_dispatch_and_validation():
    context = SimpleNamespace(arammeta=client(), names=NameCatalog(data={"champions": [], "augments": [], "metadata": {}}))
    executor = LeagueToolExecutor(context)
    r = json.loads(await executor.execute("get_arammeta_stats", '{"champion":"芸阿娜","kind":"item","query":"饮血"}'))
    assert r["records"][0]["entity"]["id"] == 3072
    r = json.loads(await executor.execute("lookup_game_item", '{"query":"狂妄"}'))
    assert r["items"][0]["nameEn"] == "Hubris"
    assert json.loads(await executor.execute("get_arammeta_stats", '{"champion":"804","kind":"item","limit":1000}'))["status"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_mcp_protocol_exposes_and_executes_new_tools():
    from mcp.shared.memory import create_connected_server_and_client_session
    from src.lol_mcp.server import create_mcp_server
    from src.lol_mcp.live_client import LeagueLiveClient
    server = create_mcp_server(LeagueLiveClient(), arammeta_client=client())
    async with create_connected_server_and_client_session(server) as session:
        tools = await session.list_tools()
        assert {"lookup_game_item", "get_arammeta_stats"} <= {t.name for t in tools.tools}
        r = await session.call_tool("lookup_game_item", {"query": "饮血"})
        assert not r.isError
        assert "Bloodthirster" in str(r.content)


def test_augment_rarity_filters_before_limit():
    data = resources()
    keys = list(data['tier-list.json']['augs'])
    for i, key in enumerate(keys):
        data['tier-list.json']['augs'][key]['rarity'] = 'kPrismatic' if i == 0 else 'kGold'
    c = AramMetaClient(cache_dir=None, fetch=lambda url: copy.deepcopy(data[url.split('/docs/api/')[1]]))
    r = c.get_stats('Yunara', 'augment', rarity='prismatic')
    assert r['status'] == 'ok'
    assert all(x['entity']['rarity'] == 'prismatic' for x in r['records'])
    assert c.get_stats('Yunara', 'item', rarity='gold')['status'] == 'invalid_arguments'


def test_overall_winrate_and_localization_object_are_not_item_stats():
    data = resources()
    data['tier-list.json']['champs']['804'].update(g=1000, wr=.49, rawWr=.48)
    data['names-zh-cn.json']['augs']['1129'] = {'n':'神射法师', 'd':'普攻效果'}
    c = AramMetaClient(cache_dir=None, fetch=lambda url: copy.deepcopy(data[url.split('/docs/api/')[1]]))
    result = c.get_stats('Yunara', 'champion')
    assert result['winRatePercent'] == 48
    assert result['winRateField'] == 'rawWr'
    assert 'records' not in result
    assert c.get_stats('Yunara', 'champion', ids=[3072])['status'] == 'invalid_arguments'
    row = c.get_stats('Yunara', 'augment', ids=[1129,2001])
    assert row['missingIds'] == [2001]
    assert row['records'][0]['entity']['nameZh'] == '神射法师'
