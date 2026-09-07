"""Bounded, local interaction telemetry with opt-in, redacted replay capture.

The default stream contains only allowlisted operational metadata. Diagnostic
capture is explicit because transcripts and tool arguments can be personal.
No network export or raw audio capture is implemented here.
"""

from __future__ import annotations

import hashlib
import atexit
import hmac
import json
import logging
import math
import os
import queue
import re
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

SAFE_FIELDS = frozenset({
    "session_id", "turn_id", "response_id", "call_id", "item_id", "provider",
    "connection_epoch", "speaker_id", "guild_id", "status", "reason", "error_code",
    "function_name", "duration_ms", "elapsed_ms", "audio_ms", "audio_bytes",
    "cache_hit", "stale", "tool_count", "tool_round", "generation", "streaming",
    "prompt_hash", "tools_hash", "model", "event_type", "keyword", "disposition",
    "capture_enabled", "dropped_events", "source_count", "correlation", "count",
    "close_code", "terminal", "frames", "muted_frames", "peak_dbfs", "gate_open", "threshold_dbfs",
})
_SECRET_KEY = re.compile(
    r"(?i)(api.?key|authorization|token|password|secret|cookie|credential|"
    r"display.?name|summoner.?name|riot.?id|puuid|user.?id|discord.?id)"
)
_TOKEN = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{8,}|xai-[A-Za-z0-9_-]{8,}|"
                    r"[A-Za-z0-9_-]{24,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{20,})\b")
_BEARER = re.compile(r"(?i)\bBearer\s+[^\s\"',;]+")
_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|password|secret|token|authorization)\s*[:=]\s*[\"']?[^\s\"',;}]+"
)
_TOKEN_COUNTS = frozenset({"max_output_tokens", "max_response_output_tokens", "max_tokens",
                           "input_tokens", "output_tokens", "cached_tokens", "total_tokens"})


