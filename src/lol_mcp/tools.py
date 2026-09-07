"""Whitelisted function-tool facade over the same adapters used by MCP."""

from __future__ import annotations

import asyncio
import math
import json

from .context import GameContextService, encode


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


CHAMPION = {
    "type": "string",
    "description": "Prefer nameGlossary opggSlug; known Chinese name/nickname or numeric champion ID also accepted",
    "maxLength": 40,
}
LEAGUE_TOOLS = [
    _tool("get_mayhem_team_comparison", "Compare BOTH current teams by champion tier. Computes all ten tiers, exact counts and verdict relative to the host (你们这边/对面). Use for which side has better tiers, never invent a win probability.", {}, []),
    _tool(
        "lookup_game_item",
        "Resolve an ITEM name/nickname (e.g. 狂妄, 饮血, 破败) to Mayhem item ID, names and effects from arammeta. Not augments. Use the original spoken name. Does not require a champion.",
        {"query": {"type": "string", "maxLength": 100}}, ["query"],
    ),
    _tool(
        "get_arammeta_stats",
        "Secondary ARAM Mayhem source: kind=champion returns OVERALL hero win rate (no equipment/augment conditioning); kind=item/augment returns conditional statistics with game counts and publisher win-rate fields. Use when OP.GG lacks comparable data. Compare BOTH offered choices in this source, never mix its wr/score with OP.GG performance. Published selections are not exhaustive. IDs must come from verified item/augment resolution.",
        {"champion": CHAMPION, "kind": {"type": "string", "enum": ["champion", "item", "augment"]},
         "rarity": {"type": "string", "enum": ["silver", "gold", "prismatic"], "description": "Augments only: 银/金/彩 rarity filter."},
         "query": {"type": "string", "maxLength": 100, "description": "One original name; not an OR/list query."},
         "ids": {"type": "array", "items": {"type": "integer", "minimum": 1}, "maxItems": 12},
         "limit": {"type": "integer", "minimum": 1, "maximum": 12}}, ["champion", "kind"],
    ),
    _tool(
        "resolve_game_name",
        "Local name database: resolve a Chinese champion nickname/name or Mayhem augment "
        "translation to canonical English name, internal key and namespaced ID. "
        "Pass the ORIGINAL name the speaker used, never your own English translation. "
        "Use when absent from nameGlossary; no web search. Clarify ambiguous matches; never guess IDs.",
        {"kind": {"type": "string", "enum": ["champion", "augment"]},
         "query": {"type": "string", "maxLength": 100,
                   "description": "Original spoken name, including Chinese; do not translate before lookup."}},
        ["kind", "query"],
    ),
    _tool(
        "get_mayhem_champion_tier",
        "CHAMPION tier/ranking ONLY for OP.GG ARAM Mayhem (英雄T几/英雄评级). "
        "Returns championTier, tierLabel, championRank; not augment tier or player rank. "
        "Use the champion's English name or slug. No win-rate or rank-bracket claims.",
        {"champion": CHAMPION},
        ["champion"],
    ),
    _tool(
        "get_live_game_state",
        "Latest background snapshot from this computer, not the speaker's identity. No history.",
        {},
        [],
    ),
    _tool(
        "get_mayhem_build",
        "Equipment ONLY: cached OP.GG Mayhem starter/core/boots alternatives. "
        "Not augment/海克斯/强化 recommendations. Use for missing equipment details; "
        "source alternatives are not win rates or proof of optimality.",
        {"champion": CHAMPION},
        ["champion"],
    ),
    _tool(
        "get_mayhem_augments",
        "Augments/海克斯/强化 ONLY: cached OP.GG Mayhem choices and effect descriptions, "
        "NOT shop equipment. Compare this champion's performance for offered choices. "
        "sourceOrder preserves list position across filtered queries. "
        "Filter offered IDs or one original name (Chinese regional names accepted and resolved locally); "
        "do not translate before lookup. performance/popularity are NOT win rates.",
        {
            "champion": CHAMPION,
            "query": {
                "type": "string",
                "maxLength": 100,
                "description": "One ORIGINAL name the speaker used: Chinese regional name or English name/key substring. Never translate it yourself. NOT a list. Omit for general candidates.",
            },
            "augment_ids": {
                "type": "array",
                "items": {"type": "integer", "minimum": 1},
                "maxItems": 12,
                "description": "Only known offered IDs; never guess IDs from names.",
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 12},
            "all_matches": {"type": "boolean", "description": "Return every matching augment without pagination. Defaults true for rarity candidate identification; descriptions remain opt-in."},
            "offset": {"type": "integer", "minimum": 0, "maximum": 1000},
            "rarity": {"type": "string", "enum": ["silver", "gold", "prismatic"], "description": "银/金/彩强化品质；not champion or augment strength tier."},
            "min_popular": {"type": "number", "minimum": 0, "maximum": 100, "description": "Minimum OP.GG popular value, in source percentage units. Candidate lists default to 0.01. Zero/unknown popularity and unnamed rows cannot rank as strongest. Not a sample-count or eligibility guarantee."},
            "sort_by": {"type": "string", "enum": ["source", "performance"], "description": "Use performance for best/top recommendations, sorted descending BEFORE pagination."},
            "include_descriptions": {
                "type": "boolean",
                "default": False,
                "description": "Default false for names/scores. Set true only for an effect or explanation query.",
            },
        },
        ["champion"],
    ),
]


