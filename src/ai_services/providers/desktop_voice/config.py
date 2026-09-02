"""Configuration for the local desktop Voice bridge."""

from typing import Any

from src.config.config import Config

DESKTOP_VOICE_SERVICE_CONFIG: dict[str, Any] = {
    "processing_audio_frame_rate": Config.DESKTOP_VOICE_SAMPLE_RATE,
    "processing_audio_channels": Config.DESKTOP_VOICE_CHANNELS,
    "response_audio_frame_rate": Config.DESKTOP_VOICE_SAMPLE_RATE,
    "response_audio_channels": Config.DESKTOP_VOICE_CHANNELS,
    "send_device": Config.DESKTOP_VOICE_SEND_DEVICE,
    "receive_device": Config.DESKTOP_VOICE_RECEIVE_DEVICE,
    "preferred_host_api": Config.DESKTOP_VOICE_HOST_API,
    "frame_ms": Config.DESKTOP_VOICE_FRAME_MS,
    "response_start_ms": Config.DESKTOP_VOICE_RESPONSE_START_MS,
    "response_silence_ms": Config.DESKTOP_VOICE_RESPONSE_SILENCE_MS,
    "response_preroll_ms": Config.DESKTOP_VOICE_RESPONSE_PREROLL_MS,
    "vad_aggressiveness": Config.DESKTOP_VOICE_VAD_AGGRESSIVENESS,
    "auto_route": Config.DESKTOP_VOICE_AUTO_ROUTE,
    "voicemeeter_remote_dll": Config.VOICEMEETER_REMOTE_DLL,
}
