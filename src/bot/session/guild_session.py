"""
Guild Session module for managing per-guild bot state and interactions.

This module defines the GuildSession class, which encapsulates all the logic,
state, and resources for the bot's operation within a single Discord guild. This
ensures that the bot can operate in multiple guilds simultaneously without any
state conflicts.
"""

import asyncio
import time
from typing import TYPE_CHECKING, Dict, Optional, Set

import discord
import openai
from discord.ext import commands
from google.genai import errors as gemini_errors

from src.audio.playback import AudioPlaybackManager
from src.audio.processing import (
    UnifiedAudioProcessor,
    AudioFormat,
    ProcessingStrategy,
    DISCORD_FORMAT,
)
from src.audio.sinks import ManualControlSink
from src.audio.continuous_sink import ContinuousAudioSink
from src.audio.input_gate import SpeechInputGate
from src.bot.session.ai_service_coordinator import AIServiceCoordinator
from src.bot.session.conversation_router import (
    GuildConversationRouter,
    HeldTurn,
    TurnDisposition,
)
from src.bot.session.interaction_handler import InteractionHandler
from src.bot.session.session_ui_manager import SessionUIManager
from src.bot.session.voice_connection_manager import VoiceConnectionManager
from src.bot.state import BotState, BotStateEnum, RecordingMethod
from src.config.config import Config
from src.exceptions import SessionConsistencyError
from src.utils.logger import get_logger

if TYPE_CHECKING:
    from src.audio.sinks import AudioSink

logger = get_logger(__name__)


