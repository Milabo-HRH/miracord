"""Compare real SeACo hotword decoding on local recordings; no live gate changes."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import time

from scripts.sensevoice_fp16 import Evaluator, ROOT, save_json

STATE = ROOT / 'logs/paraformer'
MODEL = STATE / 'seaco-model'


class ParaformerEvaluator(Evaluator):
    def __init__(self, precision='fp32'):
        os.environ['HF_HUB_OFFLINE'] = '1'
        import numpy as np
        import torch
        from funasr import AutoModel
        self.torch = torch
        self.precision = precision
        if not torch.cuda.is_available():
            raise RuntimeError('CUDA required')
        torch.set_num_threads(2)
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
        self.model_source = json.loads((STATE/'source-seaco.json').read_text())
        self.hotwords = None
        self.model = AutoModel(model=str(MODEL), device='cuda:0', fp16=precision == 'fp16',
                               disable_update=True, disable_pbar=True,
                               trust_remote_code=False, ncpu=2)
        self.model.model.eval()
        assert type(self.model.model).__name__ == 'SeacoParaformer'
        expected = torch.float16 if precision == 'fp16' else torch.float32
        assert {p.dtype for p in self.model.model.parameters() if p.is_floating_point()} == {expected}
        self.dtype = str(expected)
        self.transcribe(np.zeros(16000, dtype=np.float32))
        torch.cuda.empty_cache()

    def transcribe(self, samples):
        torch = self.torch
        torch.cuda.synchronize()
        started = time.perf_counter()
        with torch.inference_mode(), torch.autocast('cuda', dtype=torch.float16, enabled=self.precision == 'fp16'):
            rows = self.model.generate(input=samples, cache={}, batch_size=1,
                                       hotword=self.hotwords, disable_pbar=True)
        torch.cuda.synchronize()
        actual_hotwords = self.model.model.hotword_list
        if self.hotwords:
            assert actual_hotwords is not None and len(actual_hotwords) == 4
        else:
            assert actual_hotwords is None
        return ' '.join(row.get('text', '') for row in rows), round((time.perf_counter()-started)*1000, 2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('audio', nargs='*', type=Path)
    parser.add_argument('--precision', choices=['fp16', 'fp32'], default='fp32')
    args = parser.parse_args()
    model_path = MODEL/'model.pt'
    digest = hashlib.sha256()
    with model_path.open('rb') as source:
        for chunk in iter(lambda:source.read(4*1024*1024), b''):
            digest.update(chunk)
    save_json(STATE/'source-seaco.json', dict(
        repo='iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch',
        revision='sha256:'+digest.hexdigest(), downloaded_ref='master',
        runtime='funasr==1.4.14'))
    evaluator = ParaformerEvaluator(args.precision)
    paths = args.audio or sorted(p for p in (ROOT/'logs/sensevoice/inbox').glob('*.wav')
                                if not p.name.startswith('check-'))
    if not args.audio:
        paths += [ROOT/'logs/sensevoice/inbox'/f'check-negative-{lang}.wav' for lang in ['zh','en']]
    summary = []
    for enabled in [False, True]:
        evaluator.hotwords = '豆包 闭嘴 结束' if enabled else None
        mode = 'hotwords' if enabled else 'baseline'
        for path in paths:
            result = evaluator.evaluate(path, rolling=True)
            result['hotwords'] = evaluator.hotwords
            result['hotword_module_verified'] = True
            save_json(STATE/'results'/args.precision/mode/(path.name+'.json'), result)
            summary.append(dict(file=path.name, mode=mode, hits=result['hits'],
                                final_text=result['windows'][-1]['text'],
                                total_inference_ms=result['total_inference_ms'],
                                peak_allocated_mib=result['peak_allocated_mib']))
            print(json.dumps(summary[-1], ensure_ascii=False), flush=True)
    save_json(STATE/'results'/args.precision/'summary.json', summary)


if __name__ == '__main__':
    main()
