"""Opt-in live provider smoke test. Saves WAV/trace locally; never joins Discord."""
import argparse
import asyncio
import audioop
import json
import os
from pathlib import Path
import time
import wave


def save_wav(path, pcm, rate=24000):
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)


async def run(args):
    import aiohttp
    from src.ai_services.providers.pipecat_voice.config import service_config
    from src.ai_services.providers.pipecat_voice.doubao import pcm_chunks, read_api_key
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    config = service_config()
    input_voice = "zh_female_tianmeitaozi_uranus_bigtts"
    if args.tts_provider:
        config.update(tts_provider=args.tts_provider, tts_model="", tts_voice="")
    if args.tts_voice:
        config["tts_voice"] = args.tts_voice
    if args.fish_key_file:
        config["fish_key_file"] = args.fish_key_file
    # Synthetic speech only. Never capture a device, join Discord, fetch a live
    # match, or expose game tools in this smoke test.
    config.update(league_context_enabled=False, opgg_prefetch_enabled=False,
                  league_tools_enabled=False)
    key = read_api_key(config)
    started, chunks, first = time.monotonic(), [], None
    if args.input_wav:
        with wave.open(args.input_wav, "rb") as wav:
            if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, 24000):
                raise ValueError("Expected mono PCM16 at 24 kHz")
            pcm = wav.readframes(wav.getnframes())
    else:
        async with aiohttp.ClientSession() as session:
            async for chunk in pcm_chunks(session, key=key, text=args.text,
                    voice=input_voice):
                if first is None:
                    first = round((time.monotonic()-started)*1000)
                chunks.append(chunk)
        pcm = b"".join(chunks)
    save_wav(output / "input.wav", pcm)
    report = {"tts_first_audio_ms": first, "tts_total_ms": round((time.monotonic()-started)*1000),
        "input_seconds": len(pcm)/48000, "text": args.text}
    if not args.tts_only:
        from src.ai_services.providers.pipecat_voice.manager import PipecatVoiceManager
        class Playback:
            def __init__(self):
                self.data = bytearray()
                self.done = asyncio.Event()
                self.first_at = None
            def interrupt_audio_stream(self):
                pass
            async def start_new_audio_stream(self, *_):
                if self.first_at is None:
                    self.first_at = time.monotonic()
            async def add_audio_chunk(self, chunk):
                self.data.extend(chunk)
            async def end_audio_stream(self, *_):
                if self.data:
                    self.done.set()
        async def noop():
            pass
        playback = Playback()
        manager = PipecatVoiceManager(playback, config)
        try:
            connected_at = time.monotonic()
            await manager.connect(noop, noop)
            report["prewarm_ms"] = round((time.monotonic()-connected_at)*1000)
            first_worker = manager._worker
            audio, _ = audioop.ratecv(pcm, 2, 1, 24000, 16000, None)
            speech_bytes = len(audio)
            audio += b"\0"*64000
            report["rounds"] = []
            for turn in range(args.rounds):
                if turn:
                    if args.compact_between:
                        await manager.end_conversation(reason=args.compact_between)
                    else:
                        await manager.cancel_ongoing_response()
                    await asyncio.sleep(args.idle_seconds)
                playback.data.clear()
                playback.done.clear()
                playback.first_at = None
                speaker = 1 if args.switch_speaker_last and turn == args.rounds-1 else 0
                admission_at = time.monotonic()
                if not await manager.send_turn_context(speaker, f"synthetic-speaker-{speaker}", streaming=True):
                    raise RuntimeError("Pipeline rejected context")
                context_ms = round((time.monotonic()-admission_at)*1000)
                speech_end = None
                for offset in range(0, len(audio), 640):
                    if not await manager.send_audio_chunk(audio[offset:offset+640]):
                        raise RuntimeError("Pipeline rejected audio")
                    await asyncio.sleep(.02)
                    if speech_end is None and offset+640 >= speech_bytes:
                        speech_end = time.monotonic()
                await manager.finalize_input_and_request_response()
                await asyncio.wait_for(playback.done.wait(), 60)
                report["reply_audio_seconds"] = len(playback.data)/48000
                report["speech_end_to_first_audio_ms"] = round((playback.first_at-speech_end)*1000)
                report["rounds"].append({"turn": turn+1, "speaker": speaker,
                    "context_ms": context_ms, "pipeline_reused": manager._worker is first_worker,
                    "context_messages": len(manager._chat.get_messages()),
                    "speech_end_to_first_audio_ms": report["speech_end_to_first_audio_ms"],
                    "reply_audio_seconds": report["reply_audio_seconds"]})
                save_wav(output / ("reply.wav" if turn == 0 else f"reply-{turn+1}.wav"), playback.data)
            report["pipeline"] = f'{config["stt_model"]} -> {config["llm_model"]} -> {config["tts_provider"]}'
            report["voice"] = config["tts_voice"]
        finally:
            await manager.disconnect()
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--tts-only", action="store_true", help="Only generate the Doubao input fixture")
    parser.add_argument("--tts-provider", choices=["doubao", "fish", "gemini", "minimax"])
    parser.add_argument("--tts-voice")
    parser.add_argument("--fish-key-file")
    parser.add_argument("--input-wav", help="Reuse a previously generated synthetic 24 kHz WAV")
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--idle-seconds", type=float, default=0)
    parser.add_argument("--switch-speaker-last", action="store_true")
    parser.add_argument("--compact-between", choices=["idle", "explicit_stop"])
    parser.add_argument("--text", default="豆包，你好，能听到我说话吗？")
    parser.add_argument("--output", default="logs/pipecat/smoke")
    args = parser.parse_args()
    os.environ["VOICE_OBSERVABILITY_DIR"] = str(Path(args.output) / "trace")
    os.environ["VOICE_DIAGNOSTIC_CAPTURE"] = "true"
    try:
        asyncio.run(run(args))
    except Exception as exc:
        # Never print client objects or credential-bearing exception details.
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__,
            "detail": str(exc) if str(exc).startswith("Doubao ") else "See sanitized trace"}))
        raise SystemExit(1)
