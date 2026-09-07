"""Keep opt-in reasoning compatible with both new and legacy voice models."""

import pytest

from src.ai_services.providers.openai.config import realtime_reasoning_config
from src.exceptions import ConfigurationError


@pytest.mark.parametrize("effort", ["minimal", "low", "medium", "high", "xhigh"])
def test_realtime_reasoning_efforts(effort):
    assert realtime_reasoning_config("gpt-realtime-2.1", effort) == {
        "reasoning": {"effort": effort}
    }


@pytest.mark.parametrize("model", ["gpt-realtime", "gpt-realtime-2025-08-28", "gpt-realtime-1.5"])
def test_legacy_realtime_does_not_receive_reasoning(model):
    assert realtime_reasoning_config(model, "") == {}
    with pytest.raises(ConfigurationError):
        realtime_reasoning_config(model, "medium")


def test_realtime_rejects_unknown_reasoning_effort():
    with pytest.raises(ConfigurationError):
        realtime_reasoning_config("gpt-realtime-2.1", "none")
