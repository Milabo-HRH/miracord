"""Compare prompt snapshots on reviewed frozen cases; no Discord or live tools."""

import argparse
import asyncio
import json
from pathlib import Path

from src.evaluation.interactions import replay


async def run(args):
    suite = json.loads(args.cases.read_text(encoding="utf-8"))
    variants = {name: json.loads(path.read_text(encoding="utf-8"))
                for name, path in [("baseline", args.baseline), ("candidate", args.candidate)]}
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "cases.json").write_text(json.dumps(suite, ensure_ascii=False, indent=2), encoding="utf-8")
    reports = []
    for repetition in range(args.repeat):
        for case in suite["cases"]:
            # Alternate order to reduce a systematic first/second-run bias.
            for name in list(variants)[::1 if repetition % 2 == 0 else -1]:
                stem = f"{case['id']}.{name}.{repetition + 1}"
                report = await replay(case, snapshot=variants[name], live=args.live,
                                      timeout=args.timeout,
                                      trace_path=args.output / f"{stem}.trace.jsonl")
                report.update(variant=name, repetition=repetition + 1, origin="eval",
                              case=case, question=case["question"],
                              review_rubric=case.get("review_rubric", []))
                (args.output / f"{stem}.json").write_text(
                    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                reports.append(report)
                print(json.dumps({"case": case["id"], "variant": name,
                                  "repeat": repetition + 1, "passed": report["passed"],
                                  "error": report["error"], "answers": report["answers"]},
                                 ensure_ascii=True), flush=True)
    summary = {"mode": "live" if args.live else "offline", "reports": [
        {k: r[k] for k in ("case_id", "variant", "repetition", "passed", "error", "checks",
                           "answers", "case_sha256", "snapshot_sha256", "elapsed_ms")}
        for r in reports], "limitations": "Rule checks are not a calibrated quality score. "
        "Human review required. Frozen synthetic facts, text input, no ASR or Discord."}
    (args.output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if all(r["passed"] for r in reports) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=Path("tests/fixtures/interactions/answer_quality.json"))
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeat", type=int, choices=range(1, 4), default=1)
    parser.add_argument("--timeout", type=float, default=40)
    parser.add_argument("--live", action="store_true", help="Uses provider API quota; may incur charges")
    args = parser.parse_args()
    if not 1 <= args.timeout <= 120:
        parser.error("timeout must be between 1 and 120 seconds")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
