from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.session.ai_service_coordinator import AIServiceCoordinator
from src.ai_services.interface import ProviderCapabilities


@pytest.fixture
def coordinator():
    bot_state = MagicMock()
    bot_state.is_active_participant.side_effect = lambda user_id: user_id == 1
    manager = MagicMock()
    manager.is_connected.return_value = True
    manager.send_speaker_marker = AsyncMock(return_value=True)
    manager.send_audio_chunk = AsyncMock(return_value=True)
    manager.finalize_input_and_request_response = AsyncMock(return_value=True)
    manager.capabilities = ProviderCapabilities(
        realtime_audio_input=True, server_vad=True
    )
    result = AIServiceCoordinator(bot_state, MagicMock(), {}, guild_id=42)
    result.active_ai_service_manager = manager
    return result, manager


@pytest.mark.asyncio
async def test_upload_gate_rejects_non_participant_audio(coordinator):
    result, manager = coordinator

    assert await result.send_audio_turn(b"private", user_id=2) is False
    manager.send_audio_chunk.assert_not_awaited()


@pytest.mark.asyncio
async def test_speaker_marker_only_emitted_on_actual_switch(coordinator):
    result, manager = coordinator

    assert await result.send_audio_turn(b"one", user_id=1, display_name="Alice")
    assert await result.send_audio_turn(b"two", user_id=1, display_name="Alice")

    manager.send_speaker_marker.assert_awaited_once_with(1, "Alice")
    assert manager.send_audio_chunk.await_count == 2


@pytest.mark.asyncio
async def test_realtime_stream_chunk_does_not_finalize(coordinator):
    result, manager = coordinator

    assert await result.send_audio_stream_chunk(b"pcm", 1, "Alice")

    manager.send_audio_chunk.assert_awaited_once_with(b"pcm")
    manager.finalize_input_and_request_response.assert_not_awaited()
    assert result.supports_server_vad_streaming() is True


@pytest.mark.asyncio
async def test_realtime_stream_can_be_force_finalized(coordinator):
    result, manager = coordinator

    assert await result.finalize_audio_stream()

    manager.finalize_input_and_request_response.assert_awaited_once()
