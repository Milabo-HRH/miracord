"""Run bounded, isolated paid API probes without touching the live Discord bot."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from scripts.eval_voice_questions import evaluate, write_report
from src.lol_mcp.opgg import OpggMayhemClient


async def main() -> None:
    opgg = OpggMayhemClient()
    cases = [
        {
            "id": "explicit-ambessa",
            "question": "我玩 Ambessa，High Roller 的 performance 是多少？",
            "route": "augment",
        },
        {
            "id": "known-ambessa-followup",
            "history": [
                ["user", "我这把玩的是 Ambessa，就是狼母。"],
                ["assistant", "知道了，你玩的是 Ambessa。"],
            ],
            "question": "High Roller 的数据呢？",
            "route": "augment",
        },
        {
            "id": "known-ambessa-selection",
            "history": [
                ["user", "我这把玩的是 Ambessa，就是狼母。"],
                ["assistant", "知道了，你玩的是 Ambessa。"],
            ],
            "question": "狼母选 High Roller 怎么样？",
            "route": "augment",
        },
    ]
    report = {
        "label": "Ambessa / High Roller isolated diagnosis",
        "startedAt": datetime.now(timezone.utc).isoformat(),
        "limitations": [
            "Fresh sessions, text input, synthetic roster, no real player data.",
            "Uses production prompts/model/tools; cannot recover historical live calls.",
            "Does not test speech recognition or an accumulated live conversation.",
        ],
        "results": [],
    }
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = Path("logs") / f"diagnose-high-roller-{stamp}.json"
    for case in cases:
        result = await evaluate(case, opgg, {})
        report["results"].append(result)
        write_report(path, report)
        print(json.dumps({
            "id": result["id"],
            "completed": result["completed"],
            "model": result.get("accepted_model"),
            "reasoning": result.get("accepted_reasoning"),
            "answers": result["answers"],
            "error_code": result.get("error_code"),
            "tool_calls": [
                {
                    "name": call["name"],
                    "arguments": call["arguments"],
                    "status": call["result"].get("status"),
                    "totalMatched": call["result"].get("totalMatched"),
                    "augments": call["result"].get("augments"),
                }
                for call in result["tool_calls"]
            ],
        }, ensure_ascii=False), flush=True)
    print(f"Report: {path.with_suffix('.md')}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
