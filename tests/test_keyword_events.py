import pytest

from src.audio.keyword_events import KeywordEventTracker


@pytest.mark.parametrize('stop', ['闭嘴', '结束'])
def test_stop_excludes_old_audio_but_allows_immediate_new_wake(stop):
    tracker = KeywordEventTracker()
    assert tracker.consume(0, 800, ['豆包']) == ['豆包']
    assert tracker.consume(0, 1100, [stop]) == [stop]
    assert tracker.consume(0, 1400, ['豆包']) == []
    assert tracker.minimum_start == 1100
    assert tracker.consume(1100, 1600, ['豆包']) == ['豆包']


def test_stop_wins_ambiguous_window_and_streams_are_independent():
    first, second = KeywordEventTracker(), KeywordEventTracker()
    assert first.consume(0, 100, ['豆包', '闭嘴']) == ['闭嘴']
    assert second.consume(0, 100, ['豆包']) == ['豆包']


def test_evaluator_redecodes_only_post_stop_audio(tmp_path):
    import numpy as np
    import soundfile as sf
    from scripts.sensevoice_fp16 import Evaluator

    class Cuda:
        def reset_peak_memory_stats(self): pass
        def get_device_name(self, *_): return 'fake'
        def max_memory_allocated(self): return 0
        def memory_reserved(self): return 0

    class Fake(Evaluator):
        def __init__(self):
            self.torch = type('Torch', (), {'cuda': Cuda()})()
            self.model_source = {'revision': 'test', 'repo': 'test'}
            self.dtype = 'test'
            self.lengths = []

        def transcribe(self, samples):
            self.lengths.append(len(samples))
            return [('豆包', 0), ('闭嘴', 0), ('安静', 0), ('豆包', 0)][len(self.lengths)-1]

    path = tmp_path/'audio.wav'
    sf.write(path, np.zeros(22400), 16000)
    evaluator = Fake()
    result = evaluator.evaluate(path, rolling=True)
    assert evaluator.lengths == [8000, 12800, 4800, 9600]
    assert [h['keyword'] for h in result['hits']] == ['豆包', '闭嘴', '豆包']
    assert result['windows'][2]['start_s'] == .8
