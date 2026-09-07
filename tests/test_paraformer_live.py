import pytest
from concurrent.futures import Future
import numpy as np
from src.audio.paraformer import ParaformerWakeWordModel


@pytest.fixture(autouse=True)
def permit_test_audio(monkeypatch):
    monkeypatch.setattr('src.config.config.Config.WAKE_WORD_PHRASE', '豆包')
    monkeypatch.setattr("src.audio.paraformer.speech_metrics", lambda audio: {"speech_qualified": True})


class Jobs:
    def __init__(self):
        self.jobs = []

    def submit(self, fn, *args):
        future = Future()
        future.set_running_or_notify_cancel()
        self.jobs.append((future, fn, args))
        return future

    def finish(self):
        future, fn, args = self.jobs[-1]
        future.set_result(fn(*args))


class Runtime:
    def __init__(self, text):
        self.text = text

    def transcribe(self, audio):
        return self.text, 10


@pytest.mark.parametrize('text,expected', [('妈老豆包', {}), ('豆包，豆包', {'豆包豆包': 1.0}), ('闭嘴', {'闭嘴': 1.0})])
def test_configurable_double_wake_does_not_accept_single(monkeypatch, text, expected):
    monkeypatch.setattr('src.config.config.Config.WAKE_WORD_PHRASE', '豆包豆包')
    jobs = Jobs()
    model = ParaformerWakeWordModel(1, Runtime(text), jobs)
    model.predict(np.ones(8000, dtype=np.int16))
    jobs.finish()
    assert model.poll() == expected


def test_nonblocking_bounded_queue_and_audio():
    jobs = Jobs()
    model = ParaformerWakeWordModel(1, Runtime('豆包'), jobs)
    for _ in range(100):
        assert model.predict(np.ones(1280, dtype=np.int16)) == {}
    assert len(jobs.jobs) == 1
    assert len(model._audio) == 32000
    jobs.finish()
    assert model.poll() == {'豆包': 1.0}


def test_reset_discards_inflight_old_speaker_turn():
    jobs = Jobs()
    model = ParaformerWakeWordModel(1, Runtime('豆包'), jobs)
    model.predict(np.ones(8000, dtype=np.int16))
    model.reset()
    jobs.finish()
    assert model.poll() == {}
    model.predict(np.ones(8000, dtype=np.int16))
    jobs.finish()
    assert model.poll() == {'豆包': 1.0}


def test_stop_wins_and_new_wake_after_boundary():
    jobs = Jobs()
    runtime = Runtime('豆包闭嘴')
    model = ParaformerWakeWordModel(1, runtime, jobs)
    model.predict(np.ones(8000, dtype=np.int16))
    jobs.finish()
    assert model.poll() == {'闭嘴': 1.0}
    runtime.text = '豆包'
    model.predict(np.ones(8000, dtype=np.int16))
    assert jobs.jobs[-1][2][1] == 8000
    jobs.finish()
    assert model.poll() == {'豆包': 1.0}


def test_silent_audio_never_reaches_hotword_asr(monkeypatch):
    monkeypatch.undo()
    jobs = Jobs()
    runtime = Runtime('豆包')
    model = ParaformerWakeWordModel(1, runtime, jobs)
    model.predict(np.zeros(8000, dtype=np.int16))
    jobs.finish()
    assert model.poll() == {}


def test_long_rtp_gap_discards_old_window(monkeypatch):
    jobs = Jobs()
    model = ParaformerWakeWordModel(1, Runtime('豆包'), jobs)
    model.predict(np.ones(8000, dtype=np.int16))
    model._last_feed -= 2
    model.predict(np.ones(1280, dtype=np.int16))
    jobs.finish()
    assert model.poll() == {}
    assert len(model._audio) == 1280
