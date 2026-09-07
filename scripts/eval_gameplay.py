"""Run realistic conversations with actual prior model answers and frozen tools."""
import argparse
import asyncio
import json
import os
from pathlib import Path

from src.evaluation.gameplay import conversation


async def run(args):
    suite, data, snapshot = [json.loads(p.read_text(encoding="utf-8")) for p in (args.suite, args.data, args.snapshot)]
    args.output.mkdir(parents=True, exist_ok=True)
    os.environ["VOICE_OBSERVABILITY_DIR"] = str((args.output / "trace").resolve())
    for name, content in [("suite", suite), ("data", data), ("snapshot", snapshot)]:
        (args.output / f"{name}.json").write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
    reports = []
    for repeat in range(args.repeat):
        for scenario in suite["scenarios"]:
            report = await conversation(scenario, data, snapshot, timeout=args.timeout)
            report["repetition"] = repeat + 1
            reports.append(report)
            (args.output / f"{scenario['id']}.{repeat+1}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"scenario": scenario["id"], "repeat": repeat + 1, "turns": len(report["turns"]),
                              "passed": sum(t["passed"] for t in report["turns"]), "error": report.get("error")}), flush=True)
    (args.output / "summary.json").write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if all(r.get("complete") and all(t["passed"] for t in r["turns"]) for r in reports) else 1


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--suite", type=Path, default=Path("tests/fixtures/interactions/gameplay_conversations.json"))
    p.add_argument("--data", type=Path, required=True)
    p.add_argument("--snapshot", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--repeat", type=int, choices=[1, 2, 3], default=1)
    p.add_argument("--timeout", type=float, default=40)
    p.add_argument("--live", action="store_true", help="Explicitly allow Gemini API quota use")
    args = p.parse_args()
    if not args.live:
        p.error("Actual model answers require --live; offline tests use pytest")
    if not 1 <= args.timeout <= 120:
        p.error("Timeout must be between 1 and 120 seconds")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
