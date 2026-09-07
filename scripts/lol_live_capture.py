"""Capture privacy-safe League Live Client Data for schema discovery.

The collector uses the game-stats endpoint to detect an active match. During a
match it polls every documented Live Client Data endpoint, combines the results
into snapshots, redacts player identifiers, and records JSONL under the
Git-ignored logs directory. A loopback-only HTTP service exposes the current
status and latest snapshot for future MCP integration.
"""

from __future__ import annotations

import argparse
import copy
import json
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


DEFAULT_API_BASE_URL = "https://127.0.0.1:2999"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8766
DETECTION_PATH = "/liveclientdata/gamestats"
OPENAPI_PATH = "/swagger/v3/openapi.json"

# These endpoints do not require a player identifier and are refreshed on every
# normal polling cycle. The OpenAPI document may add more compatible endpoints
# at runtime.
FALLBACK_SHARED_PATHS = (
    "/liveclientdata/activeplayer",
    "/liveclientdata/activeplayername",
    "/liveclientdata/activeplayerabilities",
    "/liveclientdata/activeplayerrunes",
    "/liveclientdata/allgamedata",
    "/liveclientdata/eventdata",
    "/liveclientdata/gamestats",
    "/liveclientdata/playerlist",
)

# These endpoints require the current Riot ID. They are polled less frequently
# for every player, then cached into each combined snapshot.
FALLBACK_PLAYER_PATHS = (
    "/liveclientdata/playeritems",
    "/liveclientdata/playermainrunes",
    "/liveclientdata/playerscores",
    "/liveclientdata/playersummonerspells",
)

PRIVATE_KEYS = {
    "accountid",
    "acer",
    "assisters",
    "game_name",
    "gamename",
    "killername",
    "playername",
    "puuid",
    "recipient",
    "riotid",
    "riotidgamename",
    "riotidtagline",
    "summonerid",
    "summonername",
    "tag_line",
    "tagline",
    "victimname",
}
SENSITIVE_KEY_FRAGMENTS = ("password", "secret", "token")
EXPECTED_CAPTURE_ERRORS = (
    OSError,
    TimeoutError,
    ValueError,
    json.JSONDecodeError,
    urllib.error.URLError,
)


def utc_now() -> str:
    """Return an RFC 3339 timestamp with second precision."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def sanitize_payload(value: Any) -> Any:
    """Recursively remove account and player identifiers from a payload."""
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            normalized = key.casefold().replace("_", "")
            if normalized in PRIVATE_KEYS or any(
                fragment in normalized for fragment in SENSITIVE_KEY_FRAGMENTS
            ):
                sanitized[key] = "<redacted>"
            else:
                sanitized[key] = sanitize_payload(item)
        return sanitized
    if isinstance(value, list):
        return [sanitize_payload(item) for item in value]
    return value


def get_player_identifier(player: Any) -> str | None:
    """Extract an identifier suitable only for an in-memory API request."""
    identifiers = get_player_identifiers(player)
    return identifiers[0] if identifiers else None


def get_player_identifiers(player: Any) -> tuple[str, ...]:
    """Return request-compatible player identifiers in observed success order."""
    if not isinstance(player, dict):
        return ()
    candidates: list[Any] = [player.get("summonerName")]
    game_name = player.get("riotIdGameName") or player.get("gameName")
    tag_line = player.get("riotIdTagLine") or player.get("tagLine")
    if isinstance(game_name, str) and isinstance(tag_line, str):
        candidates.append(f"{game_name}#{tag_line}")
    candidates.append(player.get("riotId"))
    return tuple(dict.fromkeys(
        value for value in candidates if isinstance(value, str) and value
    ))


def annotate_all_game_data(payload: Any) -> Any:
    """Tag the local player before identifying fields are redacted."""
    if not isinstance(payload, dict):
        return payload
    active_player = payload.get("activePlayer")
    active_identifiers = set(get_player_identifiers(active_player))
    players = payload.get("allPlayers")
    if isinstance(players, list) and active_identifiers:
        for player in players:
            if isinstance(player, dict):
                player["isActivePlayer"] = bool(
                    active_identifiers.intersection(get_player_identifiers(player))
                )
    return payload


def prepare_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    """Prepare one legacy all-game-data snapshot for tests and consumers."""
    prepared = annotate_all_game_data(copy.deepcopy(payload))
    return {
        "capturedAt": utc_now(),
        "source": "/liveclientdata/allgamedata",
        "data": sanitize_payload(prepared),
    }


def create_local_ssl_context() -> ssl.SSLContext:
    """Create a TLS context for Riot's loopback self-signed certificate."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def fetch_json(
    url: str,
    *,
    timeout: float = 1.5,
    ssl_context: ssl.SSLContext | None = None,
) -> Any:
    """Fetch JSON from Riot's local game process."""
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "MIRA.CORD/1"},
    )
    with urllib.request.urlopen(
        request,
        timeout=timeout,
        context=ssl_context or create_local_ssl_context(),
    ) as response:
        return json.load(response)


