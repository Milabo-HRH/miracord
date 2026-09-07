"""
Configuration for the Gemini service adapter.
"""

import os
from typing import Dict, Any

from src.config.config import Config

# Name of the Gemini model for real-time services.
GEMINI_REALTIME_MODEL_NAME: str = os.getenv(
    "GEMINI_MODEL",
    os.getenv("GEMINI_REALTIME_MODEL_NAME", "gemini-3.1-flash-live-preview"),
)
GEMINI_GOOGLE_SEARCH_ENABLED = os.getenv("GEMINI_GOOGLE_SEARCH_ENABLED", "false").lower() in {"1", "true", "yes"}

# Default LiveConnectConfig parameters, can be overridden by environment or specific needs
GEMINI_DEFAULT_LIVE_CONNECT_CONFIG: Dict[str, Any] = {
    "response_modalities": ["AUDIO"],  # Expect audio responses
    "max_output_tokens": 2048,
    "system_instruction": {
        "parts": [{"text": os.getenv("GEMINI_INSTRUCTIONS", Config.ASSISTANT_SYSTEM_INSTRUCTIONS)}]
    },
    "media_resolution": "MEDIA_RESOLUTION_MEDIUM",  # Default from example
    "speech_config": {  # Default from example
        "voice_config": {"prebuilt_voice_config": {"voice_name": "Zephyr"}}
    },
    "context_window_compression": {  # Default from example
        "trigger_tokens": 25600,
        "sliding_window": {"target_tokens": 12800},
    },
    "realtime_input_config": {"automatic_activity_detection": {"disabled": False}},
}

if GEMINI_GOOGLE_SEARCH_ENABLED:
    # Separate opt-in so another provider's search setting does not enable billing.
    GEMINI_DEFAULT_LIVE_CONNECT_CONFIG["tools"] = [{"google_search": {}}]

# Assembles the complete service configuration dictionary for Gemini.
# This dictionary is imported by the bot's main entry point to be used in the factory.
GEMINI_SERVICE_CONFIG: Dict[str, Any] = {
    "api_key": Config.GEMINI_API_KEY,
    "model_name": GEMINI_REALTIME_MODEL_NAME,
    "live_connect_config": GEMINI_DEFAULT_LIVE_CONNECT_CONFIG,
    "connection_timeout": Config.AI_SERVICE_CONNECTION_TIMEOUT,
    "processing_audio_frame_rate": 16000,  # As per Gemini docs
    "processing_audio_channels": 1,
    "response_audio_frame_rate": 24000,  # As per Gemini docs
    "response_audio_channels": 1,
    "native_web_search": GEMINI_GOOGLE_SEARCH_ENABLED,
    "league_context_enabled": Config.LEAGUE_CONTEXT_ENABLED,
    "league_tools_enabled": Config.LEAGUE_TOOLS_ENABLED,
    "opgg_prefetch_enabled": Config.OPGG_PREFETCH_ENABLED,
}
