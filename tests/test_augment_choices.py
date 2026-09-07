import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.lol_mcp.augment_choices import compare, identify
from src.lol_mcp.tools import LeagueToolExecutor
from src.lol_mcp.name_catalog import NameCatalog


@pytest.mark.asyncio
@pytest.mark.parametrize('rarity', [None, 'all'])
async def test_unknown_color_returns_complete_multilingual_catalog_in_one_call(rarity):
    executor = fake_executor()
    executor.context.names.resolve_augment.side_effect = lambda _: {'status': 'not_found'}
    rows = [{'id': i, 'nameEn': f'Augment {i}', 'rarity': [1, 4, 8][i % 3],
             'namesByLocale': {'en_US': f'Augment {i}', 'zh_CN': f'强化{i}', 'zh_TW': f'強化{i}'},
             'performance': 99, 'performanceRank': 1} for i in range(135)]
    rows[-1]['namesByLocale']['zh_CN'] = '升级：中娅'
    executor.execute = AsyncMock(return_value=json.dumps({'status': 'ok', 'augments': rows}))
    result = await identify(executor, 'aurora', rarity, '中娅')
    assert len(result['augments']) == 135
    assert result['rarity'] == 'all'
    assert result['resolvedAugment']['id'] == 134
    assert result['resolvedAugment']['namesByLocale']['zh_TW'] == '強化134'
    assert all('performance' not in row and 'performanceRank' not in row for row in result['augments'])
    executor.execute.assert_awaited_once()
    args = json.loads(executor.execute.await_args.args[1])
    assert args == {'champion': 'aurora', 'all_matches': True}


@pytest.mark.asyncio
async def test_all_colors_does_not_auto_pick_ambiguous_nickname():
    executor = fake_executor()
    executor.context.names.resolve_augment.side_effect = lambda _: {'status': 'not_found'}
    executor.execute = AsyncMock(return_value=json.dumps({'status': 'ok', 'augments': [
        {'id': 1, 'rarity': 1, 'namesByLocale': {'zh_CN': '升级：中娅'}},
        {'id': 2, 'rarity': 8, 'namesByLocale': {'zh_CN': '中娅试验'}}]}))
    result = await identify(executor, 'aurora', 'all', '中娅')
    assert result['resolvedAugment'] is None
    assert len(result['augments']) == 2


def test_english_possessive_omission_is_source_backed_and_collisions_stay_ambiguous():
    row = {'augmentId':1154, 'namespace':'aram_mayhem', 'apiName':'ARAM_Quest_UrfsChampion',
           'nameEn':"Urf's Champion", 'nameZh':'海牛阿福的勇士'}
    catalog = NameCatalog(data={'champions':[], 'augments':[row]})
    for query in ['Urf Champion', "Urf's Champion", 'URF’S CHAMPION']:
        assert catalog.resolve_augment(query)['augment']['augmentId'] == 1154
    assert catalog.resolve_augment('Worlds Champion')['status'] == 'not_found'
    other = {**row, 'augmentId':1155, 'nameEn':'Urf Champion'}
    assert NameCatalog(data={'champions':[], 'augments':[row,other]}).resolve_augment('Urf Champion')['status'] == 'ambiguous'


def fake_executor(missing=False):
    names = MagicMock()
    names.resolve_augment.side_effect = lambda q: {'status': 'ok', 'augment': {
        'augmentId': {'A': 1, 'B': 2, 'C': 3}[q], 'nameEn': q, 'namesByLocale': {'en_US': q}}}
    async def execute(name, arguments):
        args = json.loads(arguments)
        if name == 'get_arammeta_stats':
            return json.dumps({'status': 'ok', 'knownEntities': [{'nameEn': 'UNRELATED'}],
                               'records': [{'entity': {'nameEn': args['query']}, 'metrics': {'wr': .55, 'g': 200}}]})
        return json.dumps({'status': 'ok', 'augments': [] if missing and args.get('query') == 'B' else [
            {'id': i, 'name': n, 'performance': p, 'popular': 1} for i,n,p in [(1,'A',80),(2,'B',90),(3,'C',70),(4,'UNRELATED',180)]]})
    return SimpleNamespace(context=SimpleNamespace(names=names), execute=AsyncMock(side_effect=execute))


@pytest.mark.asyncio
async def test_identity_browse_never_exposes_strength_or_ranking():
    executor = fake_executor()
    result = await identify(executor, 'zeri', 'prismatic')
    assert len(result['augments']) == 4
    assert all('performance' not in row and 'popular' not in row for row in result['augments'])


@pytest.mark.asyncio
async def test_three_choices_cannot_leak_unoffered_stronger_row():
    executor = fake_executor()
    result = await compare(executor, 'zeri', ['A', 'B', 'C'])
    assert result['comparisonSource'] == 'opgg'
    assert [c['requestedName'] for c in result['choices']] == ['A','B','C']
    assert 'UNRELATED' not in json.dumps(result)
    assert executor.execute.await_count == 3


@pytest.mark.asyncio
async def test_missing_one_option_checks_same_options_in_fallback():
    executor = fake_executor(missing=True)
    result = await compare(executor, 'zeri', ['A','B'])
    assert result['comparisonSource'] == 'arammeta'
    assert 'UNRELATED' not in json.dumps(result)
    fallbacks = [json.loads(c.args[1])['query'] for c in executor.execute.await_args_list if c.args[0] == 'get_arammeta_stats']
    assert fallbacks == ['A','B']


@pytest.mark.asyncio
@pytest.mark.parametrize('options', [[], ['A','B','C','D'], [1], ['']])
async def test_compare_rejects_empty_oversized_or_invalid_options(options):
    executor = LeagueToolExecutor(SimpleNamespace())
    result = json.loads(await executor.execute('compare_mayhem_choices', json.dumps({'champion':'zeri','options':options})))
    assert result['status'] == 'invalid_arguments'


@pytest.mark.asyncio
async def test_unresolved_option_requests_identification_before_fallback():
    executor = fake_executor()
    executor.context.names.resolve_augment.side_effect = lambda q: {'status': 'not_found'}
    result = await compare(executor, 'zeri', ['帽子'], rarity='prismatic')
    assert result['status'] == 'needs_identification'
    assert result['choices'][0]['nextAction'] == {
        'tool':'identify_mayhem_augment', 'champion':'zeri', 'query':'帽子', 'rarity':'prismatic'}
    assert all(c.args[0] != "get_arammeta_stats" for c in executor.execute.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize('count', [1, 2])
async def test_hat_nickname_requires_unique_source_name(count):
    executor = fake_executor()
    executor.context.names.resolve_augment.side_effect = lambda q: {'status': 'not_found'}
    executor.execute = AsyncMock(return_value=json.dumps({'status':'ok', 'augments':[
        {'id': i, 'nameEn': f'Hat{i}', 'namesByLocale': {'zh_CN': f'法帽{i}'}, 'performance':99}
        for i in range(count)]}))
    result = await identify(executor, 'aurora', 'prismatic', '帽子')
    assert bool(result['resolvedAugment']) is (count == 1)
    assert all('performance' not in row for row in result['augments'])
