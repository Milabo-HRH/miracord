"""Same-corpus local detector comparison. Run each GPU backend in its own process."""
import argparse
import hashlib
import json
from pathlib import Path
import time

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly
import math

from scripts.sensevoice_fp16 import ROOT, save_json

OUT = ROOT/'logs/evals/detector-comparison-20260906'
ASSETS = ROOT/'assets/wakeword_models'
CN = ASSETS/'sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01'
BI = ASSETS/'sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20'


def prepare():
    paths = sorted(p for p in (ROOT/'logs/sensevoice/inbox').glob('*.wav')
                   if p.stem.startswith(('豆包', '闭嘴', '结束')))
    assert len(paths) == 7
    paths += sorted((BI/'test_wavs').glob('*.wav'))
    cases = []
    for path in paths:
        samples, rate = sf.read(path, dtype='float32', always_2d=True)
        samples = samples.mean(axis=1)
        divisor = math.gcd(rate, 16000)
        samples = resample_poly(samples, 16000//divisor, rate//divisor)
        seconds = len(samples)/16000
        samples = np.concatenate([samples, np.zeros(9600)])
        target = OUT/'audio'/path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        sf.write(target, samples, 16000, subtype='PCM_16')
        cases.append(dict(file=target.name, source=str(path.relative_to(ROOT)),
                          source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                          sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
                          original_duration_s=seconds, tail_s=0.6,
                          expected=next((w for w in ['豆包','闭嘴','结束'] if path.stem.startswith(w)), None)))
    save_json(OUT/'cases.json', cases)
    print('Prepared', len(cases), 'identical inputs with 600 ms tail silence.')


def run(backend):
    cases = json.loads((OUT/'cases.json').read_text(encoding='utf-8'))
    if backend == 'sensevoice':
        from scripts.sensevoice_fp16 import Evaluator
        evaluator = Evaluator('fp16')
    elif backend.startswith('paraformer'):
        from scripts.eval_paraformer_keywords import ParaformerEvaluator
        evaluator = ParaformerEvaluator('fp32')
        evaluator.hotwords = '豆包 闭嘴 结束' if backend.endswith('hotwords') else None
    for case in cases:
        path = OUT/'audio'/case['file']
        assert hashlib.sha256(path.read_bytes()).hexdigest() == case['sha256']
        if backend.startswith('kws'):
            from src.audio.wakeword import SherpaWakeWordModel
            directory = CN if backend == 'kws-chinese' else BI
            started = time.perf_counter()
            detector = SherpaWakeWordModel(model_dir=directory,
                keywords_file=directory/'keywords_control.txt', phrase='豆包',
                keywords_score=3, keywords_threshold=.1, num_threads=2)
            load_ms = (time.perf_counter()-started)*1000
            samples, rate = sf.read(path, dtype='int16')
            assert rate == 16000 and samples.ndim == 1
            hits, calls = [], []
            for start in range(0, len(samples), 640):
                chunk = samples[start:start+640]
                end = min(start+640, len(samples))/16000
                started = time.perf_counter()
                values = detector.predict(chunk)
                elapsed = (time.perf_counter()-started)*1000
                calls.append(elapsed)
                hits += [dict(keyword=k, available_audio_s=end) for k,v in values.items() if v > .5]
            result = dict(hits=hits, duration_s=len(samples)/16000,
                          total_inference_ms=sum(calls), call_ms=calls, load_ms=load_ms,
                          device='cpu', peak_allocated_mib=0,
                          keywords=(directory/'keywords_control.txt').read_text(encoding='utf-8'),
                          model_dir=str(directory.relative_to(ROOT)))
            del detector
        else:
            result = evaluator.evaluate(path, rolling=True)
            result['call_ms'] = [w['inference_ms'] for w in result['windows']]
            result['hotwords'] = getattr(evaluator, 'hotwords', None)
        result.update(backend=backend, case=case)
        save_json(OUT/backend/(path.name+'.json'), result)
        print(json.dumps(dict(backend=backend, file=path.name, hits=result['hits']), ensure_ascii=False),flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=['prepare','kws-chinese','kws-bilingual','sensevoice','paraformer-baseline','paraformer-hotwords'])
    args = parser.parse_args()
    prepare() if args.stage == 'prepare' else run(args.stage)
