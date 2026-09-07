"""Doubao v3 HTTP chunked adapter to Pipecat's existing TTSService contract.

Protocol reference: ByteDance agentkit-samples byted-text-to-speech and
https://www.volcengine.com/docs/6561/1598757 . No vendor SDK is required.
"""
import base64
import json
import uuid
from pathlib import Path

import aiohttp
from pipecat.frames.frames import TTSStartedFrame, TTSStoppedFrame, TTSAudioRawFrame, ErrorFrame
from pipecat.services.tts_service import TTSService
from pipecat.services.settings import TTSSettings


def read_api_key(config):
    key = config.get("doubao_key", "").strip()
    if not key and config.get("doubao_key_file"):
        key = Path(config["doubao_key_file"]).read_text(encoding="utf-8-sig").strip()
    if not key or "\n" in key or "\r" in key:
        raise ValueError("Provide DOUBAO_API_KEY or a single-key DOUBAO_API_KEY_FILE")
    return key


async def pcm_chunks(session, *, key, text, voice, resource="seed-tts-2.0", sample_rate=24000):
    """Yield PCM incrementally; cancellation closes the HTTP response immediately."""
    headers = {"X-Api-Key": key, "X-Api-Resource-Id": resource,
        "X-Api-Request-Id": str(uuid.uuid4()), "Content-Type": "application/json"}
    body = {"user": {"uid": "voicecord"}, "req_params": {
        "text": text, "speaker": voice, "sample_rate": sample_rate,
        "audio_params": {"format": "pcm", "sample_rate": sample_rate}}}
    timeout = aiohttp.ClientTimeout(total=60, connect=15, sock_read=20)
    async with session.post("https://openspeech.bytedance.com/api/v3/tts/unidirectional",
                            headers=headers, json=body, timeout=timeout) as response:
        if response.status != 200:
            # Never include headers, keys or echoed request bodies in errors.
            raise RuntimeError(f"Doubao HTTP {response.status}")
        received = False
        async for raw_line in response.content:
            line = raw_line.decode("utf-8").strip()
            if not line:
                continue
            event = json.loads(line[5:].strip() if line.startswith("data:") else line)
            code = event.get("code", 0)
            if code not in (0, 20000000):
                raise RuntimeError(f"Doubao API code {code}")
            if event.get("data"):
                data = base64.b64decode(event["data"], validate=True)
                received = True
                yield data
        if not received:
            raise RuntimeError("Doubao returned no audio")


class DoubaoTTSService(TTSService):
    def __init__(self, config, session):
        self.key = read_api_key(config)
        self.session = session
        self.resource = config.get("tts_model") or "seed-tts-2.0"
        self.voice = config.get("tts_voice") or "zh_female_vv_uranus_bigtts"
        super().__init__(sample_rate=24000,
            settings=TTSSettings(model=self.resource, voice=self.voice, language=None))

    async def run_tts(self, text, context_id):
        await self.start_ttfb_metrics()
        yield TTSStartedFrame(context_id=context_id)
        first = True
        try:
            async for chunk in pcm_chunks(self.session, key=self.key, text=text,
                    voice=self.voice, resource=self.resource, sample_rate=self.sample_rate):
                if first:
                    await self.stop_ttfb_metrics()
                    first = False
                yield TTSAudioRawFrame(chunk, self.sample_rate, 1, context_id=context_id)
            await self.start_tts_usage_metrics(text)
        except Exception as exc:
            yield ErrorFrame(error=f"Doubao synthesis failed ({type(exc).__name__})")
        finally:
            await self.stop_ttfb_metrics()
        yield TTSStoppedFrame(context_id=context_id)
