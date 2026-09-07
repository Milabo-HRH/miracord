"""Paid isolated champion/augment routing probes; no Discord or audio devices."""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from scripts.eval_voice_questions import evaluate, write_report
from src.lol_mcp.opgg import OpggMayhemClient


async def main() -> None:
    report = {"label": "Champion tier integration", "results": []}
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = Path("logs") / f"eval-champion-tiers-{stamp}.json"
    opgg = OpggMayhemClient()
    cases = [
        {
            "id": "ambessa-tier", "route": "champion_tier",
            "question": "OPGG 上 Ambessa 在大乱斗海克斯里是 T 几？只说英雄评级。",
        },
        {
            "id": "known-wolf-tier", "route": "champion_tier",
            "history": [
                ["user", "我玩的是 Ambessa，也叫狼母。"],
                ["assistant", "你玩的是 Ambessa。"],
            ],
            "question": "那狼母在 Mayhem 里 OPGG 排第几，T 几？",
        },
        {
            "id": "augment-not-champion", "route": "augment",
            "question": "Ambessa 的 High Roller performance 是多少？我问的是海克斯，不是英雄 tier。",
        },
    ]
    for case in cases:
        result = await evaluate(case, opgg, {})
        report["results"].append(result)
        write_report(path, report)
        print(json.dumps({
            "id": result["id"], "completed": result["completed"],
            "route_check": result["route_check"], "answers": result["answers"],
            "model": result.get("accepted_model"), "error": result.get("error_code"),
            "first_audio_seconds": result["first_audio_seconds"],
            "calls": result["tool_calls"],
        }, ensure_ascii=False), flush=True)
    print(f"Report: {path.with_suffix('.md')}", flush=True)
    if not all(row["completed"] and row["route_check"] for row in report["results"]):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
