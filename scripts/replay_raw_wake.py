"""Replay local raw captures through the deployed wake-word conversion path."""
import argparse
import json
import wave
from collections import defaultdict
from pathlib import Path

import numpy as np

from src.audio.processing import UnifiedAudioProcessor, DISCORD_FORMAT, WAKE_WORD_FORMAT, ProcessingStrategy
from src.audio.wakeword import SherpaWakeWordModel
from src.config.config import Config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    records = defaultdict(list)
    for line in (args.directory / 'frames.jsonl').read_text(encoding='utf-8').splitlines():
        row = json.loads(line)
        records[row['speaker']].append(row)
    report = {'model': str(Config.SHERPA_WAKE_WORD_MODEL_DIR),
              'keywords': Config.SHERPA_WAKE_WORD_KEYWORDS_PATH.read_text(encoding='utf-8'),
              'method': 'Original PCM conversion and recorded per-speaker arrival gaps; six 40 ms silent KWS chunks after a 350 ms RTP gap.',
              'speakers': {}}
    for speaker, rows in records.items():
        if any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789_' for c in speaker):
            raise ValueError('Invalid speaker filename')
        detector = SherpaWakeWordModel(model_dir=Config.SHERPA_WAKE_WORD_MODEL_DIR,
            keywords_file=Config.SHERPA_WAKE_WORD_KEYWORDS_PATH, phrase=Config.WAKE_WORD_PHRASE,
            keywords_score=Config.SHERPA_WAKE_WORD_SCORE, keywords_threshold=Config.SHERPA_WAKE_WORD_THRESHOLD)
        processor = UnifiedAudioProcessor()
        raw = bytearray()
        mono = bytearray()
        hits = []
        def predict(chunk, at_ms):
            result = detector.predict(chunk)
            positive = [k for k,v in result.items() if v > 0.5]
            if positive:
                hits.extend({'keyword': k, 'arrival_ms': round(at_ms, 1)} for k in positive)
                detector.reset()
                raw.clear()
                mono.clear()
                return True
            return False
        previous = None
        with wave.open(str(args.directory / f'{speaker}.wav')) as source:
            assert (source.getframerate(), source.getnchannels(), source.getsampwidth()) == (48000, 2, 2)
            for row in rows:
                at = row['arrival_ms']
                if previous is not None and at - previous >= 350:
                    for n in range(min(6, int((at - previous - 350) // 20) + 1)):
                        if predict(np.zeros(640, dtype=np.int16), previous + 350 + n * 20):
                            break
                source.setpos(row['offset_frames'])
                raw.extend(source.readframes(row['pcm_frames']))
                while len(raw) >= 7680:
                    chunk = bytes(raw[:7680]); del raw[:7680]
                    mono.extend(processor.convert_sync(DISCORD_FORMAT, WAKE_WORD_FORMAT,
                        chunk, strategy=ProcessingStrategy.REALTIME, state_key=speaker))
                while len(mono) >= 1280:
                    chunk = bytes(mono[:1280]); del mono[:1280]
                    if predict(np.frombuffer(chunk, dtype=np.int16), at):
                        break
                previous = at
            if previous is not None:
                for n in range(6):
                    if predict(np.zeros(640, dtype=np.int16), previous + 350 + n * 20):
                        break
        report['speakers'][speaker] = {'received_frames': len(rows), 'hits': hits}
    path = args.directory / 'wake-replay.json'
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report['speakers'], ensure_ascii=False))


if __name__ == '__main__':
    main()
