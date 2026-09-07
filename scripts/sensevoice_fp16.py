"""Local-only GPU FP16 recording evaluator; never controls Discord admission.

Run with the isolated logs/sensevoice/venv interpreter. --watch consumes stable
audio files in logs/sensevoice/inbox and writes JSON reports alongside results.
"""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
import unicodedata
import sys

ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ''):
    sys.path.insert(0, str(ROOT))

from src.audio.keyword_events import KeywordEventTracker

STATE = ROOT / 'logs/sensevoice'
KEYWORDS = ('豆包', '闭嘴', '结束')


def keyword_candidates(text):
    clean = re.sub(r'<\|.*?\|>', '', text)
    clean = ''.join(c for c in unicodedata.normalize('NFKC', clean) if c.isalnum())
    return [word for word in KEYWORDS if word in clean]


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


class Evaluator:
    def __init__(self, precision='fp16'):
        # No model downloads or uploaded audio during inference.
        os.environ['HF_HUB_OFFLINE'] = '1'
        os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
        import numpy as np
        import torch
        from funasr import AutoModel
        self.torch = torch
        if precision not in {'fp16', 'fp32'}:
            raise ValueError('precision must be fp16 or fp32')
        self.precision = precision
        self.model_source = json.loads((STATE/'source.json').read_text())
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
        if not torch.cuda.is_available():
            raise RuntimeError('CUDA unavailable; refusing a silent CPU fallback')
        torch.set_num_threads(2)
        self.model = AutoModel(
            model=str(STATE / 'model'), device='cuda:0', fp16=precision == 'fp16',
            disable_update=True, disable_pbar=True, trust_remote_code=False,
            ncpu=2,
        )
        self.model.model.eval()
        self.dtype = str(next(self.model.model.parameters()).dtype)
        expected_dtype = torch.float16 if precision == 'fp16' else torch.float32
        assert {p.dtype for p in self.model.model.parameters() if p.is_floating_point()} == {expected_dtype}
        self.transcribe(np.zeros(16000, dtype=np.float32))
        torch.cuda.empty_cache()

    def transcribe(self, samples):
        torch = self.torch
        torch.cuda.synchronize()
        started = time.perf_counter()
        with torch.inference_mode(), torch.autocast('cuda', dtype=torch.float16, enabled=self.precision == 'fp16'):
            rows = self.model.generate(input=samples, cache={}, language='zh',
                                       use_itn=False, batch_size=1, disable_pbar=True)
        torch.cuda.synchronize()
        return ' '.join(row.get('text', '') for row in rows), round((time.perf_counter()-started)*1000, 2)

    def evaluate(self, path, *, rolling=False):
        import numpy as np
        import soundfile as sf
        from scipy.signal import resample_poly
        path = Path(path)
        info = sf.info(path)
        if not 0 < info.duration <= 60 or info.channels > 8:
            raise ValueError('Use a recording between 0 and 60 seconds, at most 8 channels')
        samples, rate = sf.read(path, dtype='float32', always_2d=True)
        samples = samples.mean(axis=1)
        if not np.isfinite(samples).all():
            raise ValueError('Nonfinite audio samples')
        divisor = math.gcd(rate, 16000)
        samples = resample_poly(samples, 16000//divisor, rate//divisor).astype(np.float32)
        self.torch.cuda.reset_peak_memory_stats()
        windows = []
        if rolling:
            ends = list(range(8000, len(samples), 4800)) + [len(samples)]
            spans = [(max(0, end-32000), end) for end in ends]
        else:
            spans = [(start, min(start+160000, len(samples))) for start in range(0, len(samples), 160000)]
        tracker = KeywordEventTracker()
        hits = []
        for start, end in spans:
            start = max(start, tracker.minimum_start)
            if start >= end:
                continue
            text, elapsed = self.transcribe(samples[start:end])
            candidates = keyword_candidates(text)
            windows.append(dict(start_s=round(start/16000, 3), end_s=round(end/16000, 3),
                                text=text, inference_ms=elapsed, candidates=candidates))
            events = tracker.consume(start, end, candidates)
            for word in events:
                hits.append(dict(keyword=word, available_audio_s=round(end/16000, 3)))
        return dict(status='completed', file=path.name, sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    model_revision=self.model_source['revision'],
                    model_repo=self.model_source['repo'],
                    device=self.torch.cuda.get_device_name(0), weights_dtype=self.dtype,
                    tf32_enabled=False,
                    mode='rolling_2s_hop_300ms' if rolling else 'chunks_10s',
                    duration_s=round(len(samples)/16000, 3), windows=windows, hits=hits,
                    total_inference_ms=round(sum(w['inference_ms'] for w in windows), 2),
                    peak_allocated_mib=round(self.torch.cuda.max_memory_allocated()/2**20, 1),
                    reserved_mib=round(self.torch.cuda.memory_reserved()/2**20, 1),
                    limitations='Offline recording replay, keyword candidates only. No live gate action. '
                                 'Hit times are window endpoints, not word timestamps or measured live latency. '
                                 'Stops reset the input window to the stop detection endpoint; '
                                 'same-window stop overrides wake. No word-level alignment.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('audio', nargs='*', type=Path)
    parser.add_argument('--watch', action='store_true')
    parser.add_argument('--rolling', action='store_true')
    args = parser.parse_args()
    STATE.mkdir(parents=True, exist_ok=True)
    inbox, results = STATE/'inbox', STATE/'results'
    inbox.mkdir(exist_ok=True)
    results.mkdir(exist_ok=True)
    status_path = STATE/'status.json'
    if args.watch:
        save_json(status_path, dict(status='loading', pid=os.getpid()))
    evaluator = Evaluator()
    for path in args.audio:
        result = evaluator.evaluate(path, rolling=args.rolling)
        save_json(results/(path.stem+('.rolling' if args.rolling else '')+'.json'), result)
        print(json.dumps({k:v for k,v in result.items() if k != 'windows'}, ensure_ascii=False), flush=True)
    if not args.watch:
        return
    ready = dict(status='ready', pid=os.getpid(), dtype=evaluator.dtype,
                 gpu=evaluator.torch.cuda.get_device_name(0), mode='recording_test_only',
                 allocated_mib=round(evaluator.torch.cuda.memory_allocated()/2**20, 1))
    seen = {}
    stable = {}
    try:
        while True:
            save_json(status_path, dict(ready, heartbeat_unix=time.time()))
            for path in inbox.iterdir():
                if not path.is_file() or path.suffix.lower() not in {'.wav', '.flac', '.ogg', '.mp3'}:
                    continue
                stat = path.stat()
                signature = (stat.st_size, stat.st_mtime_ns)
                if seen.get(path.name) == signature:
                    continue
                if stable.get(path.name) != signature:
                    stable[path.name] = signature
                    continue
                try:
                    result = evaluator.evaluate(path, rolling=True)
                except Exception as exc:
                    result = dict(status='failed', file=path.name, error=type(exc).__name__, detail=str(exc)[:500])
                save_json(results/(path.name+'.json'), result)
                seen[path.name] = signature
                print(json.dumps(dict(file=path.name, status=result['status'], hits=result.get('hits')), ensure_ascii=False), flush=True)
            time.sleep(1)
    finally:
        save_json(status_path, dict(status='stopped', pid=os.getpid()))


if __name__ == '__main__':
    main()
