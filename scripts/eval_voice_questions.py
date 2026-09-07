"""Paid, isolated Realtime answer/tool evaluation; never joins Discord or uses devices."""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from scripts.smoke_test_openai import ProbePlayback
from src.ai_services.providers.openai.config import OPENAI_SERVICE_CONFIG
from src.ai_services.providers.openai.manager import OpenAIRealtimeManager
from src.lol_mcp.context import encode
from src.lol_mcp.opgg import OpggMayhemClient


def cases(offered: list[str]) -> list[dict]:
    """Mix user-history questions with clearly marked regression scenarios."""
    return [
        {
            "id": "augment_cn",
            "origin": "regression",
            "question": "豆包，我玩萨弥拉，这把海克斯有什么推荐？",
            "route": "augment",
        },
        {
            "id": "augment_en",
            "origin": "regression",
            "question": "I'm playing Samira in ARAM Mayhem. Which augments should I look for?",
            "route": "augment",
        },
        {
            "id": "offered_three",
            "origin": "regression; real OP.GG names",
            "question": "我玩萨弥拉，海克斯三选一："
            + "、".join(offered)
            + "。选哪个？简短说原因。",
            "route": "augment",
        },
        {
            "id": "correct_item_drift",
            "origin": "regression",
            "history": [
                ("user", "我玩萨弥拉，这把海克斯怎么选？"),
                ("assistant", "你可以第一件出收集者，然后考虑无尽。"),
            ],
            "question": "不是装备，我问的是augment，海克斯！推荐什么强化？",
            "route": "augment",
        },
        {
            "id": "missing_options",
            "origin": "regression",
            "question": "我玩萨弥拉，眼前这三个海克斯选哪个？",
            "route": "clarify",
        },
        {
            "id": "unknown_speaker_champion",
            "origin": "regression",
            "question": "我的海克斯选啥？",
            "route": "clarify",
        },
        {
            "id": "ad_first_item",
            "origin": "user history, lightly punctuated",
            "question": "AD英雄，OPGG一般推荐第一件做什么？",
            "route": "item_or_clarify",
        },
        {
            "id": "ap_first_item",
            "origin": "user history, lightly punctuated",
            "question": "有蓝条AP英雄第一件出什么？",
            "route": "item_or_clarify",
        },
        {
            "id": "fighter_start",
            "origin": "user history, lightly punctuated",
            "question": "战士出门装推不推荐做守护者之刃？",
            "route": "item_or_clarify",
        },
        {
            "id": "hextech_item",
            "origin": "regression",
            "question": "我玩火男，海克斯科技火箭腰带这件装备适合第一件出吗？",
            "route": "item_or_clarify",
        },
        {
            "id": "popularity_not_winrate",
            "origin": "regression from user concern",
            "question": "OPGG的Mayhem热门度最高，是不是就代表胜率最高？",
            "route": "explain",
        },
        {
            "id": "augment_score",
            "origin": "database lookup regression",
            "question": "查一下萨弥拉的 Soul Siphon，performance 是多少？只报数字和字段名。",
            "route": "augment",
        },
        {
            "id": "augment_effect",
            "origin": "database lookup regression",
            "question": "我玩萨弥拉，Soul Siphon这个强化的效果是什么？",
            "route": "augment",
        },
        {
            "id": "missing_augment_data",
            "origin": "regression; tool outage injected",
            "question": "我玩萨弥拉，Tooth Fairy这个海克斯现在是什么效果，值得选吗？",
            "route": "augment",
            "unavailable": True,
        },
    ]


def route_check(case: dict, calls: list[dict]) -> bool:
    """Check routing only; strategic correctness still requires human review."""
    names = {call["name"] for call in calls}
    if case["route"] == "search":
        return "search_web" in names
    if case["route"] == "champion_tier":
        return names == {"get_mayhem_champion_tier"}
    if case["route"] == "augment":
        return "get_mayhem_augments" in names and "get_mayhem_build" not in names
    if case["route"] == "clarify":
        return "get_mayhem_build" not in names
    if case["route"] == "item_or_clarify":
        return "get_mayhem_augments" not in names
    return True


