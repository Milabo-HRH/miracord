"""
Configuration for the OpenAI service adapter.
"""

import os
from typing import Any

from src.config.config import Config
from src.exceptions import ConfigurationError


def realtime_reasoning_config(model: str, effort: str) -> dict[str, Any]:
    """Opt into reasoning only for verified reasoning-capable Realtime models."""
    effort = effort.strip().lower()
    if not effort:
        return {}
    if effort not in {"minimal", "low", "medium", "high", "xhigh"}:
        raise ConfigurationError("Unsupported OPENAI_REASONING_EFFORT.")
    supported = {"gpt-realtime-2", "gpt-realtime-2.1", "gpt-realtime-2.1-mini"}
    if model not in supported:
        raise ConfigurationError(
            "OPENAI_REASONING_EFFORT requires a supported reasoning Realtime model. "
            "Clear it when switching back to gpt-realtime or gpt-realtime-1.5."
        )
    return {"reasoning": {"effort": effort}}

# Name of the OpenAI model for real-time services.
OPENAI_REALTIME_MODEL_NAME: str = os.getenv(
    "OPENAI_REALTIME_MODEL_NAME", "gpt-realtime"
)

# Default initial session data for OpenAI.
OPENAI_SERVICE_INITIAL_SESSION_DATA: dict[str, Any] = {
    "type": "realtime",
    **realtime_reasoning_config(
        OPENAI_REALTIME_MODEL_NAME, os.getenv("OPENAI_REASONING_EFFORT", "")
    ),
    "instructions": os.getenv(
        "OPENAI_INSTRUCTIONS", Config.ASSISTANT_SYSTEM_INSTRUCTIONS
    ),
    "output_modalities": ["audio"],
    "audio": {
        "input": {
            "format": {"type": "audio/pcm", "rate": 24000},
            "turn_detection": {
                "type": "server_vad",
                "threshold": 0.5,
                "silence_duration_ms": 600,
                "prefix_padding_ms": 300,
                "create_response": True,
                "interrupt_response": True,
            }
            if Config.REALTIME_SERVER_VAD
            else None,
        },
        "output": {
            "format": {"type": "audio/pcm", "rate": 24000},
            "voice": os.getenv("OPENAI_VOICE", "marin"),
        },
    },
}

# Default data for creating a response.
OPENAI_SERVICE_RESPONSE_CREATION_DATA: dict[str, Any] = {"output_modalities": ["audio"]}

# Assembles the complete service configuration dictionary for OpenAI.
# This dictionary is imported by the bot's main entry point to be used in the factory.
OPENAI_SERVICE_CONFIG: dict[str, Any] = {
    "api_key": Config.OPENAI_API_KEY,
    "model_name": OPENAI_REALTIME_MODEL_NAME,
    "session_config": OPENAI_SERVICE_INITIAL_SESSION_DATA,
    "league_context_enabled": Config.LEAGUE_CONTEXT_ENABLED,
    "league_tools_enabled": Config.LEAGUE_TOOLS_ENABLED,
    "opgg_prefetch_enabled": Config.OPGG_PREFETCH_ENABLED,
    "web_search_enabled": Config.NATIVE_WEB_SEARCH_MODE != "off",
    "web_search_model": os.getenv("OPENAI_WEB_SEARCH_MODEL", "gpt-5.4-mini"),
    "web_search_reasoning_effort": os.getenv("OPENAI_WEB_SEARCH_REASONING_EFFORT", "none"),
    "web_search_timeout": float(os.getenv("OPENAI_WEB_SEARCH_TIMEOUT_SECONDS", "15")),
    "web_search_cache_seconds": float(os.getenv("OPENAI_WEB_SEARCH_CACHE_SECONDS", "60")),
    "preferred_search_sources": Config.PREFERRED_SEARCH_SOURCES,
    "connection_timeout": Config.AI_SERVICE_CONNECTION_TIMEOUT,
    "response_creation_data": OPENAI_SERVICE_RESPONSE_CREATION_DATA,
    "processing_audio_frame_rate": 24000,
    "processing_audio_channels": 1,
    "response_audio_frame_rate": 24000,
    "response_audio_channels": 1,
}
