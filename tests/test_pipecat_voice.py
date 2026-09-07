"""Tests require the optional requirements-pipecat.txt environment."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
pytest.importorskip("pipecat")
from pipecat.frames.frames import (
    InterruptionFrame, LLMFullResponseEndFrame, LLMTextFrame, OutputAudioRawFrame)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from src.ai_services.providers.pipecat_voice.config import service_config
from src.ai_services.providers.pipecat_voice.manager import PipecatVoiceManager
from src.ai_services.providers.pipecat_voice.pipeline import ReplyGate, DiscordOutput
from src.ai_services.providers.pipecat_voice.services import vocabulary, make_tts, make_llm


def manager():
    playback = SimpleNamespace(interrupt_audio_stream=Mock(),
        start_new_audio_stream=AsyncMock(), add_audio_chunk=AsyncMock(), end_audio_stream=AsyncMock())
    config = service_config()
    config.update(api_key="test", prewarm=False, observer=Mock(), context_service=SimpleNamespace(
        start=Mock(), close=AsyncMock(), turn_context=lambda uid, name: f"VOICE_CONTEXT\n{uid}:{name}"))
    obj = PipecatVoiceManager(playback, config)
    return obj


@pytest.mark.asyncio
async def test_marker_split_across_tokens_never_reaches_tts(monkeypatch):
    monkeypatch.setattr(FrameProcessor, "process_frame", AsyncMock())
    m = manager()
    m._admitted_audio = True
    gate = ReplyGate(m, 0)
    gate.push_frame = AsyncMock()
    for text in ["<NO", "_REPLY>"]:
        await gate.process_frame(LLMTextFrame(text), FrameDirection.DOWNSTREAM)
    await gate.process_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)
    assert not any(isinstance(c.args[0], LLMTextFrame) for c in gate.push_frame.call_args_list)


@pytest.mark.asyncio
async def test_interrupted_partial_answer_is_not_spoken_in_next_reply(monkeypatch):
    monkeypatch.setattr(FrameProcessor, "process_frame", AsyncMock())
    m = manager()
    m._admitted_audio = True
    gate = ReplyGate(m, 0)
    gate.push_frame = AsyncMock()
    for frame in [LLMTextFrame("旧回答"), InterruptionFrame(), LLMTextFrame("新回答"), LLMFullResponseEndFrame()]:
        await gate.process_frame(frame, FrameDirection.DOWNSTREAM)
    texts = [c.args[0].text for c in gate.push_frame.call_args_list if isinstance(c.args[0], LLMTextFrame)]
    # The first fragment was already streamed; interruption must not replay it.
    assert texts == ["旧回答", "新回答"]


@pytest.mark.asyncio
async def test_first_text_reaches_tts_before_response_end(monkeypatch):
    monkeypatch.setattr(FrameProcessor, "process_frame", AsyncMock())
    m = manager()
    m._admitted_audio = True
    gate = ReplyGate(m, 0)
    gate.push_frame = AsyncMock()
    await gate.process_frame(LLMTextFrame("选"), FrameDirection.DOWNSTREAM)
    assert gate.push_frame.call_args.args[0].text == "选"
    await gate.process_frame(LLMTextFrame("这个。"), FrameDirection.DOWNSTREAM)
    assert gate.push_frame.call_args.args[0].text == "这个。"
    await gate.process_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)
    assert len([c for c in gate.push_frame.call_args_list if isinstance(c.args[0], LLMTextFrame)]) == 2


@pytest.mark.asyncio
async def test_context_snapshot_replacement_and_idle_memory_are_bounded():
    m = manager()
    m._service_config['tts_provider'] = 'gemini'
    await m.connect(AsyncMock(), AsyncMock())
    m._speaker = (1, 'one')
    for i in range(50):
        m.replace_voice_context('VOICE_CONTEXT\n' + str(i))
        m._turn_id = str(i)
        m.remember_question('兔子选哪个？' + str(i))
        m.remember_answer('选帽子。' + str(i))
        m._chat.add_message({'role':'assistant', 'content':'old answer'})
    assert sum(x.get('content','').startswith('VOICE_CONTEXT') for x in m._chat.get_messages()) == 1
    await m.end_conversation(reason='idle')
    messages = m._chat.get_messages()
    assert len(messages) == 1 and messages[0]['content'].startswith('CONVERSATION_MEMORY')
    assert '兔子选哪个？49' in messages[0]['content']
    assert 'old answer' not in messages[0]['content']
    assert len(messages[0]['content']) < 3000
    epoch = m.connection_epoch
    m.replace_voice_context('VOICE_CONTEXT\nnew speaker')
    assert len(m._chat.get_messages()) == 2
    await m.end_conversation(reason='explicit_stop')
    assert m._chat.get_messages() == [] and m._recent_turns == []
    assert m.connection_epoch == epoch
    await m.disconnect()


@pytest.mark.asyncio
async def test_late_audio_after_cancel_and_after_new_speaker_is_rejected():
    m = manager()
    out = DiscordOutput(m, 0)
    audio = OutputAudioRawFrame(b"\0"*960, 24000, 1)
    assert not await out.write_audio_frame(audio)  # No admitted user audio.
    m._admitted_audio = True
    assert await out.write_audio_frame(audio)  # Prove playback worked before cancellation.
    await m.cancel_ongoing_response()
    m._admitted_audio = True  # New user is now speaking, old epoch must stay silent.
    assert not await out.write_audio_frame(audio)
    assert m._audio_playback_manager.add_audio_chunk.await_count == 1


@pytest.mark.asyncio
async def test_context_precedes_audio_and_history_survives_takeover():
    m = manager()
    await m.connect(AsyncMock(), AsyncMock())
    order = []
    async def build(text):
        order.append(text)
        def queued(frame):
            if hasattr(frame, "acknowledged"):
                frame.acknowledged.set()
            else:
                order.append("audio")
        m._worker = SimpleNamespace(queue_frame=AsyncMock(side_effect=queued), cancel=AsyncMock())
        m._run_task = asyncio.create_task(asyncio.sleep(0))
        # A mock task that represents a live pipeline without needing a socket.
        m._run_task.cancel()
        await asyncio.gather(m._run_task, return_exceptions=True)
        m._run_task = Mock(done=lambda: False)
    m._build_pipeline = build
    m._stt = SimpleNamespace(_do_reconnect=AsyncMock(side_effect=lambda: order.append("stt_reset")))
    m._user = SimpleNamespace(reset=AsyncMock(), _user_turn_controller=SimpleNamespace(
        process_frame=AsyncMock(), _trigger_user_turn_stop=AsyncMock(),
        user_turn_strategies=SimpleNamespace(stop=[])))
    # Replace teardown only here: lifecycle invalidation is exercised in the audio test.
    async def stop():
        m.connection_epoch += 1
        m._worker = m._run_task = None
    m._stop_pipeline = stop
    await m.send_turn_context(1, "one")
    await m.send_audio_chunk(b"\0"*640)
    m._chat.add_message({"role": "assistant", "content": "previous answer"})
    await m.send_turn_context(2, "two")
    await m.send_audio_chunk(b"\0"*640)
    assert order == ["VOICE_CONTEXT\n1:one", "audio", "stt_reset", "audio"]
    assert m._chat.get_messages()[-1]["content"] == "VOICE_CONTEXT\n2:two"
    assert any(v.get("content") == "previous answer" for v in m._chat.get_messages())


@pytest.mark.asyncio
async def test_end_input_does_not_cancel_output():
    m = manager()
    m._worker = SimpleNamespace(queue_frame=AsyncMock())
    m._run_task = Mock(done=lambda: False)
    m._admitted_audio = True
    assert await m.finalize_input_and_request_response()
    assert m._admitted_audio
    m._audio_playback_manager.interrupt_audio_stream.assert_not_called()


def test_vocabulary_excludes_ids_and_context_prose():
    import json
    text = "VOICE_CONTEXT\n" + json.dumps({"speaker": {"displayName": "secret"},
        "nameGlossary": {"champions": [{"championId": 254, "nameZh": "蔚",
            "nameEn": "Vi", "aliasesZh": ["皮城执法官"]}],
            "augments": [{"namesByLocale": {"en_US": "Draw Your Sword", "zh_CN": "拔剑"}}]}})
    assert vocabulary(text) == ["蔚", "Vi", "皮城执法官", "Draw Your Sword", "拔剑"]
    assert len(vocabulary(text, 2)) == 2


@pytest.mark.asyncio
async def test_factory_uses_actual_pipecat_google_services():
    import aiohttp
    config = service_config()
    config.update(api_key="test", tts_provider="gemini")
    llm = make_llm(config, "test instructions")
    async with aiohttp.ClientSession() as http:
        tts = make_tts(config, http)
        assert llm.__class__.__name__ == "GoogleLLMService"
        assert tts.__class__.__name__ == "GeminiTTSService"
        config.update(tts_provider="fish", fish_key="", fish_key_file="", tts_voice="")
        with pytest.raises(ValueError, match="FISH_API_KEY"):
            make_tts(config, http)


@pytest.mark.asyncio
async def test_real_framework_pipeline_runs_transcript_to_playback(monkeypatch):
    """Real Pipecat workers, aggregators and output; only external models are fake."""
    from pipecat.frames.frames import (LLMContextFrame, LLMFullResponseStartFrame,
        TranscriptionFrame, VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame,
        TTSStartedFrame, TTSAudioRawFrame, TTSStoppedFrame)
    from pipecat.services.tts_service import TTSService, TextAggregationMode
    from pipecat.services.settings import TTSSettings
    from src.observability import JsonlObserver
    from src.ai_services.providers.pipecat_voice import services
    from src.bot.session.ai_service_coordinator import AIServiceCoordinator
    import pipecat.services.google.gemini_live.stt as stt_module
    class FakeSTT(FrameProcessor):
        Settings = stt_module.GeminiSTTService.Settings
        def __init__(self, **_):
            super().__init__()
            self._do_reconnect = AsyncMock()
        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            await self.push_frame(frame, direction)
    seen = []
    class FakeLLM(FrameProcessor):
        def register_function(self, *_a, **_k):
            pass
        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            if isinstance(frame, LLMContextFrame):
                seen.append([dict(m) for m in frame.context.get_messages()])
                await self.push_frame(LLMFullResponseStartFrame())
                await self.push_frame(LLMTextFrame("听到了。"))
                if len(seen) == 1:
                    # Do not finish the model response until downstream speech
                    # has started: full-response buffering would deadlock here.
                    async with asyncio.timeout(5):
                        while not m._audio_playback_manager.add_audio_chunk.await_count:
                            await asyncio.sleep(.01)
                await self.push_frame(LLMFullResponseEndFrame())
            else:
                await self.push_frame(frame, direction)
    class FakeTTS(TTSService):
        def __init__(self):
            super().__init__(sample_rate=24000, text_aggregation_mode=TextAggregationMode.TOKEN,
                settings=TTSSettings(model="fake", voice="fake"))
        async def run_tts(self, text, context_id):
            yield TTSStartedFrame(context_id=context_id)
            yield TTSAudioRawFrame(b"\1\0"*4800, 24000, 1, context_id=context_id)
            yield TTSStoppedFrame(context_id=context_id)
    monkeypatch.setattr(stt_module, "GeminiSTTService", FakeSTT)
    monkeypatch.setattr(services, "make_llm", lambda *_: FakeLLM())
    monkeypatch.setattr(services, "make_tts", lambda *_: FakeTTS())
    m = manager()
    m.observer = JsonlObserver("unused.jsonl", enabled=False)
    m._service_config.update(tts_provider="fake", turn_silence_seconds=.05)
    state = SimpleNamespace(current_session_id=1, is_active_participant=lambda _uid: True)
    coordinator = AIServiceCoordinator(state, m._audio_playback_manager, {}, 0)
    coordinator.active_ai_service_manager = m
    try:
        await m.connect(AsyncMock(), AsyncMock())
        assert await coordinator.send_audio_stream_chunk(b"\0"*640, 1, "one")
        await m._worker.queue_frame(VADUserStartedSpeakingFrame())
        await m._worker.queue_frame(TranscriptionFrame("你好", "1", "2026-09-06T00:00:00Z"))
        await m._worker.queue_frame(VADUserStoppedSpeakingFrame())
        async with asyncio.timeout(8):
            while not m._audio_playback_manager.add_audio_chunk.await_count:
                await asyncio.sleep(.05)
        assert m._audio_playback_manager.start_new_audio_stream.await_count == 1
        assert any(v.get("content") == "你好" for v in m._chat.get_messages())
        assert seen[0][-2]["content"] == "VOICE_CONTEXT\n1:one"
        assert seen[0][-1]["content"] == "你好"
        async with asyncio.timeout(8):
            while not m._audio_playback_manager.end_audio_stream.await_count:
                await asyncio.sleep(.05)
        async with asyncio.timeout(5):
            while not any(v.get('role') == 'assistant' for v in m._chat.get_messages()):
                await asyncio.sleep(.01)
        await m._worker.queue_frame(VADUserStartedSpeakingFrame())
        await m._worker.queue_frame(TranscriptionFrame("第二句呢", "1", "2026-09-06T00:00:03Z"))
        await m._worker.queue_frame(VADUserStoppedSpeakingFrame())
        async with asyncio.timeout(8):
            while len(seen) < 2:
                await asyncio.sleep(.05)
        assert seen[1][-2]["content"] == "VOICE_CONTEXT\n1:one"
        assert seen[1][-1]["content"] == "第二句呢"
        assert any(m.get("role") == "assistant" for m in seen[1])
        await m.cancel_ongoing_response()
        original_worker = m._worker
        assert original_worker is not None
        cancelled_epoch = m.connection_epoch
        assert await coordinator.send_audio_stream_chunk(b"\0"*640, 2, "two")
        assert m.connection_epoch == cancelled_epoch
        assert m._speaker == (2, "two")
        assert m._worker is original_worker
        await m._worker.queue_frame(VADUserStartedSpeakingFrame())
        await m._worker.queue_frame(TranscriptionFrame("换人后第一句", "2", "2026-09-06T00:00:06Z"))
        await m._worker.queue_frame(VADUserStoppedSpeakingFrame())
        async with asyncio.timeout(8):
            while len(seen) < 3:
                await asyncio.sleep(.05)
        assert seen[2][-1]["content"] == "换人后第一句"
        assert seen[2][-2]["content"] == "VOICE_CONTEXT\n2:two"
        async with asyncio.timeout(8):
            while m._audio_playback_manager.end_audio_stream.await_count < 2:
                await asyncio.sleep(.05)
        # A completed same-speaker turn reuses STT too, with no cold start.
        m._stt_pending = False
        m._stt._do_reconnect.reset_mock()
        await coordinator.cancel_ongoing_response()
        assert await coordinator.send_audio_stream_chunk(b"\0"*640, 2, "two")
        assert m._worker is original_worker
        m._stt._do_reconnect.assert_not_awaited()
        await m._worker.queue_frame(TranscriptionFrame("再次唤醒", "2", "", finalized=True))
        async with asyncio.timeout(8):
            while len(seen) < 4:
                await asyncio.sleep(.05)
        assert seen[3][-1]["content"] == "再次唤醒"
        assert seen[3][-2]["content"] == "VOICE_CONTEXT\n2:two"
        async with asyncio.timeout(8):
            while m._audio_playback_manager.end_audio_stream.await_count < 3:
                await asyncio.sleep(.05)
        await coordinator.end_conversation(reason="idle")
        assert m._worker is original_worker
        assert len(m._chat.get_messages()) == 1
        assert m._chat.get_messages()[0]['content'].startswith('CONVERSATION_MEMORY')
        assert await coordinator.send_audio_stream_chunk(b"\0"*640, 2, "two")
        await m._worker.queue_frame(TranscriptionFrame("那另一个呢", "2", "", finalized=True))
        async with asyncio.timeout(8):
            while len(seen) < 5:
                await asyncio.sleep(.05)
        assert seen[4][-1]['content'] == '那另一个呢'
        assert seen[4][-2]['content'] == 'VOICE_CONTEXT\n2:two'
        assert seen[4][0]['content'].startswith('CONVERSATION_MEMORY')
        await coordinator.end_conversation(reason="explicit_stop")
        assert m._worker is original_worker and m._chat.get_messages() == []
    finally:
        await m.disconnect()


@pytest.mark.asyncio
async def test_tool_result_from_cancelled_session_is_not_returned():
    m = manager()
    async def execute(*_):
        await m.cancel_ongoing_response()
        return '{"old": true}'
    m._tools = SimpleNamespace(execute=execute)
    params = SimpleNamespace(function_name="get_game_context", arguments={}, result_callback=AsyncMock())
    await m._tool_call(params)
    params.result_callback.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("lines,expected,error", [
    ([b'{"code":0,"data":"AQI="}\n', b'{"code":20000000}\n'], b'\x01\x02', None),
    ([b'{"code":45000030,"message":"private echo"}\n'], None, "Doubao API code 45000030"),
    ([b'{"code":20000000}\n'], None, "Doubao returned no audio"),
])
async def test_doubao_stream_and_safe_errors(lines, expected, error):
    from src.ai_services.providers.pipecat_voice.doubao import pcm_chunks
    async def content():
        for line in lines:
            yield line
    response = SimpleNamespace(status=200, content=content())
    ctx = AsyncMock()
    ctx.__aenter__.return_value = response
    session = SimpleNamespace(post=Mock(return_value=ctx))
    async def collect():
        return b''.join([c async for c in pcm_chunks(session, key="test", text="你好", voice="test")])
    if error:
        with pytest.raises(RuntimeError) as exc:
            await collect()
        assert str(exc.value) == error
    else:
        assert await collect() == expected
    ctx.__aexit__.assert_awaited_once()


@pytest.mark.asyncio
async def test_doubao_cancellation_closes_response():
    from src.ai_services.providers.pipecat_voice.doubao import pcm_chunks
    entered = asyncio.Event()
    async def content():
        entered.set()
        await asyncio.Event().wait()
        yield b''
    ctx = AsyncMock()
    ctx.__aenter__.return_value = SimpleNamespace(status=200, content=content())
    session = SimpleNamespace(post=Mock(return_value=ctx))
    async def consume():
        async for _ in pcm_chunks(session, key="test", text="test", voice="test"):
            pass
    task = asyncio.create_task(consume())
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    ctx.__aexit__.assert_awaited_once()


@pytest.mark.asyncio
async def test_fish_manbo_factory_reads_external_key_and_voice(tmp_path, monkeypatch):
    import pipecat.services.fish.tts as fish_module
    key_file = tmp_path / "fish.txt"
    key_file.write_text("synthetic-test-key\n", encoding="utf-8")
    factory = Mock()
    factory.Settings = fish_module.FishAudioTTSService.Settings
    monkeypatch.setattr(fish_module, "FishAudioTTSService", factory)
    config = service_config()
    config.update(tts_provider="fish", tts_voice="", tts_model="",
                  fish_key="", fish_key_file=str(key_file))
    make_tts(config, None)
    kwargs = factory.call_args.kwargs
    assert kwargs["api_key"] == "synthetic-test-key"
    assert kwargs["settings"].voice == "0f08cacd3e354471a4b94dd00b4cc4a3"
    assert kwargs["sample_rate"] == 24000
    assert kwargs["settings"].model == "s2.1-pro-free"


@pytest.mark.asyncio
async def test_playout_clock_absorbs_scheduler_jitter_without_accumulating(monkeypatch):
    import src.ai_services.providers.pipecat_voice.pipeline as module
    clock = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    async def oversleep(delay):
        if delay > 0:
            clock[0] += delay + .011  # Windows scheduling delay on every sleep.
    monkeypatch.setattr(module.asyncio, "sleep", oversleep)
    m = manager()
    m._admitted_audio = True
    out = DiscordOutput(m, 0)
    for _ in range(250):  # Five seconds of PCM.
        await out.write_audio_frame(OutputAudioRawFrame(b'\0'*960, 24000, 1))
    assert 4.87 < clock[0]-100 < 4.91
    assert m._audio_playback_manager.add_audio_chunk.await_count == 250


@pytest.mark.asyncio
async def test_prewarm_is_muted_and_finishes_before_connect_callback():
    m = manager()
    m._service_config.update(prewarm=True, tts_provider="gemini")
    order = []
    async def build(text):
        order.append("ready")
        assert not m._admitted_audio and m._speaker is None
    async def connected():
        order.append("connected")
    m._build_pipeline = build
    await m.connect(connected, AsyncMock())
    assert order == ["ready", "connected"]
    assert not m._admitted_audio
    await m.disconnect()


@pytest.mark.asyncio
async def test_lifetime_retires_idle_pipeline_but_defers_active_turn():
    m = manager()
    m._service_config["connection_lifetime_seconds"] = .01
    m._connected = True
    m._admitted_audio = True
    m._response_active = True
    retirement = asyncio.create_task(m._retire_when_idle(0))
    await asyncio.sleep(.05)
    assert m.connection_epoch == 0
    m._admitted_audio = False
    await asyncio.wait_for(retirement, 2)
    assert m.connection_epoch == 1


@pytest.mark.asyncio
async def test_lifetime_rewarms_in_background():
    m = manager()
    m._service_config.update(connection_lifetime_seconds=.01, prewarm=True)
    m._connected = True
    m._build_pipeline = AsyncMock()
    await m._retire_when_idle(0)
    m._build_pipeline.assert_awaited_once()
    assert not m._admitted_audio


@pytest.mark.asyncio
async def test_vocabulary_refresh_reconnects_only_stt_and_failed_reset_blocks_input_context():
    import json
    from pipecat.services.google.gemini_live.stt import GeminiSTTService
    m = manager()
    m._service_config["tts_provider"] = "gemini"
    await m.connect(AsyncMock(), AsyncMock())
    m._worker = original = SimpleNamespace(queue_frame=AsyncMock())
    m._run_task = Mock(done=lambda: False)
    m._user = SimpleNamespace(reset=AsyncMock(), _user_turn_controller=SimpleNamespace(
        process_frame=AsyncMock(), _trigger_user_turn_stop=AsyncMock(),
        user_turn_strategies=SimpleNamespace(stop=[])))
    m._stt = SimpleNamespace(Settings=GeminiSTTService.Settings,
        _settings=GeminiSTTService.Settings(adaptation_phrases=[]),
        _do_reconnect=AsyncMock(side_effect=RuntimeError("test transport failure")))
    m._context.turn_context = lambda *_: "VOICE_CONTEXT\n" + json.dumps({
        "nameGlossary": {"champions": [{"nameZh": "蔚", "nameEn": "Vi"}]}})
    assert not await m.send_turn_context(1, "one")
    assert not m._admitted_audio
    assert m._stt_needs_reset
    m._stt._do_reconnect.side_effect = None
    assert await m.send_turn_context(1, "one")
    assert "蔚" in m._stt._settings.adaptation_phrases
    assert await m.send_turn_context(1, "one")
    assert m._stt._do_reconnect.await_count == 2  # Failure + retry, no third reconnect.
    assert m._worker is original and m.connection_epoch == 0
    m._worker = m._run_task = None
    await m.disconnect()


@pytest.mark.asyncio
async def test_late_transcript_while_muted_never_reaches_aggregator(monkeypatch):
    from src.ai_services.providers.pipecat_voice.pipeline import TraceFrames
    from pipecat.frames.frames import TranscriptionFrame
    monkeypatch.setattr(FrameProcessor, "process_frame", AsyncMock())
    m = manager()
    trace = TraceFrames(m, 0, metrics=False)
    trace.push_frame = AsyncMock()
    await trace.process_frame(TranscriptionFrame("旧语音", "1", "", finalized=True), FrameDirection.DOWNSTREAM)
    trace.push_frame.assert_not_awaited()


@pytest.mark.asyncio
async def test_old_tts_context_stays_muted_after_new_answer_starts(monkeypatch):
    from pipecat.frames.frames import TTSStartedFrame, TTSAudioRawFrame
    from pipecat.transports.base_output import BaseOutputTransport
    monkeypatch.setattr(BaseOutputTransport, "process_frame", AsyncMock())
    m = manager()
    m._admitted_audio = True
    output = DiscordOutput(m, 0)
    await output.process_frame(TTSStartedFrame(context_id="old"), FrameDirection.DOWNSTREAM)
    old = TTSAudioRawFrame(b"\0"*960, 24000, 1, context_id="old")
    assert await output.write_audio_frame(old)
    await m.cancel_ongoing_response()
    await output.process_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM)
    m._admitted_audio = True
    await output.process_frame(TTSStartedFrame(context_id="new"), FrameDirection.DOWNSTREAM)
    assert not await output.write_audio_frame(old)
    assert await output.write_audio_frame(TTSAudioRawFrame(b"\0"*960, 24000, 1, context_id="new"))
    assert m._audio_playback_manager.add_audio_chunk.await_count == 2
