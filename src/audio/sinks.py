"""
Audio Sink Implementations for voice audio processing.

This module provides the ManualControlSink class for processing audio from
Discord voice channels. The sink handles wake word detection and voice
activity detection for push-to-talk and wake-word triggered interactions.
"""

import asyncio
import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable, Dict, Optional, Set

import discord
import numpy as np
import openwakeword
import webrtcvad
from discord.ext import voice_recv
from openwakeword.model import Model

from src.audio.processing import (
    UnifiedAudioProcessor,
    AudioFormat,
    DISCORD_FORMAT,
    WAKE_WORD_FORMAT,
    VAD_FORMAT,
    ProcessingStrategy,
)
from src.audio.wakeword import SherpaWakeWordModel
from src.audio.raw_capture import claim_raw_capture
from src.observability import get_observer
from src.bot.state import BotState, BotStateEnum, RecordingMethod
from src.config.config import Config
from src.exceptions import SessionConsistencyError
from src.utils.logger import get_logger

logger = get_logger(__name__)

# Detect OpenWakeWord version for API compatibility
OWW_VERSION = getattr(openwakeword, "__version__", "0.4.0")
if "0.4" in OWW_VERSION:
    OWW_PARAM_NAME = "wakeword_model_paths"
elif "0.6" in OWW_VERSION:
    OWW_PARAM_NAME = "wakeword_models"
else:
    # Default to newer API for future versions
    OWW_PARAM_NAME = "wakeword_models"

logger.info(
    f"OpenWakeWord version {OWW_VERSION} detected, using parameter '{OWW_PARAM_NAME}'"
)


class CleanupMetrics:
    """Tracks cleanup operations and race condition prevention."""

    def __init__(self):
        self.comprehensive_cleanups = 0
        self.race_conditions_prevented = 0
        self.cleanup_errors = 0
        self.zero_byte_recordings_prevented = 0
        self.successful_interactions = 0
        self.last_successful_cleanup = time.time()

    def record_comprehensive_cleanup(self, stats: Dict[str, int]):
        """Record results of comprehensive cleanup operation."""
        self.comprehensive_cleanups += 1
        self.cleanup_errors += stats.get("errors", 0)
        self.last_successful_cleanup = time.time()

        if stats.get("buffers_cleared", 0) > 0:
            logger.info(f"Comprehensive cleanup cleared stale data: {stats}")

    def record_race_condition_prevention(self, captured_id, current_id):
        """Track when race condition would have occurred but was prevented."""
        if captured_id != current_id:
            self.race_conditions_prevented += 1
            logger.warning(
                f"Race condition prevented: captured={captured_id}, current={current_id}"
            )

    def record_session_id_mismatch(self, expected_session_id, actual_session_id):
        """Track when session ID mismatch prevents cross-session contamination."""
        if expected_session_id != actual_session_id:
            self.race_conditions_prevented += 1
            logger.warning(
                f"Session ID mismatch prevented contamination: expected={expected_session_id}, actual={actual_session_id}"
            )

    def record_successful_interaction(self, audio_size: int):
        """Track successful audio interactions."""
        self.successful_interactions += 1
        if audio_size > 0:
            logger.debug(f"Successful interaction recorded: {audio_size} bytes")

    def export_health_report(self) -> Dict[str, Any]:
        """Export comprehensive health metrics for monitoring."""
        return {
            "total_cleanups": self.comprehensive_cleanups,
            "race_conditions_prevented": self.race_conditions_prevented,
            "cleanup_error_rate": self.cleanup_errors
            / max(1, self.comprehensive_cleanups),
            "successful_interactions": self.successful_interactions,
            "time_since_last_cleanup": time.time() - self.last_successful_cleanup,
            "health_status": "healthy" if self.cleanup_errors == 0 else "degraded",
        }


class AudioSink(voice_recv.AudioSink, ABC):
    """
    Abstract base class for all custom audio sinks.

    Defines a common interface for dynamic user management and resource cleanup
    that all operational-mode-specific sinks must implement.
    """

    @abstractmethod
    def add_user(self, user_id: int) -> None:
        """
        Dynamically starts processing audio for a new user.

        Args:
            user_id: The ID of the user to start processing.
        """
        raise NotImplementedError

    @abstractmethod
    def remove_user(self, user_id: int) -> None:
        """
        Dynamically stops processing audio for a user.

        Args:
            user_id: The ID of the user to stop processing.
        """
        raise NotImplementedError

    @abstractmethod
    def cleanup(self) -> None:
        """
        Release all underlying resources held by the sink.

        This must be called to ensure no memory leaks or orphaned tasks.
        """
        raise NotImplementedError


class VADAnalyzer:
    """
    A stateful Voice Activity Detection (VAD) analyzer for detecting speech boundaries.

    This class implements a robust VAD algorithm that tracks speech and silence periods
    to reliably detect when a user has finished speaking. It uses configurable thresholds
    to avoid false positives from short pauses or background noise.

    Threading Model:
    - VAD processing is called from audio sink write() methods (Discord's audio thread)
    - Callbacks are scheduled on the main event loop using call_soon_threadsafe()
    - This ensures thread-safe communication between audio processing and async logic

    State Machine:
    - SILENT: Initial state, waiting for sustained speech
    - SPEAKING: Speech detected, monitoring for end-of-speech silence
    - TRIGGERED: Speech end detected, callback scheduled (terminal state)
    """

    def __init__(
        self,
        on_speech_end: Callable[[], Awaitable[None]],
        sample_rate: int,
        frame_duration_ms: int,
        min_speech_duration_ms: int,
        silence_timeout_ms: int,
        grace_period_ms: int,
        loop: asyncio.AbstractEventLoop,
    ):
        self._on_speech_end = on_speech_end
        self._loop = loop
        self._sample_rate = sample_rate
        self._frame_duration_ms = frame_duration_ms
        self._min_speech_frames = min_speech_duration_ms // frame_duration_ms
        self._silence_frames_timeout = silence_timeout_ms // frame_duration_ms
        self._grace_period_frames = grace_period_ms // frame_duration_ms
        self._frame_size = (sample_rate * frame_duration_ms) // 1000
        self._frame_bytes = self._frame_size * Config.SAMPLE_WIDTH
        self._vad = webrtcvad.Vad(Config.VAD_AGGRESSIVENESS)
        self._speech_frame_count = 0
        self._silence_frame_count = 0
        self._frames_processed = 0
        self._is_speech = False
        self._triggered = False

    def process(self, frame: bytes):
        if len(frame) != self._frame_bytes:
            logger.debug(
                f"Invalid VAD frame size: expected {self._frame_bytes}, got {len(frame)}"
            )
            return
        self._frames_processed += 1
        is_speech = self._vad.is_speech(frame, self._sample_rate)
        if not self._is_speech:
            if is_speech:
                self._speech_frame_count += 1
                if self._speech_frame_count >= self._min_speech_frames:
                    self._is_speech = True
                    self._speech_frame_count = 0
                    self._silence_frame_count = 0
            else:
                self._speech_frame_count = 0
                # A wake phrase can end before the realtime input gate opens.
                # In that case this analyzer may never observe a post-wake
                # speech frame. Treat a full timeout of initial silence as an
                # empty turn instead of leaving the bot in RECORDING forever.
                if (
                    not self._triggered
                    and self._frames_processed >= self._silence_frames_timeout
                    and self._frames_processed > self._grace_period_frames
                ):
                    self._triggered = True
                    self._loop.call_soon_threadsafe(
                        asyncio.create_task, self._on_speech_end()
                    )
        else:
            if not is_speech:
                self._silence_frame_count += 1
                if self._silence_frame_count >= self._silence_frames_timeout:
                    if (
                        not self._triggered
                        and self._frames_processed > self._grace_period_frames
                    ):
                        self._triggered = True
                        self._loop.call_soon_threadsafe(
                            asyncio.create_task, self._on_speech_end()
                        )
            else:
                self._silence_frame_count = 0

    def reset(self):
        self._speech_frame_count = 0
        self._silence_frame_count = 0
        self._frames_processed = 0
        self._is_speech = False
        self._triggered = False


