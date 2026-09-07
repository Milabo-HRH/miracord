"""Small, loopback-only client for League Live Client Data."""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_API_BASE_URL = "https://127.0.0.1:2999"
DEFAULT_CAPTURE_SNAPSHOT_PATH = (
    Path(__file__).resolve().parents[2]
    / "logs"
    / "lol_live_capture"
    / "latest.json"
)
EXPECTED_LIVE_CLIENT_ERRORS = (
    OSError,
    TimeoutError,
    ValueError,
    json.JSONDecodeError,
    urllib.error.URLError,
)


class LiveClientUnavailable(RuntimeError):
    """Raised when no usable League Live Client endpoint is available."""


def _loopback_base_url(value: str) -> str:
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("The Live Client base URL must use HTTP or HTTPS.")
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("The Live Client base URL must resolve to loopback.")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("The Live Client base URL cannot contain a path or query.")
    return value.rstrip("/")


def _local_ssl_context() -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


class LeagueLiveClient:
    """Read-only client whose destination is restricted to loopback."""

    def __init__(
        self,
        api_base_url: str = DEFAULT_API_BASE_URL,
        *,
        timeout: float = 1.5,
        latest_snapshot_path: str | Path | None = None,
    ) -> None:
        self.api_base_url = _loopback_base_url(api_base_url)
        self.timeout = timeout
        self.latest_snapshot_path = (
            Path(latest_snapshot_path) if latest_snapshot_path is not None else None
        )
        self._ssl_context = _local_ssl_context()

    def get_all_game_data(self) -> dict[str, Any]:
        """Fetch the canonical complete match snapshot."""
        return self._get_object("/liveclientdata/allgamedata")

    def get_game_stats(self) -> dict[str, Any]:
        """Fetch the lightweight endpoint used for availability checks."""
        return self._get_object("/liveclientdata/gamestats")

    def get_latest_captured_game_data(self) -> tuple[dict[str, Any], str | None]:
        """Read only the newest captured allgamedata snapshot, never its history."""
        if self.latest_snapshot_path is None:
            raise LiveClientUnavailable
        try:
            snapshot = json.loads(
                self.latest_snapshot_path.read_text(encoding="utf-8")
            )
            game_data = snapshot["sharedEndpoints"]["allgamedata"]["data"]
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise LiveClientUnavailable from error
        if not isinstance(game_data, dict):
            raise LiveClientUnavailable
        captured_at = snapshot.get("capturedAt")
        return game_data, captured_at if isinstance(captured_at, str) else None

    def _get_object(self, path: str) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.api_base_url}{path}",
            headers={
                "Accept": "application/json",
                "User-Agent": "MIRA.CORD-League-MCP/1",
            },
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=self.timeout,
                context=self._ssl_context,
            ) as response:
                payload = json.load(response)
        except EXPECTED_LIVE_CLIENT_ERRORS as error:
            raise LiveClientUnavailable from error
        if not isinstance(payload, dict):
            raise LiveClientUnavailable
        return payload