async def evaluate(case: dict, opgg: OpggMayhemClient, builds: dict) -> dict:
    config = copy.deepcopy(OPENAI_SERVICE_CONFIG)
    config.update(
        league_context_enabled=False, opgg_prefetch_enabled=False, connection_timeout=15
    )
    # Keep the production model, reasoning, prompts and tools. Only bound cost
    # and disable VAD: text-input evaluation is not an ASR/wake-word test.
    config["session_config"]["max_output_tokens"] = 3072
    config["session_config"]["audio"]["input"]["turn_detection"] = None
    playback = ProbePlayback()
    manager = OpenAIRealtimeManager(playback, config)
    manager._context.opgg = opgg
    manager._context._builds = copy.deepcopy(builds)
    snapshot = {
        "status": "ok",
        "scope": "shared_reference",
        "ageSeconds": 0,
        "source": "synthetic_test_match; not a user's real game",
        "game": {"isAramMayhem": True, "mode": "KIWI", "gameTimeSeconds": 300},
        "players": [
            {
                "slot": index,
                "champion": champion,
                "team": "ORDER",
                "observedAugments": [],
                "items": [],
            }
            for index, champion in enumerate(["Samira", "Brand", "Riven"])
        ],
    }
    manager._context.snapshot = lambda: copy.deepcopy(snapshot)
    done = asyncio.Event()
    result = {
        **case,
        "tool_calls": [],
        "answers": [],
        "response_events": [],
        "model": config["model_name"],
        "reasoning": config["session_config"].get("reasoning"),
        "input_mode": "text",
        "output_mode": "audio + actual transcript",
        "instructions": manager._session_config["instructions"],
        "tool_definitions": manager._session_config.get("tools", []),
        "instructions_sha256": hashlib.sha256(
            manager._session_config["instructions"].encode()
        ).hexdigest(),
    }
    execute = manager._execute_tool

    async def trace_tool(name: str, arguments: str) -> str:
        start = time.monotonic()
        output = (
            encode({"status": "unavailable", "reason": "injected_eval_outage"})
            if case.get("unavailable") and name == "get_mayhem_augments"
            else await execute(name, arguments)
        )
        result["tool_calls"].append(
            {
                "name": name,
                "arguments": json.loads(arguments),
                "result": json.loads(output),
                "seconds": round(time.monotonic() - start, 3),
            }
        )
        return output

    manager._execute_tool = trace_tool
    dispatch = manager._dispatch_event

    async def record_event(event: dict) -> None:
        await dispatch(event)
        if event.get("type") == "session.updated":
            session = event.get("session", {})
            result["accepted_model"] = session.get("model")
            result["accepted_reasoning"] = session.get("reasoning")
        if event.get("type") == "error":
            result["error_code"] = event.get("error", {}).get("code", "unknown")
            done.set()
        if event.get("type") != "response.done":
            return
        response = event.get("response") or {}
        result["response_events"].append(
            {
                "status": response.get("status"),
                "usage": response.get("usage"),
                "details": response.get("status_details"),
            }
        )
        if response.get("status") == "cancelled":
            return
        output = response.get("output", [])
        for item in output:
            for part in item.get("content", []):
                text = part.get("transcript") or part.get("text")
                if text:
                    result["answers"].append(text)
        if not any(item.get("type") == "function_call" for item in output):
            result["status"] = response.get("status")
            done.set()

    manager._dispatch_event = record_event

    async def noop() -> None:
        pass

    async def message(role: str, text: str) -> None:
        await manager._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": role,
                    "content": [
                        {
                            "type": "input_text" if role == "user" else "output_text",
                            "text": text,
                        }
                    ],
                },
            }
        )

    started = time.monotonic()
    try:
        if not await manager.connect(noop, noop):
            result["error_code"] = "connection_or_configuration_failed"
            return result
        result["context"] = manager._context.turn_context(0, "Test speaker")
        await message("user", result["context"])
        for role, text in case.get("history", []):
            await message(role, text)
        await message("user", case["question"])
        result["request_at"] = time.monotonic()
        await manager._send({"type": "response.create"})
        await asyncio.wait_for(done.wait(), timeout=45)
    except asyncio.TimeoutError:
        result["error_code"] = "response_timeout"
    except Exception as error:  # noqa: BLE001 - never print keys or raw exceptions
        result["error_code"] = type(error).__name__
    finally:
        await manager.disconnect()
        request_at = result.pop("request_at", started)
        result["first_audio_seconds"] = (
            round(playback.first_audio_at - request_at, 2)
            if playback.first_audio_at
            else None
        )
        result["elapsed_seconds"] = round(time.monotonic() - started, 2)
        result["audio_bytes"] = playback.audio_bytes
        result["route_check"] = route_check(case, result["tool_calls"])
        result["completed"] = (
            result.get("status") == "completed"
            and bool(result["answers"])
            and not result.get("error_code")
        )
    return result


