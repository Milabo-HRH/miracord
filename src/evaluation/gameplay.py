"""Multi-turn gameplay replay through production tools and frozen public data."""

import asyncio
import copy
import json
import re
import time
from types import SimpleNamespace

from src.evaluation.interactions import fingerprint, function_declarations, grade, production_manager
from src.lol_mcp.name_catalog import NameCatalog
from src.lol_mcp.opgg import OpggMayhemClient, champion_slug
from src.lol_mcp.tools import LeagueToolExecutor


NO_REPLY = "<NO_REPLY>"


def reply_action(text):
    """Protocol evaluation only: never use late audio transcripts as a mute gate."""
    if text.strip() == NO_REPLY:
        return "no_reply"
    return "invalid_control_output" if NO_REPLY in text else "reply"


class FrozenOpgg(OpggMayhemClient):
    """Keep production filtering/pagination; replace only upstream retrieval."""

    def __init__(self, records):
        self.records = copy.deepcopy(records)
        self.gaps = []

    def _get(self, champion, kind):
        key = "global-champion-tiers" if kind == "champion_tiers" else f"{champion_slug(champion)}-{kind}"
        if key not in self.records:
            self.gaps.append(key)
            return {"status": "unavailable", "reason": "eval_data_missing"}
        record = self.records[key]
        return {"status": "ok", "champion": champion_slug(champion), "mode": "aram-mayhem",
                "source": {"name": "OP.GG frozen public cache", "patch": record["data"]["patch"],
                           "fetchedAtEpoch": record["fetchedAtEpoch"]},
                "cache": {"hit": True, "stale": False, "replay": True},
                **copy.deepcopy(record["data"])}


class GameplayTools:
    def __init__(self, bundle, state):
        self.opgg = FrozenOpgg(bundle["records"])
        self.state = copy.deepcopy(state)
        self.names = NameCatalog(data=bundle["catalog"])
        from src.lol_mcp.arammeta import AramMetaClient
        def frozen_arammeta(url):
            resource = url.split("/docs/api/", 1)[-1]
            resources = bundle.get("arammeta", {}).get("resources", {})
            if resource not in resources:
                self.opgg.gaps.append("arammeta/" + resource)
                raise ValueError("eval_data_missing")
            return copy.deepcopy(resources[resource])
        revision = bundle.get("arammeta", {}).get("revision")
        arammeta = AramMetaClient(cache_dir=None, fetch=frozen_arammeta,
                                 **({"revision": revision} if revision else {}))
        self.executor = LeagueToolExecutor(SimpleNamespace(
            opgg=self.opgg, names=self.names, arammeta=arammeta,
            snapshot=lambda: copy.deepcopy(self.state)))
        self.calls = []

    async def execute(self, name, arguments):
        started = time.monotonic()
        output = await self.executor.execute(name, arguments)
        try:
            parsed = json.loads(arguments)
        except (ValueError, TypeError):
            parsed = None
        self.calls.append({"name": name, "arguments": parsed, "result": json.loads(output),
                           "duration_ms": round((time.monotonic() - started) * 1000)})
        return output


