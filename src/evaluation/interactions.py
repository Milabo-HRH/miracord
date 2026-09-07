"""Local trace inspection and frozen-input Realtime regression evaluation.

Offline replay exercises production tool orchestration, not model intelligence.
Live replay consumes provider quota (and may incur charges), but never connects
to Discord or game services.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def read_events(path: Path) -> list[dict]:
    """Ignore an incomplete final write, but reject corruption in complete rows."""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    result = []
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            if index == len(lines) - 1 and not line.endswith("\n"):
                break
            raise ValueError(f"Invalid JSONL row {index + 1}") from None
        if not isinstance(event, dict) or event.get("schema_version") != 1:
            raise ValueError(f"Unsupported event schema at row {index + 1}")
        result.append(event)
    return result


def timeline(events: list[dict], turn_id: str | None = None) -> str:
    """Display only safe metadata and deliberately captured conversation text."""
    rows = []
    for event in events:
        if turn_id and event.get("turn_id") != turn_id:
            continue
        fields = " ".join(f"{key}={event[key]}" for key in (
            "turn_id", "response_id", "call_id", "function_name", "tool_name", "status", "reason",
            "error_code", "close_code", "duration_ms", "cache_hit", "stale") if key in event)
        payload = event.get("payload") or {}
        text = payload.get("text", "") if isinstance(payload, dict) else ""
        rows.append(f"{event.get('timestamp', '')} {event.get('event', '')} {fields}"
                    + (f"\n  {text}" if text else ""))
    return "\n".join(rows)


def export_case(events: list[dict], turn_id: str) -> dict:
    """Make one self-contained fixture; refuse missing or truncated capture data."""
    rows = [row for row in events if row.get("turn_id") == turn_id]
    if not rows:
        raise ValueError("Turn not found")
    if any(row.get("payload_truncated") for row in rows):
        raise ValueError("Capture is truncated; use a complete manually reviewed fixture")
    sessions = {row.get("session_id") for row in rows}
    if len(sessions) != 1:
        raise ValueError("Ambiguous turn across sessions")
    questions = [row.get("payload", {}).get("text") for row in rows
                 if row.get("event") == "transcript.user"]
    if not questions or not all(isinstance(q, str) and q for q in questions):
        raise ValueError("User transcript missing; enable local diagnostic capture first")
    if len(questions) != 1:
        raise ValueError("Expected one user utterance per turn; split or review this capture manually")
    epoch = rows[0].get("connection_epoch")
    if any(row.get("session_id") in sessions and row.get("connection_epoch") == epoch
           and (row.get("correlation") == "ambiguous" or row.get("dropped_events"))
           for row in events):
        raise ValueError("Session has ambiguous response linkage or dropped events; review manually")
    configs = [row for row in events if row.get("session_id") in sessions
               and row.get("event") == "session.configured"
               and row.get("connection_epoch") == epoch]
    if not configs:
        raise ValueError("Captured session configuration missing (check rotated logs)")
    if not configs[-1].get("payload") or configs[-1].get("payload_truncated"):
        raise ValueError("Full captured session configuration required")
    config = configs[-1]["payload"]
    snapshot = copy.deepcopy(config.get("snapshot", config))
    snapshot.setdefault("provider", configs[-1].get("provider"))
    if "session_config" not in snapshot or "model" not in snapshot:
        raise ValueError("Full captured session configuration required")
    calls = []
    for row in rows:
        if row.get("event") != "tool.started":
            continue
        payload = row.get("payload") or {}
        completed = next((candidate for candidate in rows
                          if candidate.get("event") == "tool.completed"
                          and candidate.get("call_id") == row.get("call_id")), None)
        if (not completed or "arguments" not in payload
                or "result" not in completed.get("payload", {})):
            raise ValueError("Complete tool input/output capture required")
        calls.append({"name": payload.get("name", row.get("function_name", row.get("tool_name"))),
                      "arguments": payload["arguments"],
                      "result": completed["payload"]["result"],
                      "round": row.get("response_id", "round-1")})
    case = {"schema_version": 1, "id": turn_id, "origin": "local_diagnostic_capture",
            "question": "\n".join(questions), "snapshot": snapshot,
            "tool_calls": calls,
            "expected": {"tool_names": [call["name"] for call in calls]},
            "reference_answers": [row.get("payload", {}).get("text", "") for row in rows
                                  if row.get("event") == "transcript.assistant"],
            "history": [["user", row["payload"]["text"]] for row in rows
                        if row.get("event") in {"turn.context.sent", "turn.context.reused"}
                        and isinstance(row.get("payload", {}).get("text"), str)
                        and row["payload"]["text"]],
            "limitations": ["Transcription substitutes for audio; no Discord or wake-word replay.",
                            "One turn only; prior conversation must be added as history if needed."]}
    if snapshot.get("provider") == "gemini":
        # Preserve the actual native text prefix across export/replay. Wrapping
        # it in another synthetic history block on every replay changes inputs.
        case["context_text"] = "\n".join(text for _role, text in case["history"])
        case["history"] = []
    serialized = json.dumps(case)
    if "[TRUNCATED]" in serialized or '"truncated": true' in serialized.lower():
        raise ValueError("Capture is truncated; use a complete manually reviewed fixture")
    return case


class SilentPlayback:
    """Measure output without storing PCM or opening an audio device."""

    def __init__(self):
        self.audio_bytes = 0
        self.first_audio_at = None

    async def start_new_audio_stream(self, *_args):
        pass

    async def add_audio_chunk(self, audio):
        self.first_audio_at = self.first_audio_at or time.monotonic()
        self.audio_bytes += len(audio)

    async def end_audio_stream(self, *_args):
        pass

    def interrupt_audio_stream(self) -> None:
        """Match production playback cancellation without opening an audio device."""
        pass

    def get_played_ms(self, *_args):
        return 0


def production_manager(provider: str, *, live: bool = False):
    if provider == "openai":
        from src.ai_services.providers.openai.config import OPENAI_SERVICE_CONFIG
        from src.ai_services.providers.openai.manager import OpenAIRealtimeManager
        cls, config = OpenAIRealtimeManager, copy.deepcopy(OPENAI_SERVICE_CONFIG)
    elif provider == "grok":
        from src.ai_services.providers.grok.config import GROK_SERVICE_CONFIG
        from src.ai_services.providers.grok.manager import GrokRealtimeManager
        cls, config = GrokRealtimeManager, copy.deepcopy(GROK_SERVICE_CONFIG)
    elif provider == "gemini":
        from src.ai_services.providers.gemini.config import GEMINI_SERVICE_CONFIG
        from src.ai_services.providers.gemini.manager import GeminiRealtimeManager
        cls, config = GeminiRealtimeManager, copy.deepcopy(GEMINI_SERVICE_CONFIG)
    else:
        raise ValueError("Provider must be openai, grok, or gemini")
    if live and not config.get("api_key"):
        raise ValueError("Provider API key is missing")
    if not live:
        config["api_key"] = "offline-replay-no-network"
    config.update(league_context_enabled=False, opgg_prefetch_enabled=False,
                  connection_timeout=15)
    return cls(SilentPlayback(), config)


def snapshot_manager(manager) -> dict:
    session = copy.deepcopy(manager._session_config)
    return {"provider": manager.provider, "model": manager._model_name,
            "session_config": session,
            "instructions_sha256": fingerprint(session.get(
                "system_instruction" if manager.provider == "gemini" else "instructions", "")),
            "tools_sha256": fingerprint(session.get("tools", []))}


def function_declarations(session: dict, provider: str) -> list[dict]:
    """Validate frozen function-only schemas without enabling native network tools."""
    tools = session.get("tools", [])
    if not isinstance(tools, list):
        raise ValueError("Frozen replay tools must be an array")
    declarations = []
    for tool in tools:
        if not isinstance(tool, dict):
            raise ValueError("Frozen replay requires named function tools only")
        if provider == "gemini":
            if set(tool) != {"function_declarations"} or not isinstance(tool["function_declarations"], list):
                raise ValueError("Frozen live replay requires function tools only; remove native tools explicitly")
            functions = tool["function_declarations"]
        else:
            if tool.get("type") != "function":
                raise ValueError("Frozen live replay requires function tools only; remove native tools explicitly")
            functions = [tool]
        if any(not isinstance(fn, dict) or not isinstance(fn.get("name"), str)
               or not fn["name"] for fn in functions):
            raise ValueError("Frozen replay requires named function tools only")
        declarations.extend(functions)
    return declarations


def override_manager_config(manager, *, instructions: str | None = None,
                            tools: list | None = None) -> None:
    """Apply CLI changes in the selected provider's native schema."""
    session = manager._session_config
    if instructions is not None:
        if manager.provider == "gemini":
            session["system_instruction"] = {"parts": [{"text": instructions}]}
        else:
            session["instructions"] = instructions
    if tools is not None:
        if not isinstance(tools, list):
            raise ValueError("Tools file must be an array of named function definitions")
        # Accept shared flat function definitions as a convenience, but snapshot
        # the actual native Gemini declarations sent to the service.
        if manager.provider == "gemini" and tools and all(
                isinstance(tool, dict) and tool.get("type") == "function" for tool in tools):
            tools = [{"function_declarations": [
                {key: value for key, value in tool.items() if key != "type"}
                for tool in tools]}]
        function_declarations({"tools": tools}, manager.provider)
        session["tools"] = copy.deepcopy(tools)