def write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# Realtime 问题与实际回答",
        "",
        f"批次：{report['label']}",
        "",
        "真实模型、生产提示词和真实 OP.GG 适配器；游戏阵容为合成测试场景。",
        "文字输入 → Realtime 语音输出及原始转写；没有接入 Discord、麦克风或扬声器。",
        "路由检查不等于攻略正确性验证。异常数据场景会明确标注。",
        "",
    ]
    for row in report["results"]:
        lines.extend([f"## {row['id']}", "", f"问题：{row['question']}", ""])
        for role, text in row.get("history", []):
            lines.append(f"前文（{role}）：{text}")
        lines.extend(
            ["", "实际回答：", "", *["> " + text for text in row["answers"]], ""]
        )
        for call in row["tool_calls"]:
            payload = call["result"]
            lines.append(
                f"- `{call['name']}` {encode(call['arguments'])} → "
                f"{payload.get('status', 'unknown')} ({call['seconds']}s)"
            )
        if not row["tool_calls"]:
            lines.append("工具：未调用（有预先注入的装备参考上下文）。")
        lines.extend(
            [
                "",
                (
                    f"路由规则检查：{row['route_check']}；完成：{row['completed']}；"
                    f"首段语音（可能只是铺垫）：{row['first_audio_seconds']}s。"
                ),
                "",
            ]
        )
        if row.get("unavailable"):
            lines.extend(["测试注入：强化查询不可用，检查是否编造效果。", ""])
        if "search_route_passed" in row:
            lines.extend([f"搜索链路检查（不等于回答质量）：{row['search_route_passed']}", ""])
        for call in row["tool_calls"]:
            if call["name"] == "search_web":
                for source in call["result"].get("sources", []):
                    lines.append(f"来源：[{source['title']}]({source['url']})")
                lines.append("")
    path.with_suffix(".md").write_text("\n".join(lines), encoding="utf-8")


async def main(args) -> bool:
    if not OPENAI_SERVICE_CONFIG.get("api_key"):
        print("API key missing; no test was run.")
        return False
    opgg = OpggMayhemClient()
    builds = {}
    for champion in ["samira", "brand", "riven"]:
        builds[champion] = await asyncio.to_thread(opgg.get_build, champion)
    augments = await asyncio.to_thread(
        opgg.get_augments, "samira", limit=3, include_descriptions=True
    )
    offered = [row["name"] for row in augments.get("augments", [])]
    if len(offered) != 3:
        print(
            "Cannot obtain three real offered-name test examples; aborting paid evaluation."
        )
        return False
    report = {
        "label": args.label,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "offered_source": augments,
        "results": [],
    }
    path = (
        Path(__file__).resolve().parents[1] / "logs" / f"prompt-eval-{args.label}.json"
    )
    selected = [
        case for case in cases(offered) if not args.case or case["id"] in args.case
    ]
    for case in selected:
        row = await evaluate(case, opgg, builds)
        report["results"].append(row)
        write_report(path, report)
        print(
            json.dumps(
                {
                    key: row[key]
                    for key in (
                        "id",
                        "question",
                        "answers",
                        "route_check",
                        "completed",
                        "first_audio_seconds",
                    )
                },
                ensure_ascii=True,
            ),
            flush=True,
        )
    print(f"Report: {path.with_suffix('.md')}", flush=True)
    return all(row["completed"] for row in report["results"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--label",
        required=True,
        choices=[
            "baseline",
            "revised",
            "validation",
            "targeted",
            "lookup",
            "lookup-check",
        ],
    )
    parser.add_argument(
        "--case", action="append", help="Run only the named scenario; repeatable."
    )
    raise SystemExit(0 if asyncio.run(main(parser.parse_args())) else 1)