def gameplay_grade(turn, calls, answers):
    """Add delivery and entity checks; these still need semantic human review."""
    checks = grade(turn, calls, answers)
    if "reply_action" in turn.get("expected", {}):
        checks["reply_action"] = reply_action("\n".join(answers)) == turn["expected"]["reply_action"]
    text = re.sub(r"\s+", "", "\n".join(answers))
    if turn.get("expected", {}).get("answer_language") == "zh":
        checks["chinese_response"] = len(re.findall(r"[\u4e00-\u9fff]", text)) >= 8
    checks["no_unsolicited_confirmation"] = turn.get("expected", {}).get("identity_confirmation_allowed", False) or re.search(
        r"(?:确定|确认)[^。！？?]*[吗？?]|(?:还要|还想|还有别的|还有其他|要看看|要看它|想看看)[^。！？?]*[吗？?]", text) is None
    allowed = turn.get("expected", {}).get("allowed_champions")
    checks["required_tools"] = set(turn.get("expected", {}).get("required_tool_names", [])).issubset(
        {c["name"] for c in calls})
    if allowed is not None:
        checks["correct_champion"] = all(
            champion_slug(c["arguments"]["champion"]) in allowed
            for c in calls if isinstance(c.get("arguments"), dict) and "champion" in c["arguments"])
    expected = turn.get("expected", {})
    if 'any_required_tool_names' in expected:
        checks['required_any_tool'] = bool(set(expected['any_required_tool_names']) & {c['name'] for c in calls})
    if 'offered_options' in expected:
        offered = set(expected['offered_options'])
        checks['only_offered_choices'] = all(
            set(c.get('arguments', {}).get('options', [])) == offered
            and {r.get('requestedName') for r in c.get('result', {}).get('choices', [])} == offered
            for c in calls if c['name'] == 'compare_mayhem_choices')
    if 'offered_ids' in expected:
        comparisons = [c for c in calls if c['name'] == 'compare_mayhem_choices' and c.get('result', {}).get('status') != 'needs_identification']
        checks['verified_offered_identities'] = bool(comparisons) and all(
            {r.get('id') for r in c.get('result', {}).get('choices', [])} == set(expected['offered_ids'])
            for c in comparisons)
    checks["forbidden_tools"] = not set(expected.get("forbidden_tool_names", [])).intersection(c["name"] for c in calls)
    if expected.get("positive_popularity_results"):
        lists = [c.get("result", {}) for c in calls if c["name"] == "get_mayhem_augments"]
        checks["positive_popularity_results"] = bool(lists) and all(
            r.get("status") == "ok" and r.get("augments") and all(
                isinstance(a.get("name"), str) and a["name"].strip()
                and type(a.get("popular")) in (int, float) and a["popular"] > 0
                for a in r["augments"]) for r in lists)
    if "observed_augment_ids" in expected:
        found = {a.get("id") for c in calls for a in [*c.get("result", {}).get("augments", []), *c.get("result", {}).get("choices", [])]}
        checks["observed_augment_ids"] = set(expected["observed_augment_ids"]).issubset(found)
    if "allowed_augment_ids" in expected:
        checks["no_guessed_augment_ids"] = all(set((c.get("arguments") or {}).get("ids", (c.get("arguments") or {}).get("augment_ids", []))).issubset(expected["allowed_augment_ids"]) for c in calls)
    if "forbidden_augment_queries" in expected:
        checks["no_split_or_stale_name"] = all((c.get("arguments") or {}).get("query") not in expected["forbidden_augment_queries"] for c in calls)
    for tool, values in expected.get("required_argument_values", {}).items():
        relevant = [c for c in calls if c["name"] == tool]
        checks["argument_values_"+tool] = bool(relevant) and all(all((c.get("arguments") or {}).get(k) == v for k,v in values.items()) for c in relevant)
    if "required_champions" in expected:
        queried = {champion_slug(c["arguments"]["champion"]) for c in calls if c["name"] == "get_mayhem_champion_tier" and isinstance(c.get("arguments"), dict) and c["arguments"].get("champion")}
        checks["all_ten_tiers"] = {champion_slug(c) for c in expected["required_champions"]}.issubset(queried)
    if "team_verdict" in expected:
        comparisons = [c.get("result", {}) for c in calls if c["name"] == "get_mayhem_team_comparison"]
        checks["computed_team_verdict"] = bool(comparisons) and all(r.get("status") == "ok" and r.get("verdict") == expected["team_verdict"] and len(r.get("teams", [])) == 2 and all(sum(t.get("tierCounts", {}).values()) == 5 for t in r["teams"]) for r in comparisons)
    candidate = expected.get("candidate_lookup")
    if candidate:
        first = calls[0] if calls else {}
        args = first.get("arguments") or {}
        checks["rarity_candidates_first"] = (
            first.get("name") == "get_mayhem_augments"
            and args.get("rarity") == candidate["rarity"]
            and not args.get("query") and "augment_ids" not in args)
        matches = [row for call in calls
                   if call.get("name") == "get_mayhem_augments"
                   and (call.get("arguments") or {}).get("rarity") == candidate["rarity"]
                   and call.get("result", {}).get("status") == "ok"
                   for row in call["result"].get("augments", [])
                   if row.get("id") == candidate["id"]]
        checks["candidate_multilingual_evidence"] = any(
            row.get("rarity") == candidate["source_rarity"] and all(
                isinstance(row.get("namesByLocale", {}).get(locale), str)
                and row["namesByLocale"][locale].strip() for locale in candidate["locales"])
            for row in matches)
    return checks


