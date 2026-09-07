"""Configuration for xAI's Grok Speech-to-Speech WebSocket API."""

import os
from typing import Any

from src.config.config import Config

GROK_REALTIME_MODEL_NAME = os.getenv(
    "GROK_MODEL",
    os.getenv("GROK_REALTIME_MODEL_NAME", "grok-voice-think-fast-2.0"),
)

GROK_TOOLS: list[dict[str, Any]] = []
if Config.NATIVE_WEB_SEARCH_MODE != "off":
    GROK_TOOLS.append({"type": "web_search"})
    if Config.GROK_X_SEARCH_ENABLED:
        GROK_TOOLS.append({"type": "x_search"})

GROK_DEFAULT_SESSION_CONFIG: dict[str, Any] = {
    "voice": os.getenv("GROK_VOICE", "eve"),
    "instructions": os.getenv(
        "GROK_INSTRUCTIONS",
        Config.ASSISTANT_SYSTEM_INSTRUCTIONS,
    ),
    # Held turns are buffered locally and use manual commit when released.
    "turn_detection": {
        "type": "server_vad",
        "threshold": 0.5,
        "silence_duration_ms": 600,
        "prefix_padding_ms": 300,
    }
    if Config.REALTIME_SERVER_VAD
    else None,
    "reasoning": {"effort": os.getenv("GROK_REASONING_EFFORT", "none")},
    "audio": {
        "input": {
            "format": {"type": "audio/pcm", "rate": 16000},
            "transport": "json",
        },
        "output": {
            "format": {"type": "audio/pcm", "rate": 24000},
            "transport": "json",
        },
    },
    "tools": GROK_TOOLS,
}

GROK_SERVICE_CONFIG: dict[str, Any] = {
    "api_key": Config.XAI_API_KEY,
    "model_name": GROK_REALTIME_MODEL_NAME,
    "session_config": GROK_DEFAULT_SESSION_CONFIG,
    "league_context_enabled": Config.LEAGUE_CONTEXT_ENABLED,
    "league_tools_enabled": Config.LEAGUE_TOOLS_ENABLED,
    "opgg_prefetch_enabled": Config.OPGG_PREFETCH_ENABLED,
    "connection_timeout": Config.AI_SERVICE_CONNECTION_TIMEOUT,
    "processing_audio_frame_rate": 16000,
    "processing_audio_channels": 1,
    "response_audio_frame_rate": 24000,
    "response_audio_channels": 1,
    "native_web_search": Config.NATIVE_WEB_SEARCH_MODE != "off",
    "native_social_search": Config.GROK_X_SEARCH_ENABLED,
}
