import json
import pytest
from src.observability import JsonlObserver, fingerprint

@pytest.fixture
def observer(tmp_path):
    # Full tools appear twice in session.configured; current schema exceeds 32 KB.
    # Retention/truncation behavior is tested separately with an explicit small cap.
    instance = JsonlObserver(tmp_path / "interactions.jsonl", capture_enabled=True,
                            capture_chars=131072)
    yield instance
    instance.close()


def records(observer):
    assert observer.flush()
    return [json.loads(line) for line in observer.path.read_text(encoding="utf-8").splitlines()]


def test_default_metadata_has_no_transcripts_queries_payloads_or_identity(tmp_path):
    observer = JsonlObserver(tmp_path / "events.jsonl")
    try:
        observer.capture("tool.completed", {"text": "private transcript", "query": "private query"},
                         function_name="search_web", status="not_found", reason="no_results",
                         display_name="Alice", user_id=123456789012345678, audio=b"raw audio",
                         arguments="private query", response_id="r1", call_id="c1")
        row = records(observer)[0]
        assert row["event"] == "tool.completed"
        assert row["function_name"] == "search_web"
        assert row["status"] == "not_found"
        raw = observer.path.read_text()
        for forbidden in ["payload", "private", "Alice", "123456789012345678", "raw audio", "arguments"]:
            assert forbidden not in raw
    finally:
        observer.close()


def test_capture_redacts_known_secrets_tokens_ids_and_keeps_session_audio_config(observer):
    observer.register_secrets("a-custom-secret", "Alice")
    observer.capture("session.configured", {
        "text": "Alice said a-custom-secret sk-abcdefghijklmnop Bearer xxx password=pw123 user a@b.com 123456789012345678",
        "api_key": "plain-secret", "nested": {"authorization": "private"},
        "audio": {"input": {"format": {"type": "audio/pcm", "rate": 24000}}},
        "raw_audio": "raw audio", "pcm": b"bytes",
    })
    payload = records(observer)[0]["payload"]
    assert payload["audio"]["input"]["format"]["rate"] == 24000
    assert payload["api_key"] == "[REDACTED]"
    raw = json.dumps(payload)
    for forbidden in ["Alice", "a-custom-secret", "sk-abcdefghijklmnop", "xxx", "pw123", "a@b.com", "123456789012345678", "plain-secret", "private", "raw audio"]:
        assert forbidden not in raw


@pytest.mark.parametrize("payload", [{"text": "x" * 3000}, {"items": list(range(400))}])
def test_incomplete_payload_is_explicit_and_never_silently_replayable(tmp_path, payload):
    observer = JsonlObserver(tmp_path / "events.jsonl", capture_enabled=True, capture_chars=1024)
    try:
        observer.capture("transcript.user", payload)
        row = records(observer)[0]
        assert row["payload_truncated"] is True
        assert "payload" not in row
    finally:
        observer.close()


def test_rotation_bounds_retention_and_records_remain_json(tmp_path):
    observer = JsonlObserver(tmp_path / "events.jsonl", max_bytes=2048, backups=2)
    try:
        for number in range(60):
            observer.emit("response.completed", call_id=f"c{number}", status="ok")
        assert observer.flush()
        files = list(tmp_path.glob("events.jsonl*"))
        assert len(files) == 3
        for path in files:
            assert path.stat().st_size <= 2048
            for line in path.read_text().splitlines():
                assert json.loads(line)["schema_version"] == 1
    finally:
        observer.close()


def test_pseudonyms_are_stable_only_inside_one_run(tmp_path):
    left = JsonlObserver(tmp_path / "left", enabled=False)
    right = JsonlObserver(tmp_path / "right", enabled=False)
    assert left.pseudonym(42) == left.pseudonym(42)
    assert left.pseudonym(42) != right.pseudonym(42)
    assert fingerprint({"a": 1, "b": 2}) == fingerprint({"b": 2, "a": 1})
