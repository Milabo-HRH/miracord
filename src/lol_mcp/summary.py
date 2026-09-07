"""Convert Live Client payloads into compact, identity-safe LLM context."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

_AUGMENT_MARKER = "Spell_Augment_"
_AUGMENT_VALUE_PATTERN = re.compile(
    r"Spell_Augment_(?P<identifier>.+?)_(?:DisplayName|Description)$"
)
_EVENT_DETAIL_KEYS = (
    "DragonType",
    "InhibKilled",
    "KillStreak",
    "Result",
    "Stolen",
    "TurretKilled",
)


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _number(value: Any, *, digits: int = 1) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, int):
        return value
    return round(value, digits)


def _without_none(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item is not None}


def _player_identifiers(player: Any) -> tuple[str, ...]:
    if not isinstance(player, dict):
        return ()
    values: list[Any] = [player.get("summonerName"), player.get("riotId")]
    game_name = player.get("riotIdGameName") or player.get("gameName")
    tag_line = player.get("riotIdTagLine") or player.get("tagLine")
    values.extend(
        (game_name, f"{game_name}#{tag_line}" if game_name and tag_line else None)
    )
    return tuple(value for value in values if isinstance(value, str) and value)


def _iter_dicts(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _iter_dicts(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_dicts(item)


def parse_augments(value: Any) -> list[dict[str, Any]]:
    """Extract unique Mayhem augment stages from a nested ability container."""
    found: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for item in _iter_dicts(value):
        raw_values = (
            _text(item.get("rawDisplayName")),
            _text(item.get("rawDescription")),
        )
        raw = next(
            (entry for entry in raw_values if entry and _AUGMENT_MARKER in entry), None
        )
        if raw is None:
            continue
        match = _AUGMENT_VALUE_PATTERN.search(raw)
        if match is None:
            continue
        identifier = match.group("identifier")
        stage = 1
        if identifier.endswith(("2", "3")):
            stage = int(identifier[-1])
            identifier = identifier[:-1]
        identity = (identifier, stage)
        if identity in seen:
            continue
        seen.add(identity)
        found.append(
            _without_none(
                {
                    "name": _text(item.get("displayName")) or identifier,
                    "internalId": identifier,
                    "stage": stage,
                }
            )
        )
    return found


def _normal_summoner_spells(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return []
    spells: list[str] = []
    for spell in value.values():
        if not isinstance(spell, dict) or parse_augments(spell):
            continue
        display_name = _text(spell.get("displayName"))
        if display_name:
            spells.append(display_name)
    return spells


def _items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        if not isinstance(item, dict):
            continue
        result.append(
            _without_none(
                {
                    "id": _number(item.get("itemID"), digits=0),
                    "name": _text(item.get("displayName")),
                    "count": _number(item.get("count"), digits=0),
                    "slot": _number(item.get("slot"), digits=0),
                }
            )
        )
    return result


def _scores(value: Any) -> dict[str, int | float]:
    if not isinstance(value, dict):
        return {}
    keys = ("kills", "deaths", "assists", "creepScore")
    return {
        key: number
        for key in keys
        if (number := _number(value.get(key), digits=0)) is not None
    }


def _runes(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for output_key, source_key in (
        ("keystone", "keystone"),
        ("primaryTree", "primaryRuneTree"),
        ("secondaryTree", "secondaryRuneTree"),
    ):
        entry = value.get(source_key)
        if isinstance(entry, dict) and (name := _text(entry.get("displayName"))):
            result[output_key] = name
    return result


def _alias_players(
    players: Any,
) -> tuple[list[tuple[dict[str, Any], str]], dict[str, str]]:
    if not isinstance(players, list):
        return [], {}
    team_counts: dict[str, int] = {}
    aliased: list[tuple[dict[str, Any], str]] = []
    identifier_aliases: dict[str, str] = {}
    for player in players:
        if not isinstance(player, dict):
            continue
        team = _text(player.get("team")) or "UNKNOWN"
        team_counts[team] = team_counts.get(team, 0) + 1
        alias = f"{team.lower()}-{team_counts[team]:02d}"
        aliased.append((player, alias))
        for identifier in _player_identifiers(player):
            identifier_aliases[identifier] = alias
    return aliased, identifier_aliases


def _event_actor(value: Any, aliases: dict[str, str]) -> str | None:
    return aliases.get(value) if isinstance(value, str) else None


def _event_actors(value: Any, aliases: dict[str, str]) -> list[str]:
    if not isinstance(value, list):
        return []
    return [alias for item in value if (alias := _event_actor(item, aliases))]


def _summarize_events(
    payload: dict[str, Any], aliases: dict[str, str], limit: int
) -> list[dict[str, Any]]:
    event_container = payload.get("events")
    events = (
        event_container.get("Events") if isinstance(event_container, dict) else None
    )
    if not isinstance(events, list):
        return []
    result: list[dict[str, Any]] = []
    for event in events[-limit:]:
        if not isinstance(event, dict):
            continue
        summarized: dict[str, Any] = _without_none(
            {
                "id": _number(event.get("EventID"), digits=0),
                "name": _text(event.get("EventName")),
                "timeSeconds": _number(event.get("EventTime")),
                "killerSlot": _event_actor(event.get("KillerName"), aliases),
                "victimSlot": _event_actor(event.get("VictimName"), aliases),
                "recipientSlot": _event_actor(event.get("Recipient"), aliases),
            }
        )
        assister_slots = _event_actors(event.get("Assisters"), aliases)
        if assister_slots:
            summarized["assisterSlots"] = assister_slots
        for key in _EVENT_DETAIL_KEYS:
            value = event.get(key)
            if isinstance(value, (str, int, float, bool)):
                summarized[key[0].lower() + key[1:]] = value
        result.append(summarized)
    return result


def _game_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    game = payload.get("gameData")
    if not isinstance(game, dict):
        game = {}
    mode = _text(game.get("gameMode"))
    map_number = _number(game.get("mapNumber"), digits=0)
    return _without_none(
        {
            "mode": mode,
            "mapName": _text(game.get("mapName")),
            "mapNumber": map_number,
            "mapTerrain": _text(game.get("mapTerrain")),
            "gameTimeSeconds": _number(game.get("gameTime")),
            "isAramMayhem": mode == "KIWI" and map_number == 12,
        }
    )


def build_live_game_state(
    payload: dict[str, Any], *, recent_event_limit: int = 12
) -> dict[str, Any]:
    """Build a complete but compact match state without player identities."""
    aliased_players, aliases = _alias_players(payload.get("allPlayers"))
    active_player = payload.get("activePlayer")
    active_identifiers = set(_player_identifiers(active_player))
    active_alias: str | None = None
    players: list[dict[str, Any]] = []
    for player, alias in aliased_players:
        is_active = bool(active_identifiers.intersection(_player_identifiers(player)))
        if is_active:
            active_alias = alias
        player_summary: dict[str, Any] = _without_none(
            {
                "slot": alias,
                "team": _text(player.get("team")),
                "champion": _text(player.get("championName")),
                "level": _number(player.get("level"), digits=0),
                "position": _text(player.get("position")),
                "isBot": player.get("isBot")
                if isinstance(player.get("isBot"), bool)
                else None,
                "isDead": player.get("isDead")
                if isinstance(player.get("isDead"), bool)
                else None,
                "respawnSeconds": _number(player.get("respawnTimer")),
                "isActivePlayer": is_active,
                "scores": _scores(player.get("scores")),
                "items": _items(player.get("items")),
                "summonerSpells": _normal_summoner_spells(player.get("summonerSpells")),
                "runes": _runes(player.get("runes")),
                "mayhemAugments": parse_augments(player.get("summonerSpells")),
            }
        )
        players.append(player_summary)

    active_summary: dict[str, Any] | None = None
    if isinstance(active_player, dict):
        active_summary = _without_none(
            {
                "slot": active_alias,
                "level": _number(active_player.get("level"), digits=0),
                "currentGold": _number(active_player.get("currentGold"), digits=0),
                "mayhemAugments": parse_augments(active_player.get("abilities")),
            }
        )
        if active_alias:
            active_augments = active_summary.get("mayhemAugments", [])
            for player in players:
                if player.get("slot") == active_alias and active_augments:
                    existing = player.setdefault("mayhemAugments", [])
                    known = {
                        (entry["internalId"], entry["stage"]) for entry in existing
                    }
                    existing.extend(
                        entry
                        for entry in active_augments
                        if (entry["internalId"], entry["stage"]) not in known
                    )

    return {
        "status": "in_game",
        "available": True,
        "source": "/liveclientdata/allgamedata",
        "game": _game_metadata(payload),
        "activePlayer": active_summary,
        "players": players,
        "recentEvents": _summarize_events(payload, aliases, recent_event_limit),
        "privacy": "Player and account identifiers are omitted; slots are session-local aliases.",
    }


def build_live_game_events(
    payload: dict[str, Any], *, limit: int = 10
) -> dict[str, Any]:
    """Build a focused recent-event response from a complete snapshot."""
    _, aliases = _alias_players(payload.get("allPlayers"))
    bounded_limit = max(1, min(limit, 50))
    return {
        "status": "in_game",
        "available": True,
        "source": "/liveclientdata/allgamedata",
        "game": _game_metadata(payload),
        "events": _summarize_events(payload, aliases, bounded_limit),
        "privacy": "Player and account identifiers are replaced by session-local slots.",
    }
