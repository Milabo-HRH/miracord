from unittest.mock import AsyncMock, MagicMock

import pytest

from src.ai_services.providers.gemini.manager import GeminiRealtimeManager


@pytest.mark.asyncio
async def test_audio_uses_current_gemini_live_audio_field() -> None:
    manager = GeminiRealtimeManager.__new__(GeminiRealtimeManager)
    manager._get_active_session = AsyncMock()
    manager._processing_audio_format = (16000, 1)
    session = MagicMock()
    session.send_realtime_input = AsyncMock()
    manager._get_active_session.return_value = session

    assert await manager.send_audio_chunk(b"pcm") is True

    session.send_realtime_input.assert_awaited_once()
    kwargs = session.send_realtime_input.await_args.kwargs
    assert "audio" in kwargs
    assert "media" not in kwargs
    assert kwargs["audio"].data == b"pcm"
    assert kwargs["audio"].mime_type == "audio/pcm;rate=16000"


@pytest.mark.asyncio
async def test_audio_is_paced_in_100ms_chunks() -> None:
    manager = GeminiRealtimeManager.__new__(GeminiRealtimeManager)
    manager._get_active_session = AsyncMock()
    manager._processing_audio_format = (16000, 1)
    session = MagicMock()
    session.send_realtime_input = AsyncMock()
    manager._get_active_session.return_value = session

    with pytest.MonkeyPatch.context() as monkeypatch:
        sleep = AsyncMock()
        monkeypatch.setattr(
            "src.ai_services.providers.gemini.manager.asyncio.sleep", sleep
        )
        assert await manager.send_audio_chunk(b"x" * 6400) is True

    assert session.send_realtime_input.await_count == 2
    sleep.assert_awaited_once_with(0.1)
