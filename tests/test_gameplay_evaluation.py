import asyncio
import json

import pytest

from src.evaluation.gameplay import FrozenOpgg, GameplayTools, conversation, gameplay_grade, reply_action


def bundle():
    return {"catalog": {"champions": [], "augments": [], "metadata": {}},
            "records": {"anivia-augments": {"fetchedAtEpoch": 1, "data": {"patch": "test",
                "augments": [{"id": 1373, "name": "Shrink Engine", "key": "ShrinkEngine",
                              "description": "Test effect", "tier": 1}]}}}}


@pytest.mark.asyncio
async def test_real_tool_validation_and_filtering_allow_equivalent_inputs():
    tools = GameplayTools(bundle(), {})
    for args in [{"champion": "Anivia", "query": "Shrink", "include_descriptions": True},
                 {"champion": "anivia", "augment_ids": [1373], "include_descriptions": True}]:
        result = json.loads(await tools.execute("get_mayhem_augments", json.dumps(args)))
        assert result["totalMatched"] == 1
        assert result["augments"][0]["description"] == "Test effect"
    result = json.loads(await tools.execute("get_mayhem_augments", '{"champion":"anivia","limit":999}'))
    assert result["status"] == "invalid_arguments"


def test_dataset_gap_differs_from_actual_empty_query():
    opgg = FrozenOpgg(bundle()["records"])
    assert opgg.get_augments("anivia", query="Shrink Ray")["totalMatched"] == 0
    assert not opgg.gaps
    assert opgg.get_augments("brand")["reason"] == "eval_data_missing"
    assert opgg.gaps == ["brand-augments"]


def test_right_keywords_do_not_hide_wrong_hero_or_confirmation_loop():
    turn = {"expected": {"answer_contains": ["缩小引擎"], "allowed_champions": ["anivia"]}}
    checks = gameplay_grade(turn, [{"name": "get_mayhem_build", "arguments": {"champion": "Hweiei"}}],
                            ["选缩小引擎，确定了吗？"])
    assert checks["required_text"]
    assert not checks["correct_champion"]
    assert not checks["no_unsolicited_confirmation"]


def test_silence_marker_requires_whole_output_and_preserves_real_reply():
    assert reply_action(" <NO_REPLY>\n") == "no_reply"
    assert reply_action("T3。") == "reply"
    assert reply_action("好的<NO_REPLY>") == "invalid_control_output"


def test_review_catches_english_drift_and_unrequested_offer():
    turn = {"expected": {"answer_language": "zh"}}
    checks = gameplay_grade(turn, [], ["You said Draw Your Sword, 拔剑, right?"])
    assert not checks["chinese_response"]
    checks = gameplay_grade(turn, [], ["泽丽的拔剑数据是八十八点九。你要看看它的效果吗？"])
    assert checks["chinese_response"]
    assert not checks["no_unsolicited_confirmation"]
    checks = gameplay_grade(turn, [], ["你说的是 Draw Your Sword，也就是拔剑吧？泽丽的数据是八十八点九。"])
    assert checks["chinese_response"]
    assert checks["no_unsolicited_confirmation"]


def test_identity_confirmation_is_allowed_but_refresh_tool_is_required():
    turn = {"expected": {"identity_confirmation_allowed": True,
                          "required_tool_names": ["get_live_game_state"]}}
    checks = gameplay_grade(turn, [], ["确认一下，是主机螳螂吗？"])
    assert checks["no_unsolicited_confirmation"]
    assert not checks["required_tools"]


@pytest.mark.asyncio
async def test_followups_share_one_session_without_injected_assistant_answers():
    made = []
    class Manager:
        def __init__(self):
            self.sent = []
            self._response_completed = asyncio.Event()
            self._last_response_status = "completed"
            self.closed = False
        def apply_replay_config(self, snapshot):
            pass
        async def connect(self, *callbacks):
            return True
        async def send_text_turn(self, text, *, context_text):
            self.sent.append((text, context_text))
            self._last_response_text = "first actual answer" if len(self.sent) == 1 else "second actual answer"
            self._response_completed.set()
            return True
        async def disconnect(self):
            self.closed = True
    def factory(*args, **kwargs):
        m = Manager(); made.append(m); return m
    scenario = {"id": "test", "origin": "synthetic", "turns": [
        {"question": "first", "review_rubric": ["SECRET EXPECTATION"]}, {"question": "then?"}]}
    report = await conversation(scenario, bundle(), {"provider": "gemini", "session_config": {}}, manager_factory=factory)
    assert len(made) == 1 and made[0].closed
    assert [r["answers"][0] for r in report["turns"]] == ["first actual answer", "second actual answer"]
    assert "SECRET EXPECTATION" not in str(made[0].sent)
    assert "first actual answer" not in made[0].sent[1][1]