class GuildSession:
    """
    Manages all state and logic for the bot's interaction within a single guild.

    This class encapsulates all components required for a voice session, including
    state management, audio playback, voice connection, and AI service interaction.
    Each guild will have its own instance of this class, ensuring complete isolation.
    """

    def __init__(
        self,
        guild: discord.Guild,
        bot: commands.Bot,
        ai_service_factories: Dict[str, tuple],
    ):
        """
        Initializes a new session for a specific guild.

        Args:
            guild: The Discord guild this session belongs to.
            bot: The Discord bot instance.
            ai_service_factories: A dictionary of factories for creating AI service managers.
        """
        self.guild = guild
        self.bot = bot
        self._action_lock = asyncio.Lock()
        self._background_tasks: Set[asyncio.Task] = set()
        self._audio_sink: Optional[AudioSink] = None
        self._current_turn_disposition: TurnDisposition = TurnDisposition.SEND
        self._current_turn_user_id: Optional[int] = None
        self._current_turn_user_name: Optional[str] = None
        self._current_turn_started_at: float = 0.0
        self._agent_response_pending = False
        self._response_playback_seen = False
        self._response_pending_since = 0.0
        self._live_input_active = False
        self._live_input_user_id: Optional[int] = None
        self._live_input_user_name: Optional[str] = None
        self._live_input_generation = 0
        self._live_stream_revision = 0
        self._live_audio_queue: asyncio.Queue[tuple[int, str, bytes, int]] = asyncio.Queue(
            maxsize=500
        )
        self._live_audio_buffer = bytearray()
        self._live_send_lock = asyncio.Lock()
        self._input_gate = None
        self._input_gate_key = None
        logger.info("Realtime input gate configured: enabled=%s threshold_dbfs=%s",
                    Config.VOICE_INPUT_GATE_ENABLED, Config.VOICE_INPUT_GATE_DBFS)

        # Cached audio processor instance for efficient reuse
        self._audio_processor = UnifiedAudioProcessor()

        self.bot_state = BotState()
        self.conversation_router = GuildConversationRouter(
            active_participant_policy=Config.ACTIVE_PARTICIPANT_SPEECH_POLICY,
            new_participant_policy=Config.NEW_PARTICIPANT_WAKE_POLICY,
            idle_timeout_seconds=Config.CONVERSATION_IDLE_TIMEOUT_SECONDS,
            held_turn_max_seconds=Config.HELD_TURN_MAX_SECONDS,
            held_turn_queue_max=Config.HELD_TURN_QUEUE_MAX,
            source_bytes_per_second=(
                Config.DISCORD_AUDIO_FRAME_RATE
                * Config.DISCORD_AUDIO_CHANNELS
                * Config.SAMPLE_WIDTH
            ),
            cross_user_wake_required=getattr(Config, "CROSS_USER_WAKE_REQUIRED", True),
        )
        self.ui_manager = SessionUIManager(self.guild, self.bot_state)
        self.audio_playback_manager = AudioPlaybackManager(self.guild)
        self.voice_connection = VoiceConnectionManager(
            self.guild, self.audio_playback_manager
        )
        self.ai_coordinator = AIServiceCoordinator(
            bot_state=self.bot_state,
            audio_playback_manager=self.audio_playback_manager,
            ai_service_factories=ai_service_factories,
            guild_id=self.guild.id,
        )
        self.ai_coordinator.is_user_input_blocked = self._is_user_upload_blocked
        self.ai_coordinator.get_user_input_generation = self._user_input_generation
        # InteractionHandler must be created last, as it requires a reference to the fully initialized GuildSession
        self.interaction_handler = InteractionHandler(
            guild_id=self.guild.id,
            bot=self.bot,
            ui_manager=self.ui_manager,
            guild_session=self,
        )

    async def start_background_tasks(self) -> None:
        """Starts all persistent background tasks for the session."""
        self.ui_manager.start()
        monitor_task = asyncio.create_task(self._conversation_monitor_loop())
        self._background_tasks.add(monitor_task)
        monitor_task.add_done_callback(self._background_tasks.discard)
        live_stream_task = asyncio.create_task(self._live_audio_stream_loop())
        self._background_tasks.add(live_stream_task)
        live_stream_task.add_done_callback(self._background_tasks.discard)
        logger.info(f"Background tasks started for guild {self.guild.id}.")

    async def cleanup(self) -> None:
        """
        Gracefully shuts down the session, ensuring each cleanup step is attempted.
        """
        logger.info(f"Cleaning up session for guild {self.guild.id}")
        self._live_input_active = False

        # Cancel all background tasks managed by this session
        for task in self._background_tasks:
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)

        await self.ui_manager.cleanup()
        await self.interaction_handler.cleanup()  # No-op, but good practice

        # Clean up the audio sink
        if self._audio_sink:
            self._audio_sink.cleanup()
            self._audio_sink = None

        try:
            await self.ai_coordinator.shutdown()
        except Exception as e:
            logger.error(
                f"Error during AI provider shutdown for guild {self.guild.id}: {e}",
                exc_info=True,
            )

        try:
            if self.voice_connection.is_connected():
                await self.voice_connection.disconnect()
        except Exception as e:
            logger.error(
                f"Error during voice disconnect for guild {self.guild.id}: {e}",
                exc_info=True,
            )

        await self.bot_state.reset_to_idle()
        self.conversation_router.release_participants()
        logger.info(f"Session for guild {self.guild.id} cleaned up successfully.")

    async def handle_reaction_add(
        self, reaction: discord.Reaction, user: discord.User
    ) -> None:
        """
        Delegates reaction add events to the interaction handler.
        """
        await self.interaction_handler.handle_reaction_add(reaction, user)

    async def handle_reaction_remove(
        self, reaction: discord.Reaction, user: discord.User
    ) -> None:
        """
        Delegates reaction remove events to the interaction handler.
        """
        await self.interaction_handler.handle_reaction_remove(reaction, user)

    async def _on_ai_connect(self) -> None:
        """Callback for when the AI service connects."""
        logger.info(f"AI service connected for guild {self.guild.id}.")
        # If the voice connection is also active, attempt to recover the bot's state.
        if self.voice_connection.is_connected():
            await self.bot_state.recover_to_standby()

    async def _on_ai_disconnect(self) -> None:
        """Callback for when the AI service disconnects."""
        logger.warning(f"AI service disconnected for guild {self.guild.id}.")
        await self.bot_state.enter_connection_error_state()

    async def handle_voice_connection_update(self, is_connected: bool) -> None:
        """
        Handler called by VoiceCog on voice state changes.

        This method decides whether to recover the bot to a healthy state or enter
        an error state based on the status of both the voice and AI connections.
        """
        # Query the coordinator directly to get the authoritative AI connection status.
        if is_connected and self.ai_coordinator.is_connected():
            logger.info(
                f"Voice connection active for guild {self.guild.id}, recovering if needed."
            )
            await self.bot_state.recover_to_standby()
        elif not is_connected:
            # If the voice connection is lost, always enter an error state.
            logger.warning(f"Voice connection lost for guild {self.guild.id}.")
            await self.bot_state.enter_connection_error_state()

    # --- New Handler Methods (called by InteractionHandler) ---

    async def handle_consent_reaction(self, user: discord.User, added: bool) -> None:
        async with self._action_lock:
            if added:
                logger.info(f"User {user.id} granted consent.")
                await self.bot_state.grant_consent(user.id)
                if self._audio_sink:
                    if isinstance(self._audio_sink, ManualControlSink):
                        loop = asyncio.get_running_loop()
                        await loop.run_in_executor(
                            None, self._audio_sink.add_user, user.id
                        )
                    else:
                        self._audio_sink.add_user(user.id)
            else:
                logger.info(f"User {user.id} revoked consent.")
                await self.bot_state.revoke_consent(user.id)
                self.conversation_router.remove_participant(user.id)
                if self._audio_sink:
                    self._audio_sink.remove_user(user.id)
            self.ui_manager.schedule_update()

    async def handle_voice_member_access(
        self, member: discord.Member, joined: bool
    ) -> None:
        """Keep implicit wake-word access in sync with voice membership."""
        if Config.VOICE_ACCESS_MODE != "implicit" or member.bot:
            return
        async with self._action_lock:
            if joined:
                logger.info("Implicitly enabling voice access for user %s.", member.id)
                await self.bot_state.grant_consent(member.id)
                if isinstance(self._audio_sink, ManualControlSink):
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(
                        None, self._audio_sink.add_user, member.id
                    )
                elif self._audio_sink:
                    self._audio_sink.add_user(member.id)
            else:
                logger.info("Removing implicit voice access for user %s.", member.id)
                await self.bot_state.revoke_consent(member.id)
                self.conversation_router.remove_participant(member.id)
                if self._audio_sink:
                    self._audio_sink.remove_user(member.id)
            self.ui_manager.schedule_update()

    async def _interrupt_ongoing_playback(self) -> None:
        """Interrupts any ongoing audio playback and AI response generation.

        This method stops audio playback without affecting the audio sink/reception,
        and cancels any AI responses being generated to ensure clean state for recording.
        """
        # Stop voice client playback immediately
        self.voice_connection.stop_playback()

        # Cancel any ongoing AI response generation
        if not await self.ai_coordinator.cancel_ongoing_response():
            logger.warning(
                f"Failed to cancel ongoing AI response for guild {self.guild.id}"
            )
        self.audio_playback_manager.interrupt_audio_stream()
        self._agent_response_pending = False
        self._response_playback_seen = False
        self._response_pending_since = 0.0

    async def handle_pushtotalk_reaction(self, user: discord.User, added: bool) -> None:
        async with self._action_lock:
            if added and self.bot_state.current_state == BotStateEnum.STANDBY:
                # Interrupt any ongoing playback before starting recording
                await self._interrupt_ongoing_playback()

                # This transition is immediate, no cues.
                if isinstance(self._audio_sink, ManualControlSink):
                    self._audio_sink.enable_vad(False)
                # Pressing the PTT control is an explicit local admission into
                # the shared guild session. Open the same upload gate that a
                # positive wake-word detection opens before recording starts.
                old_floor = self.conversation_router.floor_owner_id
                self.conversation_router.route_speech(
                    user.id, via_wake_word=True, agent_speaking=False
                )
                if old_floor is not None and old_floor != user.id:
                    self._invalidate_floor_input(old_floor)
                    await self.bot_state.remove_active_participant(old_floor)
                await self.bot_state.add_active_participant(user.id)
                await self.bot_state.start_recording(user, RecordingMethod.PushToTalk)
                self._set_current_turn_context(user, TurnDisposition.SEND)
                live_input = await self._begin_live_audio_input(user)
                if live_input and isinstance(self._audio_sink, ManualControlSink):
                    self._audio_sink.enable_vad(
                        True,
                        silence_timeout_ms=Config.LIVE_INPUT_SILENCE_TIMEOUT_MS,
                    )
                # SESSION ID SYNC: Update sink session ID after new recording starts
                if isinstance(self._audio_sink, ManualControlSink):
                    self._audio_sink.update_session_id()
            elif (
                not added
                and self.bot_state.current_state == BotStateEnum.RECORDING
                and self.bot_state.recording_method == RecordingMethod.PushToTalk
                and self.bot_state.is_authorized(user)
            ):
                if self._audio_sink:
                    sink: "ManualControlSink" = self._audio_sink  # type: ignore
                    try:
                        audio_data = sink.stop_and_get_audio()
                        if self._live_input_active:
                            await self._finish_live_audio_input(
                                finalize=True, flush=True
                            )
                        else:
                            self._handle_finished_recording(audio_data)
                    except SessionConsistencyError as e:
                        logger.warning(
                            f"Recording interrupted due to session inconsistency: {e}"
                        )
                await self.bot_state.stop_recording()

    # --- New Callback Methods (passed to ManualControlSink) ---

    async def on_wake_word_detected(self, user: discord.User) -> None:
        logger.info(
            f"GuildSession: Received on_wake_word_detected event for user {user.id}"
        )
        async with self._action_lock:
            if self.bot_state.current_state == BotStateEnum.RECORDING:
                if self.conversation_router.cross_user_wake_required:
                    if user.id == self._current_turn_user_id:
                        return
                    if self._is_user_input_blocked(user.id):
                        return
                    # Preserve pending speech under the previous identity before
                    # the explicit wake claims the single floor.
                    old_user_id = self._current_turn_user_id
                    try:
                        await asyncio.wait_for(
                            self._flush_floor_input(old_user_id), timeout=2.0,
                        )
                    except asyncio.TimeoutError:
                        self.ai_coordinator.observe_input_event(
                            "input.floor.flush_timeout", old_user_id,
                            reason="handoff_tail_deadline", status="truncated",
                        )
                        self._live_audio_buffer.clear()
                        while not self._live_audio_queue.empty():
                            self._live_audio_queue.get_nowait()
                            self._live_audio_queue.task_done()
                        self._live_input_user_id = None
                        self._live_input_user_name = None
                    self._invalidate_floor_input(old_user_id)
                    if old_user_id is not None:
                        await self.bot_state.remove_active_participant(old_user_id)
                    await self.bot_state.stop_recording()
                    await self._begin_routed_recording(
                        user, via_wake_word=True, method=RecordingMethod.WakeWord
                    )
                    return
                if self.bot_state.is_active_participant(user.id) is True:
                    return
                self.conversation_router.route_speech(
                    user.id,
                    via_wake_word=True,
                    agent_speaking=self._is_agent_speaking(),
                )
                await self.bot_state.add_active_participant(user.id)
                logger.info(
                    "Admitted user %s while another guild-serial input turn is active.",
                    user.id,
                )
                return
            if self.bot_state.current_state != BotStateEnum.STANDBY:
                return
            started = await self._begin_routed_recording(
                user, via_wake_word=True, method=RecordingMethod.WakeWord
            )
            # A cue is useful for the buffered/local-VAD fallback, but it would
            # look like provider playback to the live-stream monitor and close
            # the just-opened server-VAD turn. Live input therefore starts
            # immediately and silently after the wake word.
            if started and not self._live_input_active:
                await self.audio_playback_manager.play_cue("start_recording")

    async def on_active_speech_detected(self, user: discord.User) -> None:
        """Let an admitted participant speak again without repeating the wake word."""
        async with self._action_lock:
            if self.bot_state.current_state != BotStateEnum.STANDBY:
                return
            await self._begin_routed_recording(
                user, via_wake_word=False, method=RecordingMethod.Conversation
            )

    def _is_user_input_blocked(self, user_id: int) -> bool:
        sink = getattr(self, "_audio_sink", None)
        return bool(
            sink is not None
            and getattr(sink, "is_user_input_blocked", lambda _id: False)(user_id) is True
        )

    def _is_user_upload_blocked(self, user_id: int) -> bool:
        router = getattr(self, "conversation_router", None)
        return self._is_user_input_blocked(user_id) or bool(
            router is not None and router.cross_user_wake_required is True
            and router.floor_owner_id != user_id
        )

    def _invalidate_floor_input(self, user_id: Optional[int]) -> None:
        if user_id is None:
            return
        sink = getattr(self, "_audio_sink", None)
        if sink is not None:
            invalidate = getattr(sink, "invalidate_user_input", None)
            if invalidate:
                invalidate(user_id)

    async def _flush_floor_input(self, user_id: Optional[int]) -> bool:
        """Preserve the previous speaker's pending PCM before a normal takeover."""
        if self._live_input_active:
            return await self._finish_live_audio_input(finalize=True, flush=True)
        sink = getattr(self, "_audio_sink", None)
        if user_id is None or not isinstance(sink, ManualControlSink):
            return True
        try:
            audio_data = sink.stop_and_get_audio()
            if not audio_data:
                return True
            if self._current_turn_disposition == TurnDisposition.HOLD:
                self._handle_finished_recording(audio_data)
                return True
            target = self.ai_coordinator.get_processing_audio_format()
            if not target:
                return False
            processed = await self._audio_processor.convert(
                source_format=DISCORD_FORMAT,
                target_format=AudioFormat(target[0], target[1], Config.SAMPLE_WIDTH),
                audio_data=audio_data, strategy=ProcessingStrategy.QUALITY,
            )
            return await self.ai_coordinator.send_audio_turn(
                processed, user_id=user_id,
                display_name=self._current_turn_user_name or str(user_id),
            )
        except Exception as exc:
            self.ai_coordinator.observe_input_event(
                "input.floor.flush_failed", user_id, status="failed",
                reason="buffered_handoff", error_code=type(exc).__name__,
            )
            return False

    def _user_input_generation(self, user_id: int) -> int:
        sink = getattr(self, "_audio_sink", None)
        value = getattr(sink, "get_user_input_generation", lambda _id: 0)(user_id)
        return value if isinstance(value, int) else 0

    async def on_stop_word_detected(self, user: discord.User, keyword: str) -> None:
        """Close this speaker's input; only an explicit shut-up also stops output."""
        async with self._action_lock:
            # A later wake can supersede a queued stop callback.
            if not self._is_user_input_blocked(user.id):
                return
            await self.bot_state.remove_active_participant(user.id)
            self.conversation_router.remove_participant(user.id)
            self.ai_coordinator.observe_input_event(
                "input.stop.applied", user.id, status="blocked"
            )
            owns_input = user.id in {
                self._current_turn_user_id, self._live_input_user_id
            }
            # Output control is shared; the input owner may already be cleared
            # after VAD ends, and another participant can also ask for silence.
            if keyword.strip() == "闭嘴":
                await self._interrupt_ongoing_playback()
                await self.ai_coordinator.end_conversation(reason="explicit_stop")
                self.ai_coordinator.observe_input_event(
                    "output.stop.applied", user.id, status="suppressed"
                )
            if owns_input:
                had_live_input = self._live_input_active
                if had_live_input:
                    await self._finish_live_audio_input(finalize=False, flush=False)
                if keyword.strip() != "闭嘴" and had_live_input:
                    # The stop gate already discarded unsent command frames.
                    # Finish only audio delivered before the gate closed so the
                    # agent can answer, keeping an existing reply playing.
                    finished = await self.ai_coordinator.finish_stopped_user_input(user.id)
                    if finished:
                        self._agent_response_pending = True
                        self._response_pending_since = time.monotonic()
                if self.bot_state.current_state == BotStateEnum.RECORDING:
                    await self.bot_state.stop_recording()
                self._current_turn_user_id = None
                self._current_turn_user_name = None

    def _is_agent_speaking(self) -> bool:
        return bool(
            self._agent_response_pending
            or self.audio_playback_manager.get_current_playing_response_id()
        )

    def _set_current_turn_context(
        self, user: discord.User, disposition: TurnDisposition
    ) -> None:
        self._current_turn_disposition = disposition
        self._current_turn_user_id = user.id
        self._current_turn_user_name = user.name
        self._current_turn_started_at = time.monotonic()

    async def _begin_live_audio_input(self, user: discord.User) -> bool:
        """Open a realtime provider stream for one admitted speaker."""
        if self._current_turn_disposition == TurnDisposition.HOLD:
            return False
        if not (self.ai_coordinator.supports_server_vad_streaming()
                or self.ai_coordinator.supports_client_vad_streaming() is True):
            return False
        self._live_input_active = True
        self._live_stream_revision += 1
        self._input_gate = None
        self._input_gate_key = None
        self._live_input_user_id = user.id
        self._live_input_user_name = user.name
        self._live_input_generation = self._user_input_generation(user.id)
        self._live_audio_buffer.clear()
        while True:
            try:
                self._live_audio_queue.get_nowait()
                self._live_audio_queue.task_done()
            except asyncio.QueueEmpty:
                break
        self._audio_processor.reset_state(
            f"live_input_{self.bot_state.current_session_id}"
        )
        logger.info(
            "Started realtime audio input for user %s in guild %s.",
            user.id,
            self.guild.id,
        )
        return True

    async def on_recording_audio_chunk(
        self, user: discord.User, discord_pcm: bytes
    ) -> None:
        """Convert a Discord frame and enqueue it for realtime provider upload."""
        if (
            not self._live_input_active or user.id != self._live_input_user_id
            or self._is_user_upload_blocked(user.id)
        ):
            return
        target = self.ai_coordinator.get_processing_audio_format()
        if not target:
            return
        processed = self._audio_processor.convert_sync(
            DISCORD_FORMAT,
            AudioFormat(target[0], target[1], Config.SAMPLE_WIDTH),
            discord_pcm,
            strategy=ProcessingStrategy.REALTIME,
            state_key=f"live_input_{self.bot_state.current_session_id}",
        )
        try:
            self._live_audio_queue.put_nowait(
                (user.id, user.name, processed, self._user_input_generation(user.id))
            )
        except asyncio.QueueFull:
            logger.warning(
                "Dropping realtime audio frame for guild %s because its queue is full.",
                self.guild.id,
            )

    async def _send_live_provider_chunk(
        self, user_id: int, display_name: str, pcm: bytes, *, flush_gate: bool = False
    ) -> bool:
        generation = self._user_input_generation(user_id)
        async with self._live_send_lock:
            if (
                self._is_user_upload_blocked(user_id)
                or generation != self._user_input_generation(user_id)
            ):
                return False
            if Config.VOICE_INPUT_GATE_ENABLED:
                target = self.ai_coordinator.get_processing_audio_format() or (16000, 1)
                if target[1] == 1 and target[0] in (8000, 16000, 32000, 48000):
                    key = (user_id, generation, target,
                           getattr(self, "_live_stream_revision", 0))
                    # During finish the stream revision has already advanced;
                    # the authorized old gate must be drained before retiring it.
                    old_key = getattr(self, "_input_gate_key", None)
                    same_finishing_gate = (not self._live_input_active and old_key
                                           and old_key[:3] == key[:3]
                                           and old_key[3] == key[3] - 1)
                    if old_key != key and not same_finishing_gate:
                        self._input_gate = SpeechInputGate(
                            target[0], open_dbfs=Config.VOICE_INPUT_GATE_DBFS)
                        self._input_gate_key = key
                    gate = self._input_gate
                    pcm = gate.process(pcm)
                    if flush_gate:
                        pcm += gate.flush()
                    if gate.frames >= 50 or flush_gate:
                        self.ai_coordinator.observe_input_event(
                            "input.gate.audio_summary", user_id, **gate.take_metrics())
                    if not pcm:
                        return True
            return await self.ai_coordinator.send_audio_stream_chunk(
                pcm, user_id=user_id, display_name=display_name
            )

    async def _live_audio_stream_loop(self) -> None:
        """Pace PCM chunks, including silence for server VAD and manual boundaries."""
        target = self.ai_coordinator.get_processing_audio_format() or (16000, 1)
        provider_chunk_bytes = target[0] * target[1] * Config.SAMPLE_WIDTH // 10
        try:
            while True:
                item = None
                try:
                    item = await asyncio.wait_for(
                        self._live_audio_queue.get(), timeout=0.1
                    )
                except asyncio.TimeoutError:
                    pass

                current_generation = self._user_input_generation(self._live_input_user_id)
                if current_generation != getattr(self, "_live_input_generation", 0):
                    self._live_audio_buffer.clear()
                    self._live_input_generation = current_generation

                if item is not None:
                    user_id, _display_name, pcm, *generation = item
                    self._live_audio_queue.task_done()
                    if generation and generation[0] != self._user_input_generation(user_id):
                        continue
                    if self._live_input_active and user_id == self._live_input_user_id:
                        self._live_audio_buffer.extend(pcm)
                elif self._live_input_active:
                    # Discord stops emitting RTP during silence. Send actual
                    # zero PCM so Gemini's server VAD can observe speech end.
                    self._live_audio_buffer.extend(b"\x00" * provider_chunk_bytes)

                while (
                    self._live_input_active
                    and len(self._live_audio_buffer) >= provider_chunk_bytes
                ):
                    current_generation = self._user_input_generation(self._live_input_user_id)
                    if current_generation != getattr(self, "_live_input_generation", 0):
                        self._live_audio_buffer.clear()
                        self._live_input_generation = current_generation
                        break
                    stream_revision = getattr(self, "_live_stream_revision", 0)
                    stream_user_id = self._live_input_user_id
                    chunk = bytes(self._live_audio_buffer[:provider_chunk_bytes])
                    del self._live_audio_buffer[:provider_chunk_bytes]
                    if not await self._send_live_provider_chunk(
                        stream_user_id or 0,
                        self._live_input_user_name or "unknown",
                        chunk,
                    ):
                        logger.error(
                            "Realtime audio upload failed for guild %s.", self.guild.id
                        )
                        if (
                            stream_revision == getattr(self, "_live_stream_revision", 0)
                            and stream_user_id == self._live_input_user_id
                            and current_generation == self._user_input_generation(stream_user_id)
                        ):
                            await self._recover_failed_live_input(
                                stream_user_id, stream_revision, current_generation,
                            )
                        break
        except asyncio.CancelledError:
            return

    async def _recover_failed_live_input(
        self, user_id: Optional[int], stream_revision: int, input_generation: int,
    ) -> None:
        """Retire a failed recording without replaying its unacknowledged audio."""
        async with self._action_lock:
            # A wake/stop/VAD callback may have replaced this input while the
            # provider send or action lock was awaited. Never retire that input.
            if (
                not self._live_input_active
                or stream_revision != self._live_stream_revision
                or user_id != self._live_input_user_id
                or input_generation != self._user_input_generation(user_id)
            ):
                return
            await self._finish_live_audio_input(finalize=False, flush=False)
            self._invalidate_floor_input(user_id)
            if self.bot_state.current_state == BotStateEnum.RECORDING:
                await self.bot_state.stop_recording()
            self._current_turn_user_id = None
            self._current_turn_user_name = None
            self.conversation_router.touch()
            self.ai_coordinator.observe_input_event(
                "input.stream.recovered", user_id, status="standby",
                reason="upload_failed", disposition="discarded_unsent_audio",
            )

    async def _finish_live_audio_input(self, *, finalize: bool, flush: bool) -> bool:
        """Close local streaming state, optionally flushing and committing it."""
        if not self._live_input_active:
            return False
        user_id = self._live_input_user_id or 0
        display_name = self._live_input_user_name or "unknown"
        if self._user_input_generation(user_id) != getattr(self, "_live_input_generation", 0):
            self._live_audio_buffer.clear()
        self._live_input_active = False
        self._live_stream_revision = getattr(self, "_live_stream_revision", 0) + 1
        audio_sink = getattr(self, "_audio_sink", None)
        if isinstance(audio_sink, ManualControlSink):
            audio_sink.enable_vad(False)

        while True:
            try:
                queued_user_id, queued_name, pcm, *generation = self._live_audio_queue.get_nowait()
                self._live_audio_queue.task_done()
                if generation and generation[0] != self._user_input_generation(queued_user_id):
                    continue
                if queued_user_id == user_id:
                    display_name = queued_name
                    self._live_audio_buffer.extend(pcm)
            except asyncio.QueueEmpty:
                break

        completed = True
        if flush and self._live_audio_buffer:
            target = self.ai_coordinator.get_processing_audio_format() or (16000, 1)
            provider_chunk_bytes = target[0] * target[1] * Config.SAMPLE_WIDTH // 10
            remainder = bytes(self._live_audio_buffer)
            if len(remainder) % provider_chunk_bytes:
                remainder += b"\x00" * (
                    provider_chunk_bytes - len(remainder) % provider_chunk_bytes
                )
            for offset in range(0, len(remainder), provider_chunk_bytes):
                if not await self._send_live_provider_chunk(
                    user_id,
                    display_name,
                    remainder[offset : offset + provider_chunk_bytes],
                ):
                    completed = False
                    break
        if (flush and completed and Config.VOICE_INPUT_GATE_ENABLED
                and getattr(self, "_input_gate", None) is not None):
            completed = await self._send_live_provider_chunk(
                user_id, display_name, b"", flush_gate=True)
        self._input_gate = None
        self._input_gate_key = None
        self._live_audio_buffer.clear()

        if finalize and completed and not self._is_user_input_blocked(user_id):
            completed = await self.ai_coordinator.finalize_audio_stream()
            if completed:
                self._agent_response_pending = True
                self._response_playback_seen = False
                self._response_pending_since = time.monotonic()
        self._live_input_user_id = None
        self._live_input_user_name = None
        self._audio_processor.reset_state(
            f"live_input_{self.bot_state.current_session_id}"
        )
        return completed

    async def _begin_routed_recording(
        self,
        user: discord.User,
        *,
        via_wake_word: bool,
        method: RecordingMethod,
    ) -> bool:
        if self._is_user_input_blocked(user.id):
            return False
        old_floor_owner = self.conversation_router.floor_owner_id
        disposition = self.conversation_router.route_speech(
            user.id,
            via_wake_word=via_wake_word,
            agent_speaking=self._is_agent_speaking(),
        )
        self.ai_coordinator.observe_input_event(
            "input.route", user.id, disposition=disposition.value
        )
        if disposition == TurnDisposition.IGNORE:
            return False
        if (self.conversation_router.cross_user_wake_required
                and old_floor_owner is not None and old_floor_owner != user.id):
            self._invalidate_floor_input(old_floor_owner)
            await self.bot_state.remove_active_participant(old_floor_owner)
            self.ai_coordinator.observe_input_event(
                "input.floor.changed", user.id, reason="explicit_wake_takeover"
            )

        logger.info(
            "Routing speech for user %s: method=%s disposition=%s playback=%s.",
            user.id, method.name, disposition.value,
            self.audio_playback_manager.get_current_playing_response_id(),
        )
        await self.bot_state.add_active_participant(user.id)
        if disposition == TurnDisposition.BARGE_IN:
            await self._interrupt_ongoing_playback()

        self._set_current_turn_context(user, disposition)
        await self.bot_state.start_recording(user, method)
        live_input = await self._begin_live_audio_input(user)
        if isinstance(self._audio_sink, ManualControlSink):
            client_vad = (live_input and
                          self.ai_coordinator.supports_client_vad_streaming() is True)
            vad_options = {"grace_period_ms": 200} if client_vad else {}
            self._audio_sink.enable_vad(
                True,
                silence_timeout_ms=(
                    Config.GEMINI_LOCAL_VAD_SILENCE_MS if client_vad
                    else Config.LIVE_INPUT_SILENCE_TIMEOUT_MS if live_input else None
                ),
                **vad_options,
            )
            self._audio_sink.update_session_id()
        return True

    async def on_vad_speech_end(
        self, audio_data: bytes, *, user_id: Optional[int] = None,
        input_generation: Optional[int] = None, session_id: Optional[int] = None,
    ) -> None:
        async with self._action_lock:
            if (
                (user_id is not None and user_id != self._current_turn_user_id)
                or (input_generation is not None
                    and input_generation != self._user_input_generation(user_id))
                or (session_id is not None and session_id != self.bot_state.current_session_id)
            ):
                return
            if (
                self.bot_state.current_state == BotStateEnum.RECORDING
                and self._live_input_active
            ):
                client_vad = self.ai_coordinator.supports_client_vad_streaming() is True
                logger.info(
                    "Local VAD closed realtime input after %d ms "
                    "of silence for guild %s.",
                    Config.GEMINI_LOCAL_VAD_SILENCE_MS if client_vad
                    else Config.LIVE_INPUT_SILENCE_TIMEOUT_MS,
                    self.guild.id,
                )
                await self._finish_live_audio_input(finalize=True, flush=True)
                await self.bot_state.stop_recording()
                # Both short manual endpointing and the server-VAD safety cap
                # close this input only. Pending tool work or playback can still
                # be active; only the independent bilateral-idle timer releases
                # the shared floor after the conversation actually goes quiet.
                self.conversation_router.touch()
                return

            if (
                self.bot_state.current_state != BotStateEnum.RECORDING
                or self.bot_state.recording_method
                not in {RecordingMethod.WakeWord, RecordingMethod.Conversation}
            ):
                return

            logger.info("VAD detected end of speech. Transitioning to STANDBY.")
            # No end recording cue - proceed directly to processing

            # Start processing the audio in a background task.
            self._handle_finished_recording(audio_data)
            await self.bot_state.stop_recording()

    # --- New Core Logic Methods ---
    def _handle_finished_recording(self, audio_data: bytes) -> None:
        """
        Creates a background task to process finished recording audio.

        This helper method consolidates the common logic used by both
        push-to-talk and wake word triggered recordings.
        """
        if audio_data:
            if self._current_turn_disposition == TurnDisposition.HOLD:
                if self._current_turn_user_id is None:
                    logger.warning("Dropping held turn without a routed user.")
                    return
                self.conversation_router.enqueue_held_turn(
                    HeldTurn(
                        user_id=self._current_turn_user_id,
                        display_name=self._current_turn_user_name
                        or str(self._current_turn_user_id),
                        audio_data=audio_data,
                        speech_started_at=self._current_turn_started_at,
                    )
                )
                return
            logger.info(f"Creating audio processing task for {len(audio_data)} bytes.")
            task = asyncio.create_task(
                self._process_manual_audio_task(
                    audio_data,
                    user_id=self._current_turn_user_id,
                    display_name=self._current_turn_user_name,
                )
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

    async def _process_manual_audio_task(
        self,
        audio_data: bytes,
        user_id: Optional[int] = None,
        display_name: Optional[str] = None,
    ) -> None:
        """Task to process a finished audio recording."""
        # Cross-session corruption fix: Capture session ID at start of processing
        session_id = self.bot_state.current_session_id
        input_generation = self._user_input_generation(user_id)

        try:
            # Get the audio format required by the active AI service
            target_format = self.ai_coordinator.get_processing_audio_format()
            if not target_format:
                logger.error(
                    f"Could not determine target audio format for guild {self.guild.id}. Aborting processing."
                )
                await self._safe_enter_error_state(session_id)
                return
            target_frame_rate, target_channels = target_format

            # Convert the raw audio to the required format using cached processor
            target_format = AudioFormat(
                sample_rate=target_frame_rate,
                channels=target_channels,
                sample_width=Config.SAMPLE_WIDTH,
            )
            processed_audio = await self._audio_processor.convert(
                source_format=DISCORD_FORMAT,
                target_format=target_format,
                audio_data=audio_data,
                strategy=ProcessingStrategy.QUALITY,
            )

            if self.bot_state.current_session_id != session_id:
                logger.info("Discarding audio converted after its turn was superseded.")
                return
            if (
                user_id is None or self._is_user_input_blocked(user_id)
                or input_generation != self._user_input_generation(user_id)
            ):
                return
            if not await self.ai_coordinator.send_audio_turn(
                processed_audio, user_id=user_id, display_name=display_name
            ):
                await self._safe_enter_error_state(session_id)
            else:
                self._agent_response_pending = True
                self._response_playback_seen = False
                self._response_pending_since = time.monotonic()
                self.conversation_router.touch()
        except asyncio.CancelledError:
            logger.info(
                f"Manual audio processing task cancelled for guild {self.guild.id}."
            )
        except (openai.APIError, gemini_errors.APIError) as e:
            logger.error(
                f"AI service API error in manual audio task: {e}", exc_info=True
            )
            await self._safe_enter_error_state(session_id)
        except Exception as e:
            logger.error(f"Unexpected error in manual audio task: {e}", exc_info=True)
            await self._safe_enter_error_state(session_id)

    async def _safe_enter_error_state(self, original_session_id: int) -> None:
        """
        Safely enter error state only if the session hasn't changed.

        This prevents background tasks from corrupting unrelated new sessions.
        Only enters error state if we're still processing the same session
        that this background task was created for.

        Args:
            original_session_id: The session ID when this background task started
        """
        async with self._action_lock:
            if self.bot_state.current_session_id == original_session_id:
                logger.info(
                    f"Background task entering error state for session {original_session_id}"
                )
                await self.bot_state.enter_connection_error_state()
            else:
                logger.info(
                    f"Background task from session {original_session_id} failed, but current session is "
                    f"{self.bot_state.current_session_id}. Not entering error state to avoid corruption."
                )

    async def _initialize_sink(self) -> None:
        """Creates and starts the audio sink for voice processing."""
        consented_users = self.bot_state.get_consented_user_ids()
        if (Config.DESKTOP_VOICE_ALWAYS_FORWARD
                and self.bot_state.active_ai_provider_name == 'desktop_voice'):
            manager = self.ai_coordinator.active_ai_service_manager
            if manager.processing_audio_format != (48000, 2):
                raise ValueError('Continuous desktop bridge requires 48 kHz stereo.')

            async def forward(audio):
                # Never let a provider change route this mixed stream to an API.
                if manager is not self.ai_coordinator.active_ai_service_manager:
                    return False
                return await manager.send_audio_chunk(audio)

            self._audio_sink = ContinuousAudioSink(consented_users, forward)
            self.voice_connection.start_listening(self._audio_sink)
            logger.info('Continuous desktop forwarding enabled: all admitted humans, '
                        'no wake/stop detection, no input noise gate.')
            return
        self._audio_sink = ManualControlSink(
            bot_state=self.bot_state,
            initial_consented_users=consented_users,
            on_wake_word_detected=self.on_wake_word_detected,
            on_vad_speech_end=self.on_vad_speech_end,
            action_lock=self._action_lock,
            on_active_speech_detected=self.on_active_speech_detected,
            on_recording_audio_chunk=self.on_recording_audio_chunk,
            on_stop_word_detected=self.on_stop_word_detected,
        )
        self.voice_connection.start_listening(self._audio_sink)
        logger.info("Initialized ManualControlSink for voice processing")

    async def _conversation_monitor_loop(self) -> None:
        """Drain held turns and release the upload gate after 10s silence."""
        try:
            while True:
                await asyncio.sleep(0.1)
                playing_response = self.audio_playback_manager.get_current_playing_response_id()
                playing = bool(playing_response)
                # Native server VAD needs the current owner's continuous audio
                # through playback and the next utterance. Closing here forces
                # local onset detection again and drops the start of follow-ups.
                if (playing and self._live_input_active
                        and self.ai_coordinator.supports_server_vad_streaming() is not True):
                    input_revision = self._live_stream_revision
                    async with self._action_lock:
                        current_playback = self.audio_playback_manager.get_current_playing_response_id()
                        playing = bool(current_playback)
                        if (
                            self._live_input_active
                            and input_revision == self._live_stream_revision
                            and current_playback == playing_response
                            and playing
                        ):
                            if isinstance(self._audio_sink, ManualControlSink):
                                try:
                                    self._audio_sink.stop_and_get_audio()
                                except SessionConsistencyError:
                                    pass
                            await self._finish_live_audio_input(
                                finalize=False, flush=False
                            )
                            if self.bot_state.current_state == BotStateEnum.RECORDING:
                                await self.bot_state.stop_recording()
                            self._agent_response_pending = True
                if playing:
                    self._response_playback_seen = True
                    self.conversation_router.touch()
                elif self._agent_response_pending and self._response_playback_seen:
                    self._agent_response_pending = False
                    self._response_playback_seen = False
                    self._response_pending_since = 0.0
                    self.conversation_router.touch()
                elif (
                    self._agent_response_pending
                    and self._response_pending_since
                    and time.monotonic() - self._response_pending_since > 30.0
                ):
                    logger.warning(
                        "Provider response for guild %s produced no observable playback "
                        "within 30 seconds; releasing the pending gate.",
                        self.guild.id,
                    )
                    self._agent_response_pending = False
                    self._response_pending_since = 0.0

                busy = bool(
                    playing
                    or self._agent_response_pending
                    or self.bot_state.current_state == BotStateEnum.RECORDING
                )

                if not busy and self.conversation_router.has_held_turns:
                    turn = self.conversation_router.pop_held_turn()
                    if turn:
                        task = asyncio.create_task(
                            self._process_manual_audio_task(
                                turn.audio_data,
                                user_id=turn.user_id,
                                display_name=turn.display_name,
                            )
                        )
                        self._background_tasks.add(task)
                        task.add_done_callback(self._background_tasks.discard)
                        continue

                async with self._action_lock:
                    busy = bool(self._is_agent_speaking()
                        or self.bot_state.current_state == BotStateEnum.RECORDING)
                    if self.conversation_router.release_if_idle(busy=busy):
                        await self.bot_state.clear_active_participants()
                        await self.ai_coordinator.end_conversation(reason="idle")
                        logger.info(
                            "Shared conversation for guild %s ended after %.1fs silence; "
                            "provider connection remains warm.",
                            self.guild.id,
                            Config.CONVERSATION_IDLE_TIMEOUT_SECONDS,
                        )
        except asyncio.CancelledError:
            return

    async def initialize_session(self, ctx: commands.Context) -> bool:
        """
        Handles the full connection logic when a user issues a connect command.
        """
        if ctx.author.voice is None:
            await ctx.send("You are not connected to a voice channel.")
            return False

        if not await self.ai_coordinator.ensure_connected(
            ctx, on_connect=self._on_ai_connect, on_disconnect=self._on_ai_disconnect
        ):
            return False

        voice_channel = ctx.author.voice.channel
        if not await self.voice_connection.connect_to_channel(voice_channel):
            await ctx.send("Failed to connect to the voice channel.")
            await self.bot_state.enter_connection_error_state()
            return False

        if Config.VOICE_ACCESS_MODE == "implicit":
            for member in voice_channel.members:
                if not member.bot:
                    await self.bot_state.grant_consent(member.id)

        if not await self.ui_manager.create(ctx.channel):
            await self.bot_state.reset_to_idle()
            return False

        await self._initialize_sink()
        await self.bot_state.set_state(BotStateEnum.STANDBY)

        await self.start_background_tasks()
        logger.info(
            f"Connect command successful for guild {self.guild.id}. Bot is in STANDBY."
        )
        return True

    async def set_provider(self, ctx: commands.Context, provider_name: str) -> None:
        """
        Handles the logic of the 'set' command.
        """
        provider_name = provider_name.lower()
        if provider_name not in self.ai_coordinator.ai_service_factories:
            valid_providers = ", ".join(self.ai_coordinator.ai_service_factories.keys())
            await ctx.send(
                f"Invalid provider name '{provider_name}'. Valid options are: {valid_providers}."
            )
            return

        # Provider switching race fix: Ensure atomic provider changes
        async with self._action_lock:
            if await self.ai_coordinator.switch_provider(
                provider_name,
                ctx,
                self.voice_connection.is_connected(),
                on_connect=self._on_ai_connect,
                on_disconnect=self._on_ai_disconnect,
            ):
                if (Config.DESKTOP_VOICE_ALWAYS_FORWARD
                        and self.voice_connection.is_connected()
                        and (provider_name == 'desktop_voice'
                             or isinstance(self._audio_sink, ContinuousAudioSink))):
                    self.voice_connection.stop_listening()
                    if self._audio_sink:
                        self._audio_sink.cleanup()
                    await self._initialize_sink()
                await ctx.send(f"AI provider switched to '{provider_name.upper()}'.")
