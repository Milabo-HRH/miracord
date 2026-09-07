"""Verify the actual stdio MCP against public OP.GG pages, without an LLM API."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def run(champion: str) -> None:
    script = Path(__file__).resolve().with_name("run_lol_mcp.py")
    parameters = StdioServerParameters(command=sys.executable, args=[str(script)])
    async with (
        stdio_client(parameters) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        names = {tool.name for tool in (await session.list_tools()).tools}
        assert {
            "get_mayhem_champion_tier",
            "get_mayhem_build",
            "get_mayhem_augments",
            "get_live_game_state",
        } <= names
        summary = {"champion": champion, "tools": sorted(names), "calls": []}
        for name in (
            "get_mayhem_champion_tier",
            "get_mayhem_augments",
            "get_mayhem_build",
            "get_mayhem_augments",
            "get_mayhem_build",
        ):
            arguments = {"champion": champion}
            if name == "get_mayhem_augments":
                arguments["limit"] = 200
            started = time.perf_counter()
            result = await session.call_tool(name, arguments)
            elapsed = round((time.perf_counter() - started) * 1000, 1)
            assert not result.isError, result
            data = result.structuredContent
            assert data and data["status"] == "ok", data
            assert not data["cache"]["stale"], data["cache"]
            call = {
                "tool": name,
                "milliseconds": elapsed,
                "source": data["source"],
                "cache": data["cache"],
            }
            if name == "get_mayhem_augments":
                call["count"] = data["totalAvailable"]
                call["tiers"] = sorted({row["tier"] for row in data["augments"]})
                assert {0, 1, 2} <= set(call["tiers"])
                assert data["totalAvailable"] == data["totalMatched"]
            elif name == "get_mayhem_champion_tier":
                call["championTier"] = data["tierLabel"]
                call["championRank"] = data["championRank"]
                assert data["tierType"] == "champion"
            else:
                call["starterOptions"] = len(data["starterItems"])
                call["coreOptions"] = len(data["coreBuilds"])
            summary["calls"].append(call)
        print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--champion", default="samira")
    asyncio.run(run(parser.parse_args().champion))
