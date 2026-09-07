"""Discord boundary adapters. Turn and service scheduling belongs to Pipecat."""
import asyncio
import json
import time

from pipecat.frames.frames import (
    BotStoppedSpeakingFrame, CancelFrame, ErrorFrame, InterruptionFrame,
    LLMFullResponseEndFrame, LLMFullResponseStartFrame, LLMTextFrame,
    MetricsFrame, TranscriptionFrame, LLMContextFrame, InterimTranscriptionFrame,
    VADUserStartedSpeakingFrame, TTSStartedFrame, TTSAudioRawFrame,
)
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import TransportParams


def metric_payload(data):
    value = data.model_dump() if hasattr(data, "model_dump") else {"description": str(data)}
    usage = value.get("value")
    if isinstance(usage, dict):
        # Match the observer's safe numerical usage fields; never weaken key redaction.
        for source, target in (("prompt_tokens", "input_tokens"),
                               ("completion_tokens", "output_tokens"),
                               ("cache_read_input_tokens", "cached_tokens")):
            count = usage.pop(source, None)
            if isinstance(count, (int, float)) and not isinstance(count, bool):
                usage[target] = count
    return value


class TraceFrames(FrameProcessor):
    def __init__(self, manager, epoch, metrics=True):
        super().__init__()
        self.manager, self.epoch = manager, epoch
        self.metrics = metrics

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if not self.metrics:
            if isinstance(frame, VADUserStartedSpeakingFrame):
                self.manager._stt_pending = True
            if isinstance(frame, TranscriptionFrame):
                self.manager._stt_pending = not frame.finalized
            if isinstance(frame, (TranscriptionFrame, InterimTranscriptionFrame)) and not self.manager._admitted_audio:
                return
        if self.epoch != self.manager.connection_epoch:
            # Lifecycle frames must still reach processors during teardown.
            if isinstance(frame, (TranscriptionFrame, LLMTextFrame)):
                return
        if isinstance(frame, TranscriptionFrame) and not self.metrics:
            self.manager.capture("transcript.user.fragment", {"text": frame.text})
        elif isinstance(frame, MetricsFrame) and self.metrics:
            self.manager.capture("pipeline.metrics", {"metrics": [
                metric_payload(d) for d in frame.data]})
        elif isinstance(frame, ErrorFrame):
            self.manager.observe("pipeline.error", reason=type(frame).__name__)
        await self.push_frame(frame, direction)


class PrepareLLMTurn(FrameProcessor):
    """Refresh identity/game state synchronously before the model sees a turn."""
    def __init__(self, manager, epoch):
        super().__init__()
        self.manager, self.epoch = manager, epoch
        self.last_user_message = None

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMContextFrame) and direction == FrameDirection.DOWNSTREAM:
            if self.epoch != self.manager.connection_epoch or not self.manager._admitted_audio:
                return
            self.manager._response_active = True
            messages = frame.context.get_messages()
            for index in range(len(messages)-1, -1, -1):
                message = messages[index]
                if not isinstance(message, dict) or message.get("role") != "user":
                    continue
                content = message.get("content", "")
                if isinstance(content, str) and content.startswith(("VOICE_CONTEXT", "CONVERSATION_MEMORY", "SPEAKER_CONTEXT")):
                    continue
                if message is not self.last_user_message:
                    self.last_user_message = message
                    self.manager._turn_id = self.manager.observer.new_id("turn")
                    text = self.manager._context.turn_context(*self.manager._speaker)
                    self.manager.replace_voice_context(text, before=message)
                    # Preserve historical speaker attribution without retaining
                    # an entire obsolete game snapshot for each question.
                    messages.insert(messages.index(message)-1, {"role": "user",
                        "content": "SPEAKER_CONTEXT\n" + json.dumps({
                            "speaker": self.manager._speaker,
                            "scope": "speaker of the following utterance only; not a champion binding"}, ensure_ascii=False)})
                    self.manager.remember_question(content)
                    self.manager.capture("turn.context.sent", {"text": text})
                    self.manager.capture("transcript.user", {"text": content})
                break
        await self.push_frame(frame, direction)


