from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from src.audio.wakeword import SherpaWakeWordModel


@pytest.fixture
def model_assets(tmp_path):
    for name in (
        "tokens.txt",
        "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
        "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
        "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
        "keywords.txt",
    ):
        (tmp_path / name).write_bytes(b"test")
    return tmp_path, tmp_path / "keywords.txt"


def test_sherpa_wake_word_model_detects_and_resets(model_assets):
    model_dir, keywords_file = model_assets
    spotter = MagicMock()
    stream = MagicMock()
    spotter.create_stream.return_value = stream
    spotter.is_ready.side_effect = [True, False]
    spotter.get_result.return_value = "豆包豆包"

    with patch("src.audio.wakeword.sherpa_onnx.KeywordSpotter", return_value=spotter):
        model = SherpaWakeWordModel(
            model_dir=model_dir,
            keywords_file=keywords_file,
            phrase="豆包豆包",
        )
        result = model.predict(np.array([0, 16384, -16384], dtype=np.int16))

    assert result == {"豆包豆包": 1.0}
    spotter.decode_stream.assert_called_once_with(stream)
    spotter.reset_stream.assert_called_once_with(stream)
    sample_rate, normalized = stream.accept_waveform.call_args.args
    assert sample_rate == 16000
    np.testing.assert_allclose(normalized, [0.0, 0.5, -0.5])


def test_sherpa_wake_word_model_reports_no_detection(model_assets):
    model_dir, keywords_file = model_assets
    spotter = MagicMock()
    spotter.create_stream.return_value = MagicMock()
    spotter.is_ready.return_value = False
    spotter.get_result.return_value = ""

    with patch("src.audio.wakeword.sherpa_onnx.KeywordSpotter", return_value=spotter):
        model = SherpaWakeWordModel(
            model_dir=model_dir,
            keywords_file=keywords_file,
            phrase="豆包豆包",
        )
        assert model.predict(np.zeros(1280, dtype=np.int16)) == {"豆包豆包": 0.0}


def test_sherpa_wake_word_model_rejects_missing_assets(tmp_path):
    with pytest.raises(FileNotFoundError, match="Missing sherpa-onnx"):
        SherpaWakeWordModel(
            model_dir=tmp_path,
            keywords_file=tmp_path / "keywords.txt",
            phrase="豆包豆包",
        )
