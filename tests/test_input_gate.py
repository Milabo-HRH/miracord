import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from src.audio.input_gate import SpeechInputGate
from src.bot.session.guild_session import GuildSession
from src.config.config import Config


def frame(amplitude):
    return np.full(320, amplitude, dtype='<i2').tobytes()


def gate(speech=True):
    return SpeechInputGate(vad=MagicMock(is_speech=MagicMock(return_value=speech)))


def run(g, pcm):
    return g.process(pcm) + g.flush()


def test_quiet_noise_and_short_impulse_are_silence_with_equal_duration():
    for pcm in (frame(80) * 50, frame(0) * 5 + frame(12000) + frame(0) * 20):
        output = run(gate(), pcm)
        assert output == bytes(len(pcm))


def test_loud_non_speech_is_not_admitted_by_volume_alone():
    pcm = frame(12000) * 50
    assert run(gate(speech=False), pcm) == bytes(len(pcm))


def test_speech_onset_and_soft_tail_survive_with_bounded_lookahead():
    # 40 ms of soft onset, 100 ms of clear speech and 160 ms of soft tail.
    pcm = frame(90) * 2 + frame(2500) * 5 + frame(80) * 8
    g = gate()
    first = g.process(pcm)
    assert len(pcm) - len(first) == 4 * 640
    assert first + g.flush() == pcm
    assert g.flush() == b''


def test_gate_closes_after_hangover_and_handles_fragmented_pcm():
    pcm = frame(2500) * 10 + frame(40) * 50
    expected = run(gate(), pcm)
    g = gate()
    pieces = [g.process(pcm[i:i+333]) for i in range(0, len(pcm), 333)]
    assert b''.join(pieces) + g.flush() == expected
    assert expected[-640 * 20:] == bytes(640 * 20)
    assert not g.open
    metrics = g.take_metrics()
    assert metrics['muted_frames'] > 20 and metrics['frames'] == 60
    assert g.take_metrics()['frames'] == 0


@pytest.mark.asyncio
async def test_provider_gate_isolates_speaker_and_generation_and_blocked_flush():
    s = GuildSession.__new__(GuildSession)
    s._live_send_lock = asyncio.Lock()
    s._live_input_active = True
    s._live_stream_revision = 1
    s._is_user_upload_blocked = lambda user: False
    generation = [0]
    s._user_input_generation = lambda user: generation[0]
    s.ai_coordinator = MagicMock()
    s.ai_coordinator.get_processing_audio_format.return_value = (16000, 1)
    s.ai_coordinator.send_audio_stream_chunk = AsyncMock(return_value=True)
    def new_gate(*args, **kwargs):
        return gate()
    with patch.object(Config, 'VOICE_INPUT_GATE_ENABLED', True), patch(
        'src.bot.session.guild_session.SpeechInputGate', side_effect=new_gate
    ):
        assert await s._send_live_provider_chunk(1, 'one', frame(3000) * 5)
        old = s._input_gate
        assert old.pending
        # A new speaker must not receive the old speaker's delayed speech.
        assert await s._send_live_provider_chunk(2, 'two', frame(0) * 5)
        sent = s.ai_coordinator.send_audio_stream_chunk.await_args
        assert sent.args[0] == bytes(640)
        assert sent.kwargs['user_id'] == 2
        assert s._input_gate is not old
        previous = s._input_gate
        generation[0] += 1
        assert await s._send_live_provider_chunk(2, 'two', frame(0) * 5)
        assert s._input_gate is not previous
        s.ai_coordinator.send_audio_stream_chunk.reset_mock()
        s._is_user_upload_blocked = lambda user: True
        assert not await s._send_live_provider_chunk(2, 'two', b'', flush_gate=True)
        s.ai_coordinator.send_audio_stream_chunk.assert_not_awaited()


@pytest.mark.asyncio
async def test_authorized_finish_flushes_delayed_speech_before_finalize():
    s = GuildSession.__new__(GuildSession)
    s._live_send_lock = asyncio.Lock()
    s._live_input_active = True
    s._live_stream_revision = 1
    s._live_input_user_id, s._live_input_user_name = 1, 'one'
    s._live_input_generation = 0
    s._live_audio_buffer = bytearray()
    s._live_audio_queue = asyncio.Queue()
    s._is_user_upload_blocked = s._is_user_input_blocked = lambda user: False
    s._user_input_generation = lambda user: 0
    s._audio_processor = MagicMock()
    s.bot_state = MagicMock(current_session_id=1)
    s.ai_coordinator = MagicMock()
    s.ai_coordinator.get_processing_audio_format.return_value = (16000, 1)
    order = []
    async def send(pcm, **kwargs):
        order.append(pcm)
        return True
    async def finalize():
        order.append('finalize')
        return True
    s.ai_coordinator.send_audio_stream_chunk = AsyncMock(side_effect=send)
    s.ai_coordinator.finalize_audio_stream = AsyncMock(side_effect=finalize)
    with patch.object(Config, 'VOICE_INPUT_GATE_ENABLED', True), patch(
        'src.bot.session.guild_session.SpeechInputGate', side_effect=lambda *a, **k: gate()
    ):
        await s._send_live_provider_chunk(1, 'one', frame(3000) * 5)
        assert await s._finish_live_audio_input(finalize=True, flush=True)
    assert order[-1] == 'finalize'
    assert b''.join(order[:-1]) == frame(3000) * 5
    assert s._input_gate is None
