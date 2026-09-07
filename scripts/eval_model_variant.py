"""Compare frozen gameplay on Live audio and text Gemini without changing the bot."""
import argparse
import asyncio
import json
import os
from pathlib import Path

from src.evaluation.gameplay import conversation
from src.evaluation.gemini_text import GeminiTextEvaluation


async def run(args):
    args.output.mkdir(parents=True, exist_ok=True)
    os.environ['VOICE_OBSERVABILITY_DIR'] = str((args.output / 'trace').resolve())
    inputs = {key: json.loads(getattr(args, key).read_text(encoding='utf-8')) for key in ('suite', 'data', 'snapshot')}
    for key, value in inputs.items():
        (args.output / f'{key}.json').write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    text_mode = inputs['snapshot'].get('comparison_mode') == 'generateContent_text'
    reports = []
    for repeat in range(args.repeat):
        for scenario in inputs['suite']['scenarios']:
            manager = GeminiTextEvaluation() if text_mode else None
            try:
                kwargs = {'manager_factory': lambda *_a, **_k: manager} if text_mode else {}
                report = await conversation(scenario, inputs['data'], inputs['snapshot'], timeout=90, **kwargs)
            except Exception as exc:
                report = {'scenario': scenario['id'], 'complete': False, 'turns': [],
                          'error': f'{type(exc).__name__}:{getattr(exc, "code", "")}',
                          'manager_status': getattr(manager, '_last_response_status', None)}
            report['repetition'] = repeat + 1
            if manager:
                report['usage'] = manager.usage
                report['limitations'] = 'Text generateContent + frozen tools. Same instructions/context/questions; no audio input/output. Turn latency excludes TTS and is not directly comparable to Live end-of-audio latency.'
            reports.append(report)
            (args.output / f"{scenario['id']}.{repeat+1}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps({'model':inputs['snapshot']['model'], 'repeat':repeat+1,
                              'complete':report.get('complete'), 'turns':len(report['turns']),
                              'passed':sum(t['passed'] for t in report['turns']), 'error':report.get('error'),
                              'status':report.get('manager_status')}), flush=True)
            if report.get('error'):
                break
    (args.output / 'summary.json').write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ('suite', 'data', 'snapshot', 'output'):
        parser.add_argument('--'+arg, type=Path, required=True)
    parser.add_argument('--repeat', type=int, choices=[1,2], default=2)
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    if not args.live:
        parser.error('--live is required for provider quota use')
    asyncio.run(run(args))


if __name__ == '__main__':
    main()
