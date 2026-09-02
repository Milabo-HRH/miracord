"""Wake-word detector adapters used by the Discord audio sink."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import sherpa_onnx


class SherpaWakeWordModel:
    """Expose sherpa-onnx streaming keyword spotting like openWakeWord.Model."""

    def __init__(
        self,
        *,
        model_dir: Path,
        keywords_file: Path,
        phrase: str,
        num_threads: int = 2,
        keywords_score: float = 1.0,
        keywords_threshold: float = 0.25,
    ) -> None:
        paths = {
            "tokens": model_dir / "tokens.txt",
            "encoder": model_dir
            / "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
            "decoder": model_dir
            / "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
            "joiner": model_dir
            / "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
            "keywords_file": keywords_file,
        }
        missing = [str(path) for path in paths.values() if not path.is_file()]
        if missing:
            raise FileNotFoundError(
                "Missing sherpa-onnx wake-word assets: " + ", ".join(missing)
            )

        self.phrase = phrase
        self._spotter = sherpa_onnx.KeywordSpotter(
            tokens=str(paths["tokens"]),
            encoder=str(paths["encoder"]),
            decoder=str(paths["decoder"]),
            joiner=str(paths["joiner"]),
            keywords_file=str(paths["keywords_file"]),
            num_threads=num_threads,
            keywords_score=keywords_score,
            keywords_threshold=keywords_threshold,
            provider="cpu",
        )
        self._stream = self._spotter.create_stream()

    def predict(self, samples: np.ndarray) -> dict[str, float]:
        """Consume int16 16 kHz mono PCM and report a detected phrase."""
        normalized = np.asarray(samples, dtype=np.int16).astype(np.float32) / 32768.0
        self._stream.accept_waveform(16000, normalized)
        while self._spotter.is_ready(self._stream):
            self._spotter.decode_stream(self._stream)
        result = self._spotter.get_result(self._stream)
        if result:
            self.reset()
            return {self.phrase: 1.0}
        return {self.phrase: 0.0}

    def reset(self) -> None:
        self._spotter.reset_stream(self._stream)
