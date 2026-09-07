import copy

from src.lol_mcp.opgg import OpggMayhemClient


def test_rank_uses_full_champion_pool_before_filter_and_handles_ties(monkeypatch):
    rows = [
        {'id': 1, 'name': 'A', 'key': 'a', 'performance': 80, 'popular': 1, 'rarity': 8},
        {'id': 2, 'name': 'B', 'key': 'b', 'performance': 90, 'popular': 1, 'rarity': 4},
        {'id': 3, 'name': 'C', 'key': 'c', 'performance': 80, 'popular': 1, 'rarity': 8},
        {'id': 4, 'name': 'Removed', 'key': 'd', 'performance': 100, 'popular': 0, 'rarity': 8},
        {'id': 5, 'name': 'Invalid', 'key': 'e', 'performance': None, 'popular': 1, 'rarity': 8},
    ]
    client = OpggMayhemClient(cache_dir=None)
    monkeypatch.setattr(client, '_get', lambda *_: {'status': 'ok', 'augments': copy.deepcopy(rows)})
    a = client.get_augments('vi', query='A', rarity='prismatic', limit=1)['augments'][0]
    assert a['performanceRank'] == 1  # Gold B must not outrank prismatic A.
    assert a['rankedAugmentCount'] == 2
    assert a['rankTiedCount'] == 2
    assert a['sourceOrder'] == 1  # Source order must not become the rank.
    assert a['performance'] == 80
    assert 'performanceRank' not in rows[0]
    removed = client.get_augments('vi', query='Removed')['augments'][0]
    assert 'performanceRank' not in removed
    assert removed['performance'] == 100  # Exact historical lookup preserved.