def fingerprint(value: Any) -> str:
    """Stable digest for comparing frozen prompt/tool configurations."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(os.getenv(name, str(default)))))
    except ValueError:
        return default


class JsonlObserver:
    """Thread-safe nonblocking producer with a bounded queue and rotating files."""

    def __init__(self, path: Path | str, *, enabled: bool = True,
                 capture_enabled: bool = False, max_bytes: int = 5_000_000,
                 backups: int = 3, capture_chars: int = 32768,
                 queue_size: int = 256) -> None:
        self.path = Path(path)
        self.enabled = enabled
        self.capture_enabled = enabled and capture_enabled
        self.max_bytes = max(1024, min(max_bytes, 100_000_000))
        self.backups = max(1, min(backups, 10))
        self.capture_chars = max(256, min(capture_chars, 131072))
        self.run_id = self.new_id("run")
        self._salt = secrets.token_bytes(32)
        self._secrets: set[str] = set()
        self._lock = threading.Lock()
        self._sequence = 0
        self._dropped = 0
        self._closed = False
        self._started = time.monotonic()
        self._queue: queue.Queue = queue.Queue(maxsize=max(1, queue_size))
        self._thread: threading.Thread | None = None
        # Values only, never their environment names, may reach the redactor.
        self.register_secrets(*(v for k, v in os.environ.items()
                                if _SECRET_KEY.search(k) and len(v) >= 8))
        if enabled:
            self._thread = threading.Thread(target=self._write_loop,
                                            name="interaction-log", daemon=True)
            self._thread.start()

    @staticmethod
    def new_id(prefix: str = "event") -> str:
        return f"{prefix}_{uuid.uuid4().hex}"

    def pseudonym(self, value: Any) -> str:
        """Correlate IDs within one process without storing the original ID."""
        return "anon_" + hmac.new(self._salt, str(value).encode(), hashlib.sha256).hexdigest()[:16]

    def register_secrets(self, *values: Any) -> None:
        with self._lock:
            for value in values:
                if isinstance(value, str) and len(value) >= 3 and len(self._secrets) < 1024:
                    self._secrets.add(value)

    def _redact_text(self, value: str) -> str:
        with self._lock:
            private = tuple(self._secrets)
        for token in sorted(private, key=len, reverse=True):
            value = value.replace(token, "[REDACTED]")
        value = _TOKEN.sub("[REDACTED]", value)
        value = _BEARER.sub("Bearer [REDACTED]", value)
        value = _ASSIGNMENT.sub(lambda match: match[1] + "=[REDACTED]", value)
        # Snowflakes and email addresses are never necessary to replay a voice turn.
        value = re.sub(r"\b\d{17,20}\b", "[REDACTED_ID]", value)
        return re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[REDACTED_EMAIL]", value)

    def redact(self, value: Any, *, _depth: int = 0) -> Any:
        """Bound and redact a diagnostic payload; callers must never pass audio."""
        if _depth > 12:
            return "[TRUNCATED]"
        if isinstance(value, dict):
            cleaned = {str(k)[:128]: "[REDACTED]" if _SECRET_KEY.search(str(k)) and str(k) not in _TOKEN_COUNTS else
                    self.redact(v, _depth=_depth + 1)
                    for k, v in list(value.items())[:256]
                    if not (str(k).lower() in {"audio", "delta_audio", "pcm", "raw_audio"}
                            and not isinstance(v, dict))}
            if len(value) > 256:
                cleaned["_capture_limit"] = "[TRUNCATED]"
            return cleaned
        if isinstance(value, (list, tuple)):
            cleaned = [self.redact(v, _depth=_depth + 1) for v in value[:256]]
            if len(value) > 256:
                cleaned.append("[TRUNCATED]")
            return cleaned
        if isinstance(value, str):
            cleaned = self._redact_text(value)
            return cleaned if len(cleaned) <= self.capture_chars else cleaned[:self.capture_chars] + "[TRUNCATED]"
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else None
        return "[UNSUPPORTED]"

    def emit(self, event: str, **fields: Any) -> None:
        self._enqueue(event, fields)

    def capture(self, event: str, payload: Any, **fields: Any) -> None:
        self._enqueue(event, fields, payload=payload)

    def _enqueue(self, event: str, fields: dict, *, payload: Any = None) -> None:
        if not self.enabled or self._closed:
            return
        safe = {}
        for key, value in fields.items():
            if key not in SAFE_FIELDS or not isinstance(value, (str, int, float, bool, type(None))):
                continue
            safe[key] = self._redact_text(value)[:160] if isinstance(value, str) else value
        record = {"schema_version": 1,
                  "timestamp": datetime.now(timezone.utc).isoformat(),
                  "monotonic_ms": round((time.monotonic() - self._started) * 1000, 3),
                  "run_id": self.run_id, "event": re.sub(r"[^a-zA-Z0-9_.-]", "_", event)[:80],
                  **safe}
        if payload is not None and self.capture_enabled:
            cleaned = self.redact(payload)
            serialized = json.dumps(cleaned, ensure_ascii=False)
            if len(serialized) > self.capture_chars or "[TRUNCATED]" in serialized:
                record["payload_truncated"] = True
            else:
                record["payload"] = cleaned
        with self._lock:
            self._sequence += 1
            record["sequence"] = self._sequence
            if self._dropped:
                record["dropped_events"] = self._dropped
            try:
                self._queue.put_nowait(record)
                self._dropped = 0
            except queue.Full:
                self._dropped += 1

    def _write_loop(self) -> None:
        handler = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(self.path, maxBytes=self.max_bytes,
                                          backupCount=self.backups, encoding="utf-8")
            # I/O failure must not print captured message bodies via logging.handleError.
            handler.handleError = lambda record: None
            handler.setFormatter(logging.Formatter("%(message)s"))
        except OSError:
            logging.getLogger(__name__).warning("Interaction log unavailable; check local log directory permissions.")
        while True:
            record = self._queue.get()
            try:
                if record is None:
                    break
                if isinstance(record, threading.Event):
                    if handler:
                        handler.flush()
                    record.set()
                    continue
                if handler:
                    line = json.dumps(record, ensure_ascii=False, allow_nan=False)
                    if len(line.encode("utf-8")) > self.max_bytes:
                        record.pop("payload", None)
                        record["payload_truncated"] = True
                        line = json.dumps(record, ensure_ascii=False, allow_nan=False)
                    handler.emit(logging.LogRecord("interactions", logging.INFO, "", 0, line, (), None))
            except (OSError, ValueError, TypeError):
                with self._lock:
                    self._dropped += 1
            finally:
                self._queue.task_done()
        if handler:
            handler.close()

    def flush(self, timeout: float = 2) -> bool:
        if not self.enabled or self._closed:
            return True
        completed = threading.Event()
        try:
            self._queue.put(completed, timeout=timeout)
        except queue.Full:
            return False
        return completed.wait(timeout)

    def close(self) -> None:
        if self._closed:
            return
        self.flush()
        self._closed = True
        if self._thread:
            try:
                self._queue.put(None, timeout=2)
            except queue.Full:
                return
            self._thread.join(timeout=2)


_observer: JsonlObserver | None = None
_observer_lock = threading.Lock()


def get_observer() -> JsonlObserver:
    global _observer
    with _observer_lock:
        if _observer is None:
            root = Path(__file__).resolve().parents[2]
            directory = Path(os.getenv("VOICE_OBSERVABILITY_DIR", str(root / "logs")))
            _observer = JsonlObserver(
                directory / "interactions.jsonl",
                enabled=os.getenv("VOICE_OBSERVABILITY_ENABLED", "true").lower() in {"1", "true", "yes"},
                capture_enabled=os.getenv("VOICE_DIAGNOSTIC_CAPTURE", "false").lower() in {"1", "true", "yes"},
                max_bytes=_env_int("VOICE_OBSERVABILITY_MAX_BYTES", 5_000_000, 65536, 100_000_000),
                backups=_env_int("VOICE_OBSERVABILITY_BACKUPS", 3, 1, 10),
                capture_chars=_env_int("VOICE_DIAGNOSTIC_MAX_CHARS", 32768, 1024, 131072),
            )
            atexit.register(_observer.close)
        return _observer
