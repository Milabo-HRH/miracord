"""Run one short, paid OpenAI Realtime check without Discord or audio devices."""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import time

from src.ai_services.providers.openai.config import OPENAI_SERVICE_CONFIG
from src.ai_services.providers.openai.manager import OpenAIRealtimeManager


class ProbePlayback:
    """Count returned PCM without playing it or saving recordings."""

    def __init__(self) -> None:
        self.audio_bytes = 0
        self.first_audio_at = None
        self.pcm = bytearray()

    async def start_new_audio_stream(self, *_args) -> None:
        pass

    async def add_audio_chunk(self, audio: bytes) -> None:
        if self.first_audio_at is None:
            self.first_audio_at = time.monotonic()
        self.audio_bytes += len(audio)
        self.pcm.extend(audio)

    async def end_audio_stream(self, *_args, **_kwargs) -> None:
        pass

    def get_played_ms(self, _stream_id: str) -> int:
        return 0


async def run(audio_turns: int = 0) -> bool:
    config = copy.deepcopy(OPENAI_SERVICE_CONFIG)
    if not config.get("api_key"):
        print(json.dumps({"ok": False, "reason": "key_missing"}))
        return False
    config.update(league_context_enabled=False, opgg_prefetch_enabled=False,
                  connection_timeout=15)
    # Reasoning shares the output budget with speech, even for a short answer.
    config["session_config"]["max_output_tokens"] = (
        2048 if config["session_config"].get("reasoning") else 128
    )
    config["session_config"]["instructions"] = (
        "This is an audio connection test. For each spoken input, "
        "say exactly one English word: Ready. Do not call tools."
    )
    playback = ProbePlayback()
    manager = OpenAIRealtimeManager(playback, config)
    done = asyncio.Event()
    result = {
        "ok": False, "model": config["model_name"],
        "reasoning": config["session_config"].get("reasoning"),
    }
    original_dispatch = manager._dispatch_event

    async def dispatch(event: dict) -> None:
        await original_dispatch(event)
        if event.get("type") == "session.updated":
            accepted = event.get("session") or {}
            result["accepted_model"] = accepted.get("model")
            result["accepted_reasoning"] = accepted.get("reasoning")
        if event.get("type") == "error":
            result["error_code"] = (event.get("error") or {}).get("code", "unknown")
            done.set()
        if event.get("type") == "response.done":
            response = event.get("response") or {}
            result.setdefault("response_events", []).append({
                "id": response.get("id"), "status": response.get("status"),
                "reason": (response.get("status_details") or {}).get("reason"),
            })
            if response.get("status") == "cancelled":
                # Server VAD may cancel an intermediate reply as input arrives.
                # The probe must wait for the completed reply to the full input.
                return
            result["response_status"] = response.get("status")
            details = response.get("status_details") or {}
            result["status_reason"] = details.get("reason")
            result["error_code"] = (details.get("error") or {}).get("code")
            done.set()

    async def noop() -> None:
        pass

    manager._dispatch_event = dispatch
    started = time.monotonic()
    try:
        if not await manager.connect(noop, noop):
            result["reason"] = "connection_or_configuration_failed"
            return False
        result["session_ready"] = True
        await manager._send({"type": "conversation.item.create", "item": {
            "type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "Audio connection test. Say only: Ready."}
            ],
        }})
        await manager._send({"type": "response.create", "response": {
            "tool_choice": "none", "instructions": "This is an audio connection test. Say exactly one English word: Ready."
        }})
        await asyncio.wait_for(done.wait(), timeout=20)
        result["audio_bytes"] = playback.audio_bytes
        result["ok"] = playback.audio_bytes > 0 and result.get("response_status") == "completed"
        seed_audio = bytes(playback.pcm)
        result["audio_turns"] = []
        for index in range(audio_turns):
            if not result["ok"]:
                break
            done.clear()
            playback.pcm.clear()
            await manager.send_turn_context(0, "Synthetic test speaker", streaming=True)
            rate, channels = manager.processing_audio_format
            chunk_bytes = rate * channels * 2 // 10
            for offset in range(0, len(seed_audio), chunk_bytes):
                await manager.send_audio_chunk(seed_audio[offset:offset + chunk_bytes])
                await asyncio.sleep(0.1)
            await manager.finalize_input_and_request_response()
            await asyncio.wait_for(done.wait(), timeout=20)
            result["ok"] = (
                bool(playback.pcm) and result.get("response_status") == "completed"
                and not result.get("error_code")
            )
            result["audio_turns"].append({
                "turn": index + 1, "audio_bytes": len(playback.pcm), "ok": result["ok"]
            })
        return result["ok"]
    except asyncio.TimeoutError:
        result["reason"] = "response_timeout"
        return False
    except Exception as error:  # noqa: BLE001 - never expose credentials in errors
        result["reason"] = type(error).__name__
        return False
    finally:
        await manager.disconnect()
        result["elapsed_seconds"] = round(time.monotonic() - started, 2)
        print(json.dumps(result), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio-turns", type=int, choices=range(4), default=0)
    raise SystemExit(0 if asyncio.run(run(parser.parse_args().audio_turns)) else 1)