def fetch_live_data(
    api_url: str,
    *,
    timeout: float = 1.5,
    ssl_context: ssl.SSLContext | None = None,
) -> dict[str, Any]:
    """Fetch one object payload for backward-compatible callers."""
    payload = fetch_json(api_url, timeout=timeout, ssl_context=ssl_context)
    if not isinstance(payload, dict):
        raise ValueError("League Live Client Data returned a non-object payload.")
    return payload


def endpoint_name(path: str) -> str:
    """Return a stable, compact key for one endpoint path."""
    return path.rstrip("/").rsplit("/", 1)[-1]


def discover_endpoint_paths(openapi: Any) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Discover GET endpoints that need no parameters or only one Riot ID."""
    shared = set(FALLBACK_SHARED_PATHS)
    player = set(FALLBACK_PLAYER_PATHS)
    if not isinstance(openapi, dict) or not isinstance(openapi.get("paths"), dict):
        return tuple(sorted(shared)), tuple(sorted(player))

    for path, path_item in openapi["paths"].items():
        if not isinstance(path, str) or not path.startswith("/liveclientdata/"):
            continue
        if not isinstance(path_item, dict) or not isinstance(path_item.get("get"), dict):
            continue
        operation = path_item["get"]
        parameters: list[Any] = []
        for owner in (path_item, operation):
            if isinstance(owner.get("parameters"), list):
                parameters.extend(owner["parameters"])
        parameter_names = {
            str(parameter.get("name", "")).casefold()
            for parameter in parameters
            if isinstance(parameter, dict)
        }
        required_names = {
            str(parameter.get("name", "")).casefold()
            for parameter in parameters
            if isinstance(parameter, dict) and parameter.get("required", False)
        }
        if "riotid" in parameter_names and required_names <= {"riotid"}:
            player.add(path)
            shared.discard(path)
        elif not required_names:
            shared.add(path)

    return tuple(sorted(shared)), tuple(sorted(player))


def build_player_url(api_base_url: str, path: str, riot_id: str) -> str:
    """Build a player endpoint URL without ever logging the returned value."""
    query = urllib.parse.urlencode({"riotId": riot_id})
    return f"{api_base_url}{path}?{query}"


def successful_result(data: Any) -> dict[str, Any]:
    """Wrap a sanitized endpoint response."""
    return {"ok": True, "data": sanitize_payload(data)}


def shared_successful_result(path: str, data: Any) -> dict[str, Any]:
    """Wrap shared data while protecting scalar identity endpoints."""
    if path == "/liveclientdata/activeplayername":
        return {"ok": True, "data": "<redacted>"}
    return successful_result(data)


def failed_result(error: Exception) -> dict[str, Any]:
    """Record a safe failure without a URL, message, or player identifier."""
    return {"ok": False, "errorType": type(error).__name__}


class PlayerAliases:
    """Assign stable, session-local labels without persisting source IDs."""

    def __init__(self) -> None:
        self._aliases: dict[str, str] = {}

    def get(self, riot_id: str) -> str:
        if riot_id not in self._aliases:
            self._aliases[riot_id] = f"player-{len(self._aliases) + 1:02d}"
        return self._aliases[riot_id]


def extract_players(
    player_list: Any,
    aliases: PlayerAliases,
    *,
    active_player_name: Any = None,
) -> list[tuple[str, str, dict[str, Any]]]:
    """Return request IDs, safe labels, and sanitized player summaries."""
    if not isinstance(player_list, list):
        return []
    active_identifier = (
        active_player_name if isinstance(active_player_name, str) else None
    )
    extracted: list[tuple[str, str, dict[str, Any]]] = []
    for player in player_list:
        identifiers = get_player_identifiers(player)
        if not identifiers or not isinstance(player, dict):
            continue
        riot_id = identifiers[0]
        alias = aliases.get(riot_id)
        summary = copy.deepcopy(player)
        summary["playerSlot"] = alias
        summary["isActivePlayer"] = active_identifier in identifiers
        extracted.append((riot_id, alias, sanitize_payload(summary)))
    return extracted


def poll_shared_endpoints(
    api_base_url: str,
    paths: tuple[str, ...],
    *,
    ssl_context: ssl.SSLContext,
    detection_payload: Any | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Poll all shared endpoints and retain raw values only in memory."""
    results: dict[str, dict[str, Any]] = {}
    raw: dict[str, Any] = {}
    for path in paths:
        name = endpoint_name(path)
        try:
            data = (
                detection_payload
                if path == DETECTION_PATH and detection_payload is not None
                else fetch_json(f"{api_base_url}{path}", ssl_context=ssl_context)
            )
            if path == "/liveclientdata/allgamedata":
                data = annotate_all_game_data(data)
            raw[name] = data
            results[name] = shared_successful_result(path, data)
        except EXPECTED_CAPTURE_ERRORS as error:
            results[name] = failed_result(error)
    return results, raw


