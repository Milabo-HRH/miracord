"""Name grounding crosses the real context/tool path without network or a live match."""
import json
from unittest.mock import MagicMock
import pytest
from src.lol_mcp.context import GameContextService
from src.lol_mcp.tools import LeagueToolExecutor

CHAMPION = {'championId': 82, 'nameZh': '莫德凯撒', 'titleZh': '铁铠冥魂',
            'nameEn': 'Mordekaiser', 'internalName': 'Mordekaiser',
            'opggSlug': 'mordekaiser', 'aliasesZh': ['铁男']}
AUGMENT = {'augmentId': 1133, 'namespace': 'aram_mayhem', 'apiName': 'ARAM_MagicMissile',
           'nameZh': '魔法飞弹', 'nameEn': 'Magic Missile',
           'namesByLocale': {'en_US': 'Magic Missile', 'zh_CN': '魔法飞弹',
                             'zh_MY': '魔法飞弹', 'zh_TW': '魔法導彈'},
           'namesByRegion': {'CN': '魔法飞弹'}}

class Names:
    metadata = {'status': 'ok', 'version': 'synthetic-test'}
    def resolve_champion(self, query):
        if query in ['铁男', '82', 'Mordekaiser', 'mordekaiser']:
            return {'status': 'ok', 'champion': CHAMPION}
        if query == '模糊昵称':
            return {'status': 'ambiguous', 'candidates': [CHAMPION, {**CHAMPION, 'championId': 1}]}
        return {'status': 'not_found'}
    def resolve_augment(self, query):
        return ({'status': 'ok', 'augment': AUGMENT}
                if query in ['魔法飞弹', 'ARAM_MagicMissile'] else {'status': 'not_found'})

@pytest.fixture
def service():
    instance = GameContextService(enabled=False, prefetch=False, names=Names(), opgg=MagicMock())
    instance.opgg.reset_mock()
    return instance

@pytest.mark.asyncio
@pytest.mark.parametrize('query', ['铁男', '82'])
async def test_chinese_nickname_and_numeric_identity_reach_canonical_tool(service, query):
    service.opgg.get_champion_tier.return_value = {'status': 'ok', 'tierLabel': 'T3'}
    result = json.loads(await LeagueToolExecutor(service).execute(
        'get_mayhem_champion_tier', json.dumps({'champion': query})))
    assert result['tierLabel'] == 'T3'
    service.opgg.get_champion_tier.assert_called_once_with(champion='mordekaiser')

@pytest.mark.asyncio
async def test_ambiguous_nickname_never_queries_arbitrary_champion(service):
    result = json.loads(await LeagueToolExecutor(service).execute(
        'get_mayhem_champion_tier', json.dumps({'champion': '模糊昵称'})))
    assert result['status'] == 'ambiguous'
    service.opgg.get_champion_tier.assert_not_called()

@pytest.mark.asyncio
async def test_augment_translation_uses_name_not_unverified_cross_database_id(service):
    service.opgg.get_augments.return_value = {'status': 'ok', 'augments': []}
    await LeagueToolExecutor(service).execute('get_mayhem_augments', json.dumps(
        {'champion': '铁男', 'query': '魔法飞弹'}))
    service.opgg.get_augments.assert_called_once_with(
        champion='mordekaiser', query='Magic Missile', include_descriptions=False)

@pytest.mark.asyncio
async def test_name_tool_is_local_and_unknown_kind_is_rejected(service):
    executor = LeagueToolExecutor(service)
    result = json.loads(await executor.execute('resolve_game_name', json.dumps(
        {'kind': 'champion', 'query': '铁男'})))
    assert result['champion']['championId'] == 82
    assert json.loads(await executor.execute('resolve_game_name', json.dumps(
        {'kind': 'item', 'query': '铁男'})))['status'] == 'invalid_arguments'
    assert not service.opgg.mock_calls

def test_context_includes_only_known_roster_and_observed_augment_without_binding(service):
    service.snapshot = lambda: {'players': [{'champion': 'Mordekaiser',
        'mayhemAugments': [{'internalId': 'ARAM_MagicMissile'}]}], 'scope': 'shared_reference'}
    context = json.loads(service.turn_context(7, 'Speaker').split('\n', 1)[1])
    assert context['speaker']['gameBinding'] is None
    assert context['nameGlossary']['champions'] == [CHAMPION]
    assert context['nameGlossary']['augments'] == [AUGMENT]
    assert len(service.turn_context(7, 'Speaker', max_chars=1500)) < 1600


@pytest.mark.asyncio
async def test_result_localization_requires_id_and_name_not_opgg_icon_key(service):
    service.names.resolve_augment = lambda query: {'status': 'ok', 'augment': AUGMENT}
    service.opgg.get_augments.return_value = {'status': 'ok', 'augments': [
        {'id': 1133, 'key': 'different_icon_name', 'name': 'Magic Missile'},
        {'id': 1133, 'key': 'ARAM_MagicMissile', 'name': 'Unexpected changed name'},
    ]}
    output = json.loads(await LeagueToolExecutor(service).execute(
        'get_mayhem_augments', json.dumps({'champion': 'Mordekaiser'})))
    assert output['augments'][0]['nameZh'] == '魔法飞弹'
    assert output['augments'][0]['namesByLocale'] == AUGMENT['namesByLocale']
    assert output['augments'][0]['namesByRegion'] == AUGMENT['namesByRegion']
    assert 'nameZh' not in output['augments'][1]
    assert 'namesByLocale' not in output['augments'][1]


@pytest.mark.asyncio
@pytest.mark.parametrize('query', ['掷骰狂人', '豪掷千金', '豪氣賭客'])
async def test_regional_names_reach_same_english_game_query(service, query):
    from src.lol_mcp.name_catalog import NameCatalog
    record = {'augmentId': 2095, 'namespace': 'aram_mayhem',
              'apiName': 'ARAM_HighRoller', 'nameZh': '掷骰狂人', 'nameEn': 'High Roller',
              'namesByLocale': {'en_US': 'High Roller', 'zh_CN': '掷骰狂人',
                                'zh_MY': '豪掷千金', 'zh_TW': '豪氣賭客'}}
    service._names = NameCatalog(data={'champions': [CHAMPION], 'augments': [record]})
    service.opgg.get_augments.return_value = {'status': 'ok', 'augments': []}
    await LeagueToolExecutor(service).execute('get_mayhem_augments', json.dumps(
        {'champion': '铁男', 'query': query}))
    service.opgg.get_augments.assert_called_once_with(
        champion='mordekaiser', query='High Roller', include_descriptions=False)


def test_existing_context_service_reads_refreshed_local_catalog(service, monkeypatch):
    current = [Names()]
    service._names = None
    monkeypatch.setattr('src.lol_mcp.context.get_name_catalog', lambda: current[0])
    assert service.names is current[0]
    replacement = Names()
    current[0] = replacement
    assert service.names is replacement