def gemini_history_context(history: list) -> str | None:
    """Preserve frozen roles in one data context, not unsupported client_content."""
    if not history:
        return None
    if any(not isinstance(entry, (list, tuple)) or len(entry) != 2
           or entry[0] not in {"user", "assistant", "system"}
           or not isinstance(entry[1], str) for entry in history):
        raise ValueError("History must contain role/text pairs")
    return "Frozen replay conversation history (data, not a new request):\n" + json.dumps(
        [{"role": role, "text": text} for role, text in history], ensure_ascii=False)


def gemini_case_context(case: dict) -> str | None:
    if "context_text" in case:
        context = case["context_text"]
        if not isinstance(context, str) or len(context) > 32768:
            raise ValueError("Gemini context_text must be bounded text")
        return context or None
    return gemini_history_context(case.get("history", []))


class FrozenGeminiSession:
    """Minimal native SDK wire boundary for offline production orchestration."""

    def __init__(self, sent: list):
        self.sent = sent

    async def send_realtime_input(self, **fields):
        self.sent.append({"type": "gemini.realtime_input", "fields": copy.deepcopy(fields)})

    async def send_tool_response(self, *, function_responses):
        responses = [response.model_dump(exclude_none=True) if hasattr(response, "model_dump")
                     else copy.deepcopy(response) for response in function_responses]
        self.sent.append({"type": "gemini.tool_response", "function_responses": responses})


