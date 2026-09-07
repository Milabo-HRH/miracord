"""Paid isolated web-search routing probes; never join Discord or access devices."""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from scripts.eval_voice_questions import evaluate, write_report
from src.lol_mcp.opgg import OpggMayhemClient


async def main() -> None:
    report = {"label": "Online search integration", "results": []}
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = Path("logs") / f"eval-web-search-{stamp}.json"
    opgg = OpggMayhemClient()
    cases = [
        {
            "id": "official-search", "route": "search",
            "question": "联网查一下 Riot 官方 26.3 版本里 ARAM Mayhem 的改动，只说一条并给来源。",
        },
        {
            "id": "reddit-search", "route": "search",
            "question": "去 Reddit 看看大家怎么讨论 ARAM Mayhem 的 High Roller，只说一条观点，不要当胜率。",
        },
        {
            "id": "database-stays-fast", "route": "augment",
            "question": "我玩 Ambessa，High Roller 的 performance 是多少？",
        },
    ]
    for case in cases:
        result = await evaluate(case, opgg, {})
        calls = result["tool_calls"]
        searches = [call for call in calls if call["name"] == "search_web"]
        result["search_route_passed"] = (
            len(searches) == 1 and searches[0]["result"].get("status") == "ok"
            if case["route"] == "search" else not searches and result["route_check"]
        )
        report["results"].append(result)
        write_report(path, report)
        print(json.dumps({
            "id": result["id"], "completed": result["completed"],
            "search_route_passed": result["search_route_passed"],
            "answers": result["answers"], "model": result.get("accepted_model"),
            "error": result.get("error_code"), "first_audio_seconds": result["first_audio_seconds"],
            "calls": [
                {"name": call["name"], "arguments": call["arguments"],
                 "seconds": call["seconds"], "result": call["result"]}
                for call in calls
            ],
        }, ensure_ascii=False), flush=True)
    print(f"Report: {path.with_suffix('.md')}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