def poll_player_endpoints(
    api_base_url: str,
    paths: tuple[str, ...],
    players: list[tuple[str, str, dict[str, Any]]],
    *,
    ssl_context: ssl.SSLContext,
) -> dict[str, Any]:
    """Poll every parameterized endpoint for every known player."""
    sampled_at = utc_now()
    results: dict[str, Any] = {}
    for riot_id, alias, summary in players:
        endpoint_results: dict[str, dict[str, Any]] = {}
        for path in paths:
            try:
                data = fetch_json(
                    build_player_url(api_base_url, path, riot_id),
                    ssl_context=ssl_context,
                )
                endpoint_results[endpoint_name(path)] = successful_result(data)
            except EXPECTED_CAPTURE_ERRORS as error:
                endpoint_results[endpoint_name(path)] = failed_result(error)
        results[alias] = {
            "sampledAt": sampled_at,
            "player": summary,
            "endpoints": endpoint_results,
        }
    return results


def write_json_atomic(path: Path, payload: Any) -> None:
    """Replace a JSON file without exposing a partially written document."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


@dataclass
class CaptureState:
    """Thread-safe state shared by the collector and HTTP service."""

    output_root: Path
    lock: threading.RLock = field(default_factory=threading.RLock)
    phase: str = "waiting_for_game"
    latest: dict[str, Any] | None = None
    session_name: str | None = None
    snapshot_count: int = 0
    last_success_at: str | None = None
    last_error: str | None = None
    endpoint_manifest: dict[str, Any] | None = None
    _session_dir: Path | None = None
    _last_success_monotonic: float | None = None

    def status_payload(self) -> dict[str, Any]:
        with self.lock:
            return {
                "service": "MIRA.CORD League capture",
                "phase": self.phase,
                "session": self.session_name,
                "snapshotCount": self.snapshot_count,
                "lastSuccessAt": self.last_success_at,
                "lastError": self.last_error,
                "endpointManifest": self.endpoint_manifest,
                "privacy": "Player and account identifiers are redacted.",
            }

    def begin_game(
        self,
        *,
        openapi: Any,
        shared_paths: tuple[str, ...],
        player_paths: tuple[str, ...],
    ) -> None:
        with self.lock:
            if self.phase == "in_game":
                return
            session_name = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            session_dir = self.output_root / "sessions" / session_name
            session_dir.mkdir(parents=True, exist_ok=True)
            self.phase = "in_game"
            self.session_name = session_name
            self.snapshot_count = 0
            self._session_dir = session_dir
            self.endpoint_manifest = {
                "shared": list(shared_paths),
                "perPlayer": list(player_paths),
            }
            write_json_atomic(session_dir / "openapi.json", sanitize_payload(openapi))
            write_json_atomic(session_dir / "endpoints.json", self.endpoint_manifest)
            print(f"Live game detected. Recording to {session_dir}", flush=True)

    def record(self, snapshot: dict[str, Any]) -> None:
        with self.lock:
            if self.phase != "in_game" or self._session_dir is None:
                raise RuntimeError("Cannot record before a game session starts.")
            with (self._session_dir / "snapshots.jsonl").open(
                "a", encoding="utf-8"
            ) as stream:
                stream.write(json.dumps(snapshot, ensure_ascii=False) + "\n")
            self.latest = snapshot
            self.snapshot_count += 1
            self.last_success_at = snapshot["capturedAt"]
            self.last_error = None
            self._last_success_monotonic = time.monotonic()
            write_json_atomic(self.output_root / "latest.json", snapshot)
            write_json_atomic(self.output_root / "status.json", self.status_payload())

    def note_unavailable(self, error: Exception, *, end_grace_seconds: float) -> None:
        with self.lock:
            self.last_error = type(error).__name__
            if (
                self.phase == "in_game"
                and self._last_success_monotonic is not None
                and time.monotonic() - self._last_success_monotonic
                >= end_grace_seconds
            ):
                assert self._session_dir is not None
                summary = self.status_payload()
                summary["phase"] = "game_ended"
                summary["endedAt"] = utc_now()
                write_json_atomic(self._session_dir / "summary.json", summary)
                print(
                    f"Game endpoint closed after {self.snapshot_count} snapshots. "
                    "Waiting for another game.",
                    flush=True,
                )
                self.phase = "waiting_for_game"
                self.session_name = None
                self.snapshot_count = 0
                self.endpoint_manifest = None
                self._session_dir = None
                self._last_success_monotonic = None
            write_json_atomic(self.output_root / "status.json", self.status_payload())


class CaptureRequestHandler(BaseHTTPRequestHandler):
    """Serve capture state on loopback for diagnostics and future MCP tools."""

    state: CaptureState

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == "/health":
            self.send_json(HTTPStatus.OK, self.state.status_payload())
            return
        if self.path == "/latest":
            with self.state.lock:
                latest = copy.deepcopy(self.state.latest)
            if latest is None:
                self.send_json(
                    HTTPStatus.NOT_FOUND,
                    {"error": "No active-game snapshot has been captured yet."},
                )
            else:
                self.send_json(HTTPStatus.OK, latest)
            return
        self.send_json(
            HTTPStatus.NOT_FOUND,
            {"error": "Use /health or /latest."},
        )

    def send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


def collector_loop(
    state: CaptureState,
    *,
    api_base_url: str,
    interval: float,
    player_interval: float,
    end_grace_seconds: float,
    stop_event: threading.Event,
) -> None:
    """Detect matches and poll every compatible local game endpoint."""
    ssl_context = create_local_ssl_context()
    shared_paths = FALLBACK_SHARED_PATHS
    player_paths = FALLBACK_PLAYER_PATHS
    aliases = PlayerAliases()
    player_cache: dict[str, Any] = {}
    last_player_poll = 0.0

    while not stop_event.is_set():
        try:
            detection_payload = fetch_json(
                f"{api_base_url}{DETECTION_PATH}", ssl_context=ssl_context
            )
        except EXPECTED_CAPTURE_ERRORS as error:
            state.note_unavailable(error, end_grace_seconds=end_grace_seconds)
            if state.phase == "waiting_for_game":
                aliases = PlayerAliases()
                player_cache = {}
                last_player_poll = 0.0
                shared_paths = FALLBACK_SHARED_PATHS
                player_paths = FALLBACK_PLAYER_PATHS
            stop_event.wait(interval)
            continue

        if state.phase != "in_game":
            try:
                openapi = fetch_json(
                    f"{api_base_url}{OPENAPI_PATH}", ssl_context=ssl_context
                )
            except EXPECTED_CAPTURE_ERRORS:
                openapi = {"unavailable": True}
            shared_paths, player_paths = discover_endpoint_paths(openapi)
            state.begin_game(
                openapi=openapi,
                shared_paths=shared_paths,
                player_paths=player_paths,
            )

        shared_results, raw = poll_shared_endpoints(
            api_base_url,
            shared_paths,
            ssl_context=ssl_context,
            detection_payload=detection_payload,
        )
        active_player_name = raw.get("activeplayername")
        players = extract_players(
            raw.get("playerlist"), aliases, active_player_name=active_player_name
        )
        now = time.monotonic()
        if not player_cache or now - last_player_poll >= player_interval:
            player_cache = poll_player_endpoints(
                api_base_url,
                player_paths,
                players,
                ssl_context=ssl_context,
            )
            last_player_poll = now

        snapshot = {
            "capturedAt": utc_now(),
            "source": "League Live Client Data API",
            "polling": {
                "sharedIntervalSeconds": interval,
                "perPlayerIntervalSeconds": player_interval,
            },
            "sharedEndpoints": shared_results,
            "perPlayer": player_cache,
        }
        state.record(snapshot)
        stop_event.wait(interval)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-base-url", default=DEFAULT_API_BASE_URL)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--player-interval", type=float, default=10.0)
    parser.add_argument("--end-grace-seconds", type=float, default=15.0)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("logs/lol_live_capture"),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    state = CaptureState(args.output.resolve())
    state.output_root.mkdir(parents=True, exist_ok=True)
    write_json_atomic(state.output_root / "status.json", state.status_payload())

    stop_event = threading.Event()
    collector = threading.Thread(
        target=collector_loop,
        kwargs={
            "state": state,
            "api_base_url": args.api_base_url.rstrip("/"),
            "interval": args.interval,
            "player_interval": args.player_interval,
            "end_grace_seconds": args.end_grace_seconds,
            "stop_event": stop_event,
        },
        daemon=True,
        name="lol-live-collector",
    )
    collector.start()

    handler = type("BoundCaptureRequestHandler", (CaptureRequestHandler,), {})
    handler.state = state
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(
        f"MIRA.CORD League capture is listening on "
        f"http://{args.host}:{args.port}. Waiting for a live game.",
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        server.shutdown()
        server.server_close()
        collector.join(timeout=max(2.0, args.interval + 1.0))


if __name__ == "__main__":
    main()
