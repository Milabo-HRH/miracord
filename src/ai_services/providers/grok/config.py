"""Configuration for xAI's Grok Speech-to-Speech WebSocket API."""

import os
from typing import Any, Dict

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

GROK_DEFAULT_SESSION_CONFIG: Dict[str, Any] = {
    "voice": os.getenv("GROK_VOICE", "eve"),
    "instructions": os.getenv(
        "GROK_INSTRUCTIONS",
        Config.ASSISTANT_SYSTEM_INSTRUCTIONS,
    ),
    # MIRA.CORD builds turns locally so hold never uploads audio early.
    "turn_detection": None,
    "reasoning": {"effort": os.getenv("GROK_REASONING_EFFORT", "high")},
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

GROK_SERVICE_CONFIG: Dict[str, Any] = {
    "api_key": Config.XAI_API_KEY,
    "model_name": GROK_REALTIME_MODEL_NAME,
    "session_config": GROK_DEFAULT_SESSION_CONFIG,
    "connection_timeout": Config.AI_SERVICE_CONNECTION_TIMEOUT,
    "processing_audio_frame_rate": 16000,
    "processing_audio_channels": 1,
    "response_audio_frame_rate": 24000,
    "response_audio_channels": 1,
    "native_web_search": Config.NATIVE_WEB_SEARCH_MODE != "off",
    "native_social_search": Config.GROK_X_SEARCH_ENABLED,
}