class ManualControlSink(AudioSink):
    """
    An audio sink for request/response voice interaction.

    OVERVIEW:
    This sink implements a sophisticated audio processor that alternates
    between listening for wake words from all users and recording commands from
    a single authority user. It coordinates wake word detection, voice activity
    detection, and audio buffering for command processing.

    OPERATIONAL STATES:

    1. STANDBY MODE (bot_state.current_state == BotStateEnum.STANDBY):
       - Continuous parallel wake word detection for all consented users
       - Each user gets dedicated openWakeWord model instance
       - Audio processing: 48kHz stereo -> 16kHz mono -> 80ms chunks for wake word models
       - When wake word detected: triggers callback to GuildSession, clears buffers

    2. RECORDING MODE (bot_state.current_state == BotStateEnum.RECORDING):
       - Single-user audio capture from authority_user
       - Optional VAD (Voice Activity Detection) for wake word triggered recordings
       - Audio buffering: Raw PCM accumulated in _authority_buffer
       - VAD processing: 48kHz stereo -> 16kHz mono -> configurable frame analysis
       - When speech ends: triggers callback with captured audio data

    AUDIO PROCESSING PIPELINE:

    Wake Word Path (STANDBY):
    1. Raw PCM (48kHz, 16-bit, stereo) -> _user_audio_buffers[user_id]
    2. Resample to 16kHz mono using audioop.ratecv (maintains state per user)
    3. Buffer resampled audio in _ww_resampled_buffers[user_id]
    4. Process in 1280-byte chunks (80ms of 16kHz mono) through openWakeWord models
    5. On detection: reset model state, clear buffers, schedule callback

    VAD Path (RECORDING with wake word trigger):
    1. Raw PCM -> _authority_buffer (for final output) + _vad_raw_buffer (for VAD)
    2. Resample VAD stream to 16kHz mono for webrtcvad compatibility
    3. Process in configurable frame sizes (10ms, 20ms, or 30ms)
    4. VADAnalyzer tracks speech/silence patterns with configurable thresholds
    5. On speech end: callback with buffered audio, reset all state

    THREADING MODEL:
    - write() method: Discord audio thread, must be non-blocking and thread-safe
    - Wake word processing: Synchronous in write() method (fast, <1ms per chunk)
    - VAD processing: Scheduled on event loop via run_coroutine_threadsafe()
    - _vad_monitor_loop(): Async task that injects silence when no audio received
    - Callbacks: Scheduled on main event loop for integration with GuildSession

    CONCURRENCY & SAFETY:
    - _vad_lock: Protects VAD processing from race conditions between real audio and silence injection
    - Atomic state capture in write() method prevents race conditions during state transitions
    - Thread-safe callback scheduling using run_coroutine_threadsafe()
    - Graceful cleanup with proper task cancellation and resource deallocation

    BUFFER MANAGEMENT:
    - _user_audio_buffers: Raw 48kHz audio per user for wake word detection
    - _ww_resampled_buffers: 16kHz mono audio per user, ready for wake word models
    - _authority_buffer: Final output buffer containing command audio
    - _vad_raw_buffer + _vad_resampled_buffer: VAD processing pipeline buffers
    - Automatic buffer clearing prevents memory leaks and audio contamination

    ERROR HANDLING:
    - Wake word model initialization failures are logged but don't crash the sink
    - VAD processing errors are isolated and logged
    - Resource cleanup is comprehensive to prevent memory/task leaks
    """

    def __init__(
        self,
        bot_state: BotState,
        initial_consented_users: Set[int],
        on_wake_word_detected: Callable[[discord.User], Awaitable[None]],
        on_vad_speech_end: Callable[[bytes], Awaitable[None]],
        action_lock: asyncio.Lock,
        on_active_speech_detected: Optional[
            Callable[[discord.User], Awaitable[None]]
        ] = None,
        on_recording_audio_chunk: Optional[
            Callable[[discord.User, bytes], Awaitable[None]]
        ] = None,
        on_stop_word_detected: Optional[
            Callable[[discord.User, str], Awaitable[None]]
        ] = None,
    ):
        super().__init__()
        self._bot_state = bot_state
        self._on_wake_word_detected = on_wake_word_detected
        self._on_vad_speech_end = on_vad_speech_end
        self._on_active_speech_detected = on_active_speech_detected
        self._on_recording_audio_chunk = on_recording_audio_chunk
        self._on_stop_word_detected = on_stop_word_detected
        self._input_blocked_users: Set[int] = set()
        self._input_generations: Dict[int, int] = {}
        self._loop = asyncio.get_running_loop()

        # Initialize unified audio processor for real-time processing
        self._audio_processor = UnifiedAudioProcessor()

        self._detectors: Dict[int, Any] = {}
        self._users_with_received_audio: Set[int] = set()
        self._wakeword_users: Dict[int, discord.User] = {}
        self._wakeword_last_audio_at: Dict[int, float] = {}
        self._wakeword_silence_chunks_sent: Dict[int, int] = {}
        self._wakeword_audio_frame_counts: Dict[int, int] = {}
        self._wakeword_model_chunk_counts: Dict[int, int] = {}
        self._wakeword_max_rms: Dict[int, int] = {}
        self._wakeword_max_peak: Dict[int, int] = {}
        self._user_audio_buffers: Dict[int, bytearray] = {}
        self._authority_buffer = bytearray()
        self._vad_raw_buffer = bytearray()
        self._vad_resampled_buffer = bytearray()
        self._ww_resampled_buffers: Dict[int, bytearray] = {}
        self._active_speech_pending: Set[int] = set()
        self._active_speech_preroll: Dict[int, bytearray] = {}
        self._active_speech_raw_buffers: Dict[int, bytearray] = {}
        self._active_speech_resampled_buffers: Dict[int, bytearray] = {}
        self._active_speech_vads: Dict[int, Any] = {}
        self._active_speech_frame_counts: Dict[int, int] = {}
        self._active_speech_gap_counts: Dict[int, int] = {}
        self._active_speech_last_triggered_at: Dict[int, float] = {}
        self._active_speech_latched: Set[int] = set()
        self._active_speech_last_audio_at: Dict[int, float] = {}

        # Thread-safe synchronization primitives for TOCTOU fix
        self._action_lock = action_lock
        self._ww_buffer_locks: Dict[
            int, threading.Lock
        ] = {}  # Per-user wake word buffer protection
        self._vad_flag_lock = threading.Lock()  # Protects VAD flag updates
        self._user_data_lock = (
            threading.Lock()
        )  # CONCURRENCY FIX: Protects all user data dictionaries
        self._is_vad_enabled = False
        self._vad_grace_period_frames = (
            Config.VAD_GRACE_PERIOD_MS // 20
        )  # 20ms per raw discord frame
        self._vad_frames_processed = 0
        self._ww_chunk_size = (
            Config.WAKE_WORD_CHUNK_SIZE
        )  # 80ms of 16kHz, 16-bit, mono audio
        self._wakeword_silence_chunk = np.zeros(
            self._ww_chunk_size // Config.SAMPLE_WIDTH, dtype=np.int16
        )
        self._vad_analyzer: Optional[VADAnalyzer] = None

        # VAD silence injection system - coordinates between real audio and synthetic silence
        self._has_received_audio_for_vad: bool = (
            False  # Flag: real audio received this cycle
        )
        self._vad_monitor_task: Optional[asyncio.Task] = (
            None  # Background silence injection task
        )
        self._silence_chunk_vad = (
            b"\x00" * Config.DISCORD_CHUNK_SIZE
        )  # 20ms of 48kHz stereo 16-bit PCM silence
        self._vad_lock = (
            asyncio.Lock()
        )  # Prevents race conditions between real audio and silence

        # Add cleanup metrics tracking
        self._cleanup_metrics = CleanupMetrics()

        # Session ID tracking for cross-session contamination prevention
        self._session_id_at_creation = self._bot_state.current_session_id
        self._active_session_id = self._session_id_at_creation
        logger.debug(
            f"ManualControlSink created for session {self._session_id_at_creation}"
        )

        self._raw_capture = claim_raw_capture()
        for user_id in initial_consented_users:
            self.add_user(user_id)

        self.start()

    def add_user(self, user_id: int) -> None:
        if user_id in self._detectors:
            return
        logger.info(f"Adding user {user_id} to ManualControlSink detectors.")
        try:
            # CONCURRENCY FIX: Use dedicated lock for all user data operations
            with self._user_data_lock:
                if Config.WAKE_WORD_ENGINE == "paraformer":
                    from src.audio.paraformer import ParaformerWakeWordModel
                    self._detectors[user_id] = ParaformerWakeWordModel(user_id)
                elif Config.WAKE_WORD_ENGINE == "sherpa_onnx":
                    self._detectors[user_id] = SherpaWakeWordModel(
                        model_dir=Config.SHERPA_WAKE_WORD_MODEL_DIR,
                        keywords_file=Config.SHERPA_WAKE_WORD_KEYWORDS_PATH,
                        phrase=Config.WAKE_WORD_PHRASE,
                        keywords_score=Config.SHERPA_WAKE_WORD_SCORE,
                        keywords_threshold=Config.SHERPA_WAKE_WORD_THRESHOLD,
                    )
                else:
                    # Validate wake word model file exists
                    model_path = str(Config.WAKE_WORD_MODEL_PATH)
                    if not Config.WAKE_WORD_MODEL_PATH.exists():
                        raise FileNotFoundError(
                            f"Wake word model file not found: {model_path}"
                        )

                    # Use the appropriate parameter name based on OpenWakeWord version
                    model_kwargs = {OWW_PARAM_NAME: [model_path]}
                    # Only add inference framework settings for openWakeWord 0.6+.
                    if OWW_PARAM_NAME == "wakeword_models":
                        model_kwargs["inference_framework"] = "onnx"
                        model_kwargs["vad_threshold"] = Config.WAKE_WORD_VAD_THRESHOLD

                    self._detectors[user_id] = Model(**model_kwargs)
                self._user_audio_buffers[user_id] = bytearray()
                self._ww_resampled_buffers[user_id] = bytearray()
                self._wakeword_audio_frame_counts[user_id] = 0
                self._wakeword_model_chunk_counts[user_id] = 0
                self._wakeword_max_rms[user_id] = 0
                self._wakeword_max_peak[user_id] = 0
                self._ww_buffer_locks[user_id] = (
                    threading.Lock()
                )  # Create per-user lock
                self._active_speech_raw_buffers[user_id] = bytearray()
                self._active_speech_preroll[user_id] = bytearray()
                self._active_speech_resampled_buffers[user_id] = bytearray()
                self._active_speech_vads[user_id] = webrtcvad.Vad(
                    Config.ACTIVE_SPEECH_VAD_AGGRESSIVENESS
                )
                self._active_speech_frame_counts[user_id] = 0
                self._active_speech_gap_counts[user_id] = 0
        except Exception as e:
            logger.error(
                f"Failed to initialize wake word model for user {user_id}: {e}",
                exc_info=True,
            )

    def remove_user(self, user_id: int) -> None:
        if user_id not in self._detectors:
            return
        logger.info(f"Removing user {user_id} from ManualControlSink detectors.")

        # CONCURRENCY FIX: Use dedicated lock for all user data operations
        with self._user_data_lock:
            # Clean up all user data atomically
            self._users_with_received_audio.discard(user_id)
            self._input_blocked_users.discard(user_id)
            self._input_generations[user_id] = self.get_user_input_generation(user_id) + 1
            self._wakeword_users.pop(user_id, None)
            self._wakeword_last_audio_at.pop(user_id, None)
            self._wakeword_silence_chunks_sent.pop(user_id, None)
            self._wakeword_audio_frame_counts.pop(user_id, None)
            self._wakeword_model_chunk_counts.pop(user_id, None)
            self._wakeword_max_rms.pop(user_id, None)
            self._wakeword_max_peak.pop(user_id, None)
            self._detectors.pop(user_id, None)
            self._user_audio_buffers.pop(user_id, None)
            self._ww_resampled_buffers.pop(user_id, None)
            self._ww_buffer_locks.pop(
                user_id, None
            )  # Safe to delete after data cleanup
            self._active_speech_raw_buffers.pop(user_id, None)
            self._active_speech_preroll.pop(user_id, None)
            self._active_speech_resampled_buffers.pop(user_id, None)
            self._active_speech_vads.pop(user_id, None)
            self._active_speech_frame_counts.pop(user_id, None)
            self._active_speech_gap_counts.pop(user_id, None)
            self._active_speech_last_triggered_at.pop(user_id, None)
            self._active_speech_latched.discard(user_id)
            self._active_speech_last_audio_at.pop(user_id, None)

            # Reset unified processor state for this user
            wake_word_state_key = f"wake_word_user_{user_id}"
            self._audio_processor.reset_state(wake_word_state_key)
            self._audio_processor.reset_state(f"active_speech_user_{user_id}")

    def cleanup(self) -> None:
        logger.info("Cleaning up ManualControlSink.")
        capture = getattr(self, "_raw_capture", None)
        if capture is not None:
            capture.close()
        if self._vad_monitor_task and not self._vad_monitor_task.done():
            self._vad_monitor_task.cancel()
        # Destroy VAD analyzer to ensure clean shutdown
        self._vad_analyzer = None
        self._detectors.clear()
        self._input_blocked_users.clear()
        self._input_generations.clear()
        self._users_with_received_audio.clear()
        self._wakeword_users.clear()
        self._wakeword_last_audio_at.clear()
        self._wakeword_silence_chunks_sent.clear()
        self._user_audio_buffers.clear()
        self._authority_buffer.clear()
        self._active_speech_pending.clear()
        self._active_speech_preroll.clear()
        self._active_speech_raw_buffers.clear()
        self._active_speech_resampled_buffers.clear()
        self._active_speech_vads.clear()
        self._active_speech_frame_counts.clear()
        self._active_speech_gap_counts.clear()
        self._active_speech_last_triggered_at.clear()
        self._active_speech_latched.clear()
        self._active_speech_last_audio_at.clear()

    def start(self):
        """
        Initializes and starts the VAD monitor background task.

        The VAD monitor is essential for proper speech end detection as it
        ensures continuous VAD processing even during audio gaps from Discord.
        """
        if self._vad_monitor_task is None or self._vad_monitor_task.done():
            self._vad_monitor_task = asyncio.create_task(self._vad_monitor_loop())
            logger.info("ManualControlSink VAD monitor task started.")

    def _clear_wake_word_buffers(
        self, user_ids: Optional[Set[int]] = None
    ) -> Dict[str, int]:
        """Reset wake-word state for selected users, or every user when omitted."""
        cleanup_stats = {
            "models_reset": 0,
            "buffers_cleared": 0,
            "states_reset": 0,
            "errors": 0,
        }

        with self._user_data_lock:
            targets = (
                set(self._detectors)
                if user_ids is None
                else set(user_ids).intersection(self._detectors)
            )
            for user_id in targets:
                model = self._detectors[user_id]
                try:
                    model.reset()
                    cleanup_stats["models_reset"] += 1
                    logger.debug(f"Reset wake word model for user {user_id}")
                except Exception as e:
                    cleanup_stats["errors"] += 1
                    logger.error(
                        f"Failed to reset wake word model for user {user_id}: {e}"
                    )

            for user_id in targets:
                buffer_size = len(self._user_audio_buffers[user_id])
                self._user_audio_buffers[user_id].clear()
                cleanup_stats["buffers_cleared"] += 1
                if buffer_size > 0:
                    logger.debug(
                        f"Cleared {buffer_size} bytes from user {user_id} audio buffer"
                    )

            for user_id in targets:
                buffer_size = len(self._ww_resampled_buffers[user_id])
                self._ww_resampled_buffers[user_id].clear()
                if buffer_size > 0:
                    logger.debug(
                        f"Cleared {buffer_size} bytes from user {user_id} resampled buffer"
                    )

            for user_id in targets:
                wake_word_state_key = f"wake_word_user_{user_id}"
                self._audio_processor.reset_state(wake_word_state_key)
                cleanup_stats["states_reset"] += 1

                self._wakeword_last_audio_at.pop(user_id, None)
                self._wakeword_silence_chunks_sent.pop(user_id, None)
                self._wakeword_audio_frame_counts[user_id] = 0
                self._wakeword_model_chunk_counts[user_id] = 0
                self._wakeword_max_rms[user_id] = 0
                self._wakeword_max_peak[user_id] = 0

        logger.info(
            "Wake word cleanup completed for %s user(s): %s",
            len(targets),
            cleanup_stats,
        )
        return cleanup_stats

    def _clear_all_wake_word_buffers(self) -> None:
        """Reset every wake-word detector and its buffered audio."""
        cleanup_stats = self._clear_wake_word_buffers()
        self._cleanup_metrics.record_comprehensive_cleanup(cleanup_stats)

    def enable_vad(
        self, enabled: bool, *, silence_timeout_ms: Optional[int] = None,
        grace_period_ms: Optional[int] = None,
    ):
        """
        Controls VAD processing for the current recording session.

        VAD is only used for wake word triggered recordings to detect natural
        speech end. Push-to-talk recordings don't use VAD since the user
        explicitly controls the recording duration.

        Creates a fresh VAD analyzer for each recording session to ensure
        completely clean state and eliminate any timing or state corruption issues.
        """
        self._is_vad_enabled = enabled
        self._vad_generation = getattr(self, "_vad_generation", 0) + 1
        self._has_received_audio_for_vad = False  # Reset on state change

        if enabled:
            # CREATE fresh VAD analyzer for this recording session
            vad_generation = self._vad_generation
            user_id = self._bot_state.authority_user_id
            input_generation = self.get_user_input_generation(user_id)
            session_id = self._bot_state.current_session_id

            async def finish_current_vad():
                await self._handle_vad_speech_end(
                    vad_generation=vad_generation, user_id=user_id,
                    input_generation=input_generation, session_id=session_id,
                )

            self._vad_analyzer = VADAnalyzer(
                on_speech_end=finish_current_vad,
                sample_rate=Config.VAD_SAMPLE_RATE,
                frame_duration_ms=Config.VAD_FRAME_DURATION_MS,
                min_speech_duration_ms=Config.VAD_MIN_SPEECH_DURATION_MS,
                silence_timeout_ms=(
                    Config.VAD_SILENCE_TIMEOUT_MS
                    if silence_timeout_ms is None
                    else silence_timeout_ms
                ),
                grace_period_ms=(Config.VAD_GRACE_PERIOD_MS
                                 if grace_period_ms is None else grace_period_ms),
                loop=self._loop,
            )
        else:
            # DESTROY VAD analyzer when disabled
            self._vad_analyzer = None

        # Clear VAD buffers for fresh start
        self._vad_raw_buffer.clear()
        self._vad_resampled_buffer.clear()

    def update_session_id(self) -> None:
        """
        Updates the active session ID to match the current bot state session.

        This should be called when a new recording starts to ensure the sink
        is synchronized with the current session and prevent cross-session contamination.
        """
        new_session_id = self._bot_state.current_session_id
        if new_session_id != self._active_session_id:
            logger.info(
                f"Updating ManualControlSink session ID: {self._active_session_id} -> {new_session_id}"
            )
            self._active_session_id = new_session_id

    def stop_and_get_audio(self) -> bytes:
        """
        Stop PTT recording and return captured audio with race condition protection.

        Uses comprehensive cleanup to ensure complete clean state for next interaction.

        Returns:
            bytes: The captured audio data in Discord's native PCM format
        """
        # SESSION ID VALIDATION: Fail-fast on cross-session contamination
        current_session_id = self._bot_state.current_session_id
        if current_session_id != self._active_session_id:
            self._cleanup_metrics.record_session_id_mismatch(
                self._active_session_id, current_session_id
            )
            raise SessionConsistencyError(
                f"Session ID mismatch in stop_and_get_audio: "
                f"sink={self._active_session_id}, state={current_session_id}"
            )

        # Capture authority user ID for logging purposes
        captured_authority_id = self._bot_state.authority_user_id

        self.enable_vad(False)

        audio_data = bytes(self._authority_buffer)
        self._authority_buffer.clear()

        # Clear VAD-specific state
        self._vad_raw_buffer.clear()
        self._vad_resampled_buffer.clear()
        # Destroy VAD analyzer to ensure fresh state for next session
        self._vad_analyzer = None

        if captured_authority_id is not None:
            self._clear_wake_word_buffers({captured_authority_id})

        # Enhanced logging for debugging
        logger.debug(
            f"PTT recording stopped: user_id={captured_authority_id}, "
            f"audio_size={len(audio_data)}, buffers_cleared=current_user"
        )

        # Track successful interaction
        self._cleanup_metrics.record_successful_interaction(len(audio_data))

        return audio_data

    async def validate_sink_health(self) -> Dict[str, Any]:
        """
        Comprehensive health check for the audio sink.

        Returns:
            Dict containing health status and metrics
        """
        health_report = {
            "timestamp": time.time(),
            "overall_status": "healthy",
            "issues": [],
        }

        try:
            # Check buffer states
            authority_buffer_size = len(self._authority_buffer)
            if authority_buffer_size > 1024 * 1024:  # 1MB threshold
                health_report["issues"].append(
                    f"Authority buffer unusually large: {authority_buffer_size} bytes"
                )
                health_report["overall_status"] = "warning"

            # Check wake word model states
            corrupted_models = []
            for user_id, model in self._detectors.items():
                try:
                    # Basic sanity check - ensure model has required methods
                    if not hasattr(model, "predict") or not hasattr(model, "reset"):
                        corrupted_models.append(user_id)
                except Exception as e:
                    corrupted_models.append(user_id)
                    logger.error(
                        f"Wake word model health check failed for user {user_id}: {e}"
                    )

            if corrupted_models:
                health_report["issues"].append(
                    f"Corrupted wake word models: {corrupted_models}"
                )
                health_report["overall_status"] = "critical"

            # Check background task status
            if self._vad_monitor_task and self._vad_monitor_task.done():
                exception = self._vad_monitor_task.exception()
                if exception:
                    health_report["issues"].append(
                        f"VAD monitor task failed: {exception}"
                    )
                    health_report["overall_status"] = "critical"

            # Add cleanup metrics
            health_report["metrics"] = self._cleanup_metrics.export_health_report()

            return health_report

        except Exception as e:
            logger.error(f"Sink health check failed: {e}")
            health_report["overall_status"] = "critical"
            health_report["issues"].append(f"Health check exception: {str(e)}")
            return health_report

    def wants_opus(self) -> bool:
        """
        Discord audio format preference.

        Returns False to request raw PCM data instead of Opus-encoded audio.
        PCM is required for real-time audio processing like wake word detection
        and VAD analysis.
        """
        return False

    async def _vad_monitor_loop(self):
        """
        Background task that ensures VAD continues processing during audio gaps.

        PURPOSE:
        VAD requires continuous audio input to detect silence periods that indicate
        end-of-speech. Discord only sends audio when users are actively speaking,
        so we must inject synthetic silence during gaps to maintain VAD timing.

        OPERATION:
        - Runs every 20ms (synchronized with Discord's audio frame timing)
        - Only active when VAD is enabled (_is_vad_enabled = True)
        - Injects silence only when no real audio was received in the last interval
        - Uses _has_received_audio_for_vad flag to coordinate with write() method

        This approach ensures VAD can detect the transition from speech to silence
        that indicates the user has finished their command.
        """
        try:
            while True:
                # Check every 20ms, the duration of one Discord audio frame
                await asyncio.sleep(0.02)

                self._finalize_wake_words_during_audio_gaps()

                if not self._is_vad_enabled:
                    continue

                # VAD flag race fix: Thread-safe flag check and reset
                with self._vad_flag_lock:
                    has_received_audio = self._has_received_audio_for_vad
                    self._has_received_audio_for_vad = False  # Reset atomically

                if not has_received_audio:
                    # No audio received, inject silence into the VAD process.
                    await self._process_vad_async(self._silence_chunk_vad)
        except asyncio.CancelledError:
            logger.info("ManualControlSink VAD monitor task cancelled.")
        except Exception as e:
            logger.error(
                f"Error in ManualControlSink VAD monitor loop: {e}", exc_info=True
            )

    def _finalize_wake_words_during_audio_gaps(
        self, now: Optional[float] = None
    ) -> None:
        """Feed trailing silence when Discord stops sending a user's RTP frames."""
        if self._bot_state.current_state not in {
            BotStateEnum.STANDBY,
            BotStateEnum.RECORDING,
        }:
            return

        current_time = time.monotonic() if now is None else now

        with self._user_data_lock:
            if Config.WAKE_WORD_ENGINE == "paraformer":
                for user_id, model in list(self._detectors.items()):
                    user = self._wakeword_users.get(user_id)
                    if user is not None and self._handle_keyword_prediction_locked(user, model.poll()):
                        model.reset()
                        self._user_audio_buffers[user_id].clear()
                        self._ww_resampled_buffers[user_id].clear()
                        self._wakeword_last_audio_at.pop(user_id, None)
                        self._wakeword_silence_chunks_sent.pop(user_id, None)
            for user_id, last_audio_at in list(self._wakeword_last_audio_at.items()):
                if (
                    self._bot_state.is_active_participant(user_id) is True
                    and Config.WAKE_WORD_ENGINE not in {"sherpa_onnx", "paraformer"}
                    and user_id not in self._input_blocked_users
                ):
                    continue
                # Discord can suppress RTP briefly inside a phrase. Waiting here
                # keeps a natural pause between repeated words in one stream.
                if current_time - last_audio_at < 0.35:
                    continue

                chunks_sent = self._wakeword_silence_chunks_sent.get(user_id, 0)
                if chunks_sent >= 6:
                    continue

                model = self._detectors.get(user_id)
                user = self._wakeword_users.get(user_id)
                if model is None or user is None:
                    continue

                prediction = model.predict(self._wakeword_silence_chunk)
                chunks_sent += 1
                self._wakeword_silence_chunks_sent[user_id] = chunks_sent
                if self._handle_keyword_prediction_locked(user, prediction):
                    self._log_wake_word_audio_summary_locked(user_id, detected=True)
                    logger.info(
                        "Control keyword detected for user %s after RTP gap finalization.",
                        user_id,
                    )
                    model.reset()
                    self._user_audio_buffers[user_id].clear()
                    self._ww_resampled_buffers[user_id].clear()
                    self._wakeword_last_audio_at.pop(user_id, None)
                    self._wakeword_silence_chunks_sent.pop(user_id, None)
                elif chunks_sent >= 6:
                    # KeywordSpotter streams support continuous audio. Stop
                    # injecting silence but preserve decoder context so a short
                    # RTP pause cannot split one wake phrase into two utterances.
                    self._log_wake_word_audio_summary_locked(user_id, detected=False)
                    self._wakeword_last_audio_at.pop(user_id, None)
                    self._wakeword_silence_chunks_sent.pop(user_id, None)

    def _log_wake_word_audio_summary_locked(
        self, user_id: int, *, detected: bool
    ) -> None:
        """Log signal metadata without retaining or exposing voice content."""
        logger.info(
            "Wake-word audio summary for user %s: frames=%s, model_chunks=%s, "
            "max_rms=%s, max_peak=%s, detected=%s.",
            user_id,
            self._wakeword_audio_frame_counts.get(user_id, 0),
            self._wakeword_model_chunk_counts.get(user_id, 0),
            self._wakeword_max_rms.get(user_id, 0),
            self._wakeword_max_peak.get(user_id, 0),
            detected,
        )
        self._wakeword_audio_frame_counts[user_id] = 0
        self._wakeword_model_chunk_counts[user_id] = 0
        self._wakeword_max_rms[user_id] = 0
        self._wakeword_max_peak[user_id] = 0

    def _resample_audio(
        self,
        raw_chunk: bytes,
        resample_state: Optional[Any],
        target_sample_rate: int = 16000,
    ) -> tuple[bytes, Optional[Any]]:
        """
        Generic helper method for resampling Discord audio to target format.

        Now uses the unified audio processing framework for consistent resampling
        across the codebase while maintaining backward compatibility.

        Args:
            raw_chunk: Raw PCM audio data from Discord (48kHz, 16-bit, stereo)
            target_sample_rate: Target sample rate in Hz (default: 16000)

        Returns:
            bytes: Resampled audio data
        """
        # Map target sample rate to appropriate format
        if target_sample_rate == Config.VAD_SAMPLE_RATE:
            target_format = VAD_FORMAT
            state_key = f"vad_session_{self._active_session_id}"
        elif target_sample_rate == Config.WAKE_WORD_SAMPLE_RATE:
            target_format = WAKE_WORD_FORMAT
            state_key = f"wake_word_session_{self._active_session_id}"
        else:
            # Custom target rate
            target_format = AudioFormat(target_sample_rate, 1, Config.SAMPLE_WIDTH)
            state_key = f"custom_{target_sample_rate}_session_{self._active_session_id}"

        # Use unified processor for consistent resampling
        resampled_audio = self._audio_processor.convert_sync(
            DISCORD_FORMAT,
            target_format,
            raw_chunk,
            strategy=ProcessingStrategy.REALTIME,
            state_key=state_key,
        )

        return resampled_audio

    def _resample_and_convert(self, raw_chunk: bytes, user_id: int) -> bytes:
        """
        Convert Discord audio format to wake word model requirements.

        Now uses the unified audio processing framework for consistent wake word
        audio conversion across the codebase.

        Discord provides: 48kHz, 16-bit, stereo PCM
        Wake word models need: 16kHz, 16-bit, mono PCM

        Args:
            raw_chunk: Raw PCM audio data from Discord
            user_id: User ID for per-user state management

        Returns:
            bytes: Resampled mono PCM audio for wake word detection
        """
        # Create per-user state key for wake word processing
        state_key = f"wake_word_user_{user_id}"

        # Use unified processor for consistent wake word audio conversion
        return self._audio_processor.convert_sync(
            DISCORD_FORMAT,
            WAKE_WORD_FORMAT,
            raw_chunk,
            strategy=ProcessingStrategy.REALTIME,
            state_key=state_key,
        )

    async def _handle_vad_speech_end(
        self, *, vad_generation=None, user_id=None, input_generation=None, session_id=None,
    ):
        """
        Handle VAD-detected speech end with race condition protection.

        RACE CONDITION FIX: Captures authority_user_id immediately to prevent
        race condition where concurrent state changes cause cleanup to fail.

        SIMPLIFIED CLEANUP: Uses comprehensive cleanup instead of individual
        user cleanup for better robustness and maintainability.
        """
        if vad_generation is not None and (
            vad_generation != getattr(self, "_vad_generation", 0)
            or user_id != self._bot_state.authority_user_id
            or input_generation != self.get_user_input_generation(user_id)
            or session_id != self._bot_state.current_session_id
        ):
            return
        # SESSION ID VALIDATION: Fail-fast on cross-session contamination
        # Note: This runs as an asyncio task, so we catch the exception here
        # to prevent unhandled task exceptions while still logging the issue.
        current_session_id = self._bot_state.current_session_id
        if current_session_id != self._active_session_id:
            self._cleanup_metrics.record_session_id_mismatch(
                self._active_session_id, current_session_id
            )
            logger.warning(
                f"Session consistency error in VAD speech end: "
                f"sink={self._active_session_id}, state={current_session_id}. "
                f"Recording interrupted - audio will not be processed."
            )
            return  # Early return - don't process stale audio

        # ATOMIC CAPTURE: Prevent race condition by capturing state for logging
        authority_user_id_at_speech_end = self._bot_state.authority_user_id
        authority_generation = self.get_user_input_generation(authority_user_id_at_speech_end)

        self.enable_vad(False)

        if not self._authority_buffer:
            if authority_user_id_at_speech_end is not None:
                self._clear_wake_word_buffers({authority_user_id_at_speech_end})
            logger.debug(
                f"VAD cleanup with no audio: user_id={authority_user_id_at_speech_end}"
            )
            return

        audio_data = bytes(self._authority_buffer)
        self._authority_buffer.clear()

        # Clear VAD-specific buffers
        self._vad_raw_buffer.clear()
        self._vad_resampled_buffer.clear()
        # Destroy VAD analyzer to ensure fresh state for next session
        self._vad_analyzer = None

        if authority_user_id_at_speech_end is not None:
            self._clear_wake_word_buffers({authority_user_id_at_speech_end})

        # Enhanced logging for race condition debugging
        logger.debug(
            f"VAD speech end cleanup completed: user_id={authority_user_id_at_speech_end}, "
            f"audio_size={len(audio_data)}, buffers_cleared=current_user"
        )

        # Track successful interaction
        self._cleanup_metrics.record_successful_interaction(len(audio_data))

        # Schedule the callback with captured audio
        if audio_data:
            asyncio.create_task(self._on_vad_speech_end(
                audio_data, user_id=authority_user_id_at_speech_end,
                input_generation=authority_generation, session_id=current_session_id,
            ))

    async def _process_vad_async(
        self, pcm_data: bytes, user_id: Optional[int] = None,
        input_generation: Optional[int] = None,
    ):
        """
        Thread-safe async wrapper for VAD processing.

        This method is called via run_coroutine_threadsafe() from the write() method
        to move VAD processing from Discord's audio thread to the main event loop.
        The _vad_lock prevents race conditions between real audio and silence injection.
        """
        async with self._vad_lock:
            if user_id is not None and (
                self.is_user_input_blocked(user_id)
                or (input_generation is not None
                    and input_generation != self.get_user_input_generation(user_id))
            ):
                return
            self._process_vad(pcm_data)

    def _process_vad(self, pcm_data: bytes):
        """
        Core VAD processing logic for detecting end-of-speech.

        PROCESSING PIPELINE:
        1. Buffer incoming PCM data in _vad_raw_buffer
        2. Process in 20ms chunks (3840 bytes of 48kHz stereo)
        3. Convert each chunk: stereo -> mono, 48kHz -> 16kHz (VAD sample rate)
        4. Buffer resampled audio for frame-based VAD analysis
        5. Feed VAD-compatible frames to VADAnalyzer

        The VADAnalyzer handles the state machine for detecting sustained
        speech and meaningful silence periods that indicate end-of-command.
        """
        # VAD analyzer is created by enable_vad() method for each recording session
        if not self._vad_analyzer:
            logger.warning(
                "VAD processing called but no analyzer exists - VAD not enabled"
            )
            return

        # Buffer raw audio before resampling, similar to the wake word path
        self._vad_raw_buffer.extend(pcm_data)
        min_vad_raw_bytes = (
            Config.DISCORD_CHUNK_SIZE
        )  # Process in 20ms chunks of raw 48kHz stereo

        while len(self._vad_raw_buffer) >= min_vad_raw_bytes:
            raw_chunk = self._vad_raw_buffer[:min_vad_raw_bytes]
            del self._vad_raw_buffer[:min_vad_raw_bytes]

            resampled_audio = self._resample_audio(
                raw_chunk, None, Config.VAD_SAMPLE_RATE
            )
            self._vad_resampled_buffer.extend(resampled_audio)

        # Process the resampled buffer in VAD-compatible frames
        frame_bytes = (
            (Config.VAD_SAMPLE_RATE * Config.VAD_FRAME_DURATION_MS) // 1000
        ) * Config.SAMPLE_WIDTH
        while len(self._vad_resampled_buffer) >= frame_bytes:
            frame = self._vad_resampled_buffer[:frame_bytes]
            del self._vad_resampled_buffer[:frame_bytes]
            self._vad_analyzer.process(frame)

    def write(self, user: discord.User, data: voice_recv.VoiceData):
        """
        Enhanced write method with comprehensive race condition diagnostics.

        This method is called from Discord's audio thread for every 20ms audio frame
        from each user in the voice channel. It must be fast, non-blocking, and
        thread-safe since it's not running on the main event loop.
        """
        if not user:
            return

        capture = getattr(self, "_raw_capture", None)
        if capture is not None and user.id in self._detectors:
            packet = getattr(data, "packet", None)
            capture.submit(get_observer().pseudonym(user.id), data.pcm,
                           rtp_timestamp=getattr(packet, "timestamp", None))

        # Record only the first successfully decoded frame per user. This makes
        # transport/decryption failures distinguishable from wake-word misses
        # without storing audio or logging every 20 ms packet.
        with self._user_data_lock:
            if user.id not in self._users_with_received_audio:
                self._users_with_received_audio.add(user.id)
                logger.info("Receiving decoded audio for user %s.", user.id)

        # SESSION ID VALIDATION: Early exit if session has changed
        current_session_id = self._bot_state.current_session_id
        if current_session_id != self._active_session_id:
            # Immediately update session ID to prevent race conditions
            # This eliminates the window where valid frames might be discarded
            old_session_id = self._active_session_id
            self._active_session_id = current_session_id

            # Log the session change (not every frame to avoid spam)
            if (
                hash(data.pcm) % Config.AUDIO_LOG_SAMPLING_RATE == 0
            ):  # Log ~1% of frames
                logger.info(
                    f"Auto-updated ManualControlSink session ID: {old_session_id} -> {current_session_id}"
                )
                self._cleanup_metrics.record_session_id_mismatch(
                    old_session_id, current_session_id
                )

            # Continue processing with updated session ID instead of returning
            # This ensures valid frames aren't lost during session transitions

        # Capture state atomically for consistent logging
        current_state = self._bot_state.current_state
        authority_id = self._bot_state.authority_user_id
        recording_method = self._bot_state.recording_method
        is_authorized = self._bot_state.is_authorized(user)
        is_active = self._bot_state.is_active_participant(user.id) is True

        if user.id in self._detectors:
            if self.is_user_input_blocked(user.id):
                # A muted participant only reaches the local detector, regardless
                # of the guild state or a pending asynchronous routing callback.
                self._process_standby_audio(user, data)
                return
            if Config.WAKE_WORD_ENGINE in {"sherpa_onnx", "paraformer"} and (
                is_active or (current_state == BotStateEnum.RECORDING and is_authorized)
            ):
                self._process_standby_audio(user, data)
                if self.is_user_input_blocked(user.id):
                    return

        # Observe admitted speakers continuously, including while another user
        # owns the serial input. A RECORDING -> STANDBY transition is not a new
        # speech onset and must not make ongoing speech interrupt the answer.
        retained_for_onset = False
        if (
            is_active and self._on_active_speech_detected
            and current_state in {BotStateEnum.RECORDING, BotStateEnum.STANDBY}
        ):
            retained_for_onset = self._process_active_speech_audio(
                user, data.pcm, notify=current_state == BotStateEnum.STANDBY
            )

        # Enhanced logging for race condition debugging
        logger.debug(
            f"Audio write: user={user.id}, size={len(data.pcm)}, "
            f"state={current_state.value}, method={recording_method}, "
            f"auth_user={authority_id}, is_authorized={is_authorized}, "
            f"buffer_size={len(self._authority_buffer)}"
        )

        # Use captured state for consistent behavior
        if current_state == BotStateEnum.RECORDING:
            if is_authorized:
                if retained_for_onset:
                    # Routing may have changed state before its callback has
                    # delivered the onset. That callback drains these in order.
                    return
                # TOCTOU Fix: Schedule atomic operation on event loop since write() is called from Discord thread
                asyncio.run_coroutine_threadsafe(
                    self._atomic_authority_buffer_update(
                        user, data.pcm, current_state, is_authorized,
                        self.get_user_input_generation(user.id),
                    ),
                    self._loop,
                )
                # Don't wait to avoid blocking Discord's audio thread

                if self._on_recording_audio_chunk:
                    asyncio.run_coroutine_threadsafe(
                        self._deliver_recording_audio_chunk(
                            user, data.pcm, self.get_user_input_generation(user.id)
                        ),
                        self._loop,
                    )

                # Local VAD normally handles buffered wake-word turns. It is also
                # enabled as a safety net for provider-native realtime turns, so
                # a provider that never closes a silent turn cannot leave the UI
                # stuck in RECORDING forever.
                if self._is_vad_enabled:
                    # VAD flag race fix: Thread-safe flag update
                    with self._vad_flag_lock:
                        self._has_received_audio_for_vad = True
                    # Schedule VAD processing on the event loop
                    asyncio.run_coroutine_threadsafe(
                        self._process_vad_async(
                            data.pcm, user.id, self.get_user_input_generation(user.id)
                        ), self._loop
                    )
            elif (
                user.id in self._detectors
                and self._bot_state.is_active_participant(user.id) is not True
            ):
                # A new participant must still be able to say the wake word
                # while another participant owns the current serial input turn.
                self._process_standby_audio(user, data)
        elif current_state == BotStateEnum.STANDBY and user.id in self._detectors:
            if not (is_active and self._on_active_speech_detected):
                self._reset_active_speech_vad(user.id)
                self._process_standby_audio(user, data)

    def _process_active_speech_audio(
        self, user: discord.User, pcm_data: bytes, *, notify: bool = True
    ) -> bool:
        """Emit once per utterance, rearming only after this user's silence."""
        user_id = user.id
        with self._user_data_lock:
            raw_buffer = self._active_speech_raw_buffers.get(user_id)
            resampled_buffer = self._active_speech_resampled_buffers.get(user_id)
            vad = self._active_speech_vads.get(user_id)
            if raw_buffer is None or resampled_buffer is None or vad is None:
                return False

            now = time.monotonic()
            rearm_seconds = Config.ACTIVE_SPEECH_REARM_SILENCE_MS / 1000.0
            last_audio_at = self._active_speech_last_audio_at.get(user_id)
            if last_audio_at is not None and now - last_audio_at >= rearm_seconds:
                # Discord suppresses RTP during silence, so no silent PCM may
                # arrive. Do not carry a partial onset or latch over that gap.
                self._reset_active_speech_vad_locked(user_id)
                self._active_speech_latched.discard(user_id)
            self._active_speech_last_audio_at[user_id] = now

            retained = notify or user_id in self._active_speech_pending
            preroll = self._active_speech_preroll.setdefault(user_id, bytearray())
            if retained:
                preroll.extend(pcm_data)
                # One second of raw Discord PCM bounds inactive-speaker memory
                # while retaining the 180 ms onset decision and routing delay.
                max_bytes = 48000 * 2 * Config.SAMPLE_WIDTH
                if len(preroll) > max_bytes:
                    del preroll[:-max_bytes]
            else:
                preroll.clear()

            raw_buffer.extend(pcm_data)
            while len(raw_buffer) >= Config.VAD_PROCESSING_CHUNK:
                raw_chunk = bytes(raw_buffer[: Config.VAD_PROCESSING_CHUNK])
                del raw_buffer[: Config.VAD_PROCESSING_CHUNK]
                resampled_buffer.extend(
                    self._audio_processor.convert_sync(
                        DISCORD_FORMAT,
                        VAD_FORMAT,
                        raw_chunk,
                        strategy=ProcessingStrategy.REALTIME,
                        state_key=f"active_speech_user_{user_id}",
                    )
                )

            frame_bytes = (
                Config.VAD_SAMPLE_RATE * Config.VAD_FRAME_DURATION_MS // 1000
            ) * Config.SAMPLE_WIDTH
            min_speech_frames = max(
                1,
                (
                    Config.ACTIVE_SPEECH_MIN_DURATION_MS
                    + Config.VAD_FRAME_DURATION_MS
                    - 1
                )
                // Config.VAD_FRAME_DURATION_MS,
            )
            max_gap_frames = max(
                0,
                Config.ACTIVE_SPEECH_MAX_GAP_MS // Config.VAD_FRAME_DURATION_MS,
            )
            rearm_frames = max(
                1, (Config.ACTIVE_SPEECH_REARM_SILENCE_MS
                    + Config.VAD_FRAME_DURATION_MS - 1) // Config.VAD_FRAME_DURATION_MS,
            )

            while len(resampled_buffer) >= frame_bytes:
                frame = bytes(resampled_buffer[:frame_bytes])
                del resampled_buffer[:frame_bytes]
                try:
                    is_speech = vad.is_speech(frame, Config.VAD_SAMPLE_RATE)
                    if Config.VOICE_INPUT_GATE_ENABLED:
                        values = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
                        dbfs = 20 * np.log10(max(float(np.sqrt(np.mean(values * values))), 1e-8))
                        is_speech = is_speech and dbfs >= Config.VOICE_INPUT_GATE_DBFS
                except Exception:
                    logger.warning(
                        "Active-speaker VAD failed for user %s.",
                        user_id,
                        exc_info=True,
                    )
                    self._reset_active_speech_vad_locked(user_id)
                    return retained

                if is_speech:
                    self._active_speech_frame_counts[user_id] += 1
                    self._active_speech_gap_counts[user_id] = 0
                else:
                    gap_count = self._active_speech_gap_counts[user_id] + 1
                    self._active_speech_gap_counts[user_id] = gap_count
                    if gap_count > max_gap_frames:
                        self._active_speech_frame_counts[user_id] = 0
                    if gap_count >= rearm_frames:
                        self._active_speech_latched.discard(user_id)

                if self._active_speech_frame_counts[user_id] < min_speech_frames:
                    continue
                if user_id in self._active_speech_latched:
                    continue
                if not notify:
                    self._active_speech_latched.add(user_id)
                    continue

                now = time.monotonic()
                last_triggered = self._active_speech_last_triggered_at.get(user_id, 0.0)
                cooldown_seconds = Config.ACTIVE_SPEECH_COOLDOWN_MS / 1000.0
                if (
                    user_id in self._active_speech_pending
                    or now - last_triggered < cooldown_seconds
                ):
                    continue

                self._active_speech_pending.add(user_id)
                self._active_speech_latched.add(user_id)
                self._active_speech_last_triggered_at[user_id] = now
                logger.info(
                    "New speech onset for admitted user %s in session %s.",
                    user_id, self._bot_state.current_session_id,
                )
                self._loop.call_soon_threadsafe(
                    asyncio.create_task,
                    self._notify_active_speech_detected(
                        user, self._bot_state.current_session_id,
                        self.get_user_input_generation(user_id),
                    ),
                )
            return retained

    def _reset_active_speech_vad(self, user_id: int) -> None:
        """Clear partial speech without dropping the per-user detector."""
        with self._user_data_lock:
            self._active_speech_latched.discard(user_id)
            self._active_speech_last_audio_at.pop(user_id, None)
            raw_buffer = self._active_speech_raw_buffers.get(user_id)
            resampled_buffer = self._active_speech_resampled_buffers.get(user_id)
            if not (
                raw_buffer
                or resampled_buffer
                or self._active_speech_preroll.get(user_id)
                or self._active_speech_frame_counts.get(user_id, 0)
                or self._active_speech_gap_counts.get(user_id, 0)
            ):
                return
            self._reset_active_speech_vad_locked(user_id)

    def _reset_active_speech_vad_locked(self, user_id: int) -> None:
        """Clear active-speaker VAD state while the user-data lock is held."""
        raw_buffer = self._active_speech_raw_buffers.get(user_id)
        preroll = self._active_speech_preroll.get(user_id)
        if preroll is not None:
            preroll.clear()
        if raw_buffer is not None:
            raw_buffer.clear()
        resampled_buffer = self._active_speech_resampled_buffers.get(user_id)
        if resampled_buffer is not None:
            resampled_buffer.clear()
        if user_id in self._active_speech_frame_counts:
            self._active_speech_frame_counts[user_id] = 0
        if user_id in self._active_speech_gap_counts:
            self._active_speech_gap_counts[user_id] = 0
        self._audio_processor.reset_state(f"active_speech_user_{user_id}")

    async def _notify_active_speech_detected(
        self, user: discord.User, session_id: Optional[int] = None,
        input_generation: Optional[int] = None,
    ) -> None:
        """Debounce active-user speech frames until routing changes bot state."""
        try:
            if session_id is not None and session_id != self._bot_state.current_session_id:
                return
            if input_generation is not None and input_generation != self.get_user_input_generation(user.id):
                return
            if (
                self._on_active_speech_detected
                and not self.is_user_input_blocked(user.id)
            ):
                await self._on_active_speech_detected(user)
                routed_session_id = self._bot_state.current_session_id
                async with self._action_lock:
                    with self._user_data_lock:
                        valid = (
                            self._bot_state.current_state == BotStateEnum.RECORDING
                            and self._bot_state.current_session_id == routed_session_id
                            and self._bot_state.authority_user_id == user.id
                            and not self.is_user_input_blocked(user.id)
                            and (input_generation is None or input_generation
                                 == self.get_user_input_generation(user.id))
                        )
                        preroll = self._active_speech_preroll.get(user.id, bytearray())
                        pcm = bytes(preroll) if valid else b""
                        preroll.clear()
                        self._active_speech_pending.discard(user.id)
                    if pcm:
                        self._authority_buffer.extend(pcm)
                        await self._deliver_recording_audio_chunk(user, pcm, input_generation)
                        if self._is_vad_enabled:
                            with self._vad_flag_lock:
                                self._has_received_audio_for_vad = True
                            await self._process_vad_async(pcm, user.id, input_generation)
        finally:
            self._active_speech_pending.discard(user.id)

    async def _atomic_authority_buffer_update(
        self,
        user: discord.User,
        pcm_data: bytes,
        captured_state: BotStateEnum,
        captured_authorized: bool,
        input_generation: Optional[int] = None,
    ) -> None:
        """Atomically check state and update buffer under shared lock."""
        async with self._action_lock:
            # Re-validate state under lock
            if (
                self._bot_state.current_state
                == captured_state
                == BotStateEnum.RECORDING
                and self._bot_state.is_authorized(user)
                and captured_authorized
                and not self.is_user_input_blocked(user.id)
                and (input_generation is None or input_generation == self.get_user_input_generation(user.id))
            ):
                self._authority_buffer.extend(pcm_data)
                logger.debug(
                    f"Authority buffer updated: size={len(self._authority_buffer)}"
                )

    def is_user_input_blocked(self, user_id: int) -> bool:
        """Return the synchronous local gate used by queued delivery callbacks."""
        return user_id in self._input_blocked_users

    def get_user_input_generation(self, user_id: int) -> int:
        """Invalidate queued PCM across stop/wake transitions, even after reopening."""
        return self._input_generations.get(user_id, 0)

    def invalidate_user_input(self, user_id: int) -> None:
        """Retire queued callbacks after handoff without closing future wake access."""
        with self._user_data_lock:
            self._input_generations[user_id] = self.get_user_input_generation(user_id) + 1
            self._active_speech_pending.discard(user_id)
            self._active_speech_latched.discard(user_id)
            self._reset_active_speech_vad_locked(user_id)
            if self._bot_state.authority_user_id == user_id:
                self._authority_buffer.clear()
                self._vad_raw_buffer.clear()
                self._vad_resampled_buffer.clear()
                self._vad_generation = getattr(self, "_vad_generation", 0) + 1
                self._is_vad_enabled = False
                self._vad_analyzer = None

    async def _deliver_recording_audio_chunk(
        self, user: discord.User, pcm_data: bytes, input_generation: Optional[int] = None
    ) -> None:
        if (self._on_recording_audio_chunk and not self.is_user_input_blocked(user.id)
                and (input_generation is None or input_generation == self.get_user_input_generation(user.id))):
            await self._on_recording_audio_chunk(user, pcm_data)

    def _handle_keyword_prediction_locked(
        self, user: discord.User, prediction: Dict[str, float]
    ) -> bool:
        """Apply local control before forwarding the detection frame.

        Previously streamed PCM cannot be recalled. The guild callback cancels
        pending provider input; all frames after detection stay local until wake.
        """
        observer = get_observer()
        detected = {
            key.casefold(): key for key, score in prediction.items()
            if score > Config.WAKE_WORD_THRESHOLD
        }
        stop = next((detected[word] for word in Config.STOP_WORD_PHRASES
                     if word in detected), None)
        if Config.STOP_WORD_ENABLED and stop is not None:
            was_blocked = user.id in self._input_blocked_users
            self._input_blocked_users.add(user.id)
            self._reset_active_speech_vad_locked(user.id)
            self._active_speech_pending.discard(user.id)
            self._active_speech_latched.discard(user.id)
            if self._bot_state.authority_user_id == user.id:
                self._authority_buffer.clear()
                self._vad_raw_buffer.clear()
                self._vad_resampled_buffer.clear()
            if not was_blocked:
                self._input_generations[user.id] = self.get_user_input_generation(user.id) + 1
                observer.emit("input.gate.closed", speaker_id=observer.pseudonym(user.id),
                              reason="stop_keyword", keyword=stop)
            if self._on_stop_word_detected and (not was_blocked or stop.strip() == "闭嘴"):
                self._loop.call_soon_threadsafe(
                    asyncio.create_task, self._on_stop_word_detected(user, stop)
                )
            return True
        model_name = (Config.WAKE_WORD_PHRASE if Config.WAKE_WORD_ENGINE in {"sherpa_onnx", "paraformer"}
                      else Config.WAKE_WORD_MODEL_PATH.stem)
        if model_name.casefold() not in detected:
            return False
        was_blocked = user.id in self._input_blocked_users
        if (self._bot_state.is_active_participant(user.id) is True and not was_blocked
                and self._bot_state.current_state == BotStateEnum.RECORDING
                and self._bot_state.authority_user_id == user.id):
            return False
        self._input_blocked_users.discard(user.id)
        self._input_generations[user.id] = self.get_user_input_generation(user.id) + 1
        observer.emit("input.gate.open", speaker_id=observer.pseudonym(user.id),
                      reason="start_keyword", keyword=model_name)
        self._loop.call_soon_threadsafe(
            asyncio.create_task, self._on_wake_word_detected(user)
        )
        return True

    def _process_standby_audio(self, user: discord.User, data: voice_recv.VoiceData):
        """
        Processes audio data when the bot is in STANDBY state for wake word detection.

        This method handles the complex wake word detection pipeline including:
        - Audio buffering and resampling from Discord format to wake word model format
        - Processing audio through openWakeWord models for detection
        - Handling wake word detection events and state cleanup
        """
        # CONCURRENCY FIX: Use dedicated lock for all user data access
        with self._user_data_lock:
            # Check if user was removed during processing
            if user.id not in self._detectors:
                return  # User was removed, skip processing
            sample_bytes = len(data.pcm) - (len(data.pcm) % Config.SAMPLE_WIDTH)
            samples = np.frombuffer(data.pcm[:sample_bytes], dtype=np.int16)
            if samples.size:
                wide_samples = samples.astype(np.int32)
                rms = int(np.sqrt(np.mean(wide_samples.astype(np.float64) ** 2)))
                peak = int(np.max(np.abs(wide_samples)))
                self._wakeword_audio_frame_counts[user.id] += 1
                self._wakeword_max_rms[user.id] = max(
                    self._wakeword_max_rms[user.id], rms
                )
                self._wakeword_max_peak[user.id] = max(
                    self._wakeword_max_peak[user.id], peak
                )
            self._wakeword_users[user.id] = user
            self._wakeword_last_audio_at[user.id] = time.monotonic()
            self._wakeword_silence_chunks_sent[user.id] = 0
            self._user_audio_buffers[user.id].extend(data.pcm)
            buffer = self._user_audio_buffers[user.id]
            logger.debug(f"User {user.id} buffer size: {len(buffer)}")

            # Process in chunks large enough for at least one resample operation
            # 7680 bytes of 48kHz stereo -> 1920 frames -> 640 frames @ 16kHz mono -> 1280 bytes
            min_raw_bytes = Config.VAD_PROCESSING_CHUNK
            processed_bytes = 0
            resampled_buffer = self._ww_resampled_buffers[user.id]
            while len(buffer) - processed_bytes >= min_raw_bytes:
                raw_chunk = buffer[processed_bytes : processed_bytes + min_raw_bytes]
                resampled = self._resample_and_convert(raw_chunk, user.id)
                resampled_buffer.extend(resampled)
                processed_bytes += min_raw_bytes

            # Remove the processed raw data from the beginning of the buffer
            del buffer[:processed_bytes]

            # Process the resampled buffer for wake words
            while len(resampled_buffer) >= self._ww_chunk_size:
                ww_chunk_bytes = resampled_buffer[: self._ww_chunk_size]
                del resampled_buffer[: self._ww_chunk_size]

                # Convert bytes to numpy array for the model
                ww_chunk_np = np.frombuffer(ww_chunk_bytes, dtype=np.int16)

                model = self._detectors[user.id]
                prediction = model.predict(ww_chunk_np)
                self._wakeword_model_chunk_counts[user.id] += 1
                logger.debug(f"Wake word prediction for user {user.id}: {prediction}")

                if self._handle_keyword_prediction_locked(user, prediction):
                    self._log_wake_word_audio_summary_locked(user.id, detected=True)
                    model.reset()
                    self._user_audio_buffers[user.id].clear()
                    self._ww_resampled_buffers[user.id].clear()
                    self._wakeword_last_audio_at.pop(user.id, None)
                    self._wakeword_silence_chunks_sent.pop(user.id, None)
                    return