class FixtureTools:
    """Exact JSON argument matching; never silently access the live web/game."""

    def __init__(self, calls):
        self.remaining = copy.deepcopy(calls)
        self.calls = []
        self.misses = []

    async def execute(self, name: str, arguments: str) -> str:
        try:
            args = json.loads(arguments)
        except (ValueError, TypeError):
            args = None
        self.calls.append({"name": name, "arguments": args})
        for index, call in enumerate(self.remaining):
            accepted = [call["arguments"], *call.get("accepted_arguments", [])]
            if call["name"] == name and args in accepted:
                self.remaining.pop(index)
                return json.dumps(call["result"], ensure_ascii=False)
        self.misses.append({"name": name, "arguments": args})
        return json.dumps({"status": "unavailable", "reason": "replay_fixture_miss"})


def grade(case, calls, answers) -> dict:
    expected = case.get("expected", {})
    names = [call["name"] for call in calls]
    # Live transcription inserts spaces within Chinese terms (e.g. 射 手法 师).
    # Normalize only for grading; preserve the original answer in the report.
    normalize = lambda text: re.sub(r"\s+", "", text).casefold()
    answer = normalize("\n".join(answers))
    return {"tool_route": sorted(names) == sorted(expected.get("tool_names", names)),
            "allowed_tools": all(name in expected.get("allowed_tool_names", names) for name in names),
            "required_text": all(normalize(word) in answer
                                 for word in expected.get("answer_contains", [])),
            "forbidden_text": all(normalize(word) not in answer
                                  for word in expected.get("answer_not_contains", [])),
            "required_alternatives": all(any(normalize(word) in answer for word in group)
                                         for group in expected.get("answer_contains_any", [])),
            "forbidden_patterns": not any(re.search(pattern, answer, re.IGNORECASE)
                                          for pattern in expected.get("answer_not_regex", [])),
            "answer_length": len(answer) <= expected.get("max_answer_chars", float("inf"))}