# Keep the old adapter callable for frozen replay and internal composition only;
# advertise the two task-specific tools to live models.
LEGACY_TOOLS = [t for t in LEAGUE_TOOLS if t['name'] == 'get_mayhem_augments']
LEAGUE_TOOLS = [t for t in LEAGUE_TOOLS if t['name'] != 'get_mayhem_augments']
LEAGUE_TOOLS += [
    _tool('identify_mayhem_augment',
          'Identify an augment using exact original name or the complete multilingual catalog. rarity=all returns all colors without pagination; use it when color is unknown. Omitted rarity also browses all colors if exact lookup fails. Returns identity ONLY, no strength ranking. Not the player offered options; never recommend from this list.',
          {'champion': CHAMPION, 'query': {'type':'string','maxLength':100},
           'rarity': {'type':'string','enum':['all','silver','gold','prismatic'], 'description':'all: return all augment colors for identity lookup; use when color is not specified. 银/金/彩 are rarity, not strength tier.'}}, ['champion']),
    _tool('compare_mayhem_choices',
          'Evaluate ONLY 1 to 3 augment names supplied by the player, for this champion. Automatically use OP.GG then comparable arammeta fallback. No global recommendations. One option means evaluate that option only. Keep names exactly as spoken.',
          {'champion': CHAMPION,
           'options': {'type':'array','items':{'type':'string','maxLength':100},'minItems':1,'maxItems':3},
           'rarity': {'type':'string','enum':['silver','gold','prismatic']},
           'include_descriptions': {'type':'boolean'}}, ['champion','options']),
]


