import base64
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.ai_services.providers.grok.event_handler import GrokEventHandlerAdapter
from src.ai_services.providers.grok.manager import GrokRealtimeManager


def service_config() -> dict:
    return {
        "api_key": "xai-test",
        "model_name": "grok-voice-think-fast-2.0",
        "session_config": {"tools": [{"type": "web_search"}]},
        "processing_audio_frame_rate": 16000,
        "processing_audio_channels": 1,
        "response_audio_frame_rate": 24000,
        "response_audio_channels": 1,
        "native_web_search": True,
        "native_social_search": False,
    }


@pytest.mark.asyncio
async def test_grok_manager_configures_native_search_and_manual_turn_events():
    playback = MagicMock()
    manager = GrokRealtimeManager(playback, service_config())
    manager._connection_handler_inst.send_event = AsyncMock()
    manager._connection_handler_inst.is_connected = lambda: True

    assert await manager._post_connect_hook() is True
    assert manager.is_connected() is False
    await manager._dispatch_event({"type": "session.updated"})
    assert await manager.send_audio_chunk(b"pcm") is True
    assert await manager.finalize_input_and_request_response() is True

    events = [
        call.args[0]
        for call in manager._connection_handler_inst.send_event.await_args_list
    ]
    assert events[0]["type"] == "session.update"
    assert events[0]["session"]["tools"] == [{"type": "web_search"}]
    assert "VOICE_CONTEXT" in events[0]["session"]["instructions"]
    assert events[1] == {
        "type": "input_audio_buffer.append",
        "audio": base64.b64encode(b"pcm").decode("ascii"),
    }
    assert events[2:] == [
        {"type": "input_audio_buffer.commit"},
        {"type": "response.create"},
    ]
    assert manager.capabilities.native_web_search is True


@pytest.mark.asyncio
async def test_grok_event_handler_streams_audio_and_finishes_playback():
    playback = MagicMock()
    playback.start_new_audio_stream = AsyncMock()
    playback.add_audio_chunk = AsyncMock()
    playback.end_audio_stream = AsyncMock()
    handler = GrokEventHandlerAdapter(playback, (24000, 1))

    await handler.dispatch_event(
        {"type": "response.created", "response": {"id": "response-1"}}
    )
    await handler.dispatch_event(
        {
            "type": "response.output_audio.delta",
            "delta": base64.b64encode(b"audio").decode("ascii"),
        }
    )
    await handler.dispatch_event({"type": "response.done"})

    playback.start_new_audio_stream.assert_awaited_once_with("response-1", (24000, 1))
    playback.add_audio_chunk.assert_awaited_once_with(b"audio")
    playback.end_audio_stream.assert_awaited_once()
