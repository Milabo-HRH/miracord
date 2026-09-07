from scripts.lol_live_capture import (
    FALLBACK_PLAYER_PATHS,
    FALLBACK_SHARED_PATHS,
    PlayerAliases,
    build_player_url,
    discover_endpoint_paths,
    extract_players,
    prepare_snapshot,
    sanitize_payload,
    shared_successful_result,
)


def test_sanitize_payload_removes_player_and_account_identifiers() -> None:
    payload = {
        "summonerName": "private-name",
        "riotId": "private-name#tag",
        "puuid": "private-puuid",
        "championName": "Annie",
        "nested": {"accessToken": "private-token", "level": 12},
    }

    sanitized = sanitize_payload(payload)

    assert sanitized == {
        "summonerName": "<redacted>",
        "riotId": "<redacted>",
        "puuid": "<redacted>",
        "championName": "Annie",
        "nested": {"accessToken": "<redacted>", "level": 12},
    }


def test_prepare_snapshot_marks_active_player_before_redaction() -> None:
    payload = {
        "activePlayer": {"summonerName": "local-player"},
        "allPlayers": [
            {"summonerName": "local-player", "championName": "Annie"},
            {"summonerName": "other-player", "championName": "Garen"},
        ],
    }

    snapshot = prepare_snapshot(payload)

    assert snapshot["source"] == "/liveclientdata/allgamedata"
    assert snapshot["data"]["activePlayer"]["summonerName"] == "<redacted>"
    assert snapshot["data"]["allPlayers"] == [
        {
            "summonerName": "<redacted>",
            "championName": "Annie",
            "isActivePlayer": True,
        },
        {
            "summonerName": "<redacted>",
            "championName": "Garen",
            "isActivePlayer": False,
        },
    ]


def test_discover_endpoint_paths_adds_compatible_openapi_get_routes() -> None:
    openapi = {
        "paths": {
            "/liveclientdata/newshared": {"get": {}},
            "/liveclientdata/newplayer": {
                "get": {
                    "parameters": [
                        {"name": "riotId", "required": True, "in": "query"}
                    ]
                }
            },
            "/liveclientdata/unsupported": {
                "get": {
                    "parameters": [
                        {"name": "unknown", "required": True, "in": "query"}
                    ]
                }
            },
        }
    }

    shared, player = discover_endpoint_paths(openapi)

    assert set(FALLBACK_SHARED_PATHS) <= set(shared)
    assert set(FALLBACK_PLAYER_PATHS) <= set(player)
    assert "/liveclientdata/newshared" in shared
    assert "/liveclientdata/newplayer" in player
    assert "/liveclientdata/unsupported" not in shared
    assert "/liveclientdata/unsupported" not in player


def test_extract_players_uses_safe_stable_slots() -> None:
    players = [
        {
            "riotId": "private one#NA1",
            "summonerName": "private one",
            "championName": "Annie",
        },
        {
            "riotIdGameName": "private two",
            "riotIdTagLine": "NA2",
            "championName": "Garen",
        },
    ]
    aliases = PlayerAliases()

    extracted = extract_players(
        players, aliases, active_player_name="private one#NA1"
    )
    extracted_again = extract_players(players, aliases)

    assert [entry[1] for entry in extracted] == ["player-01", "player-02"]
    assert [entry[1] for entry in extracted_again] == ["player-01", "player-02"]
    assert extracted[0][2]["riotId"] == "<redacted>"
    assert extracted[0][2]["summonerName"] == "<redacted>"
    assert extracted[0][2]["championName"] == "Annie"
    assert extracted[0][2]["isActivePlayer"] is True
    assert extracted[0][0] == "private one"


def test_build_player_url_encodes_riot_id_as_query_data() -> None:
    url = build_player_url(
        "https://127.0.0.1:2999",
        "/liveclientdata/playeritems",
        "private name#NA 1",
    )

    assert url == (
        "https://127.0.0.1:2999/liveclientdata/playeritems?"
        "riotId=private+name%23NA+1"
    )


def test_scalar_active_player_name_is_redacted() -> None:
    result = shared_successful_result(
        "/liveclientdata/activeplayername", "private player#NA1"
    )

    assert result == {"ok": True, "data": "<redacted>"}


def test_event_recipient_is_redacted() -> None:
    sanitized = sanitize_payload(
        {"EventName": "FirstBrick", "Recipient": "private player"}
    )

    assert sanitized == {"EventName": "FirstBrick", "Recipient": "<redacted>"}
