"""Score a 16 kHz mono PCM fixture with the configured wake-word model."""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np

from src.audio.wakeword import SherpaWakeWordModel
from src.config.config import Config


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("pcm_path", type=Path)
    args = parser.parse_args()

    if Config.WAKE_WORD_ENGINE != "sherpa_onnx":
        raise RuntimeError("This smoke test currently targets the Chinese sherpa model")
    model = SherpaWakeWordModel(
        model_dir=Config.SHERPA_WAKE_WORD_MODEL_DIR,
        keywords_file=Config.SHERPA_WAKE_WORD_KEYWORDS_PATH,
        phrase=Config.WAKE_WORD_PHRASE,
        keywords_score=Config.SHERPA_WAKE_WORD_SCORE,
        keywords_threshold=Config.SHERPA_WAKE_WORD_THRESHOLD,
    )
    model_name = Config.WAKE_WORD_PHRASE
    pcm = args.pcm_path.read_bytes()
    max_score = 0.0
    detected = False
    for offset in range(0, len(pcm), Config.WAKE_WORD_CHUNK_SIZE):
        chunk = pcm[offset : offset + Config.WAKE_WORD_CHUNK_SIZE]
        if len(chunk) < Config.WAKE_WORD_CHUNK_SIZE:
            chunk += b"\x00" * (Config.WAKE_WORD_CHUNK_SIZE - len(chunk))
        prediction = model.predict(np.frombuffer(chunk, dtype=np.int16))
        score = float(prediction.get(model_name, 0.0))
        max_score = max(max_score, score)
        detected = detected or score > Config.WAKE_WORD_THRESHOLD

    # Streaming decoders may emit the result only after receiving a short
    # trailing pause, so supply one second of silence after the fixture.
    if not detected:
        silence = np.zeros(Config.WAKE_WORD_CHUNK_SIZE // 2, dtype=np.int16)
        for _ in range(13):
            score = float(model.predict(silence).get(model_name, 0.0))
            max_score = max(max_score, score)
            if score > Config.WAKE_WORD_THRESHOLD:
                detected = True
                break

    print(
        f"Wake word score: {max_score:.4f}; "
        f"threshold={Config.WAKE_WORD_THRESHOLD:.4f}; detected={detected}"
    )


if __name__ == "__main__":
    main()