async def conversation(scenario, bundle, snapshot, *, timeout=40, manager_factory=production_manager):
    """One native session per scenario; no scripted assistant history or answers."""
    if snapshot["provider"] != "gemini":
        raise ValueError("Gameplay session replay currently supports Gemini only")
    function_declarations(snapshot["session_config"], "gemini")
    manager = manager_factory("gemini", live=True)
    manager.apply_replay_config(snapshot)
    tools = GameplayTools(bundle, scenario.get("state", {"status": "unavailable"}))
    manager._execute_tool = tools.execute
    rows = []
    async def noop():
        pass
    try:
        if not await manager.connect(noop, noop):
            return {"scenario": scenario["id"], "error": "connection_failed", "turns": []}
        for index, turn in enumerate(scenario["turns"]):
            start, call_start, gap_start = time.monotonic(), len(tools.calls), len(tools.opgg.gaps)
            if "tool_state" in turn:
                # Simulate game progression visible only through a fresh tool call.
                tools.state = copy.deepcopy(turn["tool_state"])
            context = {"speaker": turn.get("speaker", "player_a"), "gameBinding": turn.get("gameBinding")}
            host_slot = (tools.state.get("activePlayer") or {}).get("slot")
            host = next((p for p in tools.state.get("players", []) if p.get("slot") == host_slot), None)
            if host:
                context["host"] = {k: host.get(k) for k in ("slot", "champion", "team")}
                context["possibleTeammateSpeakers"] = [
                    {k: p.get(k) for k in ("slot", "champion")}
                    for p in tools.state["players"] if p.get("team") == host.get("team")]
            if index == 0 or turn.get("state"):
                if turn.get("state"):
                    tools.state = copy.deepcopy(turn["state"])
                context["shared_reference"] = tools.state
            # Identity metadata is included, but no expected answer/rubric is sent.
            error = None
            try:
                if not await manager.send_text_turn(turn["question"], context_text="VOICE_CONTEXT " + json.dumps(context, ensure_ascii=False)):
                    raise ValueError("send_failed")
                await asyncio.wait_for(manager._response_completed.wait(), timeout)
            except asyncio.TimeoutError:
                error = "timeout"
            calls = tools.calls[call_start:]
            answers = [manager._last_response_text] if manager._last_response_text else []
            checks = gameplay_grade(turn, calls, answers)
            checks["valid_arguments"] = all(c["result"].get("status") != "invalid_arguments" for c in calls)
            gaps = tools.opgg.gaps[gap_start:]
            transport_ok = not error and manager._last_response_status == "completed" and bool(answers)
            rows.append({"index": index + 1, "question": turn["question"], "speaker": context["speaker"],
                         "reply_action": reply_action("\n".join(answers)),
                         "delivered_text": "\n".join(answers) if reply_action("\n".join(answers)) == "reply" else "",
                         "answers": answers, "calls": calls, "checks": checks, "data_gaps": gaps,
                         "error": error, "status": manager._last_response_status,
                         "passed": bool(transport_ok and not gaps and all(checks.values())),
                         "elapsed_ms": round((time.monotonic() - start) * 1000),
                         "review_rubric": turn.get("review_rubric", [])})
            if not transport_ok:
                break  # A broken session cannot provide valid follow-up context.
    finally:
        await manager.disconnect()
    return {"scenario": scenario["id"], "origin": scenario["origin"], "turns": rows,
            "scenario_sha256": fingerprint(scenario), "data_sha256": fingerprint(bundle),
            "snapshot_sha256": fingerprint(snapshot), "snapshot": snapshot,
            "complete": len(rows) == len(scenario["turns"]),
            "limitations": "Native multi-turn text session; no ASR, Discord, playback or automatic semantic judge."}
