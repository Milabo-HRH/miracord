"""Offline prompt wiring and augmentation payload regressions."""

import json
from unittest.mock import MagicMock

import pytest

from scripts.eval_voice_questions import cases, route_check
from src.ai_services.providers.grok.manager import GrokRealtimeManager
from src.ai_services.providers.openai.manager import OpenAIRealtimeManager
from src.lol_mcp.context import GameContextService
from src.lol_mcp.prompts import GAME_QUESTION_INSTRUCTIONS, MAYHEM_TOOL_INSTRUCTIONS
from src.lol_mcp.tools import LEAGUE_TOOLS, LeagueToolExecutor


@pytest.mark.parametrize("manager_class", [OpenAIRealtimeManager, GrokRealtimeManager])
@pytest.mark.parametrize("tools_enabled", [True, False])
def test_intent_rules_and_only_available_tool_instructions(
    manager_class, tools_enabled
):
    original = {"instructions": "Original caller policy.", "tools": []}
    manager = manager_class(
        MagicMock(),
        {
            "api_key": "test-not-a-key",
            "model_name": "mock-model",
            "league_context_enabled": False,
            "league_tools_enabled": tools_enabled,
            "session_config": original,
            "processing_audio_frame_rate": 24000,
            "processing_audio_channels": 1,
            "response_audio_frame_rate": 24000,
            "response_audio_channels": 1,
        },
    )
    actual = manager._session_config["instructions"]
    assert actual.startswith("Original caller policy.")
    assert GAME_QUESTION_INSTRUCTIONS in actual
    assert (MAYHEM_TOOL_INSTRUCTIONS in actual) is tools_enabled
    assert ("compare_mayhem_choices" in actual) is tools_enabled
    assert original == {"instructions": "Original caller policy.", "tools": []}
    names = {tool["name"] for tool in manager._session_config["tools"]}
    assert ("compare_mayhem_choices" in names) is tools_enabled


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [None, True, False])
async def test_voice_augment_descriptions_are_opt_in(explicit):
    opgg = MagicMock()
    opgg.get_augments.return_value = {"status": "ok", "augments": []}
    executor = LeagueToolExecutor(GameContextService(enabled=False, opgg=opgg))
    args = {"champion": "Samira", "query": "Soul Siphon", "limit": 3}
    if explicit is not None:
        args["include_descriptions"] = explicit
    result = await executor.execute("get_mayhem_augments", json.dumps(args))
    assert json.loads(result)["status"] == "ok"
    opgg.get_augments.assert_called_once_with(
        champion="Samira",
        query="Soul Siphon",
        limit=3,
        include_descriptions=False if explicit is None else explicit,
    )
    opgg.get_build.assert_not_called()


@pytest.mark.asyncio
async def test_item_tool_does_not_receive_augment_arguments():
    opgg = MagicMock()
    opgg.get_build.return_value = {"status": "ok"}
    executor = LeagueToolExecutor(GameContextService(enabled=False, opgg=opgg))
    await executor.execute("get_mayhem_build", '{"champion":"Brand"}')
    opgg.get_build.assert_called_once_with(champion="Brand")
    opgg.get_augments.assert_not_called()


def test_tools_separate_identity_from_player_choices():
    names = {t['name']:t for t in LEAGUE_TOOLS}
    assert 'get_mayhem_augments' not in names
    assert 'identify_mayhem_augment' in names
    options = names['compare_mayhem_choices']['parameters']['properties']['options']
    assert options['minItems'] == 1 and options['maxItems'] == 3


def test_eval_checks_route_not_just_completed_response():
    sample = cases(["A", "B", "C"])[0]
    assert not route_check(sample, [])
    assert not route_check(sample, [{"name": "get_mayhem_build"}])
    assert route_check(sample, [{"name": "get_mayhem_augments"}])
    assert not route_check(
        sample, [{"name": "get_mayhem_build"}, {"name": "get_mayhem_augments"}]
    )
