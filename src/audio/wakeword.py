"""Wake-word detector adapters used by the Discord audio sink."""

from __future__ import annotations

from pathlib import Path
import logging
import time

import numpy as np
import sherpa_onnx

logger = logging.getLogger(__name__)


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
        def model_path(component: str) -> Path:
            # Prefer a matched release and chunk size; decoder may only ship fp32.
            for suffix in (".int8.onnx", ".onnx"):
                matches = sorted(model_dir.glob(f"{component}-*-chunk-16-left-64{suffix}"))
                if matches:
                    return matches[-1]
            return model_dir / f"{component}-missing.onnx"

        paths = {
            "tokens": model_dir / "tokens.txt",
            "encoder": model_path("encoder"),
            "decoder": model_path("decoder"),
            "joiner": model_path("joiner"),
            "keywords_file": keywords_file,
        }
        missing = [str(path) for path in paths.values() if not path.is_file()]
        if missing:
            raise FileNotFoundError(
                "Missing sherpa-onnx wake-word assets: " + ", ".join(missing)
            )

        self.phrase = phrase
        self._last_unit_hit: float | None = None
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
        logger.info(
            "Sherpa keyword detector ready: model=%s phrase=%s default_score=%s "
            "default_threshold=%s keyword_file_overrides=%s",
            model_dir.name, phrase, keywords_score, keywords_threshold,
            keywords_file.read_text(encoding="utf-8").strip().splitlines(),
        )

    def predict(self, samples: np.ndarray) -> dict[str, float]:
        """Consume int16 16 kHz mono PCM and report a detected phrase."""
        normalized = np.asarray(samples, dtype=np.int16).astype(np.float32) / 32768.0
        self._stream.accept_waveform(16000, normalized)
        while self._spotter.is_ready(self._stream):
            self._spotter.decode_stream(self._stream)
        result = self._spotter.get_result(self._stream)
        if result:
            # Preserve the actual result: treating every keyword as the wake
            # phrase would turn a stop command into an activation.
            keyword = str(result).split(":", 1)[0]
            self._spotter.reset_stream(self._stream)
            if keyword == self.phrase:
                self._last_unit_hit = None
                return {self.phrase: 1.0}
            if self.phrase == keyword * 2:
                now = time.monotonic()
                previous = self._last_unit_hit
                self._last_unit_hit = now
                if previous is not None and now - previous <= 2.0:
                    self._last_unit_hit = None
                    return {self.phrase: 1.0}
                return {self.phrase: 0.0}
            return {keyword: 1.0}
        return {self.phrase: 0.0}

    def reset(self) -> None:
        self._last_unit_hit = None
        self._spotter.reset_stream(self._stream)
