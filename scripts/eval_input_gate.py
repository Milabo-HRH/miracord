"""Offline gate check on a supplied mono 16 kHz PCM speech fixture."""
import argparse
import json
import wave
from pathlib import Path

import numpy as np

from src.audio.input_gate import SpeechInputGate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('--output', type=Path, default=Path('logs/evals/input-gate-check.json'))
    parser.add_argument('--dbfs', type=float, default=-42)
    args = parser.parse_args()
    with wave.open(str(args.input)) as source:
        assert (source.getframerate(), source.getnchannels(), source.getsampwidth()) == (16000, 1, 2)
        speech = source.readframes(source.getnframes())
    noise = np.random.default_rng(42).normal(0, 80, 32000).astype('<i2').tobytes()
    rows = []
    for name, pcm in [('quiet_noise', noise), ('speech', speech)]:
        gate = SpeechInputGate(open_dbfs=args.dbfs)
        output = b''.join(gate.process(pcm[i:i+3200]) for i in range(0, len(pcm), 3200)) + gate.flush()
        row = {'case': name, 'input_bytes': len(pcm), 'output_bytes': len(output),
               'all_silent': not any(output), 'metrics': gate.take_metrics()}
        rows.append(row)
    result = {'threshold_dbfs': args.dbfs, 'cases': rows,
              'limitations': 'Local gating only; no model, Discord, microphone, echo cancellation, or speech-recognition quality measurement.'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result))
    assert rows[0]['all_silent'] and not rows[1]['all_silent']


if __name__ == '__main__':
    main()
