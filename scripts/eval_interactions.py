"""Inspect local voice traces and replay frozen prompt/tool cases.

Run from the repository root with python -m scripts.eval_interactions --help.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from src.evaluation.interactions import (
    export_case, override_manager_config, production_manager, read_events, replay, snapshot_manager, timeline,
)


def write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect", help="Read the interaction timeline (no network)")
    inspect.add_argument("log", type=Path)
    inspect.add_argument("--turn")
    export = commands.add_parser("export", help="Convert one captured turn to a local fixture")
    export.add_argument("log", type=Path)
    export.add_argument("--turn", required=True)
    export.add_argument("--output", type=Path, required=True)
    snap = commands.add_parser("snapshot", help="Freeze effective production prompt and tools")
    snap.add_argument("--provider", choices=["openai", "grok", "gemini"], default="gemini")
    snap.add_argument("--instructions", type=Path, help="Replace complete effective instructions")
    snap.add_argument("--tools", type=Path, help="Replace complete function-tool schema array")
    snap.add_argument("--output", type=Path, required=True)
    run = commands.add_parser("run", help="Offline replay by default; --live consumes provider quota and may incur charges")
    run.add_argument("case", type=Path)
    run.add_argument("--snapshot", type=Path)
    run.add_argument("--live", action="store_true", help="Explicitly allow one provider API session (quota use; may incur charges)")
    run.add_argument("--timeout", type=float, default=45)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--trace", type=Path, help="Local diagnostic JSONL; defaults beside the report")
    compare = commands.add_parser("compare", help="Compare reports from the same frozen case")
    compare.add_argument("baseline", type=Path)
    compare.add_argument("candidate", type=Path)
    compare.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "inspect":
            print(timeline(read_events(args.log), args.turn))
        elif args.command == "export":
            write(args.output, export_case(read_events(args.log), args.turn))
        elif args.command == "snapshot":
            manager = production_manager(args.provider)
            try:
                override_manager_config(
                    manager,
                    instructions=args.instructions.read_text(encoding="utf-8") if args.instructions else None,
                    tools=json.loads(args.tools.read_text(encoding="utf-8")) if args.tools else None,
                )
                write(args.output, snapshot_manager(manager))
            finally:
                asyncio.run(manager.disconnect())
        elif args.command == "run":
            if not 1 <= args.timeout <= 120:
                raise ValueError("Timeout must be between 1 and 120 seconds")
            case = json.loads(args.case.read_text(encoding="utf-8"))
            snapshot = json.loads(args.snapshot.read_text(encoding="utf-8")) if args.snapshot else None
            report = asyncio.run(replay(case, snapshot=snapshot, live=args.live, timeout=args.timeout,
                                       trace_path=args.trace or args.output.with_suffix(".trace.jsonl")))
            write(args.output, report)
            print(json.dumps({key: report[key] for key in ("case_id", "mode", "passed", "checks")},
                             ensure_ascii=True))
            return 0 if report["passed"] else 1
        elif args.command == "compare":
            baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
            candidate = json.loads(args.candidate.read_text(encoding="utf-8"))
            if baseline["case_sha256"] != candidate["case_sha256"] or baseline["mode"] != candidate["mode"]:
                raise ValueError("Compare requires identical case inputs and execution mode")
            write(args.output, {"case_id": baseline["case_id"], "mode": baseline["mode"],
                                "baseline": baseline, "candidate": candidate,
                                "regression": baseline["passed"] and not candidate["passed"],
                                "elapsed_delta_ms": candidate["elapsed_ms"] - baseline["elapsed_ms"],
                                "note": "Model outputs are stochastic; checks are explicit rules, not a correctness judge."})
        return 0
    except (ValueError, KeyError, OSError) as error:
        # File/config failures may include local paths, but never print provider payloads.
        print(f"Evaluation failed: {error}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
