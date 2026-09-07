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
