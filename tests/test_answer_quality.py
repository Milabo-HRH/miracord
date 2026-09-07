"""Regression checks for grading actual voice transcripts, not fixed phrasing."""

import json
import pytest

from src.evaluation.interactions import FixtureTools, grade


def test_spaced_chinese_cannot_bypass_forbidden_source_preamble():
    case = {"expected": {"answer_not_regex": [r"(?:根据|按照|来自).*op\.?gg"]}}
    assert not grade(case, [], ["根 据 OP.GG，T3。"])["forbidden_patterns"]


def test_source_question_can_require_provenance():
    case = {"expected": {"answer_contains_any": [["OP.GG", "OPGG"]]}}
    assert grade(case, [], ["来自 OP.GG。"])["required_alternatives"]
    assert not grade(case, [], ["T3。"])["required_alternatives"]


def test_alternative_groups_are_all_required_and_length_is_independent():
    case = {"expected": {"answer_contains_any": [["查询失败", "暂时查不了"], ["稍后", "重试"]],
                         "max_answer_chars": 4}}
    checks = grade(case, [], ["查 询 失 败，稍后重试。"])
    assert checks["required_alternatives"]
    assert not checks["answer_length"]
    assert not grade(case, [], ["查询失败"])["required_alternatives"]


@pytest.mark.asyncio
async def test_reviewed_argument_variants_do_not_allow_arbitrary_queries():
    fixture = FixtureTools([{"name": "lookup", "arguments": {"champion": "anivia"},
                             "accepted_arguments": [{"champion": "Anivia"}],
                             "result": {"status": "ok"}}])
    assert json.loads(await fixture.execute("lookup", '{"champion":"Anivia"}'))["status"] == "ok"
    assert json.loads(await fixture.execute("lookup", '{"champion":"Viego"}'))["reason"] == "replay_fixture_miss"