async def replay(case: dict, *, snapshot: dict | None = None,
                 live: bool = False, timeout: float = 45,
                 trace_path: Path | None = None) -> dict:
    """Run production orchestration offline or one explicit, bounded API evaluation."""
    selected = copy.deepcopy(snapshot or case["snapshot"])
    provider = selected["provider"]
    declarations = function_declarations(selected["session_config"], provider)
    manager = production_manager(provider, live=live)
    replay_observer = None
    if trace_path is not None:
        from src.observability import JsonlObserver
        replay_observer = JsonlObserver(trace_path, capture_enabled=True, capture_chars=131072)
        manager.observer = replay_observer
    is_gemini = provider == "gemini"
    if is_gemini:
        manager.apply_replay_config(selected)
    else:
        manager._model_name = selected["model"]
        manager._connection_handler_inst._model_name = selected["model"]
        manager._session_config = copy.deepcopy(selected["session_config"])
    # Native provider-hosted tools cannot use frozen local results. Reject them
    # even offline so a saved case cannot unexpectedly acquire network behavior.
    if not is_gemini:
        container = manager._vad_container()
        container["turn_detection"] = None
        manager._configured_vad = None
        manager._server_vad = False
        if provider == "openai":
            manager._session_config["max_output_tokens"] = min(
                3072, manager._session_config.get("max_output_tokens", 3072)
                if isinstance(manager._session_config.get("max_output_tokens", 3072), int) else 3072)
    fixture = FixtureTools(case.get("tool_calls", []))
    manager._execute_tool = fixture.execute
    sent, answers = [], []
    completed = asyncio.Event()
    status = {"value": None}
    original_dispatch = manager._dispatch_event

    async def dispatch(event):
        await original_dispatch(event)
        if event.get("type") == "error":
            status["value"] = "provider_error"
            completed.set()
        if event.get("type") == "response.done":
            response = event.get("response", {})
            for item in response.get("output", []):
                for part in item.get("content", []):
                    text = part.get("transcript") or part.get("text")
                    if text:
                        answers.append(text)
            if not any(item.get("type") == "function_call" for item in response.get("output", [])):
                status["value"] = response.get("status")
                completed.set()

    if not is_gemini:
        manager._dispatch_event = dispatch

    async def send(event):
        sent.append(copy.deepcopy(event))

    async def noop():
        pass

    started = time.monotonic()
    error = None
    rounds = {}
    for index, call in enumerate(case.get("tool_calls", [])):
        rounds.setdefault(call.get("round", "round-1"), []).append({
            "type": "function_call", "name": call["name"],
            "arguments": json.dumps(call["arguments"]), "call_id": f"fixture-{index}"})
    try:
        if live:
            # Set the actual connection model before connect; transport uses _model_name.
            if not is_gemini:
                manager._connection_handler_inst._model_name = selected["model"]
            if not await manager.connect(noop, noop):
                error = getattr(manager._connection_handler_inst, "terminal_error_code", None)
                error = error or "session_setup_failed"
                raise ValueError("Provider session setup failed")
            if is_gemini:
                if not await manager.send_text_turn(
                    case["question"], context_text=gemini_case_context(case)
                ):
                    raise ValueError("Input send failed")
                await asyncio.wait_for(manager._response_completed.wait(), timeout)
                status["value"] = manager._last_response_status
                if manager._last_response_text:
                    answers.append(manager._last_response_text)
            else:
                for role, text in [*case.get("history", []), ("user", case["question"])]:
                    ok = await manager._send({"type": "conversation.item.create", "item": {
                        "type": "message", "role": role, "content": [{
                            "type": "input_text" if role == "user" else "output_text", "text": text}]}})
                    if not ok:
                        raise ValueError("Input send failed")
                if not await manager._send({"type": "response.create"}):
                    raise ValueError("Response request failed")
                await asyncio.wait_for(completed.wait(), timeout)
        else:
            manager._connection_handler_inst.is_connected = lambda: True
            manager._accepted = True
            if is_gemini:
                session = FrozenGeminiSession(sent)
                manager._connection_handler_inst.get_active_session = lambda: session
                await manager._post_connect_hook()
                if not await manager.send_text_turn(
                    case["question"], context_text=gemini_case_context(case)
                ):
                    raise ValueError("Offline input setup failed")
                for calls in rounds.values():
                    await manager._dispatch_message({"tool_call": {"function_calls": [
                        {"id": call["call_id"], "name": call["name"],
                         "args": json.loads(call["arguments"])} for call in calls]}})
                    await asyncio.gather(*tuple(manager._tool_tasks))
            else:
                manager._accepted = True
                manager._connection_handler_inst.send_event = send
                for index, calls in enumerate(rounds.values()):
                    rid = f"fixture-response-{index}"
                    await dispatch({"type": "response.created", "response": {"id": rid}})
                    await dispatch({"type": "response.done", "response": {
                        "id": rid, "status": "completed", "output": calls}})
                    await asyncio.gather(*tuple(manager._tool_tasks))
            status["value"] = "offline_orchestration_complete"
    except asyncio.TimeoutError:
        error = "timeout"
    except Exception as exc:
        error = error or type(exc).__name__
    finally:
        await manager.disconnect()
        if replay_observer is not None:
            replay_observer.close()
    checks = grade(case, fixture.calls, answers) if live else {
        "tool_route": grade(case, fixture.calls, [])["tool_route"],
        "answer_quality": "not_evaluated_offline"}
    checks["fixture_matches"] = not fixture.misses
    checks["all_fixture_results_used"] = not any(not call.get("optional", False)
                                                for call in fixture.remaining)
    tool_names = {tool["name"] for tool in declarations}
    checks["tool_schema_available"] = all(call["name"] in tool_names for call in fixture.calls)
    if not live:
        if is_gemini:
            responses = [event for event in sent if event["type"] == "gemini.tool_response"]
            expected_ids = [[call["call_id"] for call in calls] for calls in rounds.values()]
            actual_ids = [[response.get("id") for response in event["function_responses"]]
                          for event in responses]
            checks["results_before_continuation"] = actual_ids == expected_ids
            checks["native_tool_response_protocol"] = not any(
                event["type"] in {"response.create", "conversation.item.create"} for event in sent)
        else:
            expected_sequence = []
            for calls in rounds.values():
                expected_sequence.extend(["function_call_output"] * len(calls))
                expected_sequence.append("response.create")
            actual_sequence = [event.get("item", {}).get("type", event["type"]) for event in sent]
            checks["results_before_continuation"] = actual_sequence == expected_sequence
    passed = not error and all(value is not False for value in checks.values())
    if live:
        passed = passed and status["value"] == "completed" and bool(answers)
    return {"schema_version": 1, "case_id": case["id"], "mode": "live" if live else "offline",
            "passed": bool(passed), "status": status["value"], "error": error,
            "case_sha256": fingerprint(case), "snapshot_sha256": fingerprint(selected),
            "effective_session_sha256": fingerprint(manager._session_config),
            "snapshot": selected, "checks": checks, "tool_calls": fixture.calls,
            "fixture_misses": fixture.misses, "answers": answers,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "audio_bytes": manager._audio_playback_manager.audio_bytes,
            "trace_path": str(trace_path) if trace_path is not None else None,
            "limitations": "Offline tests orchestration only. Live uses text and frozen local tool outputs; no Discord, ASR, wake-word, or calibrated answer-quality judge."}
