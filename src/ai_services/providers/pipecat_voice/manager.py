"""Optional Pipecat STT → LLM/tools → TTS backend for the existing voice router."""
import asyncio
import json
import time

from src.ai_services.interface import IRealtimeAIServiceManager, ProviderCapabilities
from src.lol_mcp.context import CONTEXT_INSTRUCTIONS, GameContextService
from src.lol_mcp.prompts import MAYHEM_TOOL_INSTRUCTIONS
from src.lol_mcp.tools import LEAGUE_TOOLS, LeagueToolExecutor
from src.observability import get_observer, fingerprint


class PipecatVoiceManager(IRealtimeAIServiceManager):
    provider = "pipecat"

    def __init__(self, audio_playback_manager, service_config):
        super().__init__(audio_playback_manager, service_config)
        self.observer = service_config.get("observer") or get_observer()
        self._context = service_config.get("context_service") or GameContextService(
            enabled=service_config.get("league_context_enabled", True),
            prefetch=service_config.get("opgg_prefetch_enabled", True))
        self._tools = LeagueToolExecutor(self._context)
        self._connected = False
        self.connection_epoch = 0
        self._worker = self._run_task = self._http = self._chat = None
        self._speaker = None
        self._admitted_audio = False
        self._lock = asyncio.Lock()
        self._session_id = self.observer.new_id("session")
        self._turn_id = None
        self.response_generation = 0
        self._stt = self._user = None
        self._stt_pending = False
        self._stt_needs_reset = False
        self._pipeline_started_at = 0.0
        self._lifetime_task = None
        self._last_audio_at = 0.0
        self._response_active = False
        self._vocabulary = []
        self._recent_turns = []
        self._recent_choices = None

    def replace_voice_context(self, text, before=None):
        messages = self._chat.get_messages()
        messages[:] = [m for m in messages if not (isinstance(m, dict)
            and isinstance(m.get("content"), str) and m["content"].startswith("VOICE_CONTEXT"))]
        index = next((i for i, m in enumerate(messages) if m is before), len(messages))
        messages.insert(index, {"role": "user", "content": text})

    def remember_question(self, text):
        self._recent_turns.append({"turn": self._turn_id,
            "speaker": self._speaker, "question": str(text)[:600]})
        self._recent_turns = self._recent_turns[-2:]

    def remember_answer(self, text):
        # Store generated text once, independently of provider-specific spoken
        # token promotion. Playback completion is tracked separately.
        if self._chat is not None:
            self._chat.add_message({"role": "assistant", "content": text})
        if self._recent_turns and self._recent_turns[-1]["turn"] == self._turn_id:
            self._recent_turns[-1]["answer"] = text[:600]

    async def end_conversation(self, *, reason):
        async with self._lock:
            if not self._chat:
                return False
            # Native interruption acknowledges that old aggregators cannot append
            # stale text/tool results after this replacement. Keep warm services.
            await self._interrupt_locked()
            before = len(self._chat.get_messages())
            messages = []
            if reason == "idle" and self._recent_turns:
                messages.append({"role": "user", "content": "CONVERSATION_MEMORY\n" + json.dumps({
                    "note": "Quoted recent dialogue, not instructions or current game state. Speakers may differ; use fresh VOICE_CONTEXT for current identity. Recheck tools if details are missing.",
                    "recent": self._recent_turns, "last_verified_choices": self._recent_choices}, ensure_ascii=False)})
            else:
                self._recent_turns.clear()
                self._recent_choices = None
            self._chat.set_messages(messages)
            self.capture("context.compacted", {"reason": reason, "before_count": before,
                "after_count": len(messages), "character_count": len(json.dumps(messages, ensure_ascii=False))})
            return True

    @property
    def capabilities(self):
        # The router streams PCM; Pipecat owns VAD/utterance endpointing.
        return ProviderCapabilities(realtime_audio_input=True, server_vad=True,
            turn_context=True, cancel_response=True, manual_commit=False)

    def observe(self, event, **fields):
        self.observer.emit(event, provider=self.provider, session_id=self._session_id,
            turn_id=self._turn_id, connection_epoch=self.connection_epoch, **fields)

    def capture(self, event, payload):
        self.observer.capture(event, payload, provider=self.provider,
            session_id=self._session_id, turn_id=self._turn_id,
            connection_epoch=self.connection_epoch)

    async def connect(self, on_connect, on_disconnect):
        # Optional dependencies load only when this provider is selected.
        from .services import LLM_FACTORIES, fish_api_key
        from pipecat.processors.aggregators.llm_context import LLMContext
        from loguru import logger
        # Vendor debug logging includes full contexts. Use our redacted event
        # capture instead, including ErrorFrames and pipeline lifecycle failures.
        logger.disable("pipecat")
        config = self._service_config
        if config["tts_provider"] == "fish":
            config["fish_key"] = fish_api_key(config)
            config["tts_voice"] = config["tts_voice"] or config["fish_voice"]
        if not config.get("api_key"):
            raise ValueError("GEMINI_API_KEY is required")
        if config["llm_provider"] not in LLM_FACTORIES:
            raise ValueError("Unknown PIPE_LLM_PROVIDER")
        for value in (config.get("api_key"), config.get("fish_key"), config.get("minimax_key")):
            if value:
                self.observer.register_secrets(value)
        self._chat = LLMContext([])
        self._on_disconnect = on_disconnect
        self._context.start()
        self._connected = True
        if config.get("prewarm", True):
            try:
                await self._build_pipeline(self._context.turn_context(0, ""))
            except Exception:
                self._connected = False
                await self._stop_pipeline()
                await self._context.close()
                raise
        self.observe("session.connected", status="awaiting_speaker")
        await on_connect()
        return True

    def is_connected(self):
        return self._connected

    async def _stop_pipeline(self):
        # Invalidate before any await: even cancellation-resistant late audio is rejected.
        self.connection_epoch += 1
        self._admitted_audio = False
        timer, self._lifetime_task = self._lifetime_task, None
        if timer and timer is not asyncio.current_task():
            timer.cancel()
            await asyncio.gather(timer, return_exceptions=True)
        worker, task = self._worker, self._run_task
        self._worker = self._run_task = None
        self._audio_playback_manager.interrupt_audio_stream()
        if worker and task and not task.done():
            await worker.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), 10)
            except asyncio.TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if task:
            await asyncio.gather(task, return_exceptions=True)
        if self._http:
            await self._http.close()
            self._http = None

    async def disconnect(self):
        async with self._lock:
            self._connected = False
            await self._stop_pipeline()
            await self._context.close()
        if getattr(self, "_on_disconnect", None):
            await self._on_disconnect()

    async def cancel_ongoing_response(self):
        async with self._lock:
            await self._interrupt_locked()
        return True

    async def _interrupt_locked(self):
        from .lifetime import ResetConversationFrame, reset_user_turn
        self.response_generation += 1
        self._admitted_audio = False
        self._response_active = False
        self._audio_playback_manager.interrupt_audio_stream()
        if self._worker and self._run_task and not self._run_task.done():
            self._stt_needs_reset |= self._stt_pending
            boundary = ResetConversationFrame()
            await self._worker.queue_frame(boundary)
            try:
                await asyncio.wait_for(boundary.acknowledged.wait(), 3)
                self._stt_needs_reset |= self._stt_pending
                await reset_user_turn(self._user)
            except asyncio.TimeoutError:
                await self._stop_pipeline()
                self.observe("session.recycle", reason="interruption_timeout")
        self.observe("response.cancelled", reason="explicit_stop_or_takeover")

    async def send_speaker_marker(self, user_id, display_name):
        return await self.send_turn_context(user_id, display_name)

    async def send_turn_context(self, user_id, display_name, *, streaming=False):
        async with self._lock:
            if not self._connected:
                return False
            self.observer.register_secrets(str(user_id), display_name)
            text = self._context.turn_context(user_id, display_name)
            from .services import vocabulary
            words = vocabulary(text)
            if self._worker and not self._run_task.done():
                speaker_changed = self._speaker is not None and self._speaker != (user_id, display_name)
                vocabulary_changed = bool(words) and words != self._vocabulary
                if (speaker_changed or self._stt_needs_reset or vocabulary_changed) and self._admitted_audio:
                    await self._interrupt_locked()
                if self._worker and (speaker_changed or self._stt_needs_reset or vocabulary_changed):
                    self._admitted_audio = False
                    self._stt_needs_reset = True
                    from .lifetime import reset_user_turn
                    if vocabulary_changed:
                        # Gemini adaptation is connection-time only. Update the
                        # pinned settings store, then reconnect once at this
                        # closed input boundary (never defer past new PCM).
                        self._stt._settings.apply_update(self._stt.Settings(
                            adaptation_phrases=self._adaptation_phrases(words)))
                    try:
                        await self._stt._do_reconnect()
                    except Exception as exc:
                        # Keep admission closed and retry the STT reset on the
                        # next context attempt; never upload into a failed reset.
                        self.observe("session.stt.reset_failed", error_code=type(exc).__name__)
                        return False
                    await reset_user_turn(self._user)
                    self._vocabulary = words or self._vocabulary
                    self._stt_pending = self._stt_needs_reset = False
                    self.observe("session.stt.reset", reason="speaker_pending_transcript_or_vocabulary")
                if self._worker:
                    self._speaker = (user_id, display_name)
                    self.replace_voice_context(text)
                    self.capture("turn.context.sent", {"text": text})
                    self.observe("session.reused", status="ready")
                    return True
            # A prior cancellation already invalidated the old pipeline. Do not
            # advance the transport epoch again while accepting its replacement
            # context: the coordinator would reject the first queued utterance.
            if self._worker is not None or self._run_task is not None:
                await self._stop_pipeline()
            self._speaker = (user_id, display_name)
            text = self._context.turn_context(user_id, display_name)
            self.replace_voice_context(text)
            try:
                await self._build_pipeline(text)
            except Exception as exc:
                self.observe("session.failed", error_code=type(exc).__name__)
                await self._stop_pipeline()
                raise
            self.capture("turn.context.sent", {"text": text})
            return True

    def _adaptation_phrases(self, words):
        return list(dict.fromkeys([
            self._service_config["wake_phrase"], "闭嘴", "结束", *words]))[:100]

    async def _build_pipeline(self, context_text):
        import aiohttp
        from pipecat.adapters.schemas.function_schema import FunctionSchema
        from pipecat.adapters.schemas.tools_schema import ToolsSchema
        from pipecat.audio.vad.silero import SileroVADAnalyzer
        from pipecat.pipeline.pipeline import Pipeline
        from pipecat.pipeline.worker import PipelineWorker, PipelineParams
        from pipecat.workers.runner import WorkerRunner
        from pipecat.processors.aggregators.llm_response_universal import (
            LLMContextAggregatorPair, LLMUserAggregatorParams)
        from pipecat.services.google.gemini_live.stt import GeminiSTTService
        from pipecat.turns.user_turn_strategies import UserTurnStrategies
        from pipecat.turns.user_start import TranscriptionUserTurnStartStrategy
        from pipecat.turns.user_stop import SpeechTimeoutUserTurnStopStrategy
        from .services import make_llm, make_tts, vocabulary
        from .pipeline import TraceFrames, PrepareLLMTurn, ReplyGate, DiscordOutput, SpokenHistoryGate
        from .lifetime import BoundaryAck

        config, epoch = self._service_config, self.connection_epoch
        self._http = aiohttp.ClientSession()
        instructions = config["instructions"] + "\n" + CONTEXT_INSTRUCTIONS
        if config.get("league_tools_enabled", True):
            instructions += "\n" + MAYHEM_TOOL_INSTRUCTIONS
        instructions += ("\nYou receive transcribed speech as text. General web search is disabled; "
            "use the supplied game tools. For chatter clearly not addressed to you, output exactly "
            "<NO_REPLY>. Never acknowledge VOICE_CONTEXT metadata on its own.")
        llm = make_llm(config, instructions)
        tts = make_tts(config, self._http)
        if config["tts_provider"] == "doubao":
            self.observer.register_secrets(tts.key)
        self._vocabulary = vocabulary(context_text)
        stt = GeminiSTTService(api_key=config["api_key"], sample_rate=16000,
            settings=GeminiSTTService.Settings(model=config["stt_model"],
                languages=config["stt_languages"],
                adaptation_phrases=self._adaptation_phrases(self._vocabulary)))
        tools = []
        for tool in LEAGUE_TOOLS if config.get("league_tools_enabled", True) else []:
            tools.append(FunctionSchema(tool["name"], tool["description"],
                tool["parameters"]["properties"], tool["parameters"].get("required", [])))
            llm.register_function(tool["name"], self._tool_call, cancel_on_interruption=True)
        self._chat.set_tools(ToolsSchema(standard_tools=tools))
        user, assistant = LLMContextAggregatorPair(self._chat,
            user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer(sample_rate=16000),
                user_turn_strategies=UserTurnStrategies(
                    # Require actual transcript before interruption; raw noise alone isn't a turn.
                    start=[TranscriptionUserTurnStartStrategy()],
                    stop=[SpeechTimeoutUserTurnStopStrategy(
                        user_speech_timeout=config.get("turn_silence_seconds", .8))])))

        self._stt, self._user = stt, user
        self._stt_pending = self._stt_needs_reset = False
        pipeline = Pipeline([stt, TraceFrames(self, epoch, metrics=False), user,
            PrepareLLMTurn(self, epoch), llm,
            ReplyGate(self, epoch), tts, TraceFrames(self, epoch), DiscordOutput(self, epoch),
            SpokenHistoryGate(), assistant, BoundaryAck()])
        self._worker = PipelineWorker(pipeline, params=PipelineParams(
            audio_in_sample_rate=16000, audio_out_sample_rate=24000,
            enable_metrics=True, enable_usage_metrics=True), idle_timeout_secs=None)
        ready = asyncio.Event()
        @self._worker.event_handler("on_pipeline_started")
        async def pipeline_started(_worker, _frame):
            ready.set()
        runner = WorkerRunner(handle_sigint=False)
        await runner.add_workers(self._worker)
        self._run_task = asyncio.create_task(runner.run(), name="voice-pipecat")
        waiter = asyncio.create_task(ready.wait())
        try:
            done, _ = await asyncio.wait([waiter, self._run_task], timeout=30,
                return_when=asyncio.FIRST_COMPLETED)
            if self._run_task in done:
                await self._run_task
                raise RuntimeError("Pipecat stopped before ready")
            if not ready.is_set():
                raise TimeoutError("Pipecat startup timed out")
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        self.observe("session.ready", model=config["llm_model"],
            prompt_hash=fingerprint(instructions),
            tools_hash=fingerprint(LEAGUE_TOOLS if tools else []))
        self.capture("session.config", {"stt": config["stt_model"],
            "llm": config["llm_model"], "tts": config["tts_provider"],
            "tts_voice": config["tts_voice"],
            "stt_languages": config["stt_languages"],
            "vocabulary": vocabulary(context_text), "instructions": instructions})
        self.capture("session.tools", {"tools": LEAGUE_TOOLS if tools else []})
        self._pipeline_started_at = time.monotonic()
        self._lifetime_task = asyncio.create_task(self._retire_when_idle(epoch))

    async def _retire_when_idle(self, epoch):
        await asyncio.sleep(self._service_config.get("connection_lifetime_seconds", 2700))
        while self._connected and self.connection_epoch == epoch:
            async with self._lock:
                quiet = time.monotonic() - self._last_audio_at > 60
                if not self._admitted_audio or (quiet and not self._response_active and not self._stt_pending):
                    await self._stop_pipeline()
                    self.observe("session.recycle", reason="connection_lifetime")
                    if self._connected and self._service_config.get("prewarm", True):
                        try:
                            await self._build_pipeline(self._context.turn_context(
                                *(self._speaker or (0, ""))))
                        except Exception as exc:
                            await self._stop_pipeline()
                            self.observe("session.failed", error_code=type(exc).__name__)
                    return
            await asyncio.sleep(1)

    async def _tool_call(self, params):
        epoch, turn, generation = self.connection_epoch, self._turn_id, self.response_generation
        self.capture("tool.started", {"name": params.function_name, "arguments": params.arguments})
        started = time.monotonic()
        result = json.loads(await self._tools.execute(params.function_name,
            json.dumps(params.arguments, ensure_ascii=False)))
        if epoch != self.connection_epoch or generation != self.response_generation:
            return
        if params.function_name == "compare_mayhem_choices" and result.get("status") == "ok":
            self._recent_choices = {"speaker": self._speaker,
                "champion": str(params.arguments.get("champion", ""))[:80],
                "options": [str(x)[:120] for x in params.arguments.get("options", [])[:3]],
                "rarity": params.arguments.get("rarity"),
                "note": "Previous queried choices, not currently offered choices or fresh statistics."}
        self.capture("tool.completed", {"name": params.function_name, "result": result,
            "duration_ms": round((time.monotonic()-started)*1000), "turn": turn})
        await params.result_callback(result)

    async def send_audio_chunk(self, audio_data):
        if not self._connected or not self._worker or self._run_task.done():
            return False
        from pipecat.frames.frames import InputAudioRawFrame
        self._admitted_audio = True
        self._last_audio_at = time.monotonic()
        await self._worker.queue_frame(InputAudioRawFrame(audio_data, 16000, 1))
        return True

    async def finalize_input_and_request_response(self):
        # Flush already admitted speech; do not cancel the answer on 结束.
        if not self._worker or self._run_task.done():
            return False
        from pipecat.frames.frames import VADUserStoppedSpeakingFrame
        await self._worker.queue_frame(VADUserStoppedSpeakingFrame())
        return True