class ReplyGate(FrameProcessor):
    """Hold only a possible leading NO_REPLY marker, then stream immediately."""
    def __init__(self, manager, epoch):
        super().__init__()
        self.manager, self.epoch, self.text = manager, epoch, ""
        self.generation = manager.response_generation
        self.prefix = ""
        self.streaming = self.suppressed = False

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, (InterruptionFrame, CancelFrame, LLMFullResponseStartFrame)):
                self.text = ""
                self.prefix = ""
                self.streaming = self.suppressed = False
                self.generation = self.manager.response_generation
            elif isinstance(frame, LLMTextFrame):
                if self.epoch == self.manager.connection_epoch and self.generation == self.manager.response_generation and self.manager._admitted_audio:
                    self.text += frame.text
                    if self.suppressed:
                        return
                    if self.streaming:
                        await self.push_frame(frame, direction)
                    else:
                        self.prefix += frame.text
                        candidate = self.prefix.lstrip()
                        if candidate.startswith("<NO_REPLY>"):
                            self.suppressed = True
                        elif candidate and not "<NO_REPLY>".startswith(candidate):
                            self.streaming = True
                            self.manager.observe("response.text.first")
                            await self.push_frame(LLMTextFrame(self.prefix), direction)
                            self.prefix = ""
                return
            elif isinstance(frame, LLMFullResponseEndFrame):
                text, self.text = self.text.strip(), ""
                if (self.epoch == self.manager.connection_epoch and text
                        and self.generation == self.manager.response_generation
                        and self.manager._admitted_audio):
                    self.manager.capture("transcript.assistant", {"text": text})
                    if self.streaming:
                        self.manager.remember_answer(text)
                    else:
                        self.manager._response_active = False
                        self.manager.observe("response.suppressed", reason="no_reply")
        await self.push_frame(frame, direction)


class SpokenHistoryGate(FrameProcessor):
    """Keep native tool aggregation, but don't duplicate generated text history."""
    async def process_frame(self, frame, direction):
        from pipecat.frames.frames import TextFrame
        await super().process_frame(frame, direction)
        if isinstance(frame, TextFrame):
            frame.append_to_context = False
        await self.push_frame(frame, direction)


class DiscordOutput(BaseOutputTransport):
    """Pace PCM into the existing Discord player, with Pipecat interruption flush."""
    def __init__(self, manager, epoch):
        super().__init__(TransportParams(audio_out_enabled=True,
            audio_out_sample_rate=24000, audio_out_channels=1, audio_out_10ms_chunks=2))
        self.manager, self.epoch = manager, epoch
        self.stream = None
        self.deadline = None
        self.buffer_seconds = .12
        self.generation = manager.response_generation
        self.contexts = set()
        self.retired_contexts = set()

    async def start(self, frame):
        await super().start(frame)
        await self.set_transport_ready(frame)

    async def process_frame(self, frame, direction):
        if isinstance(frame, (InterruptionFrame, CancelFrame)):
            self.retired_contexts.update(self.contexts)
            self.contexts.clear()
            self.manager._audio_playback_manager.interrupt_audio_stream()
            self.stream, self.deadline = None, None
            self.manager.observe("output.stop.applied", reason="pipecat_interruption")
        if isinstance(frame, TTSStartedFrame) and self.manager._admitted_audio:
            self.generation = self.manager.response_generation
            self.contexts.add(frame.context_id)
        if isinstance(frame, TTSAudioRawFrame) and frame.context_id in self.retired_contexts:
            return
        await super().process_frame(frame, direction)

    async def write_audio_frame(self, frame):
        if (self.epoch != self.manager.connection_epoch or not self.manager._admitted_audio
                or self.generation != self.manager.response_generation
                or getattr(frame, "context_id", None) in self.retired_contexts):
            return False
        if self.stream is None:
            self.stream = self.manager.observer.new_id("response")
            await self.manager._audio_playback_manager.start_new_audio_stream(
                self.stream, (frame.sample_rate, frame.num_channels))
            self.manager.observe("response.audio.first", response_id=self.stream)
        await self.manager._audio_playback_manager.add_audio_chunk(frame.audio)
        duration = len(frame.audio) / (2 * frame.sample_rate * frame.num_channels)
        now = time.monotonic()
        # Use cumulative audio time: resetting to now for every 20 ms frame
        # accumulates Windows timer/executor overhead and starves Discord.
        # A bounded lead absorbs jitter; interruption still flushes the player.
        if self.deadline is None:
            self.deadline = now
        elif now - self.deadline > self.buffer_seconds:
            self.manager.observe("output.pacing.late",
                duration_ms=round((now-self.deadline)*1000))
            self.deadline = now
        self.deadline += duration
        await asyncio.sleep(max(0, self.deadline-self.buffer_seconds-time.monotonic()))
        return True

    async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
        if isinstance(frame, BotStoppedSpeakingFrame) and self.stream:
            self.manager._response_active = False
            await self.manager._audio_playback_manager.end_audio_stream(self.stream)
            self.stream, self.deadline = None, None
        await super().push_frame(frame, direction)
