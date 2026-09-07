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