class LeagueToolExecutor:
    """Bound arguments, parallelism, time, and returned text; never run arbitrary tools."""

    def __init__(self, context: GameContextService, *, timeout: float = 12.0) -> None:
        self.context = context
        self.timeout = timeout
        self._semaphore = asyncio.Semaphore(2)

    async def execute(self, name: str, arguments: str) -> str:
        try:
            schema = next(
                (t["parameters"] for t in LEAGUE_TOOLS + LEGACY_TOOLS if t["name"] == name), None
            )
            if (
                schema is None
                or not isinstance(arguments, str)
                or len(arguments) > 4096
            ):
                raise ValueError
            args = json.loads(arguments)
            if not isinstance(args, dict) or set(args) - set(schema["properties"]):
                raise ValueError
            if any(key not in args for key in schema["required"]):
                raise ValueError
            for key, value in args.items():
                spec = schema["properties"][key]
                expected = {
                    "string": str,
                    "integer": int,
                    "boolean": bool,
                    "number": (int, float),
                    "array": list,
                }[spec["type"]]
                if not (type(value) in expected if isinstance(expected, tuple) else type(value) is expected):
                    raise ValueError
                if "enum" in spec and value not in spec["enum"]:
                    raise ValueError
                if isinstance(value, str) and len(value) > spec.get("maxLength", 100):
                    raise ValueError
                if spec["type"] == "number" and (not math.isfinite(value) or not spec.get("minimum", 0) <= value <= spec.get("maximum", 100)):
                    raise ValueError
                if type(value) is int and not spec.get(
                    "minimum", 0
                ) <= value <= spec.get("maximum", 1000):
                    raise ValueError
                if isinstance(value, list):
                    if not spec.get("minItems", 0) <= len(value) <= spec.get("maxItems", 12):
                        raise ValueError
                    if spec["items"]["type"] == "string":
                        if any(not isinstance(v, str) or not v.strip() or len(v) > 100 for v in value):
                            raise ValueError
                    elif any(type(v) is not int or v < 1 for v in value):
                        raise ValueError
            if name in {"identify_mayhem_augment", "compare_mayhem_choices"}:
                from .augment_choices import identify, compare
                result = await (identify if name == "identify_mayhem_augment" else compare)(self, **args)
            elif name in {"lookup_game_item", "get_arammeta_stats"}:
                from .arammeta import AramMetaClient
                client = self.context.__dict__.get("arammeta")
                if client is None:
                    client = AramMetaClient()
                    self.context.arammeta = client
                if name == "get_arammeta_stats":
                    # Preserve the item's category; augment aliases resolve separately.
                    if args["kind"] == "augment" and args.get("query"):
                        mapped = self.context.names.resolve_augment(args["query"])
                        if mapped.get("status") == "ok":
                            args["query"] = mapped["augment"]["nameEn"]
                    mapped = self.context.names.resolve_champion(args["champion"])
                    if mapped.get("status") == "ok":
                        args["champion"] = mapped["champion"]["nameEn"]
                method = client.lookup_item if name == "lookup_game_item" else client.get_stats
                async with asyncio.timeout(self.timeout):
                    async with self._semaphore:
                        result = await asyncio.to_thread(method, **args)
            elif name == "resolve_game_name":
                resolver = (self.context.names.resolve_champion if args["kind"] == "champion"
                            else self.context.names.resolve_augment)
                result = resolver(args["query"])
            elif name == "get_mayhem_team_comparison":
                from .team_comparison import compare_team_tiers
                async with asyncio.timeout(self.timeout):
                    result = await asyncio.to_thread(compare_team_tiers, self.context.snapshot(), self.context.opgg.get_champion_tier)
            elif name == "get_live_game_state":
                result = self.context.snapshot()
            else:
                champion = args.get("champion", "")
                if any(ord(char) > 127 for char in champion) or champion.isdigit():
                    mapped = self.context.names.resolve_champion(champion)
                    if mapped.get("status") != "ok":
                        return encode({"status": mapped.get("status", "not_found"),
                                       "reason": "champion_name_unresolved", "nameResolution": mapped})
                    args["champion"] = mapped["champion"]["opggSlug"]
                unresolved_augment_query = None
                if name == "get_mayhem_augments":
                    query = args.get("query", "")
                    if any(ord(char) > 127 for char in query):
                        mapped = self.context.names.resolve_augment(query)
                        if mapped.get("status") != "ok":
                            if args.get("rarity"):
                                unresolved_augment_query = query
                                args["query"] = ""
                                args["all_matches"] = True
                                args["offset"] = 0
                            else:
                                return encode({"status": mapped.get("status", "not_found"),
                                               "reason": "augment_name_unresolved", "nameResolution": mapped})
                        else:
                            # Resolve translation, not a guessed numeric ID.
                            args["query"] = mapped["augment"]["nameEn"]
                    # Keep routine list/score queries compact, like the bulk MCP.
                    args.setdefault("include_descriptions", False)
                    if args.get("rarity") and not args.get("query") and not args.get("augment_ids") and args.get("sort_by", "source") == "source":
                        args["all_matches"] = True
                method = getattr(self.context.opgg, {
                    "get_mayhem_build": "get_build",
                    "get_mayhem_augments": "get_augments",
                    "get_mayhem_champion_tier": "get_champion_tier",
                }[name])
                async with asyncio.timeout(self.timeout):
                    async with self._semaphore:
                        result = await asyncio.to_thread(method, **args)
                if name == "get_mayhem_augments" and isinstance(result, dict):
                    if unresolved_augment_query:
                        result["unresolvedNameQuery"] = unresolved_augment_query
                        result["queryMode"] = "rarity_candidates_after_unresolved_name"
                        result["nameResolutionInstruction"] = "Original phrase was not an exact name. Match corrected speech against this complete filtered multilingual candidate list; these are NOT exact query matches. Missing statistics do not prove ineligibility."
                    for row in result.get("augments", []):
                        mapped = self.context.names.resolve_augment(str(row.get("id", "")))
                        if mapped.get("status") == "ok":
                            record = mapped["augment"]
                            if (record.get("augmentId") == row.get("id")
                                    and record.get("nameEn", "").casefold() == str(row.get("name", "")).casefold()):
                                row["nameZh"] = record["nameZh"]
                                row["nameEn"] = record["nameEn"]
                                row["idNamespace"] = record["namespace"]
                                if record.get("namesByLocale"):
                                    row["namesByLocale"] = record["namesByLocale"]
                                if record.get("namesByRegion"):
                                    row["namesByRegion"] = record["namesByRegion"]
            output = encode(result)
            return (
                output
                if len(output) <= (250000 if name in {"get_mayhem_augments", "identify_mayhem_augment", "compare_mayhem_choices"} else 16000)
                else encode(
                    {
                        "status": "too_large",
                        "message": "Narrow the query or reduce limit.",
                    }
                )
            )
        except (ValueError, TypeError):
            return encode({"status": "invalid_arguments"})
        except asyncio.TimeoutError:
            return encode({"status": "unavailable", "reason": "tool_timeout"})
        except Exception:  # noqa: BLE001 - isolate untrusted upstream failures
            # Do not expose filesystem paths, account data, or raw exceptions.
            return encode({"status": "unavailable", "reason": "tool_failed"})
