import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from src.audio.continuous_sink import ContinuousAudioSink
from src.bot.session.guild_session import GuildSession
from src.config.config import Config


def packet(sink, user_id, level, *, bot=False, frames=1):
    pcm = np.full(1920 * frames, level, dtype='<i2').tobytes()
    sink.write(SimpleNamespace(id=user_id, bot=bot), SimpleNamespace(pcm=pcm))


@pytest.mark.asyncio
async def test_mixes_overlapping_humans_without_stretching_or_filtering_quiet_audio():
    sink = ContinuousAudioSink([1, 2], AsyncMock(return_value=True))
    try:
        packet(sink, 1, 4)  # Very quiet input is still forwarded.
        packet(sink, 2, 5)
        packet(sink, 3, 2000)  # Not admitted.
        packet(sink, 1, 3000, bot=True)
        assert np.all(np.frombuffer(sink.mix_frame(), dtype='<i2') == 9)
        assert sink.mix_frame() == bytes(3840)
        packet(sink, 1, 30000)
        packet(sink, 2, 30000)
        assert np.all(np.frombuffer(sink.mix_frame(), dtype='<i2') == 32767)
    finally:
        sink.cleanup()


@pytest.mark.asyncio
async def test_membership_cleanup_and_backlog_are_bounded():
    sink = ContinuousAudioSink([], AsyncMock(return_value=True))
    try:
        sink.add_user(1)
        packet(sink, 1, 9, frames=20)
        assert len(sink._buffers[1]) == sink.MAX_BUFFER
        sink.remove_user(1)
        assert sink.mix_frame() == bytes(3840)
        sink.cleanup()
        sink.add_user(1)
        packet(sink, 1, 9)
        assert not sink._buffers
    finally:
        sink.cleanup()


@pytest.mark.asyncio
async def test_silence_continues_without_wake_and_false_send_stops_loop():
    send = AsyncMock(side_effect=[True, True, False])
    sink = ContinuousAudioSink([1], send)
    await asyncio.wait_for(sink._task, timeout=1)
    assert send.await_count == 3
    assert all(call.args == (bytes(3840),) for call in send.await_args_list)
    assert sink._closed


@pytest.mark.asyncio
async def test_desktop_session_bypasses_keyword_sink_and_prevents_provider_leak(monkeypatch):
    monkeypatch.setattr(Config, 'DESKTOP_VOICE_ALWAYS_FORWARD', True)
    session = GuildSession.__new__(GuildSession)
    session.bot_state = MagicMock(active_ai_provider_name='desktop_voice')
    session.bot_state.get_consented_user_ids.return_value = [1]
    manager = MagicMock(processing_audio_format=(48000, 2))
    manager.send_audio_chunk = AsyncMock(return_value=True)
    session.ai_coordinator = MagicMock(active_ai_service_manager=manager)
    session.voice_connection = MagicMock()
    await session._initialize_sink()
    sink = session._audio_sink
    try:
        assert isinstance(sink, ContinuousAudioSink)
        packet(sink, 1, 7)
        assert await sink._send_audio(sink.mix_frame())
        assert np.all(np.frombuffer(manager.send_audio_chunk.call_args.args[0], dtype='<i2') == 7)
        session.ai_coordinator.active_ai_service_manager = MagicMock()
        assert not await sink._send_audio(bytes(3840))
        assert manager.send_audio_chunk.await_count == 1
    finally:
        sink.cleanup()