def test_candidate_lookup_requires_first_call_and_multilingual_tool_evidence():
    import copy
    turn = {'expected': {'forbidden_tool_names': ['lookup_game_item'], 'candidate_lookup': {
        'rarity': 'prismatic', 'source_rarity': 8, 'id': 1156,
        'locales': ['en_US', 'zh_CN', 'zh_MY', 'zh_TW']}}}
    call = {'name': 'get_mayhem_augments', 'arguments': {'champion': 'Aurora', 'rarity': 'prismatic'},
            'result': {'status': 'ok', 'augments': [{'id': 1156, 'rarity': 8, 'namesByLocale': {
                'en_US': "Wooglet's Witchcap", 'zh_CN': '沃格勒特的巫师帽',
                'zh_MY': '乌莉特的法帽', 'zh_TW': '烏莉特的法帽'}}]}}
    assert all(gameplay_grade(turn, [call], ['彩色池里有沃格勒特的巫师帽。']).values())
    guessed = copy.deepcopy(call); guessed['arguments']['query'] = "Wooglet's Witchcap"
    assert not gameplay_grade(turn, [guessed], ['有帽子。'])['rarity_candidates_first']
    missing = copy.deepcopy(call); del missing['result']['augments'][0]['namesByLocale']['zh_TW']
    assert not gameplay_grade(turn, [missing], ['有帽子。'])['candidate_multilingual_evidence']
    wrong = copy.deepcopy(call); wrong['result']['augments'][0]['rarity'] = 4
    assert not gameplay_grade(turn, [wrong], ['有帽子。'])['candidate_multilingual_evidence']
    assert not gameplay_grade(turn, [call, {'name': 'lookup_game_item', 'arguments': {}}], ['有帽子。'])['forbidden_tools']


def test_incident_grader_rejects_zero_usage_and_wrong_statistic_kind():
    turn = {'expected': {'positive_popularity_results': True,
                        'required_argument_values': {'get_arammeta_stats': {'kind':'champion'}}}}
    calls = [{'name':'get_mayhem_augments', 'arguments':{}, 'result':{'status':'ok','augments':[{'name':'Old','popular':0,'performance':170}]}},
             {'name':'get_arammeta_stats', 'arguments':{'kind':'item'},'result':{'status':'ok'}}]
    checks = gameplay_grade(turn, calls, ['一个听起来很合理的回答'])
    assert not checks['positive_popularity_results']
    assert not checks['argument_values_get_arammeta_stats']


@pytest.mark.asyncio
async def test_unresolved_rarity_name_returns_candidates_not_false_absence():
    data = bundle()
    row = data['records']['anivia-augments']['data']['augments'][0]
    row.update(rarity=4, popular=1, performance=80)
    tools = GameplayTools(data, {})
    result = json.loads(await tools.execute('get_mayhem_augments', json.dumps({'champion':'Anivia','rarity':'gold','query':'难学射手法师'})))
    assert result['status'] == 'ok'
    assert result['queryMode'] == 'rarity_candidates_after_unresolved_name'
    assert result['unresolvedNameQuery'] == '难学射手法师'
    assert result['augments'][0]['id'] == 1373
    for value in [True, -1, 101, float('nan')]:
        invalid = json.loads(await tools.execute('get_mayhem_augments', json.dumps({'champion':'Anivia','min_popular':value})))
        assert invalid['status'] == 'invalid_arguments'


@pytest.mark.asyncio
async def test_rarity_identification_returns_all_rows_beyond_twelve():
    data = bundle()
    data['records']['anivia-augments']['data']['augments'] = [
        {'id': i, 'name': f'Candidate {i}', 'rarity': 8, 'popular': 1, 'performance': 80}
        for i in range(1000, 1040)
    ]
    tools = GameplayTools(data, {})
    result = json.loads(await tools.execute('get_mayhem_augments', json.dumps(
        {'champion':'anivia', 'rarity':'prismatic', 'limit':12})))
    assert len(result['augments']) == 40
    assert result['nextOffset'] is None
    assert result['allMatches'] is True
    ranked = json.loads(await tools.execute('get_mayhem_augments', json.dumps(
        {'champion':'anivia', 'rarity':'prismatic', 'sort_by':'performance', 'limit':3})))
    assert len(ranked['augments']) == 3
