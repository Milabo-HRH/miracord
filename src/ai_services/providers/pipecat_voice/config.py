"""Configuration shared by the bot and standalone cascade smoke tests."""
import os
from src.config.config import Config


def service_config():
    return {
        "api_key": Config.GEMINI_API_KEY,
        "processing_audio_frame_rate": 16000,
        "processing_audio_channels": 1,
        "response_audio_frame_rate": 24000,
        "response_audio_channels": 1,
        "stt_model": os.getenv("PIPE_STT_MODEL", "gemini-3.5-transcribe-live"),
        "stt_languages": [v.strip() for v in os.getenv("PIPE_STT_LANGUAGES", "cmn-Hans-CN,en-US").split(",") if v.strip()],
        "wake_phrase": Config.WAKE_WORD_PHRASE,
        "llm_provider": os.getenv("PIPE_LLM_PROVIDER", "google"),
        "llm_model": os.getenv("PIPE_LLM_MODEL", "gemini-3.8-flash"),
        "thinking": os.getenv("PIPE_THINKING", "medium"),
        "tts_provider": os.getenv("PIPE_TTS_PROVIDER", "doubao"),
        "tts_model": os.getenv("PIPE_TTS_MODEL", ""),
        "tts_voice": os.getenv("PIPE_TTS_VOICE", ""),
        "fish_key": os.getenv("FISH_API_KEY", ""),
        "fish_key_file": os.getenv("FISH_API_KEY_FILE", ""),
        "fish_voice": os.getenv("FISH_TTS_VOICE", "0f08cacd3e354471a4b94dd00b4cc4a3"),
        "doubao_key": os.getenv("DOUBAO_API_KEY", ""),
        "doubao_key_file": os.getenv("DOUBAO_API_KEY_FILE", ""),
        "minimax_key": os.getenv("MINIMAX_API_KEY", ""),
        "minimax_group": os.getenv("MINIMAX_GROUP_ID", ""),
        "minimax_url": os.getenv("MINIMAX_TTS_URL", "https://api.minimax.io/v1/t2a_v2"),
        "instructions": Config.ASSISTANT_SYSTEM_INSTRUCTIONS,
        "league_context_enabled": Config.LEAGUE_CONTEXT_ENABLED,
        "league_tools_enabled": Config.LEAGUE_TOOLS_ENABLED,
        "opgg_prefetch_enabled": Config.OPGG_PREFETCH_ENABLED,
        "turn_silence_seconds": float(os.getenv("PIPE_TURN_SILENCE_SECONDS", "0.8")),
        "connection_lifetime_seconds": float(os.getenv("PIPE_CONNECTION_LIFETIME_SECONDS", "2700")),
        "prewarm": os.getenv("PIPE_PREWARM", "true").lower() == "true",
    }


PIPECAT_SERVICE_CONFIG = service_config()
